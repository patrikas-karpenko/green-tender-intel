"""Shared helpers: DB, TED value/text parsing, FX, classification."""
import os
import psycopg2

FX_TO_EUR = {"EUR":1.0,"BGN":0.511,"DKK":0.134,"CZK":0.040,"PLN":0.235,"HUF":0.0025,
             "RON":0.201,"SEK":0.088,"NOK":0.086,"GBP":1.18,"CHF":1.06,"ISK":0.0068}

def get_conn():
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL is not set.")
    return psycopg2.connect(dsn)

def text_of(value):
    if value is None: return ""
    if isinstance(value, str): return value
    if isinstance(value, list): return text_of(value[0]) if value else ""
    if isinstance(value, dict):
        return text_of(value["eng"]) if "eng" in value else text_of(next(iter(value.values())))
    return str(value)

def safe_float(v):
    try: return float(v)
    except (TypeError, ValueError): return None

def first_num(value):
    if isinstance(value, list): value = value[0] if value else None
    return safe_float(value)

def to_eur(amount, ccy):
    x = safe_float(amount); r = FX_TO_EUR.get((ccy or "EUR").upper())
    return round(x*r, 2) if (x is not None and r) else None

def make_classifier(rules):
    def classify(cpv_all):
        for code in str(cpv_all or "").split():
            for prefix, label in rules:
                if code.startswith(prefix): return label
        return "Other"
    return classify