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

# enrichment columns that get added by enrich_companies.py / build_groups.py and
# must SURVIVE a rebuild (group_id is deliberately excluded — rebuild groups after).
ENRICH_COLS = ["website", "lei", "registered_as", "gleif_status", "city",
               "parent_name", "is_subsidiary"]

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

def keys_for(name, oid):
    """Stable keys for matching a company across a rebuild: id first, then name."""
    ks = []
    if oid and str(oid).strip():
        ks.append(f"id:{str(oid).strip()}")
    ks.append(f"name:{norm_name(name)}")
    return ks

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

# --- 1. SNAPSHOT existing enrichment before we wipe the company layer ---
cur.execute("""SELECT column_name FROM information_schema.columns
               WHERE table_schema='public' AND table_name='companies'""")
present = {r[0] for r in cur.fetchall()}
enrich_cols = [c for c in ENRICH_COLS if c in present]

snapshot = {}      # stable_key -> {col: value}
if enrich_cols:
    cur.execute(f"SELECT canonical_name, official_id, {', '.join(enrich_cols)} FROM companies")
    for row in cur.fetchall():
        cname, oid, vals = row[0], row[1], dict(zip(enrich_cols, row[2:]))
        if not any(v is not None for v in vals.values()):
            continue                       # nothing to preserve for this row
        for k in keys_for(cname, oid):
            snapshot.setdefault(k, vals)
print(f"Snapshotted enrichment for {len(snapshot)} keys across {len(enrich_cols)} columns.")

# --- 2. reset the company layer (awards + tenders untouched), then rebuild cleanly ---
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

# --- 3. RE-ATTACH the snapshotted enrichment to the rebuilt companies ---
restored = 0
if snapshot and enrich_cols:
    cur.execute("SELECT id, canonical_name, official_id FROM companies")
    for cid, cname, oid in tqdm(cur.fetchall(), desc="restoring enrichment"):
        vals = next((snapshot[k] for k in keys_for(cname, oid) if k in snapshot), None)
        if not vals:
            continue
        set_cols = [c for c in enrich_cols if vals.get(c) is not None]
        if not set_cols:
            continue
        clause = ", ".join(f"{c}=%s" for c in set_cols)
        cur.execute(f"UPDATE companies SET {clause} WHERE id=%s",
                    [vals[c] for c in set_cols] + [cid])
        restored += 1
print(f"Restored enrichment on {restored} companies.")

conn.commit()
cur.execute("SELECT count(*) FROM companies")
print("companies:", cur.fetchone()[0])
cur.execute("SELECT count(*) FROM awards WHERE company_id IS NULL")
print("awards still unlinked:", cur.fetchone()[0])
cur.close()
conn.close()