"""Enrich a niche's tenders + write lots, from notice XML.
Usage: python enrich_tenders_xml.py --niche green --limit 20"""
from dotenv import load_dotenv
load_dotenv()

import argparse, time
import requests
import xml.etree.ElementTree as ET
from tqdm import tqdm
import core
from niches import get_niche

def loc(el): return el.tag.split('}')[-1]
def first_named(root, name):
    for e in root.iter():
        if loc(e) == name: return e
    return None
def child_text(el, name):
    for c in el:
        if loc(c) == name: return (c.text or "").strip() or None
    return None

def notice_fields(root):
    pp = first_named(root, "ProcurementProject")
    description = child_text(pp, "Description") if pp is not None else None
    nuts = None
    rl = first_named(root, "RealizedLocation")
    if rl is not None:
        for c in rl.iter():
            if loc(c) == "CountrySubentityCode" and c.get("listName") == "nuts":
                nuts = (c.text or "").strip(); break
    fp = first_named(root, "FundingProgramCode")
    eu = (fp.text or "").strip() if (fp is not None and fp.text) else None
    return description, nuts, eu

def lot_rows(root, notice_nuts, classify):
    rows = []
    for lot in [e for e in root.iter() if loc(e) == "ProcurementProjectLot"]:
        lot_id = next(((c.text or "").strip() for c in lot
                       if loc(c) == "ID" and c.get("schemeName") == "Lot"), None)
        if not lot_id: continue
        pp = next((c for c in lot.iter() if loc(c) == "ProcurementProject"), None)
        title = child_text(pp, "Name") if pp is not None else None
        desc = child_text(pp, "Description") if pp is not None else None
        cpv = None
        if pp is not None:
            for c in pp.iter():
                if loc(c) == "ItemClassificationCode" and c.get("listName") == "cpv":
                    cpv = (c.text or "").strip(); break
        val = ccy = None
        for c in lot.iter():
            if loc(c) == "EstimatedOverallContractAmount":
                val = (c.text or "").strip(); ccy = c.get("currencyID"); break
        lnuts = None
        for c in lot.iter():
            if loc(c) == "CountrySubentityCode" and c.get("listName") == "nuts":
                lnuts = (c.text or "").strip(); break
        lnuts = lnuts or notice_nuts
        weights = {}
        for sac in [e for e in lot.iter() if loc(e) == "SubordinateAwardingCriterion"]:
            ctype = next(((c.text or "").strip() for c in sac.iter() if loc(c) == "AwardingCriterionTypeCode"), None)
            wt = next(((c.text or "").strip() for c in sac.iter() if loc(c) == "ParameterNumeric"), None)
            if not ctype: continue
            weights[ctype] = (weights.get(ctype) or 0) + (core.safe_float(wt) or 0)
        criteria = ", ".join(f"{t}:{int(w)}" if w else t for t, w in weights.items()) if weights else None
        rows.append({"lot_id": lot_id, "title": title, "description": desc, "cpv": cpv,
                     "subsector": classify(cpv), "value": core.safe_float(val), "currency": ccy,
                     "value_eur": core.to_eur(val, ccy), "region_nuts": lnuts, "criteria": criteria})
    return rows

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--niche", default="green")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    niche = get_niche(args.niche)
    classify = core.make_classifier(niche["subsector_rules"])

    conn = core.get_conn(); cur = conn.cursor()
    cur.execute("SELECT publication_number FROM tenders WHERE niche=%s ORDER BY publication_date DESC", (args.niche,))
    pubs = [r[0] for r in cur.fetchall()]
    if args.limit: pubs = pubs[:args.limit]
    print(len(pubs), "notices for niche", args.niche)

    upd = """UPDATE tenders SET description_original=COALESCE(%s,description_original),
             region_nuts=%s, eu_programme=%s WHERE publication_number=%s"""
    ins = """INSERT INTO lots (publication_number, niche, lot_id, title, description_original,
               cpv_main, subsector, estimated_value, estimated_value_currency, value_eur, region_nuts, award_criteria)
             VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
             ON CONFLICT (publication_number, lot_id) DO UPDATE SET
               niche=EXCLUDED.niche, title=EXCLUDED.title, description_original=EXCLUDED.description_original,
               cpv_main=EXCLUDED.cpv_main, subsector=EXCLUDED.subsector, estimated_value=EXCLUDED.estimated_value,
               estimated_value_currency=EXCLUDED.estimated_value_currency, value_eur=EXCLUDED.value_eur,
               region_nuts=EXCLUDED.region_nuts, award_criteria=EXCLUDED.award_criteria"""

    ok = fail = nlots = 0
    for pub in tqdm(pubs, desc="XML enrich + lots"):
        try:
            resp = requests.get(f"https://ted.europa.eu/en/notice/{pub}/xml", timeout=60)
            if resp.status_code != 200: fail += 1; continue
            root = ET.fromstring(resp.text)
        except Exception:
            fail += 1; continue
        desc, nuts, eu = notice_fields(root)
        cur.execute(upd, (desc, nuts, eu, pub))
        for L in lot_rows(root, nuts, classify):
            cur.execute(ins, (pub, args.niche, L["lot_id"], L["title"], L["description"], L["cpv"],
                              L["subsector"], L["value"], L["currency"], L["value_eur"], L["region_nuts"], L["criteria"]))
            nlots += 1
        ok += 1
        if ok % 50 == 0: conn.commit()
        time.sleep(0.3)
    conn.commit()
    print(f"Done. {ok} notices ({fail} failed), {nlots} lot rows.")
    cur.close(); conn.close()

if __name__ == "__main__":
    main()