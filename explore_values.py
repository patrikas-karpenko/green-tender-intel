import requests, sys
import xml.etree.ElementTree as ET
pub = sys.argv[1]
xml = requests.get(f"https://ted.europa.eu/en/notice/{pub}/xml", timeout=60).text
root = ET.fromstring(xml)
def loc(e): return e.tag.split('}')[-1]
print("=== monetary amounts ===")
for e in root.iter():
    if "Amount" in loc(e) and e.text and e.text.strip():
        print(f"  {loc(e)} = {e.text.strip()} {e.get('currencyID','')}")
print("\n=== framework-agreement indicators ===")
for e in root.iter():
    if loc(e) == "ContractingSystemTypeCode" and e.get("listName") == "framework-agreement":
        print("  framework-agreement:", e.text)
print("\n=== lots ===", len([e for e in root.iter() if loc(e) == "ProcurementProjectLot"]))