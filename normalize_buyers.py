from dotenv import load_dotenv
load_dotenv()
from tqdm import tqdm

import re
import core

def norm(s):
    s = (s or "").lower()
    s = re.sub(r"[^\w ]", " ", s, flags=re.UNICODE)   # keep letters/digits incl. ł, ó, ß…
    s = re.sub(r"\s+", " ", s).strip()
    return s

conn = core.get_conn(); cur = conn.cursor()
cur.execute("SELECT DISTINCT buyer_name, country FROM tenders WHERE buyer_name IS NOT NULL AND buyer_name <> ''")
rows = cur.fetchall()
print(len(rows), "distinct buyer strings")

for name, country in tqdm(rows, desc="normalizing..." ):
    key = f"{(country or '').upper()}:{norm(name)}"
    cur.execute("SELECT id FROM buyers WHERE norm_key = %s", (key,))
    r = cur.fetchone()
    if r:
        bid = r[0]
    else:
        cur.execute("INSERT INTO buyers (canonical_name, country, norm_key) VALUES (%s,%s,%s) RETURNING id",
                    (name, country, key))
        bid = cur.fetchone()[0]
    cur.execute("UPDATE tenders SET buyer_id = %s WHERE buyer_name = %s AND COALESCE(country,'') = COALESCE(%s,'')",
                (bid, name, country))

conn.commit()
cur.execute("SELECT count(*) FROM buyers")
print("buyers:", cur.fetchone()[0])
cur.close(); conn.close()