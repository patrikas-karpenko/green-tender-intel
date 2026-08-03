import requests
from extract_awards_xml import parse_notice
xml = requests.get("https://ted.europa.eu/en/notice/157314-2026/xml", timeout=60).text
for w in parse_notice(xml):
    print(w["lot"], "|", w["value"], w["currency"], "|", w["name"])