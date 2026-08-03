import requests, sys

name = sys.argv[1] if len(sys.argv) > 1 else "VENSYS Energy AG"
BASE = "https://api.gleif.org/api/v1/lei-records"
H = {"Accept": "application/vnd.api+json"}

r = requests.get(BASE, params={"filter[entity.legalName]": name, "page[size]": 3}, headers=H, timeout=30)
recs = r.json().get("data", [])
print(f"'{name}' -> {len(recs)} match(es)\n")
for rec in recs:
    a = rec["attributes"]; ent = a["entity"]
    print("LEI:         ", a.get("lei"))
    print("  legal name:", (ent.get("legalName") or {}).get("name"))
    print("  registered:", ent.get("registeredAs"))
    print("  legal form:", (ent.get("legalForm") or {}).get("id"))
    print("  status:    ", (a.get("registration") or {}).get("status"))
    print("  address:   ", (ent.get("legalAddress") or {}).get("city"), ent.get("jurisdiction"))
    print()

if recs:
    lei = recs[0]["attributes"]["lei"]
    for rel in ("direct-parent", "ultimate-parent"):
        pr = requests.get(f"{BASE}/{lei}/{rel}", headers=H, timeout=30)
        if pr.status_code == 200:
            p = pr.json().get("data", {}).get("attributes", {})
            print(f"{rel}:", (p.get("entity", {}).get("legalName") or {}).get("name"))
        else:
            print(f"{rel}: none ({pr.status_code})")