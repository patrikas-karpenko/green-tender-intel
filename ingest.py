"""
Ingest tenders for a NICHE from TED into Supabase.
Usage: python ingest.py --niche green --days 30
"""
from dotenv import load_dotenv
load_dotenv()

import argparse, time, requests
from psycopg2.extras import execute_values
import core
from niches import get_niche

URL = "https://api.ted.europa.eu/v3/notices/search"
FIELDS = ["publication-number","notice-title","buyer-name","organisation-country-buyer",
          "publication-date","deadline-receipt-tender-date-lot","classification-cpv","notice-type",
          "estimated-value-lot","estimated-value-cur-lot","description-lot","award-criterion-type-lot",
          "internal-identifier-proc"]

def fetch_all(query):
    page, out = 1, []
    while True:
        resp = requests.post(URL, json={"query": query, "fields": FIELDS, "limit": 250, "page": page}, timeout=60)
        if resp.status_code != 200:
            print(f"\nHTTP {resp.status_code} on page {page}: {resp.text[:300]}"); break
        data = resp.json(); notices = data.get("notices", [])
        if not notices: break
        out.extend(notices); total = data.get("totalNoticeCount", 0)
        print(f"  page {page}: {len(out)}/{total}")
        if len(out) >= total or len(out) >= 15000: break
        page += 1; time.sleep(0.5)
    return out

def to_row(n, niche_name, classify):
    cpvs = n.get("classification-cpv") or []
    if isinstance(cpvs, str): cpvs = [cpvs]
    cpvs = [str(c) for c in cpvs if c]
    main = cpvs[0] if cpvs else ""
    nt = core.text_of(n.get("notice-type")).lower()
    ntype = "contract_award" if "can" in nt else ("contract_notice" if "cn" in nt else nt or None)
    pub = core.text_of(n.get("publication-number"))
    est = core.first_num(n.get("estimated-value-lot"))
    ccy = core.text_of(n.get("estimated-value-cur-lot")) or "EUR"
    crit = n.get("award-criterion-type-lot")
    if crit:
        items = crit if isinstance(crit, list) else [crit]
        criteria = ", ".join(sorted({str(x).lower() for x in items if x})) or None
    else:
        criteria = None
    return (pub, "api", niche_name, ntype,
            core.text_of(n.get("notice-title")), core.text_of(n.get("buyer-name")),
            core.text_of(n.get("organisation-country-buyer")),
            core.text_of(n.get("publication-date"))[:10] or None,
            core.text_of(n.get("deadline-receipt-tender-date-lot"))[:10] or None,
            main, " ".join(cpvs), classify(" ".join(cpvs)),
            est, ccy, core.to_eur(est, ccy),
            core.text_of(n.get("description-lot")) or None, criteria,
            core.text_of(n.get("internal-identifier-proc")) or None,
            f"https://ted.europa.eu/en/notice/-/detail/{pub}")

UPSERT = """
INSERT INTO tenders
  (publication_number, source, niche, notice_type, title, buyer_name, country,
   publication_date, deadline, cpv_main, cpv_all, subsector,
   estimated_value, estimated_value_currency, value_eur, description_original, award_criteria,
   procedure_ref, url)
VALUES %s
ON CONFLICT (publication_number) DO UPDATE SET
  niche=EXCLUDED.niche, title=EXCLUDED.title, buyer_name=EXCLUDED.buyer_name, deadline=EXCLUDED.deadline,
  estimated_value=EXCLUDED.estimated_value, estimated_value_currency=EXCLUDED.estimated_value_currency,
  value_eur=EXCLUDED.value_eur, description_original=EXCLUDED.description_original,
  award_criteria=EXCLUDED.award_criteria, procedure_ref=EXCLUDED.procedure_ref, subsector=EXCLUDED.subsector
"""

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--niche", default="green")
    ap.add_argument("--days", type=int, default=730)
    args = ap.parse_args()

    niche = get_niche(args.niche)
    classify = core.make_classifier(niche["subsector_rules"])
    query = f"classification-cpv IN ({' '.join(niche['cpv_codes'])}) AND publication-date >= today(-{args.days})"
    print(f"[{args.niche}] {query}")
    notices = fetch_all(query)
    print("Fetched", len(notices), "notices")

    rows = [to_row(n, args.niche, classify) for n in notices if core.text_of(n.get("publication-number"))]
    conn = core.get_conn(); cur = conn.cursor()
    for i in range(0, len(rows), 500):
        execute_values(cur, UPSERT, rows[i:i+500], page_size=500); conn.commit()
    cur.execute("SELECT count(*) FROM tenders WHERE niche=%s", (args.niche,))
    print(f"Done. {cur.fetchone()[0]} '{args.niche}' tenders in DB.")
    cur.close(); conn.close()

if __name__ == "__main__":
    main()