"""
Corporate-group resolver — cluster legal entities into groups.

Makes the corporate group a first-class entity so "Company X Germany GmbH",
"Company X France SAS", "Company X Polska" read as ONE competitor. Precision-first:
we cluster ONLY via the explicit parent/subsidiary links we already hold from
GLEIF — a subsidiary's parent_name matching a real company, and siblings that
share the same parent — never fuzzy name matching (which causes false merges).

Creates `company_groups` and `companies.group_id`. Only multi-member groups get a
row; a standalone company stays group_id NULL (it is its own group implicitly).

Dry-run by default.
    python build_groups.py            # report the groups it WOULD form
    python build_groups.py --apply     # write company_groups + group_id
"""
from dotenv import load_dotenv
load_dotenv()

import argparse, re, collections
import core
from psycopg2.extras import RealDictCursor
from tqdm import tqdm

# strip punctuation + common legal forms so a parent_name matches the parent's name
_LEGAL = re.compile(
    r"\b(gmbh|ag|sas|sarl|sa|sl|slu|sau|sp\s*z\s*o\s*o|sp\s*k|bv|nv|ltd|limited|plc|"
    r"srl|spa|oy|ab|a\s*s|as|se|group|groupe|holding|holdings|co|kg)\b", re.I)

def norm(name):
    s = (name or "").lower()
    s = re.sub(r"[^\w\s]", " ", s)
    s = _LEGAL.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# ---------- union-find ----------
class UF:
    def __init__(self):
        self.p = {}
    def find(self, x):
        self.p.setdefault(x, x)
        root = x
        while self.p[root] != root:
            root = self.p[root]
        while self.p[x] != root:      # path compression
            self.p[x], x = root, self.p[x]
        return root
    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb


def build(cur):
    """Return (components, meta): components = {root_id: [member_ids]},
    meta = per-company dict. Clusters via parent links only."""
    cur.execute("""SELECT id, canonical_name, country, parent_name, is_subsidiary
                   FROM companies""")
    companies = cur.fetchall()
    meta = {c["id"]: c for c in companies}

    # index: normalized canonical name -> company ids
    by_name = collections.defaultdict(list)
    for c in tqdm(companies, desc="Indexing names"):
        by_name[norm(c["canonical_name"])].append(c["id"])

    # award counts (to pick a sensible group root / name)
    cur.execute("""SELECT company_id, count(*) AS wins FROM awards
                   WHERE company_id IS NOT NULL GROUP BY 1""")
    wins = {r["company_id"]: r["wins"] for r in cur.fetchall()}

    uf = UF()
    # 1) each subsidiary links to its parent company (if the parent is in our DB)
    # 2) siblings that share the same parent_name link together
    by_parent = collections.defaultdict(list)
    for c in tqdm(companies, desc="Linking parents"):
        pn = c.get("parent_name")
        if not pn:
            continue
        pk = norm(pn)
        if not pk:
            continue
        by_parent[pk].append(c["id"])
        for pid in by_name.get(pk, []):        # link to the parent entity itself
            if pid != c["id"]:
                uf.union(c["id"], pid)
    for pk, members in tqdm(by_parent.items(), desc="Linking siblings", total=len(by_parent)):
        for m in members[1:]:
            uf.union(members[0], m)

    comps = collections.defaultdict(list)
    for c in tqdm(companies, desc="Building components"):
        comps[uf.find(c["id"])].append(c["id"])

    return {r: m for r, m in comps.items() if len(m) >= 2}, meta, wins


def group_name(members, meta, wins):
    """Pick a display name + root company for a group."""
    # prefer a non-subsidiary member (likely the parent), by most wins
    non_subs = [m for m in members if not meta[m]["is_subsidiary"]]
    pool = non_subs or members
    root = max(pool, key=lambda m: wins.get(m, 0))
    # if all members are subsidiaries, prefer the shared parent_name as the label
    if not non_subs:
        pnames = [meta[m]["parent_name"] for m in members if meta[m]["parent_name"]]
        if pnames:
            label = collections.Counter(pnames).most_common(1)[0][0]
            return label, None
    return meta[root]["canonical_name"], root

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--samples", type=int, default=15)
    args = ap.parse_args()

    conn = core.get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    groups, meta, wins = build(cur)
    total_members = sum(len(m) for m in groups.values())
    print(f"{len(groups)} multi-entity groups covering {total_members} legal entities.\n")

    ranked = sorted(groups.values(), key=len, reverse=True)
    print(f"Biggest groups:")
    for members in tqdm(groups.values(), desc="Writing groups", total=len(groups)):
        name, root = group_name(members, meta, wins)
        countries = sorted({meta[m]["country"] or "—" for m in members})
        print(f"\n  {name}  ({len(members)} entities · {', '.join(countries)})")
        for m in sorted(members, key=lambda m: wins.get(m, 0), reverse=True)[:8]:
            print(f"     {(meta[m]['canonical_name'] or '')[:36]:36} "
                  f"{meta[m]['country'] or '—':4} {wins.get(m,0):>4}w  "
                  f"parent → {(meta[m]['parent_name'] or '—')[:26]}")

    if args.apply:
        cur.execute("CREATE TABLE IF NOT EXISTS company_groups ("
                    "id bigint generated always as identity primary key, "
                    "name text, root_company_id bigint, member_count int, "
                    "created_at timestamptz default now())")
        cur.execute("ALTER TABLE companies ADD COLUMN IF NOT EXISTS group_id bigint")
        cur.execute("UPDATE companies SET group_id=NULL")
        cur.execute("DELETE FROM company_groups")
        conn.commit()
        for members in tqdm(groups.values(),desc="doing stuff..."):
            name, root = group_name(members, meta, wins)
            cur.execute("INSERT INTO company_groups (name, root_company_id, member_count) "
                        "VALUES (%s,%s,%s) RETURNING id", (name, root, len(members)))
            gid = cur.fetchone()["id"]
            cur.execute("UPDATE companies SET group_id=%s WHERE id = ANY(%s)", (gid, members))
        conn.commit()
        print(f"\nApplied: {len(groups)} groups written.")
    else:
        print(f"\n(dry-run — nothing written. Re-run with --apply to commit.)")

    cur.close(); conn.close()

if __name__ == "__main__":
    main()