"""
Market analytics — the shape of the market, from our own data.

Turns the single "market trend" line into a real section:
  - new opportunities by sub-sector (volume + value),
  - by country,
  - movers: which sub-sectors are heating up or cooling vs the previous period,
  - a monthly timeline of activity.

Everything is scopeable to a customer's countries / sub-sectors, so the
personalized brief shows *their* market, not the whole EU.

Public:
    market_overview(cur, niche, days=30, countries=None, subsectors=None) -> dict
    render_market_overview(data) -> html fragment

Standalone preview:
    python analytics.py --niche green --days 30
"""
from dotenv import load_dotenv
load_dotenv()

import argparse, os
import core
from psycopg2.extras import RealDictCursor
from profiles import _one, _all, eur, esc, _bars, PROFILE_CSS

import report          # for the canonical market_window (single source of truth)


# ---------- scope fragment (personalization) ----------
def _scope(countries, subsectors, params):
    frag = ""
    if countries:
        params.append(countries); frag += " AND t.country = ANY(%s)"
    if subsectors:
        params.append(subsectors); frag += " AND l.subsector = ANY(%s)"
    return frag


# ---------- pieces ----------
def by_subsector(cur, niche, days, countries, subsectors):
    params = [niche, days]; sc = _scope(countries, subsectors, params)
    return _all(cur, f"""
        SELECT l.subsector,
               count(DISTINCT t.publication_number) AS notices,
               COALESCE(sum(l.value_eur),0)         AS value_eur
        FROM lots l JOIN tenders t ON t.publication_number=l.publication_number
        WHERE t.niche=%s AND t.notice_type='contract_notice'
          AND t.publication_date >= current_date - make_interval(days => %s)
          AND l.subsector NOT IN ('Other','Other green'){sc}
        GROUP BY 1 ORDER BY 2 DESC""", params)


def by_country(cur, niche, days, countries, subsectors):
    params = [niche, days]; sc = _scope(countries, subsectors, params)
    return _all(cur, f"""
        SELECT t.country,
               count(DISTINCT t.publication_number) AS notices,
               COALESCE(sum(l.value_eur),0)         AS value_eur
        FROM lots l JOIN tenders t ON t.publication_number=l.publication_number
        WHERE t.niche=%s AND t.notice_type='contract_notice'
          AND t.publication_date >= current_date - make_interval(days => %s)
          AND l.subsector NOT IN ('Other','Other green'){sc}
        GROUP BY 1 ORDER BY 2 DESC""", params)


def _subsector_counts(cur, niche, start_days, end_days, countries, subsectors):
    """{subsector: notices} for the window [now-start_days, now-end_days)."""
    params = [niche, start_days, end_days]; sc = _scope(countries, subsectors, params)
    rows = _all(cur, f"""
        SELECT l.subsector, count(DISTINCT t.publication_number) AS n
        FROM lots l JOIN tenders t ON t.publication_number=l.publication_number
        WHERE t.niche=%s AND t.notice_type='contract_notice'
          AND t.publication_date >= current_date - make_interval(days => %s)
          AND t.publication_date <  current_date - make_interval(days => %s)
          AND l.subsector NOT IN ('Other','Other green'){sc}
        GROUP BY 1""", params)
    return {r["subsector"]: r["n"] for r in rows}


def movers(cur, niche, days, countries, subsectors):
    """Compare this window vs the previous same-length window, per sub-sector."""
    now  = _subsector_counts(cur, niche, days,     0,    countries, subsectors)
    prev = _subsector_counts(cur, niche, days * 2, days, countries, subsectors)
    out = []
    for sub in set(now) | set(prev):
        n, p = now.get(sub, 0), prev.get(sub, 0)
        if n + p < 3:                       # ignore tiny, noisy segments
            continue
        change = None if p == 0 else round((n - p) / p * 100)
        out.append({"subsector": sub, "now": n, "prev": p, "change": change})
    # biggest absolute movement first
    out.sort(key=lambda m: abs(m["now"] - m["prev"]), reverse=True)
    return out[:6]


def timeline(cur, niche, countries, subsectors, months=6):
    params = [niche, months]; sc = _scope(countries, subsectors, params)
    return _all(cur, f"""
        SELECT to_char(date_trunc('month', t.publication_date), 'YYYY-MM') AS ym,
               count(DISTINCT t.publication_number) AS notices
        FROM lots l JOIN tenders t ON t.publication_number=l.publication_number
        WHERE t.niche=%s AND t.notice_type='contract_notice'
          AND t.publication_date >= date_trunc('month', current_date) - make_interval(months => %s)
          AND l.subsector NOT IN ('Other','Other green'){sc}
        GROUP BY 1 ORDER BY 1""", params)


def market_overview(cur, niche, days=30, countries=None, subsectors=None):
    subs = by_subsector(cur, niche, days, countries, subsectors)
    mw = report.market_window(cur, niche, days, 0, countries, subsectors)   # canonical total
    return {
        "days": days,
        "by_subsector": subs,
        "by_country":   by_country(cur, niche, days, countries, subsectors),
        "movers":       movers(cur, niche, days, countries, subsectors),
        "timeline":     timeline(cur, niche, countries, subsectors),
        "total_notices": mw["opportunities"],
        "total_value":   float(mw["value_eur"] or 0),
    }

# ---------- render ----------
def render_market_overview(d, show_kpis=True):
    subs = _bars(d["by_subsector"], "subsector", "notices")
    geo  = _bars(d["by_country"],   "country",   "notices")

    mv = ""
    for m in d["movers"]:
        if m["change"] is None:
            arrow, txt, cls = "▲", "new", "up"
        elif m["change"] > 0:
            arrow, txt, cls = "▲", f"+{m['change']}%", "up"
        elif m["change"] < 0:
            arrow, txt, cls = "▼", f"{m['change']}%", "down"
        else:
            arrow, txt, cls = "—", "0%", "flat"
        mv += (f"<tr><td>{esc(m['subsector'])}</td><td>{m['prev']}</td><td>{m['now']}</td>"
               f"<td class='mv-{cls}'>{arrow} {txt}</td></tr>")
    mv = mv or "<tr><td colspan=4 class='muted'>Not enough data to compute movers.</td></tr>"

    tl = _bars([{"ym": r["ym"], "n": r["notices"]} for r in d["timeline"]], "ym", "n", n=12) \
         if d["timeline"] else "<div class='muted'>—</div>"

    kpis_html = (f"""<div class='pkpis'>
        <div class='pkpi'><div class='n'>{d['total_notices']}</div><div class='l'>New opportunities ({d['days']}d)</div></div>
        <div class='pkpi'><div class='n'>{eur(d['total_value'])}</div><div class='l'>Combined value</div></div>
        <div class='pkpi'><div class='n'>{len(d['by_country'])}</div><div class='l'>Countries active</div></div>
      </div>""" if show_kpis else "")

    return f"""
    <div class='prof'>
      {kpis_html}
      <div class='prof-cols'>
        <div><div class='prof-sub'>By sub-sector (new opportunities)</div>{subs}</div>
        <div><div class='prof-sub'>By country</div>{geo}</div>
      </div>
      <div class='prof-sub'>Movers — this period vs previous {d['days']} days</div>
      <table><thead><tr><th>Sub-sector</th><th>Prev</th><th>Now</th><th>Change</th></tr></thead>
        <tbody>{mv}</tbody></table>
      <div class='prof-sub'>Activity by month</div>{tl}
    </div>"""

EXTRA_CSS = ".mv-up{color:#1f8a4c;font-weight:600}.mv-down{color:#b9691a;font-weight:600}.mv-flat{color:#6b7c74}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--niche", default="green")
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()

    conn = core.get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    d = market_overview(cur, args.niche, args.days)
    conn.commit(); conn.close()

    os.makedirs("reports", exist_ok=True)
    out = os.path.join("reports", "analytics_preview.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(f"<!doctype html><html><head><meta charset='utf-8'><title>Market analytics</title>"
                f"<style>{PROFILE_CSS}{EXTRA_CSS}</style></head><body>"
                f"{render_market_overview(d)}</body></html>")
    print("Wrote", out,
          f"— {d['total_notices']} opportunities, {len(d['movers'])} movers, {len(d['timeline'])} months")


if __name__ == "__main__":
    main()