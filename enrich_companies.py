from dotenv import load_dotenv
load_dotenv()

import argparse, time, requests
from tqdm import tqdm
import core

BASE = "https://api.gleif.org/api/v1/lei-records"
H = {"Accept": "application/vnd.api+json"}
ISO3_TO_2 = {"DEU":"DE","POL":"PL","FRA":"FR","ESP":"ES","ITA":"IT","CZE":"CZ","NLD":"NL",
             "BEL":"BE","AUT":"AT","ROU":"RO","BGR":"BG","GRC":"GR","SVN":"SI","HRV":"HR",
             "SVK":"SK","LTU":"LT","LVA":"LV","EST":"EE","FIN":"FI","SWE":"SE","DNK":"DK",
             "PRT":"PT","IRL":"IE","HUN":"HU","LUX":"LU","CYP":"CY","MLT":"MT"}

def norm(s): return "".join(ch for ch in (s or "").lower() if ch.isalnum())

def gleif_lookup(name, country3):
    params = {"filter[entity.legalName]": name, "page[size]": 5}
    iso2 = ISO3_TO_2.get(country3)
    if iso2:
        params["filter[entity.legalAddress.country]"] = iso2
    try:
        recs = requests.get(BASE, params=params, headers=H, timeout=30).json().get("data", [])
    except Exception:
        return None
    if not recs:
        return None
    tgt = norm(name)[:12]
    best = None
    for r in recs:
        gname = (r["attributes"]["entity"].get("legalName") or {}).get("name")
        if norm(gname)[:12] == tgt:
            best = r; break
    best = best or recs[0]
    a = best["attributes"]; ent = a["entity"]
    lei = a.get("lei")
    info = {"lei": lei, "registered_as": ent.get("registeredAs"),
            "status": (a.get("registration") or {}).get("status"),
            "city": (ent.get("legalAddress") or {}).get("city"),
            "parent_name": None, "is_subsidiary": False}
    try:
        pr = requests.get(f"{BASE}/{lei}/ultimate-parent", headers=H, timeout=30)
        if pr.status_code == 200:
            p = pr.json().get("data", {}).get("attributes", {})
            info["parent_name"] = (p.get("entity", {}).get("legalName") or {}).get("name")
            info["is_subsidiary"] = bool(info["parent_name"])
    except Exception:
        pass
    return info

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--niche", default="green")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    conn = core.get_conn(); cur = conn.cursor()
    cur.execute("""SELECT DISTINCT c.id, c.canonical_name, c.country
                   FROM companies c JOIN awards a ON a.company_id=c.id
                   WHERE a.niche=%s AND c.lei IS NULL""", (args.niche,))
    rows = cur.fetchall()
    if args.limit: rows = rows[:args.limit]
    print(len(rows), "companies to enrich")

    upd = """UPDATE companies SET lei=%s, registered_as=%s, gleif_status=%s,
             city=%s, parent_name=%s, is_subsidiary=%s WHERE id=%s"""
    found = 0
    for cid, name, country in tqdm(rows, desc="GLEIF enrich"):
        info = gleif_lookup(name, country)
        if info and info.get("lei"):
            cur.execute(upd, (info["lei"], info["registered_as"], info["status"], info["city"],
                              info["parent_name"], info["is_subsidiary"], cid))
            found += 1
        conn.commit()
        time.sleep(0.6)
    print(f"Matched & enriched {found} of {len(rows)} companies.")
    cur.close(); conn.close()

if __name__ == "__main__":
    main()