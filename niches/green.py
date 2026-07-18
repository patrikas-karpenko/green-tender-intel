NICHE = {
    "name": "green",
    "label": "Green Procurement",
    "cpv_codes": ["09330000","09331000","09331100","09331200","09332000",
                  "45261215","31121340","42511110","45251100"],
    "subsector_rules": [("09331","Solar PV"),("09332","Solar PV"),("45261215","Solar PV"),
        ("09330","Solar (other)"),("31121340","Wind"),("42511","Heat pumps"),
        ("45251","Power plant / grid")],
    "keyword_regex": (r"solar|solaire|photovolta|fotowolta|fotovolta|photovoltaik|pv[- ]?anlage|"
                      r"panele słoneczne|éolien|windkraft|windpark|wiatrow|heat ?pump|pompe à chaleur|"
                      r"wärmepumpe|pompa ciepła|pompa di calore|warmtepomp|tepelné čerpadlo|"
                      r"odnawial|renewable|erneuerbare|renouvelable|geotherm|biomas|"
                      r"magazyn energii|battery|bess|\boze\b"),
}