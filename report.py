"""
Weekly brief for a NICHE. Usage: python report.py --niche green --days 14
Writes reports/<niche>_<date>.html. Translation via DeepL (DEEPL_API_KEY in .env).
"""
from dotenv import load_dotenv
load_dotenv()

import argparse, os, re, html, datetime as dt
import requests
import core
from niches import get_niche
from psycopg2.extras import RealDictCursor

# ---------- formatting ----------
def eur(n):
    if not n: return "—"
    n = float(n)
    if n >= 1e6: return f"€{n/1e6:.1f}M"
    if n >= 1e3: return f"€{n/1e3:.0f}k"
    return f"€{n:.0f}"
def esc(s): return html.escape(str(s or ""))
def pct(old, new): return "—" if not old else f"{(new-old)/old*100:+.0f}%"

# ---------- lazy translation (DeepL, cached in lots.description_en) ----------
DEEPL_KEY = os.environ.get("DEEPL_API_KEY")
DEEPL_URL = ("https://api-free.deepl.com/v2/translate"
             if (DEEPL_KEY or "").endswith(":fx")
             else "https://api.deepl.com/v2/translate")

def deepl(text):
    if not (text and DEEPL_KEY):
        return None
    try:
        r = requests.post(DEEPL_URL,
                          headers={"Authorization": f"DeepL-Auth-Key {DEEPL_KEY}"},
                          data={"text": text[:900], "target_lang": "EN"},
                          timeout=20)
        return r.json()["translations"][0]["text"]
    except Exception:
        return None

def shown_description(cur, lot):
    if lot.get("description_en"):
        return lot["description_en"]
    original = lot.get("description_original") or lot.get("lot_title") or ""
    en = deepl(original)
    if en:
        cur.execute("UPDATE lots SET description_en=%s WHERE id=%s", (en, lot["id"]))
        return en
    return original

def shown_title(cur, lot):
    if lot.get("title_en"):
        return lot["title_en"]
    original = lot.get("lot_title") or lot.get("notice_title") or ""
    en = deepl(original)
    if en:
        cur.execute("UPDATE lots SET title_en=%s WHERE id=%s", (en, lot["id"]))
        return en
    return original

# ---------- queries ----------
def window(cur, niche, start, end):
    cur.execute("""SELECT COALESCE(sum(l.value_eur),0) v, count(*) n
                   FROM lots l JOIN tenders t USING(publication_number)
                   WHERE t.niche=%s AND t.notice_type='contract_notice'
                     AND t.publication_date>=%s AND t.publication_date<%s
                     AND l.subsector NOT IN ('Other','Other green')""", (niche, start, end))
    return cur.fetchone()

def new_opportunities(cur, niche, prefixes, green_re, days, limit=15):
    cur.execute("""SELECT l.id, t.publication_number, t.title AS notice_title, t.buyer_name,
                          t.country, t.publication_date, t.deadline, t.url, l.title AS lot_title, l.title_en,
                          l.description_original, l.description_en, l.cpv_main, l.subsector,
                          l.value_eur, l.region_nuts, l.award_criteria
                   FROM lots l JOIN tenders t USING(publication_number)
                   WHERE t.niche=%s AND t.notice_type='contract_notice'
                     AND t.publication_date >= current_date - make_interval(days => %s)
                   ORDER BY l.value_eur DESC NULLS LAST, t.publication_date DESC""", (niche, days))
    out = []
    for r in cur.fetchall():
        text = f"{r['lot_title']} {r['notice_title']} {r['description_original']}"
        if (r["cpv_main"] and r["cpv_main"].startswith(prefixes)) or green_re.search(text or ""):
            out.append(r)
        if len(out) >= limit:
            break
    return out

def leaderboard(cur, niche, days=365, limit=15):
    cur.execute("""SELECT COALESCE(c.canonical_name, a.winner_name_raw) AS winner, c.country,
                          count(*) AS wins, COALESCE(sum(a.award_value_eur),0) AS total,
                          round(avg(a.num_bids),1) AS avg_bids,
                          (SELECT string_agg(DISTINCT l.subsector, ', ')
                             FROM awards a2 JOIN lots l
                               ON l.publication_number=a2.publication_number AND l.lot_id=a2.lot_id
                            WHERE a2.company_id=c.id AND l.subsector NOT IN ('Other','Other green')) AS sectors
                   FROM awards a LEFT JOIN companies c ON c.id=a.company_id
                   WHERE a.niche=%s AND a.award_date >= current_date - make_interval(days => %s)
                   GROUP BY 1,2,c.id ORDER BY total DESC, wins DESC LIMIT %s""", (niche, days, limit))
    return cur.fetchall()

def under_competed(cur, niche, days=365, limit=8):
    cur.execute("""SELECT l.subsector, a.winner_country AS country,
                          count(*) AS awards, round(avg(a.num_bids),1) AS avg_bids
                   FROM awards a JOIN lots l
                     ON l.publication_number=a.publication_number AND l.lot_id=a.lot_id
                   WHERE a.niche=%s AND a.num_bids IS NOT NULL
                     AND a.award_date >= current_date - make_interval(days => %s)
                     AND l.subsector NOT IN ('Other','Other green')
                   GROUP BY 1,2 HAVING count(*) >= 3
                   ORDER BY avg_bids ASC LIMIT %s""", (niche, days, limit))
    return cur.fetchall()

def trend_paragraph(cur, niche):
    today = dt.date.today()
    c = window(cur, niche, today-dt.timedelta(days=30), today)
    p = window(cur, niche, today-dt.timedelta(days=60), today-dt.timedelta(days=30))
    y = window(cur, niche, today-dt.timedelta(days=395), today-dt.timedelta(days=365))
    if c["n"] == 0:
        return "Not enough recent data to compute a trend yet."
    return (f"In the last 30 days there were <b>{c['n']} new lots</b> worth <b>{eur(c['v'])}</b> "
            f"({pct(p['v'], c['v'])} vs the previous 30 days, {pct(y['v'], c['v'])} year-on-year).")

# ---------- HTML ----------
CSS = """
:root{--ink:#12261f;--green:#1f8a4c;--soft:#e5f3ea;--muted:#6b7c74;--line:#e2e8e4}
*{box-sizing:border-box}body{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:var(--ink);
margin:0;background:#f4f7f5}.wrap{max-width:840px;margin:0 auto;background:#fff;padding:40px 46px}
.header{border-bottom:3px solid var(--green);padding-bottom:16px;margin-bottom:22px}
.brand{color:var(--green);font-weight:700;letter-spacing:.5px;text-transform:uppercase;font-size:12px}
h1{font-size:23px;margin:6px 0 4px}.sub{color:var(--muted);font-size:13px}
.kpis{display:flex;gap:14px;margin:22px 0;flex-wrap:wrap}.kpi{flex:1;min-width:140px;background:var(--soft);border-radius:10px;padding:15px}
.kpi .num{font-size:24px;font-weight:700;color:var(--green)}.kpi .lbl{font-size:12px;color:var(--muted)}
h2{font-size:15px;text-transform:uppercase;letter-spacing:.4px;border-bottom:1px solid var(--line);padding-bottom:6px;margin:30px 0 14px}
.take{background:#f0f6f2;border-left:4px solid var(--green);padding:14px 16px;border-radius:6px;font-size:14.5px;line-height:1.55}
.op{border-bottom:1px solid var(--line);padding:12px 0}.op .t{font-weight:600}.op .t a{color:var(--green);text-decoration:none}
.op .meta{color:var(--muted);font-size:12px;margin:3px 0}.op .desc{font-size:13px;margin-top:5px}
.pill{background:var(--soft);color:var(--green);border-radius:20px;padding:2px 8px;font-size:11px}
table{width:100%;border-collapse:collapse;font-size:13px}th{text-align:left;color:var(--muted);font-weight:600;font-size:11px;
text-transform:uppercase;padding:8px;border-bottom:1px solid var(--line)}td{padding:9px 8px;border-bottom:1px solid var(--line)}
.rank{font-weight:700;color:var(--green);width:22px}.muted{color:var(--muted)}
.footer{margin-top:30px;padding-top:14px;border-top:1px solid var(--line);color:var(--muted);font-size:11px}
@media print{body{background:#fff}.wrap{padding:0}}
"""

def render(cur, niche, label, prefixes, green_re, days, title):
    ops = new_opportunities(cur, niche, prefixes, green_re, days)
    board = leaderboard(cur, niche)
    uncontested = under_competed(cur, niche)
    total_val = sum((o["value_eur"] or 0) for o in ops)
    countries = {o["country"] for o in ops if o["country"]}

    op_html = ""
    for o in ops:
        desc = shown_description(cur, o)
        title = shown_title(cur, o)
        loc = " · ".join(x for x in [esc(o["country"]), esc(o["region_nuts"])] if x and x != "None")
        op_html += (f"<div class='op'><div class='t'><a href='{esc(o['url'])}'>{esc(title)}</a></div>"
                    f"<div class='meta'>{esc(o['buyer_name'])} · {loc} · <span class='pill'>{esc(o['subsector'])}</span> · {eur(o['value_eur'])}"
                    f"{' · deadline ' + esc(o['deadline']) if o['deadline'] else ''}"
                    f"{' · ' + esc(o['award_criteria']) if o['award_criteria'] else ''}</div>"
                    f"<div class='desc'>{esc((desc or '')[:280])}</div></div>")

    board_html = "".join(
        f"<tr><td class='rank'>{i}</td><td>{esc(r['winner'])}"
        f"<br><span class='muted' style='font-size:11px'>{esc(r['sectors'])}</span></td>"
        f"<td>{esc(r['country'])}</td><td>{r['wins']}</td><td>{eur(r['total'])}</td>"
        f"<td>{r['avg_bids'] if r['avg_bids'] is not None else '—'}</td></tr>"
        for i, r in enumerate(board, 1)) or "<tr><td colspan=6 class=muted>No award data.</td></tr>"

    uc_html = "".join(
        f"<tr><td>{esc(r['subsector'])}</td><td>{esc(r['country'])}</td>"
        f"<td>{r['awards']}</td><td><b>{r['avg_bids']}</b></td></tr>"
        for r in uncontested) or "<tr><td colspan=4 class=muted>Not enough data.</td></tr>"

    today = dt.date.today()
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)}</title><style>{CSS}</style></head>
<body><div class="wrap"><div class="header"><div class="brand">{esc(label)} Intelligence</div><h1>{esc(title)}</h1>
    <div class="sub">Window: last {days} days · Generated {today} · Source: TED (EU)</div></div>
  <div class="kpis">
    <div class="kpi"><div class="num">{len(ops)}</div><div class="lbl">Opportunities</div></div>
    <div class="kpi"><div class="num">{eur(total_val)}</div><div class="lbl">Combined value</div></div>
    <div class="kpi"><div class="num">{len(countries)}</div><div class="lbl">Countries</div></div></div>
  <h2>Market trend</h2><div class="take">{trend_paragraph(cur, niche)}</div>
  <h2>New opportunities</h2>{op_html or "<p class='muted'>No opportunities in this window.</p>"}
  <h2>Where you can win — least-contested segments (12 mo)</h2>
  <table><thead><tr><th>Sub-sector</th><th>Country</th><th>Awards</th><th>Avg bidders</th></tr></thead>
    <tbody>{uc_html}</tbody></table>
  <h2>Who is winning? (12 mo, by value)</h2>
  <table><thead><tr><th>#</th><th>Supplier</th><th>Country</th><th>Wins</th><th>Total awarded</th><th>Avg bids</th></tr></thead>
    <tbody>{board_html}</tbody></table>
  <div class="footer">Data © European Union, reused from TED · Values EUR-normalized (approx FX) · Names normalized best-effort.</div>
</div></body></html>"""

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--niche", default="green")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--out", default=None)   # default: reports/<niche>_<date>.html
    args = ap.parse_args()
    niche = get_niche(args.niche)
    prefixes = tuple(p for p, _ in niche["subsector_rules"])
    green_re = re.compile(niche["keyword_regex"], re.I)
    title = f"Weekly {niche['label']} Brief — EU"

    out = args.out or os.path.join("reports", f"{args.niche}_{dt.date.today():%Y-%m-%d}.html")
    folder = os.path.dirname(out)
    if folder:
        os.makedirs(folder, exist_ok=True)

    conn = core.get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    page = render(cur, args.niche, niche["label"], prefixes, green_re, args.days, title)
    conn.commit(); conn.close()
    with open(out, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"Wrote {out} for niche '{args.niche}' (last {args.days} days).")

if __name__ == "__main__":
    main()