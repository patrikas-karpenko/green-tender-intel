from dotenv import load_dotenv
from tqdm import tqdm
load_dotenv()

import os
import re
import psycopg2

LEGAL_SUFFIXES = ["sp z o o", "sp zoo", "s a", "s p a", "gmbh", "ag", "ltd",
                  "limited", "oy", "ab", "as", "srl", "s r l", "bv", "b v",
                  "nv", "n v", "sarl", "s l", "sl", "spa", "plc", "sp k", "sp j",
                  "d o o", "a e b e", "aebe", "s r o", "sro"]

def norm_name(name):
    s = (name or "").lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)     # drop punctuation
    s = re.sub(r"\s+", " ", s).strip()    # collapse spaces
    changed = True
    while changed:                        # strip trailing legal forms repeatedly
        changed = False
        for suf in LEGAL_SUFFIXES:
            if s.endswith(" " + suf):
                s = s[:-len(suf)].strip()
                changed = True
    return s

def get_company(cur, name, oid, country):
    """Find-or-create a company, matching on ID key OR name key, and register
    both aliases so future records converge no matter which key they carry."""
    id_key = f"id:{str(oid).strip()}" if oid and str(oid).strip() else None
    name_key = f"name:{norm_name(name)}"

    company_id = None
    for key in (id_key, name_key):        # ID first, then name fallback
        if not key:
            continue
        cur.execute("SELECT company_id FROM company_aliases WHERE alias_norm = %s", (key,))
        row = cur.fetchone()
        if row:
            company_id = row[0]
            break

    if company_id is None:                # brand-new company
        cur.execute(
            "INSERT INTO companies (canonical_name, official_id, country) VALUES (%s,%s,%s) RETURNING id",
            (name, oid, country))
        company_id = cur.fetchone()[0]

    # make sure BOTH keys point at this company (idempotent)
    for key, src, conf in ((id_key, "id_match", 1.0), (name_key, "name_norm", 0.9)):
        if key:
            cur.execute(
                """INSERT INTO company_aliases
                     (alias_norm, alias_name, official_id, company_id, match_source, confidence)
                   VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (alias_norm) DO NOTHING""",
                (key, name, oid, company_id, src, conf))
    return company_id

conn = psycopg2.connect(os.environ["DATABASE_URL"])
cur = conn.cursor()

# reset the company layer (awards + tenders untouched), then rebuild cleanly
cur.execute("UPDATE awards SET company_id = NULL")
cur.execute("DELETE FROM company_aliases")
cur.execute("DELETE FROM companies")

cur.execute("""
    SELECT DISTINCT winner_name_raw, winner_official_id, winner_country
    FROM awards WHERE winner_name_raw IS NOT NULL
""")
winners = cur.fetchall()
print("Distinct winner rows:", len(winners))

for name, oid, country in tqdm(winners, desc="normalizing..."):
    company_id = get_company(cur, name, oid, country)
    cur.execute(
        """UPDATE awards SET company_id = %s
           WHERE winner_name_raw = %s
             AND COALESCE(winner_official_id,'') = COALESCE(%s,'')""",
        (company_id, name, oid))

conn.commit()
cur.execute("SELECT count(*) FROM companies")
print("companies:", cur.fetchone()[0])
cur.execute("SELECT count(*) FROM awards WHERE company_id IS NULL")
print("awards still unlinked:", cur.fetchone()[0])
cur.close()
conn.close()