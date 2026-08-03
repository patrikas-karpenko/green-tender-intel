"""
The tiered brief — assembles every feature into one report.
"""
from dotenv import load_dotenv
load_dotenv()

import argparse, os, re, datetime as dt
import core
from psycopg2.extras import RealDictCursor
from niches import get_niche

import report
import profiles, opportunity, buyer, analytics, news


def _title(cur, o):
    """Translated title, but never a placeholder: if the (translated) lot title is
    a generic 'Default lot', fall back to the descriptive English notice title."""
    t = report.shown_title(cur, o)
    if opportunity._is_generic(t):
        t = o.get("notice_title") or t
    return t


def featured_company_ids(cur, niche, countries, subsectors, n=2):
    params = [niche]
    cc = sc = ""
    if countries:
        params.append(countries); cc = " AND a.winner_country = ANY(%s)"
    if subsectors:
        params.append(subsectors); sc = " AND l.subsector = ANY(%s)"
    cur.execute(f"""
        SELECT a.company_id, count(*) AS wins
        FROM awards a
        JOIN lots l ON l.publication_number=a.publication_number AND l.lot_id=a.lot_id
        WHERE a.niche=%s AND a.company_id IS NOT NULL
          AND l.subsector NOT IN ('Other','Other green'){cc}{sc}
        GROUP BY 1 ORDER BY 2 DESC LIMIT {int(n)}""", params)
    return [r["company_id"] for r in cur.fetchall()]


def spotlight_buyer(cur, niche, countries):
    params = [niche]
    cc = ""
    if countries:
        params.append(countries); cc = " AND t.country = ANY(%s)"
    cur.execute(f"""
        SELECT t.buyer_name AS name, t.country, count(*) AS awards
        FROM awards a JOIN tenders t ON t.publication_number=a.publication_number
        WHERE a.niche=%s AND t.buyer_name IS NOT NULL AND t.buyer_name <> ''{cc}
        GROUP BY 1,2 ORDER BY 3 DESC LIMIT 1""", params)
    return cur.fetchone()


def _css():
    return (profiles.PROFILE_CSS + opportunity.OPP_CSS + news.NEWS_CSS
            + analytics.EXTRA_CSS + report.CSS
            + ".tier{margin:34px 0 8px;font-size:12px;letter-spacing:1px;text-transform:uppercase;"
              "color:var(--green);font-weight:700;border-top:2px solid var(--green);padding-top:10px}"
            + ".hl{border-bottom:1px solid var(--line);padding:9px 0;font-size:13.5px}"
              ".hl a{color:var(--green);text-decoration:none;font-weight:600}"
              ".hl .m{color:var(--muted);font-size:12px}"
            + ".wrap .prof{max-width:none;background:transparent;border:0;border-radius:0;"
              "padding:0;margin:0 0 18px}"
            + "body{padding:0}")


def _tables(cur, niche, countries, subsectors):
    board = report.leaderboard(cur, niche, countries=countries)
    active = report.most_active(cur, niche, countries=countries)
    uncontested = report.under_competed(cur, niche, countries=countries, subsectors=subsectors)
    spots = report.hotspots(cur, niche, countries=countries, subsectors=subsectors)

    board_rows = "".join(
        f"<tr><td class='rank'>{i}</td><td>{report.esc(r['winner'])}</td><td>{report.esc(r['country'])}</td>"
        f"<td>{r['wins']}</td><td>{report.eur(r['total'])}</td><td>{report.esc(r['last_win'] or '—')}</td></tr>"
        for i, r in enumerate(board, 1)) or "<tr><td colspan=6 class='muted'>No award data.</td></tr>"
    active_rows = "".join(
        f"<tr><td class='rank'>{i}</td><td>{report.esc(r['winner'])}</td><td>{report.esc(r['country'])}</td><td>{r['wins']}</td></tr>"
        for i, r in enumerate(active, 1)) or "<tr><td colspan=4 class='muted'>No data.</td></tr>"
    uc_rows = "".join(
        f"<tr><td>{report.esc(r['subsector'])}</td><td>{report.esc(r['country'])}</td><td>{r['awards']}</td><td><b>{r['avg_bids']}</b></td></tr>"
        for r in uncontested) or "<tr><td colspan=4 class='muted'>Not enough data.</td></tr>"
    spot_rows = "".join(
        f"<tr><td>{report.esc(r['country'])}</td><td>{report.esc(r['region_nuts'])}</td><td>{r['lots']}</td><td>{report.eur(r['value_eur'])}</td></tr>"
        for r in spots) or "<tr><td colspan=4 class='muted'>Not enough data.</td></tr>"

    return f"""
      <h2>Hotspots — where activity clusters (90 days)</h2>
      <table><thead><tr><th>Country</th><th>Region</th><th>Opportunities</th><th>Value</th></tr></thead><tbody>{spot_rows}</tbody></table>
      <h2>Lower-competition signals (12 mo)</h2>
      <p class="muted" style="font-size:12px;margin:-6px 0 10px">Fewest average bidders — a screening signal, not a win-probability prediction.</p>
      <table><thead><tr><th>Sub-sector</th><th>Country</th><th>Awards</th><th>Avg bidders</th></tr></thead><tbody>{uc_rows}</tbody></table>
      <h2>Largest winners (by awarded value, 12 mo)</h2>
      <table><thead><tr><th>#</th><th>Supplier</th><th>Country</th><th>Wins</th><th>Total awarded</th><th>Last win</th></tr></thead><tbody>{board_rows}</tbody></table>
      <h2>Most active suppliers (by wins, 12 mo)</h2>
      <table><thead><tr><th>#</th><th>Supplier</th><th>Country</th><th>Wins</th></tr></thead><tbody>{active_rows}</tbody></table>"""


def build_brief(cur, niche_name, days, cust=None, title=None):
    cust = cust or {}
    countries  = cust.get("countries")
    subsectors = cust.get("subsectors")
    min_value  = cust.get("min_value_eur") or 0
    kw_re      = cust.get("kw_re")

    niche = get_niche(niche_name)
    label = niche["label"]
    prefixes = tuple(p for p, _ in niche["subsector_rules"])
    green_re = re.compile(niche["keyword_regex"], re.I)
    title = title or f"Weekly {label} Brief — EU"
    today = dt.date.today()

    ops_all = report.new_opportunities(cur, niche_name, prefixes, green_re, days, limit=10000,
                                       countries=countries, subsectors=subsectors,
                                       min_value=min_value, kw_re=kw_re)
    ops = ops_all[:15]
    cust_market = {"opportunities": len(ops_all),
                   "value_eur": sum((o["value_eur"] or 0) for o in ops_all)}
    d = analytics.market_overview(cur, niche_name, 30, countries, subsectors)
    summary = report.summary_paragraph(ops,
                                       report.hotspots(cur, niche_name, countries=countries, subsectors=subsectors),
                                       report.leaderboard(cur, niche_name, countries=countries),
                                       report.under_competed(cur, niche_name, countries=countries, subsectors=subsectors),
                                       market=cust_market)
    trend = report.trend_paragraph(cur, niche_name, countries, subsectors)

    total_val = cust_market["value_eur"]
    op_countries = {o["country"] for o in ops_all if o["country"]}
    op_regions = {o["region_nuts"] for o in ops_all if o.get("region_nuts")}
    third_num, third_lbl = ((len(op_regions), "Regions") if len(op_countries) <= 1
                            else (len(op_countries), "Countries"))

    # tier-1 highlights (top 3 opportunities)
    hl = ""
    for o in ops[:3]:
        t = _title(cur, o)
        hl += (f"<div class='hl'><a href='{report.esc(o['url'])}'>{report.esc(t)}</a>"
               f"<div class='m'>{report.esc(o['buyer_name'])} · {report.esc(o['country'])} · "
               f"<span class='pill'>{report.esc(o['subsector'])}</span> · "
               f"{('est. ' + report.eur(o['value_eur'])) if o['value_eur'] else '—'}</div></div>")
    hl = hl or "<p class='muted'>No opportunities in this window.</p>"

    # movers one-liner for the exec tier
    mv_parts = []
    for m in d["movers"][:4]:
        arrow = "▲" if (m["change"] or 0) >= 0 else "▼"
        chg = "new" if m["change"] is None else f"{m['change']:+d}%"   # +25% / -23%, no double sign
        mv_parts.append(f"{report.esc(m['subsector'])} {arrow} {chg}")
    mv = ", ".join(mv_parts) or "—"

    # tier-2 full opportunity cards
    op_cards = ""
    for o in ops:
        t = _title(cur, o); desc = report.shown_description(cur, o)
        op_cards += opportunity.render_opportunity(cur, niche_name, o, title=t, desc=desc)
    op_cards = op_cards or "<p class='muted'>No opportunities in this window.</p>"
    op_note = (f"<p class='muted' style='font-size:12px;margin:0 0 6px'>Showing the top {len(ops)} "
               f"of {len(ops_all)} matching opportunities, by estimated value.</p>"
               if len(ops_all) > len(ops) else "")

    # featured company profiles
    prof_html = ""
    for cid in featured_company_ids(cur, niche_name, countries, subsectors, n=2):
        p = profiles.company_profile(cur, cid)
        if p:
            prof_html += profiles.render_company_profile(p)
    prof_html = prof_html or "<p class='muted'>No featured companies yet.</p>"

    # buyer spotlight
    sb = spotlight_buyer(cur, niche_name, countries)
    buyer_html = ""
    if sb:
        bp = buyer.buyer_profile(cur, niche_name, sb["name"], sb["country"])
        buyer_html = buyer.render_buyer_profile(bp)

    # news + analytics + tables
    news_html = news.render_news(news.fetch_news(news.feeds_for(niche_name), limit=6))
    analytics_html = analytics.render_market_overview(d, show_kpis=False)  # exec strip already has KPIs
    tables_html = _tables(cur, niche_name, countries, subsectors)

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{report.esc(title)}</title>
<style>{_css()}</style></head>
<body><div class="wrap">
  <div class="header"><div class="brand">{report.esc(label)} Intelligence</div><h1>{report.esc(title)}</h1>
    <div class="sub">Window: last {days} days · Generated {today} · Source: TED (EU)</div></div>

  <div class="tier">Executive brief</div>
  <div class="kpis">
    <div class="kpi"><div class="num">{cust_market['opportunities']}</div><div class="lbl">Opportunities</div></div>
    <div class="kpi"><div class="num">{report.eur(total_val)}</div><div class="lbl">Combined value (est.)</div></div>
    <div class="kpi"><div class="num">{third_num}</div><div class="lbl">{third_lbl}</div></div></div>
  <div class="take">{summary}</div>
  <p style="font-size:13.5px;margin:12px 0 4px">{trend}</p>
  <p style="font-size:12.5px;color:var(--muted);margin:4px 0 14px"><b>Movers:</b> {mv}</p>
  <h2>Top opportunities</h2>{hl}

  <div class="tier">Opportunities in detail</div>{op_note}{op_cards}

  <div class="tier">Market analytics</div>{analytics_html}

  <div class="tier">Featured suppliers</div>{prof_html}

  <div class="tier">Buyer spotlight</div>{buyer_html}

  <div class="tier">Market tables</div>{tables_html}

  <div class="tier">Industry news</div>{news_html}

  <div class="footer">Data © European Union, reused from TED · Values EUR-normalized (approx FX) ·
    Names normalized best-effort · Screening intelligence, not advice.</div>
</div></body></html>"""


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
        cur.execute("SELECT * FROM customers WHERE id=%s", (args.customer,))
        cust = cur.fetchone()
        if not cust:
            raise SystemExit(f"No customer with id {args.customer}")
        cust = dict(cust)
        kws = cust.get("keywords")
        cust["kw_re"] = re.compile("|".join(re.escape(k) for k in kws), re.I) if kws else None
        niche_name = cust.get("niche") or "green"
        title = f"{cust['name']} — Weekly Brief"
        slug = f"customer{args.customer}"
    else:
        niche_name = args.niche
        title = None
        slug = args.niche

    days = (cust or {}).get("window_days") or args.days
    page = build_brief(cur, niche_name, days, cust, title)
    conn.commit(); conn.close()

    os.makedirs("reports", exist_ok=True)
    out = args.out or os.path.join("reports", f"brief_{slug}_{dt.date.today():%Y-%m-%d}.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(page)
    print("Wrote", out)


if __name__ == "__main__":
    main()