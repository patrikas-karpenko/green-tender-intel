"""
Value sanity flags — re-detect fat-finger award values (value_suspect).

Extracting awards resets this flag, so it must re-run afterward. An award is
flagged suspect when, within its own notice (which has >= 2 awards to compare),
its value is BOTH >= EUR 10M AND more than 100x the notice's median award value
— the classic mistyped-amount pattern (e.g. 748,000,000 PLN among ~4M siblings).

Money rankings then use `... FILTER (WHERE NOT value_suspect)` to stay honest.

Dry-run by default.
    python flag_values.py            # report what WOULD be flagged
    python flag_values.py --apply     # write value_suspect
"""
from dotenv import load_dotenv
load_dotenv()

import argparse
import core
from psycopg2.extras import RealDictCursor

FLOOR_EUR = 10_000_000     # absolute floor: ignore anything under EUR 10M
RATIO     = 100            # ... and must exceed 100x the notice median


def find_suspects(cur):
    cur.execute(f"""
        WITH med AS (
            SELECT publication_number,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY award_value_eur) AS med_val,
                   count(*) AS n
            FROM awards WHERE award_value_eur IS NOT NULL
            GROUP BY publication_number)
        SELECT a.id, a.award_value_eur AS val, m.med_val, m.n,
               COALESCE(c.canonical_name, a.winner_name_raw) AS winner
        FROM awards a
        JOIN med m USING (publication_number)
        LEFT JOIN companies c ON c.id = a.company_id
        WHERE m.n >= 2
          AND a.award_value_eur >= {FLOOR_EUR}
          AND a.award_value_eur > {RATIO} * NULLIF(m.med_val, 0)
        ORDER BY a.award_value_eur DESC""")
    return cur.fetchall()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    conn = core.get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("ALTER TABLE awards ADD COLUMN IF NOT EXISTS value_suspect boolean DEFAULT false")
    conn.commit()

    suspects = find_suspects(cur)
    print(f"{len(suspects)} awards flagged as fat-finger outliers "
          f"(>= EUR {FLOOR_EUR/1e6:.0f}M and > {RATIO}x notice median):\n")
    for s in suspects[:20]:
        val = float(s["val"])
        med = float(s["med_val"] or 0)
        print(f"  EUR {val/1e6:8.1f}M   notice median EUR {med/1e6:6.2f}M   "
              f"({s['n']} awards)   {(s['winner'] or '')[:40]}")
              
    if args.apply:
        cur.execute("UPDATE awards SET value_suspect = false")
        ids = [s["id"] for s in suspects]
        if ids:
            cur.execute("UPDATE awards SET value_suspect = true WHERE id = ANY(%s)", (ids,))
        conn.commit()
        print(f"\nApplied: {len(ids)} awards flagged, the rest cleared.")
    else:
        print("\n(dry-run — nothing written. Re-run with --apply to commit.)")

    cur.close(); conn.close()


if __name__ == "__main__":
    main()