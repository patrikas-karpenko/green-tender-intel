from dotenv import load_dotenv
load_dotenv()

import os
import psycopg2

conn = psycopg2.connect(os.environ["DATABASE_URL"])
cur = conn.cursor()

cur.execute("""
    SELECT COALESCE(c.canonical_name, a.winner_name_raw) AS winner,
           c.country,
           count(*) AS wins
    FROM awards a
    LEFT JOIN companies c ON c.id = a.company_id
    GROUP BY 1, 2
    ORDER BY wins DESC
    LIMIT 15
""")

print(f"{'#':>2}  {'Winner':<40} {'Country':<8} {'Wins':>5}")
print("-" * 62)
for i, (winner, country, wins) in enumerate(cur.fetchall(), 1):
    print(f"{i:>2}  {winner[:40]:<40} {country or '—':<8} {wins:>5}")

cur.close()
conn.close()