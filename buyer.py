"""
Buyer profiles — the "sales-target" view.

For a procuring body, this shows what they buy, how much they spend, how
contested their contracts are, and — most valuable — which suppliers already
win their work. That's the list a bidder uses to decide who to court and who
they'd have to beat.

Renewals: predicting re-tender dates needs each contract's duration/end date.
Until that's extracted from the notice XML, `upcoming_renewals` returns None and
the section stays hidden; the moment a `contract_end` column exists it lights up.

Public functions:
    buyer_profile(cur, niche, name, country=None) -> dict
    render_buyer_profile(p)                        -> html fragment

Standalone preview:
    python buyer.py --niche green                    # profiles the most active buyer
    python buyer.py --niche green --name "Iasi"      # profiles the first match
    python buyer.py --niche green --with-renewals    # a buyer that has renewals
"""
from dotenv import load_dotenv
load_dotenv()

import argparse, os, re
import core
from psycopg2.extras import RealDictCursor
from profiles import _one, _all, _columns, eur, esc, _bars, _years_active, PROFILE_CSS


# ---------- helper: build an optional "AND country=%s" fragment ----------
def _cc(country, params, alias="t"):
    if country:
        params.append(country)
        return f" AND {alias}.country=%s"
    return ""


# ============================================================================
# DATA
# ============================================================================
def buyer_profile(cur, niche, name, country=None):
    p = {"name": name, "country": country, "niche": niche}

    # activity totals (call notices vs award notices, active span)
    params = [niche, name]; cc = _cc(country, params, "t")
    p["totals"] = _one(cur, f"""
        SELECT count(*) FILTER (WHERE t.notice_type='contract_notice') AS calls,
               count(*) FILTER (WHERE t.notice_type='contract_award')  AS award_notices,
               min(t.publication_date) AS first_seen,
               max(t.publication_date) AS last_seen
        FROM tenders t WHERE t.niche=%s AND t.buyer_name=%s{cc}""", params)

    # spend + competition (from the awards on their notices)
    params = [niche, name]; cc = _cc(country, params, "t")
    p["spend"] = _one(cur, f"""
        SELECT count(*) AS awards,
               COALESCE(sum(a.award_value_eur) FILTER (WHERE NOT a.value_suspect),0) AS awarded_eur,
               round(avg(a.num_bids),1) AS avg_bids
        FROM awards a JOIN tenders t ON t.publication_number=a.publication_number
        WHERE t.niche=%s AND t.buyer_name=%s{cc}""", params)

    # what they buy (by sub-sector). Use the notice-level subsector, which is
    # populated on every tender — award notices don't get lots enriched, so a
    # lots-based breakdown would be empty for award-heavy buyers.
    params = [niche, name]; cc = _cc(country, params, "t")
    p["by_subsector"] = _all(cur, f"""
        SELECT t.subsector, count(*) AS notices,
               COALESCE(sum(t.value_eur),0) AS value_eur
        FROM tenders t
        WHERE t.niche=%s AND t.buyer_name=%s{cc}
          AND t.subsector IS NOT NULL AND t.subsector NOT IN ('Other','Other green')
        GROUP BY 1 ORDER BY 2 DESC""", params)

    # preferred suppliers — who wins this buyer's contracts
    params = [niche, name]; cc = _cc(country, params, "t")
    p["suppliers"] = _all(cur, f"""
        SELECT COALESCE(c.canonical_name, a.winner_name_raw) AS supplier, c.country,
               count(*) AS wins,
               COALESCE(sum(a.award_value_eur) FILTER (WHERE NOT a.value_suspect),0) AS value_eur,
               max(COALESCE(a.award_date, t.publication_date)) AS last_win
        FROM awards a JOIN tenders t ON t.publication_number=a.publication_number
        LEFT JOIN companies c ON c.id=a.company_id
        WHERE t.niche=%s AND t.buyer_name=%s{cc}
          AND a.winner_name_raw IS NOT NULL AND a.winner_name_raw <> ''
        GROUP BY 1,2 ORDER BY 3 DESC, 4 DESC LIMIT 6""", params)

    # activity by year
    params = [niche, name]; cc = _cc(country, params, "t")
    p["by_year"] = _all(cur, f"""
        SELECT left(t.publication_date::text,4) AS year, count(*) AS notices
        FROM tenders t WHERE t.niche=%s AND t.buyer_name=%s{cc}
          AND t.publication_date IS NOT NULL
        GROUP BY 1 ORDER BY 1""", params)

    # recent notices (calls and awards). For award notices the value lives in the
    # awards rows, not on the tender, so fall back to the awarded sum.
    params = [niche, name]; cc = _cc(country, params, "t")
    p["recent"] = _all(cur, f"""
        SELECT t.publication_date, t.title, t.notice_type, t.subsector, t.deadline, t.url,
               COALESCE(t.value_eur,
                 (SELECT sum(aw.award_value_eur) FILTER (WHERE NOT aw.value_suspect)
                    FROM awards aw WHERE aw.publication_number=t.publication_number)) AS value_eur
        FROM tenders t WHERE t.niche=%s AND t.buyer_name=%s{cc}
        ORDER BY t.publication_date DESC NULLS LAST LIMIT 8""", params)

    # upcoming renewals (only if contract-end data has been extracted)
    p["renewals"] = upcoming_renewals(cur, niche, name, country)

    return p


def upcoming_renewals(cur, niche, name, country, horizon_days=540):
    """Contracts whose end date falls within the next ~18 months = tenders about
    to reappear. Requires a `contract_end` column (on lots or awards). Returns
    None if that data hasn't been extracted yet, so the section can stay hidden."""
    lots_cols, awards_cols = _columns(cur, "lots"), _columns(cur, "awards")
    if "contract_end" in lots_cols:      end_expr, end_tbl = "l.contract_end", "lots"
    elif "contract_end" in awards_cols:  end_expr, end_tbl = "a.contract_end", "awards"
    else:
        return None  # feature not available yet

    params = [niche, name]; cc = _cc(country, params, "t")
    join_lot = ("JOIN lots l ON l.publication_number=a.publication_number AND l.lot_id=a.lot_id"
                if end_tbl == "lots" else "")
    # One row per contract, not per lot: a multi-lot project (each package won by
    # a different firm) is a single renewal event. DISTINCT ON collapses it; the
    # window count keeps how many packages/incumbents sit under it.
    return _all(cur, f"""
        SELECT * FROM (
          SELECT DISTINCT ON (t.publication_number)
                 {end_expr} AS ends, t.title, t.publication_number,
                 max(l2.subsector) OVER (PARTITION BY t.publication_number) AS subsector,
                 COALESCE(c.canonical_name, a.winner_name_raw) AS incumbent,
                 count(*) OVER (PARTITION BY t.publication_number) AS packages
          FROM awards a
          JOIN tenders t ON t.publication_number=a.publication_number
          LEFT JOIN lots l2 ON l2.publication_number=a.publication_number AND l2.lot_id=a.lot_id
          LEFT JOIN companies c ON c.id=a.company_id
          {join_lot}
          WHERE t.niche=%s AND t.buyer_name=%s{cc}
            AND {end_expr} IS NOT NULL
            AND {end_expr} BETWEEN current_date AND current_date + {int(horizon_days)}
          ORDER BY t.publication_number, {end_expr}
        ) q ORDER BY ends ASC LIMIT 8""", params)


# ============================================================================
# RENDER  (reuses the .prof / .pkpi / .bar / table / chip classes from profiles)
# ============================================================================
def render_buyer_profile(p):
    tot, sp = p["totals"], p["spend"]
    name, country = p["name"], p["country"]

    kpis = f"""
      <div class='pkpi'><div class='n'>{tot['calls']}</div><div class='l'>Calls published</div></div>
      <div class='pkpi'><div class='n'>{eur(sp['awarded_eur'])}</div><div class='l'>Awarded (clean)</div></div>
      <div class='pkpi'><div class='n'>{sp['avg_bids'] if sp['avg_bids'] is not None else '—'}</div><div class='l'>Avg bidders</div></div>
      <div class='pkpi'><div class='n'>{_years_active(tot['first_seen'], tot['last_seen'])}</div><div class='l'>Active</div></div>"""

    buys = _bars(p["by_subsector"], "subsector", "notices")

    suppliers = "".join(
        f"<tr><td>{esc(s['supplier'])}</td><td>{esc(s['country'])}</td>"
        f"<td>{s['wins']}</td><td>{eur(s['value_eur'])}</td>"
        f"<td class='muted'>{esc(s['last_win'] or '—')}</td></tr>"
        for s in p["suppliers"]) or "<tr><td colspan=5 class='muted'>No awards on record.</td></tr>"

    rec = ""
    for r in p["recent"]:
        kind = "call" if r["notice_type"] == "contract_notice" else "award"
        title = esc((r["title"] or "")[:90])
        link = f"<a href='{esc(r['url'])}'>{title}</a>" if r.get("url") else title
        rec += (f"<tr><td class='muted'>{esc(r['publication_date'] or '—')}</td>"
                f"<td><span class='pill'>{kind}</span></td>"
                f"<td>{link}</td>"
                f"<td>{esc(r['subsector'] or '')}</td>"
                f"<td>{eur(r['value_eur'])}</td></tr>")
    rec = rec or "<tr><td colspan=5 class='muted'>—</td></tr>"

    # renewals: three states — no data yet (None), none upcoming ([]), or rows
    if p["renewals"] is None:
        ren_html = ("<div class='prof-sub'>Upcoming renewals</div>"
                    "<div class='muted' style='font-size:12px'>Awaiting contract-duration extraction.</div>")
    elif not p["renewals"]:
        ren_html = ("<div class='prof-sub'>Upcoming renewals</div>"
                    "<div class='muted' style='font-size:12px'>None ending in the next 18 months.</div>")
    else:
        rows = ""
        for x in p["renewals"]:
            inc = esc(x["incumbent"])
            if x.get("packages", 1) > 1:
                inc += f" <span class='muted'>+{x['packages'] - 1} more packages</span>"
            rows += (f"<tr><td class='muted'>{esc(x['ends'])}</td><td>{esc((x['title'] or '')[:80])}</td>"
                     f"<td>{esc(x['subsector'] or '')}</td><td>{inc}</td></tr>")
        ren_html = ("<div class='prof-sub'>Upcoming renewals (next 18 months)</div>"
                    "<table><thead><tr><th>Ends</th><th>Contract</th><th>Sector</th><th>Incumbent(s)</th></tr></thead>"
                    f"<tbody>{rows}</tbody></table>")

    return f"""
    <div class='prof'>
      <div class='prof-head'>
        <div><span class='prof-name'>{esc(name)}</span></div>
        <div class='muted' style='font-size:12px'>{esc(country)} · public buyer · {tot['award_notices']} award notices</div>
      </div>
      <div class='pkpis'>{kpis}</div>
      <div class='prof-sub'>What they buy (notices by sub-sector)</div>{buys}
      <div class='prof-sub'>Preferred suppliers (who wins their contracts)</div>
      <table><thead><tr><th>Supplier</th><th>Country</th><th>Wins</th><th>Awarded</th><th>Last win</th></tr></thead>
        <tbody>{suppliers}</tbody></table>
      <div class='prof-sub'>Recent activity</div>
      <table><thead><tr><th>Date</th><th>Type</th><th>Title</th><th>Sector</th><th>Value</th></tr></thead>
        <tbody>{rec}</tbody></table>
      {ren_html}
    </div>"""


# ============================================================================
# standalone
# ============================================================================
def most_active_buyer(cur, niche):
    return _one(cur, """
        SELECT buyer_name AS name, country, count(*) AS notices
        FROM tenders WHERE niche=%s AND buyer_name IS NOT NULL AND buyer_name <> ''
        GROUP BY 1,2 ORDER BY 3 DESC LIMIT 1""", (niche,))

def find_buyer(cur, niche, name):
    return _one(cur, """
        SELECT buyer_name AS name, country, count(*) AS notices
        FROM tenders WHERE niche=%s AND buyer_name ILIKE %s
        GROUP BY 1,2 ORDER BY 3 DESC LIMIT 1""", (niche, f"%{name}%"))

def buyer_with_renewals(cur, niche, horizon_days=540):
    """The buyer with the most contracts ending soon — handy for eyeballing the
    renewals table on a buyer that actually has some."""
    return _one(cur, f"""
        SELECT t.buyer_name AS name, t.country, count(*) AS ending
        FROM awards a JOIN tenders t ON t.publication_number=a.publication_number
        WHERE a.niche=%s AND a.contract_end IS NOT NULL
          AND a.contract_end BETWEEN current_date AND current_date + {int(horizon_days)}
          AND t.buyer_name IS NOT NULL AND t.buyer_name <> ''
        GROUP BY 1,2 ORDER BY 3 DESC LIMIT 1""", (niche,))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--niche", default="green")
    ap.add_argument("--name", help="buyer name to search for (default: most active)")
    ap.add_argument("--with-renewals", action="store_true",
                    help="profile the buyer with the most upcoming renewals")
    args = ap.parse_args()

    conn = core.get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    if args.with_renewals:
        b = buyer_with_renewals(cur, args.niche)
    elif args.name:
        b = find_buyer(cur, args.niche, args.name)
    else:
        b = most_active_buyer(cur, args.niche)
    if not b:
        raise SystemExit("No matching buyer.")
    count = b.get("notices") or b.get("ending") or 0
    print(f"Buyer: {b['name']} ({b['country']}) — {count}")

    p = buyer_profile(cur, args.niche, b["name"], b["country"])
    conn.commit(); conn.close()

    # quick terminal signal — no need to open the file to know renewals worked
    ren = p["renewals"]
    print("Renewals found:", "n/a (no contract_end column)" if ren is None else len(ren))
    for x in (ren or [])[:8]:
        print(f"   ends {x['ends']} · {x['incumbent']} · {(x['title'] or '')[:60]}")

    os.makedirs("reports", exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "_", b["name"].lower()).strip("_")[:40] or "buyer"
    out = os.path.join("reports", f"buyer_{slug}.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(f"<!doctype html><html><head><meta charset='utf-8'><title>{esc(b['name'])}</title>"
                f"<style>{PROFILE_CSS}</style></head><body>{render_buyer_profile(p)}</body></html>")
    print("Wrote", out)


if __name__ == "__main__":
    main()