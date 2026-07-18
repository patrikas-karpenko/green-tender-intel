"""Extract accurate winners for a niche from award-notice XML.
Usage: python extract_awards_xml.py --niche green --limit 20"""
from dotenv import load_dotenv
load_dotenv()

import argparse, time
import requests
import xml.etree.ElementTree as ET
from tqdm import tqdm
import core
from niches import get_niche

def loc(el): return el.tag.split('}')[-1]
def scheme_id(el, scheme):
    for c in el.iter():
        if loc(c) == "ID" and c.get("schemeName") == scheme: return (c.text or "").strip()
    return None
def text_of(el, name):
    for c in el.iter():
        if loc(c) == name: return (c.text or "").strip()
    return None

def parse_notice(xml_text):
    root = ET.fromstring(xml_text); els = list(root.iter())
    def named(n): return [e for e in els if loc(e) == n]
    orgs = {}
    for org in named("Organization"):
        oid = scheme_id(org, "organization")
        if oid:
            orgs[oid] = {"name": text_of(org, "Name"), "national_id": text_of(org, "CompanyID"),
                         "country": text_of(org, "IdentificationCode"), "nuts": text_of(org, "CountrySubentityCode")}
    tparty = {}
    for tp in named("TenderingParty"):
        tid = scheme_id(tp, "tendering-party")
        refs = [scheme_id(t, "organization") for t in tp.iter() if loc(t) == "Tenderer"]
        refs = [o for o in refs if o]
        if tid and refs: tparty[tid] = refs
    tenders = {}
    for lt in named("LotTender"):
        tid = scheme_id(lt, "tender")
        payable = next((c for c in lt.iter() if loc(c) == "PayableAmount"), None)
        tpa = scheme_id(lt, "tendering-party")
        if tid and (payable is not None or tpa):
            tenders[tid] = {"value": (payable.text.strip() if payable is not None and payable.text else None),
                            "currency": (payable.get("currencyID") if payable is not None else None), "tpa": tpa}
    dates = {}
    for sc in named("SettledContract"):
        d, con = text_of(sc, "AwardDate"), scheme_id(sc, "contract")
        if con and d: dates[con] = d[:10]
    out = []
    for lr in named("LotResult"):
        if text_of(lr, "TenderResultCode") != "selec-w": continue
        lot, ten, con = scheme_id(lr, "Lot"), scheme_id(lr, "tender"), scheme_id(lr, "contract")
        bids = None
        for rss in [e for e in lr.iter() if loc(e) == "ReceivedSubmissionsStatistics"]:
            if text_of(rss, "StatisticsCode") == "tenders": bids = text_of(rss, "StatisticsNumeric")
        t = tenders.get(ten, {}); refs = tparty.get(t.get("tpa"), [])
        w = orgs.get(refs[0], {}) if refs else {}
        out.append({"lot": lot, "name": w.get("name"), "national_id": w.get("national_id"),
                    "country": w.get("country"), "nuts": w.get("nuts"), "value": t.get("value"),
                    "currency": t.get("currency"), "num_bids": int(bids) if (bids and bids.isdigit()) else None,
                    "award_date": dates.get(con)})
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--niche", default="green")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    get_niche(args.niche)  # validate niche exists

    conn = core.get_conn(); cur = conn.cursor()
    cur.execute("""SELECT publication_number, publication_date FROM tenders
                   WHERE notice_type='contract_award' AND niche=%s ORDER BY publication_date DESC""", (args.niche,))
    pub_rows = cur.fetchall()
    if args.limit: pub_rows = pub_rows[:args.limit]
    print(len(pub_rows), "award notices for niche", args.niche)

    ins = """INSERT INTO awards
      (publication_number, niche, lot_id, winner_name_raw, winner_official_id, winner_country,
       winner_nuts, award_value, award_currency, award_value_eur, num_bids, award_date)
      VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"""

    ok = fail = rows = 0
    for pub, pubdate in tqdm(pub_rows, desc="Extracting winners"):
        try:
            resp = requests.get(f"https://ted.europa.eu/en/notice/{pub}/xml", timeout=60)
            if resp.status_code != 200: fail += 1; continue
            winners = parse_notice(resp.text)
        except Exception:
            fail += 1; continue
        cur.execute("DELETE FROM awards WHERE publication_number=%s", (pub,))
        for w in winners:
            cur.execute(ins, (pub, args.niche, w["lot"], w["name"], w["national_id"], w["country"], w["nuts"],
                              core.safe_float(w["value"]), w["currency"], core.to_eur(w["value"], w["currency"]),
                              w["num_bids"], w["award_date"] or pubdate))
            rows += 1
        ok += 1
        if ok % 50 == 0: conn.commit()
        time.sleep(0.3)
    conn.commit()
    print(f"Done. Parsed {ok} notices ({fail} failed), wrote {rows} award rows.")
    cur.close(); conn.close()

if __name__ == "__main__":
    main()