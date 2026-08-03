from dotenv import load_dotenv
load_dotenv()

import os, re, ssl, smtplib, argparse, datetime as dt
from email.message import EmailMessage
import core
from psycopg2.extras import RealDictCursor
import report
from playwright.sync_api import sync_playwright

SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASS = os.environ.get("SMTP_PASS")
BRIEF_FROM = os.environ.get("BRIEF_FROM", SMTP_USER)

def customer_html(cur, cust):
    cust = dict(cust)
    niche_name = cust.get("niche") or "green"
    kws = cust.get("keywords")
    cust["kw_re"] = re.compile("|".join(re.escape(k) for k in kws), re.I) if kws else None
    days = cust.get("window_days") or 14
    niche = report.get_niche(niche_name)
    prefixes = tuple(p for p, _ in niche["subsector_rules"])
    green_re = re.compile(niche["keyword_regex"], re.I)
    title = f"{cust['name']} — Weekly Brief"
    return report.render(cur, niche_name, niche["label"], prefixes, green_re, days, title, cust)

def html_to_pdf(html, path):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html, wait_until="load")
        page.pdf(path=path, format="A4", print_background=True,
                 margin={"top": "12mm", "bottom": "12mm", "left": "10mm", "right": "10mm"})
        browser.close()

def send_email(to_addr, subject, pdf_path):
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS):
        raise SystemExit("Set SMTP_HOST / SMTP_USER / SMTP_PASS in .env to send email.")
    msg = EmailMessage()
    msg["From"] = BRIEF_FROM; msg["To"] = to_addr; msg["Subject"] = subject
    msg.set_content("Your weekly procurement brief is attached as a PDF.\n\n— Green Tender Intelligence")
    with open(pdf_path, "rb") as f:
        msg.add_attachment(f.read(), maintype="application", subtype="pdf",
                           filename=os.path.basename(pdf_path))
    ctx = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls(context=ctx)
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="actually email the PDFs (default: just generate them)")
    args = ap.parse_args()

    os.makedirs("reports", exist_ok=True)
    conn = core.get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM customers WHERE active AND email IS NOT NULL")
    custs = cur.fetchall()
    print(len(custs), "active customers")
    for cust in custs:
        html = customer_html(cur, cust)
        pdf = os.path.join("reports", f"customer{cust['id']}_{dt.date.today():%Y-%m-%d}.pdf")
        html_to_pdf(html, pdf)
        print("Wrote", pdf)
        if args.send:
            send_email(cust["email"], f"Your weekly brief — {dt.date.today():%d %b %Y}", pdf)
            print("  emailed to", cust["email"])
    conn.commit(); conn.close()

if __name__ == "__main__":
    main()