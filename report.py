"""
Weekly brief. Usage:
  python report.py --niche green --days 14          # generic niche brief
  python report.py --customer 1 --days 14           # personalized to a customer profile
Writes reports/<niche|customer>_<date>.html.
"""
from dotenv import load_dotenv
load_dotenv()

import argparse, os, re, html, datetime as dt
import requests
import core
from niches import get_niche
from psycopg2.extras import RealDictCursor

def eur(n):
    if not n: return "—"
    n = float(n)
    if n >= 1e6: return f"€{n/1e6:.1f}M"
    if n >= 1e3: return f"€{n/1e3:.0f}k"
    return f"€{n:.0f}"
def esc(s): return html.escape(str(s or ""))
def pct(old, new): return "—" if not old else f"{(new-old)/old*100:+.0f}%"

# personalization: build a country/subsector WHERE fragment + append its param
def _country(alias, countries, params):
    if countries:
        params.append(countries); return f" AND {alias} = ANY(%s)"
    return ""
def _subsector(alias, subsectors, params):
    if subsectors:
        params.append(subsectors); return f" AND {alias} = ANY(%s)"
    return ""

DEEPL_KEY = os.environ.get("DEEPL_API_KEY")
DEEPL_URL = ("https://api-free.deepl.com/v2/translate"
             if (DEEPL_KEY or "").endswith(":fx")
             else "https://api.deepl.com/v2/translate")

def deepl(text):
    if not (text and DEEPL_KEY):
        return None
    try:
        r = requests.post(DEEPL_URL, headers={"Authorization": f"DeepL-Auth-Key {DEEPL_KEY}"},
                          data={"text": text[:900], "target_lang": "EN"}, timeout=20)
        return r.json()["translations"][0]["text"]
    except Exception:
        return None

def shown_description(cur, lot):
    if lot.get("description_en"):
        return lot["description_en"]
    original = lot.get("description_original") or lot.get("lot_title") or ""
    en = deepl(original)
    if en:
        cur.execute("UPDATE lots SET description_en=%s WHERE id=%s", (en, lot["id"])); return en
    return original

def shown_title(cur, lot):
    if lot.get("title_en"):
        return lot["title_en"]
    original = lot.get("lot_title") or lot.get("notice_title") or ""
    en = deepl(original)
    if en:
        cur.execute("UPDATE lots SET title_en=%s WHERE id=%s", (en, lot["id"])); return en
    return original

def window(cur, niche, start, end, countries=None, subsectors=None):
    params = [niche, start, end]
    cc = _country("t.country", countries, params); sc = _subsector("l.subsector", subsectors, params)
    cur.execute(f"""SELECT COALESCE(sum(l.value_eur),0) v, count(*) n
                    FROM lots l JOIN tenders t USING(publication_number)
                    WHERE t.niche=%s AND t.notice_type='contract_notice'
                      AND t.publication_date>=%s AND t.publication_date<%s
                      AND l.subsector NOT IN ('Other','Other green'){cc}{sc}""", params)
    return cur.fetchone()

def market_window(cur, niche, start_days, end_days=0, countries=None, subsectors=None):
    """THE canonical market total for a window — every headline uses this so the
    numbers can't disagree. One row per *notice* (an "opportunity"), valued at its
    highest lot estimate (avoids double-counting notice-level values across lots).
    Window is half-open [today-start_days, today-end_days); noise + Other excluded.
    Returns {opportunities, value_eur} — value_eur is ESTIMATED value."""
    params = [niche, start_days, end_days]
    cc = _country("t.country", countries, params); sc = _subsector("l.subsector", subsectors, params)
    cur.execute(f"""
        SELECT count(*) AS opportunities, COALESCE(sum(v), 0) AS value_eur
        FROM (SELECT t.publication_number, max(l.value_eur) AS v
              FROM lots l JOIN tenders t ON t.publication_number=l.publication_number
              WHERE t.niche=%s AND t.notice_type='contract_notice'
                AND t.publication_date >= current_date - make_interval(days => %s)
                AND t.publication_date <  current_date - make_interval(days => %s)
                AND l.subsector NOT IN ('Other','Other green')
                AND (l.relevance IS DISTINCT FROM 'noise'){cc}{sc}
              GROUP BY t.publication_number) q""", params)
    return cur.fetchone()

def new_opportunities(cur, niche, prefixes, green_re, days, limit=15,
                      countries=None, subsectors=None, min_value=0, kw_re=None,
                      include_expired=False):
    """ONE card per notice (DISTINCT ON collapses a notice's multiple lots to its
    highest-value one, so multi-lot notices don't triplicate), noise excluded, and
    — unless include_expired — only opportunities whose deadline hasn't passed."""
    params = [niche, days]
    cc = _country("t.country", countries, params); sc = _subsector("l.subsector", subsectors, params)
    dl = "" if include_expired else " AND (t.deadline IS NULL OR t.deadline >= current_date)"
    cur.execute(f"""
        SELECT * FROM (
          SELECT DISTINCT ON (t.publication_number)
                 l.id, t.publication_number, t.title AS notice_title, t.buyer_name,
                 t.country, t.publication_date, t.deadline, t.url, l.title AS lot_title, l.title_en,
                 l.description_original, l.description_en, l.cpv_main, l.subsector,
                 l.value_eur, l.region_nuts, l.award_criteria
          FROM lots l JOIN tenders t ON t.publication_number=l.publication_number
          WHERE t.niche=%s AND t.notice_type='contract_notice'
            AND t.publication_date >= current_date - make_interval(days => %s)
            AND (l.relevance IS DISTINCT FROM 'noise'){dl}{cc}{sc}
          ORDER BY t.publication_number, l.value_eur DESC NULLS LAST
        ) q
        ORDER BY value_eur DESC NULLS LAST, publication_date DESC""", params)
    out = []
    for r in cur.fetchall():
        if min_value and r["value_eur"] is not None and r["value_eur"] < min_value:
            continue
        text = f"{r['lot_title']} {r['notice_title']} {r['description_original']}"
        if not ((r["cpv_main"] and r["cpv_main"].startswith(prefixes)) or green_re.search(text or "")):
            continue
        if kw_re and not kw_re.search(text or ""):
            continue
        out.append(r)
        if len(out) >= limit:
            break
    return out
  

def leaderboard(cur, niche, days=365, limit=15, countries=None):
    params = [niche, days]
    cc = _country("a.winner_country", countries, params)
    cur.execute(f"""SELECT COALESCE(c.canonical_name, a.winner_name_raw) AS winner, c.country,
                          count(*) AS wins,
                          COALESCE(sum(a.award_value_eur) FILTER (WHERE NOT a.value_suspect), 0) AS total,
                          round(avg(a.num_bids),1) AS avg_bids,
                          min(a.award_date) AS first_win, max(a.award_date) AS last_win,
                          c.parent_name, c.is_subsidiary, c.city,
                          (SELECT string_agg(DISTINCT l.subsector, ', ')
                             FROM awards a2 JOIN lots l
                               ON l.publication_number=a2.publication_number AND l.lot_id=a2.lot_id
                            WHERE a2.company_id=c.id AND l.subsector NOT IN ('Other','Other green')) AS sectors
                   FROM awards a LEFT JOIN companies c ON c.id=a.company_id
                   WHERE a.niche=%s AND a.award_date >= current_date - make_interval(days => %s)
                     AND a.winner_name_raw IS NOT NULL AND a.winner_name_raw <> ''{cc}
                   GROUP BY 1,2,c.id ORDER BY total DESC, wins DESC LIMIT {int(limit)}""", params)
    return cur.fetchall()

def most_active(cur, niche, days=365, limit=10, countries=None):
    params = [niche, days]
    cc = _country("a.winner_country", countries, params)
    cur.execute(f"""SELECT COALESCE(c.canonical_name, a.winner_name_raw) AS winner, c.country,
                          count(*) AS wins
                   FROM awards a LEFT JOIN companies c ON c.id=a.company_id
                   WHERE a.niche=%s AND a.award_date >= current_date - make_interval(days => %s)
                     AND a.winner_name_raw IS NOT NULL AND a.winner_name_raw <> ''{cc}
                   GROUP BY 1,2 ORDER BY wins DESC LIMIT {int(limit)}""", params)
    return cur.fetchall()

def under_competed(cur, niche, days=365, limit=8, countries=None, subsectors=None):
    params = [niche, days]
    cc = _country("a.winner_country", countries, params); sc = _subsector("l.subsector", subsectors, params)
    cur.execute(f"""SELECT l.subsector, a.winner_country AS country,
                          count(*) AS awards, round(avg(a.num_bids),1) AS avg_bids
                   FROM awards a JOIN lots l
                     ON l.publication_number=a.publication_number AND l.lot_id=a.lot_id
                   WHERE a.niche=%s AND a.num_bids IS NOT NULL
                     AND a.award_date >= current_date - make_interval(days => %s)
                     AND l.subsector NOT IN ('Other','Other green'){cc}{sc}
                     AND a.winner_country IS NOT NULL AND a.winner_country <> ''
                   GROUP BY 1,2 HAVING count(*) >= 3
                   ORDER BY avg_bids ASC LIMIT {int(limit)}""", params)
    return cur.fetchall()

def hotspots(cur, niche, days=90, limit=8, countries=None, subsectors=None):
    params = [niche, days]
    cc = _country("t.country", countries, params); sc = _subsector("l.subsector", subsectors, params)
    cur.execute(f"""SELECT t.country, l.region_nuts,
                          count(*) AS lots, COALESCE(sum(l.value_eur),0) AS value_eur
                   FROM lots l JOIN tenders t USING(publication_number)
                   WHERE t.niche=%s AND t.notice_type='contract_notice'
                     AND l.subsector NOT IN ('Other','Other green') AND l.region_nuts IS NOT NULL
                     AND t.publication_date >= current_date - make_interval(days => %s){cc}{sc}
                   GROUP BY 1,2 ORDER BY lots DESC, value_eur DESC LIMIT {int(limit)}""", params)
    return cur.fetchall()

_GENERIC_TITLE = re.compile(r"^\s*(default lot|lot[e]?\s*\d*|partie\s*\d+|part\s*\d+)\s*$", re.I)

def _best_title(o):
    """The most useful, non-placeholder title for an opportunity row."""
    for cand in (o.get("title_en"), o.get("lot_title"), o.get("notice_title")):
        if cand and cand.strip() and not _GENERIC_TITLE.match(cand):
            return cand
    return o.get("notice_title") or o.get("lot_title") or ""

def summary_paragraph(ops, spots, board, uncontested, market=None):
    parts = []
    if market and market["opportunities"]:
        parts.append(f"<b>{market['opportunities']} new opportunities</b> this period, "
                     f"worth {eur(market['value_eur'])} (est.) combined.")
    elif ops:
        total = sum((o["value_eur"] or 0) for o in ops)
        parts.append(f"<b>{len(ops)} new opportunities</b> this period, worth {eur(total)} (est.) combined.")
    if ops:
        biggest = max(ops, key=lambda o: (o["value_eur"] or 0))
        if biggest["value_eur"]:
            parts.append(f"Largest: “{esc(_best_title(biggest))}” "
                         f"({esc(biggest['country'])}, {eur(biggest['value_eur'])} est.).")
    if spots:
        parts.append(f"Activity is clustering in <b>{esc(spots[0]['region_nuts'])}</b> ({esc(spots[0]['country'])}).")
    if uncontested:
        u = uncontested[0]
        parts.append(f"Thinnest competition: {esc(u['subsector'])} in {esc(u['country'])} (avg {u['avg_bids']} bidders).")
    if board:
        parts.append(f"<b>{esc(board[0]['winner'])}</b> leads award value over the past year.")
    return " ".join(parts) if parts else "No activity to summarize this period."

def trend_paragraph(cur, niche, countries=None, subsectors=None):
    c = market_window(cur, niche, 30, 0, countries, subsectors)
    p = market_window(cur, niche, 60, 30, countries, subsectors)
    y = market_window(cur, niche, 395, 365, countries, subsectors)
    if c["opportunities"] == 0:
        return "Not enough recent data to compute a trend yet."
    return (f"Market activity — last 30 days: <b>{c['opportunities']} tenders published</b> worth "
            f"<b>{eur(c['value_eur'])}</b> (est.) — {pct(p['value_eur'], c['value_eur'])} vs the previous "
            f"30 days, {pct(y['value_eur'], c['value_eur'])} year-on-year. "
            f"<span class='muted'>(Includes tenders already closed; the KPI above counts only open, matching opportunities.)</span>")

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

def render(cur, niche, label, prefixes, green_re, days, title, cust=None):
    cust = cust or {}
    countries = cust.get("countries"); subsectors = cust.get("subsectors")
    min_value = cust.get("min_value_eur") or 0
    kw_re = cust.get("kw_re")

    ops = new_opportunities(cur, niche, prefixes, green_re, days,
                            countries=countries, subsectors=subsectors, min_value=min_value, kw_re=kw_re)
    board = leaderboard(cur, niche, countries=countries)
    uncontested = under_competed(cur, niche, countries=countries, subsectors=subsectors)
    spots = hotspots(cur, niche, countries=countries, subsectors=subsectors)
    active = most_active(cur, niche, countries=countries)
    summary = summary_paragraph(ops, spots, board, uncontested)
    total_val = sum((o["value_eur"] or 0) for o in ops)
    op_countries = {o["country"] for o in ops if o["country"]}

    op_html = ""
    for o in ops:
        desc = shown_description(cur, o)
        t_title = shown_title(cur, o)
        loc = " · ".join(x for x in [esc(o["country"]), esc(o["region_nuts"])] if x and x != "None")
        op_html += (f"<div class='op'><div class='t'><a href='{esc(o['url'])}'>{esc(t_title)}</a></div>"
                    f"<div class='meta'>{esc(o['buyer_name'])} · {loc} · <span class='pill'>{esc(o['subsector'])}</span> · {eur(o['value_eur'])}"
                    f"{' · deadline ' + esc(o['deadline']) if o['deadline'] else ''}"
                    f"{' · ' + esc(o['award_criteria']) if o['award_criteria'] else ''}</div>"
                    f"<div class='desc'>{esc((desc or '')[:280])}</div></div>")

    board_rows = []
    for i, r in enumerate(board, 1):
        bits = []
        if r["sectors"]: bits.append(esc(r["sectors"]))
        if r["city"]: bits.append(esc(r["city"]))
        if r["first_win"]: bits.append(f"since {r['first_win']}")
        if r["is_subsidiary"] and r["parent_name"]: bits.append(f"<b>subsidiary of {esc(r['parent_name'])}</b>")
        board_rows.append(
            f"<tr><td class='rank'>{i}</td><td>{esc(r['winner'])}<br><span class='muted' style='font-size:11px'>{' · '.join(bits)}</span></td>"
            f"<td>{esc(r['country'])}</td><td>{r['wins']}</td><td>{eur(r['total'])}</td>"
            f"<td>{r['avg_bids'] if r['avg_bids'] is not None else '—'}</td><td>{esc(r['last_win'] or '—')}</td></tr>")
    board_html = "".join(board_rows) or "<tr><td colspan=7 class=muted>No award data.</td></tr>"

    uc_html = "".join(
        f"<tr><td>{esc(r['subsector'])}</td><td>{esc(r['country'])}</td><td>{r['awards']}</td><td><b>{r['avg_bids']}</b></td></tr>"
        for r in uncontested) or "<tr><td colspan=4 class=muted>Not enough data.</td></tr>"
    spots_html = "".join(
        f"<tr><td>{esc(r['country'])}</td><td>{esc(r['region_nuts'])}</td><td>{r['lots']}</td><td>{eur(r['value_eur'])}</td></tr>"
        for r in spots) or "<tr><td colspan=4 class=muted>Not enough data.</td></tr>"
    active_html = "".join(
        f"<tr><td class='rank'>{i}</td><td>{esc(r['winner'])}</td><td>{esc(r['country'])}</td><td>{r['wins']}</td></tr>"
        for i, r in enumerate(active, 1)) or "<tr><td colspan=4 class=muted>No data.</td></tr>"

    today = dt.date.today()
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)}</title><style>{CSS}</style></head>
<body><div class="wrap"><div class="header"><div class="brand">{esc(label)} Intelligence</div><h1>{esc(title)}</h1>
    <div class="sub">Window: last {days} days · Generated {today} · Source: TED (EU)</div></div>
  <div class="kpis">
    <div class="kpi"><div class="num">{len(ops)}</div><div class="lbl">Opportunities</div></div>
    <div class="kpi"><div class="num">{eur(total_val)}</div><div class="lbl">Combined value</div></div>
    <div class="kpi"><div class="num">{len(op_countries)}</div><div class="lbl">Countries</div></div></div>
  <h2>This week at a glance</h2><div class="take">{summary}</div>
  <h2>Market trend</h2><div class="take">{trend_paragraph(cur, niche, countries, subsectors)}</div>
  <h2>New opportunities</h2>{op_html or "<p class='muted'>No opportunities in this window.</p>"}
  <h2>Hotspots — where activity clusters (90 days)</h2>
  <table><thead><tr><th>Country</th><th>Region (NUTS)</th><th>Opportunities</th><th>Value</th></tr></thead><tbody>{spots_html}</tbody></table>
  <h2>Lower-competition signals (12 mo)</h2>
  <p class="muted" style="font-size:12px;margin:-6px 0 10px">Segments with the fewest average bidders — a screening signal, not a win-probability prediction. Low counts can reflect incumbents, qualification requirements, or local barriers.</p>
  <table><thead><tr><th>Sub-sector</th><th>Country</th><th>Awards</th><th>Avg bidders</th></tr></thead><tbody>{uc_html}</tbody></table>
  <h2>Largest winners (by awarded value, 12 mo)</h2>
  <table><thead><tr><th>#</th><th>Supplier</th><th>Country</th><th>Wins</th><th>Total awarded</th><th>Avg bids</th><th>Last win</th></tr></thead><tbody>{board_html}</tbody></table>
  <h2>Most active suppliers (by number of wins, 12 mo)</h2>
  <table><thead><tr><th>#</th><th>Supplier</th><th>Country</th><th>Wins</th></tr></thead><tbody>{active_html}</tbody></table>
  <div class="footer">Data © European Union, reused from TED · Values EUR-normalized (approx FX) · Names normalized best-effort.</div>
</div></body></html>"""

def load_customer(cur, cid):
    cur.execute("SELECT * FROM customers WHERE id=%s", (cid,))
    return cur.fetchone()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--niche", default="green")
    ap.add_argument("--customer", type=int, default=None)
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    conn = core.get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)

    cust = None
    if args.customer:
        cust = load_customer(cur, args.customer)
        if not cust:
            raise SystemExit(f"No customer with id {args.customer}")
        niche_name = cust["niche"] or "green"
        kws = cust.get("keywords")
        cust = dict(cust)
        cust["kw_re"] = re.compile("|".join(re.escape(k) for k in kws), re.I) if kws else None
        days = cust.get("window_days") or args.days
        title = f"{cust['name']} — Weekly Brief"
        slug = f"customer{args.customer}"
    else:
        niche_name = args.niche
        days = args.days
        title = f"Weekly {get_niche(niche_name)['label']} Brief — EU"
        slug = args.niche

    niche = get_niche(niche_name)
    prefixes = tuple(p for p, _ in niche["subsector_rules"])
    green_re = re.compile(niche["keyword_regex"], re.I)

    out = args.out or os.path.join("reports", f"{slug}_{dt.date.today():%Y-%m-%d}.html")
    folder = os.path.dirname(out)
    if folder:
        os.makedirs(folder, exist_ok=True)

    page = render(cur, niche_name, niche["label"], prefixes, green_re, days, title, cust)
    conn.commit(); conn.close()
    with open(out, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"Wrote {out}.")

if __name__ == "__main__":
    main()