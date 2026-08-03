"""
Extract contract end dates -> awards.contract_end. Powers "upcoming renewals".

For each awarded contract we read the notice XML and find the lot's planned
period. A contract's end date is whichever of these we can get, in order:
  1. an explicit EndDate on the lot,
  2. StartDate + DurationMeasure,
  3. award/publication date + DurationMeasure.
That end date is the moment the contract lapses = when the buyer must re-tender.

Incremental: only fills awards where contract_end IS NULL, one XML fetch per
notice. Safe to re-run.

Usage:
    python add_contract_end.py --niche green
    python add_contract_end.py --niche green --limit 200   # cap the batch
"""
from dotenv import load_dotenv
load_dotenv()

import argparse, time, datetime as dt
import xml.etree.ElementTree as ET
import requests
import core
from psycopg2.extras import RealDictCursor
from tqdm import tqdm

XML_URL = "https://ted.europa.eu/en/notice/{pub}/xml"

# DurationMeasure unitCode -> days. TED uses UN/CEFACT codes; map the common ones.
_UNIT_DAYS = {"DAY": 1, "DAI": 1, "WEEK": 7, "WEE": 7,
              "MONTH": 30, "MON": 30, "YEAR": 365, "ANN": 365}


# same tag helper the winner extractor uses: strip the namespace, keep local name
def loc(el):
    return el.tag.split('}')[-1]

def _to_date(s):
    if not s:
        return None
    try:
        return dt.date.fromisoformat(s.strip()[:10])   # handles "2027-03-01+02:00" too
    except ValueError:
        return None


def parse_periods(xml_text):
    """{lot_id: {'start':date|None, 'end':date|None, 'dur_days':int|None}} from a notice.
    lot_id is matched the same way extract_awards_xml stores it: the ID whose
    schemeName='Lot' (e.g. 'LOT-0001')."""
    out = {}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    for lot in root.iter():
        if loc(lot) != "ProcurementProjectLot":
            continue
        # lot id — first ID with schemeName="Lot" inside this lot
        lid = None
        for c in lot.iter():
            if loc(c) == "ID" and c.get("schemeName") == "Lot":
                lid = (c.text or "").strip(); break
        if not lid:
            continue
        # the lot's planned period (scoped to this lot, not the notice-level one)
        pp = next((e for e in lot.iter() if loc(e) == "PlannedPeriod"), None)
        if pp is None:
            continue
        start = end = dur_days = None
        for e in pp.iter():
            tag = loc(e)
            if tag == "StartDate":
                start = _to_date(e.text)
            elif tag == "EndDate":
                end = _to_date(e.text)
            elif tag == "DurationMeasure" and e.text:
                try:
                    dur_days = int(float(e.text) * _UNIT_DAYS.get((e.get("unitCode") or "").upper(), 1))
                except ValueError:
                    pass
        out[lid] = {"start": start, "end": end, "dur_days": dur_days}
    return out


def contract_end(period, award_date):
    """Resolve one lot's end date from its period + the award date fallback."""
    if period.get("end"):
        return period["end"]
    start = period.get("start") or award_date
    if start and period.get("dur_days"):
        return start + dt.timedelta(days=period["dur_days"])
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--niche", default="green")
    ap.add_argument("--limit", type=int, default=None, help="max notices to process this run")
    args = ap.parse_args()

    conn = core.get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # 1. make sure the column exists (idempotent)
    cur.execute("ALTER TABLE awards ADD COLUMN IF NOT EXISTS contract_end date")
    conn.commit()

    # 2. awards still missing an end date, grouped by their notice
    cur.execute("""SELECT id, publication_number, lot_id, award_date
                   FROM awards
                   WHERE niche=%s AND contract_end IS NULL
                   ORDER BY publication_number""", (args.niche,))
    rows = cur.fetchall()

    by_notice = {}
    for r in rows:
        by_notice.setdefault(r["publication_number"], []).append(r)

    pubs = list(by_notice)
    if args.limit:
        pubs = pubs[:args.limit]
    print(f"{len(rows)} awards missing contract_end across {len(by_notice)} notices; "
          f"processing {len(pubs)} this run.")

    filled = fetched = 0
    for pub in tqdm(pubs, desc="Notices"):
        try:
            resp = requests.get(XML_URL.format(pub=pub), timeout=60)
            if resp.status_code != 200:
                continue
        except requests.RequestException:
            continue
        fetched += 1
        periods = parse_periods(resp.text)
        if not periods:
            continue
        for a in by_notice[pub]:
            period = periods.get(a["lot_id"])
            if not period:
                continue
            end = contract_end(period, a["award_date"])
            if end:
                cur.execute("UPDATE awards SET contract_end=%s WHERE id=%s", (end, a["id"]))
                filled += 1
        conn.commit()
        time.sleep(0.3)   # be polite to TED, same as extract_awards_xml

    print(f"Fetched {fetched} notices, set contract_end on {filled} awards.")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()