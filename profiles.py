"""
Deep company profiles — the intelligence centrepiece.

Builds a full picture of one supplier from the data we already have:
market position, footprint by year and country, the sub-sectors they win in,
their top buyers, the competitors they beat, and a recent award timeline.

Two public functions:
    company_profile(cur, company_id) -> dict          # the data
    render_company_profile(p)        -> html fragment  # the card

Test / view it:
    python profiles.py "NOFRAYANE"           # print the data as JSON
    python profiles.py "NOFRAYANE" --html    # write reports/profile_<id>.html to open
    python profiles.py --id 42 --html
"""
from dotenv import load_dotenv
load_dotenv()

import argparse, json, html, datetime as dt
import core
from psycopg2.extras import RealDictCursor


# ---------- tiny query helpers ----------
def _one(cur, sql, params):
    cur.execute(sql, params); return cur.fetchone()

def _all(cur, sql, params):
    cur.execute(sql, params); return cur.fetchall()

def _columns(cur, table):
    """Set of column names on a table (empty set if the table doesn't exist).
    Lets the code adapt to whatever the live schema actually has."""
    cur.execute("""SELECT column_name FROM information_schema.columns
                   WHERE table_schema='public' AND table_name=%s""", (table,))
    return {r["column_name"] for r in cur.fetchall()}


# ---------- formatting (self-contained so this file stands alone) ----------
def eur(n):
    if not n: return "—"
    n = float(n)
    if n >= 1e6: return f"€{n/1e6:.1f}M"
    if n >= 1e3: return f"€{n/1e3:.0f}k"
    return f"€{n:.0f}"

def esc(s): return html.escape(str(s if s is not None else ""))

def _years_active(first, last):
    if not first: return "—"
    if not last or last == first: return str(first)[:4]
    return f"{str(first)[:4]}–{str(last)[:4]}"


# ============================================================================
# DATA
# ============================================================================
def company_profile(cur, company_id):
    """Rich profile dict for one company. Money totals exclude suspect
    (fat-finger) award values; win counts include everything."""
    p = {"id": company_id}

    p["company"] = _one(cur, """
        SELECT id, canonical_name, country, city, lei, registered_as,
               parent_name, is_subsidiary, website
        FROM companies WHERE id=%s""", (company_id,))
    if not p["company"]:
        return None

    # Adapt to the live schema: prefer normalized buyers if they exist,
    # otherwise fall back to the raw buyer_name on the tender.
    tcols = _columns(cur, "tenders")
    has_buyers = bool(_columns(cur, "buyers"))
    if "buyer_id" in tcols and has_buyers:
        buyer_expr = "COALESCE(b.canonical_name, t.buyer_name)"
        buyer_join = "LEFT JOIN buyers b ON b.id=t.buyer_id"
        buyer_col  = "buyer_id"            # column that identifies "the same buyer"
    else:
        buyer_expr = "t.buyer_name"
        buyer_join = ""
        buyer_col  = "buyer_name"

    # An award's effective date: use the award date when present, else fall back
    # to the notice's publication date (many older award rows have a null award_date).
    EFF_DATE = "COALESCE(a.award_date, t.publication_date)"

    # headline market position
    p["totals"] = _one(cur, f"""
        SELECT count(*)                                                          AS wins,
               COALESCE(sum(a.award_value_eur) FILTER (WHERE NOT a.value_suspect),0) AS total_eur,
               count(*) FILTER (WHERE a.value_suspect)                          AS suspect_wins,
               min({EFF_DATE})                                                  AS first_win,
               max({EFF_DATE})                                                  AS last_win,
               round(avg(a.num_bids),1)                                         AS avg_bids
        FROM awards a JOIN tenders t USING(publication_number)
        WHERE a.company_id=%s""", (company_id,))

    # activity by year (volume + value)
    p["by_year"] = _all(cur, f"""
        SELECT left({EFF_DATE}::text,4)                                         AS year,
               count(*)                                                         AS wins,
               COALESCE(sum(a.award_value_eur) FILTER (WHERE NOT a.value_suspect),0) AS value_eur
        FROM awards a JOIN tenders t USING(publication_number)
        WHERE a.company_id=%s AND {EFF_DATE} IS NOT NULL
        GROUP BY 1 ORDER BY 1""", (company_id,))

    # sub-sectors they win in
    p["by_subsector"] = _all(cur, """
        SELECT l.subsector, count(*) AS wins,
               COALESCE(sum(a.award_value_eur) FILTER (WHERE NOT a.value_suspect),0) AS value_eur
        FROM awards a
        JOIN lots l ON l.publication_number=a.publication_number AND l.lot_id=a.lot_id
        WHERE a.company_id=%s AND l.subsector NOT IN ('Other','Other green')
        GROUP BY 1 ORDER BY 2 DESC""", (company_id,))

    # geographic footprint (which buyer countries they win in)
    p["by_country"] = _all(cur, """
        SELECT t.country, count(*) AS wins,
               COALESCE(sum(a.award_value_eur) FILTER (WHERE NOT a.value_suspect),0) AS value_eur
        FROM awards a JOIN tenders t USING(publication_number)
        WHERE a.company_id=%s AND t.country IS NOT NULL
        GROUP BY 1 ORDER BY 2 DESC""", (company_id,))

    # top buyers they win from
    p["top_buyers"] = _all(cur, f"""
        SELECT {buyer_expr} AS buyer, t.country,
               count(*) AS wins,
               COALESCE(sum(a.award_value_eur) FILTER (WHERE NOT a.value_suspect),0) AS value_eur,
               max(a.award_date) AS last_win
        FROM awards a JOIN tenders t USING(publication_number)
        {buyer_join}
        WHERE a.company_id=%s
        GROUP BY 1,2 ORDER BY 3 DESC, 4 DESC LIMIT 6""", (company_id,))

    # competitors: other suppliers who win from the SAME buyers.
    # Two things matter here:
    #  - reference each tenders alias explicitly (t vs t2) so the inner query is a
    #    real "NOFRAYANE's buyers" set, not an accidental self-correlation that
    #    matches every notice;
    #  - NULLIF(btrim(...),'') drops blank buyer keys, so empty buyers don't all
    #    "match" each other and drag in the whole DB's top winners.
    bk_t  = f"NULLIF(btrim(t.{buyer_col}::text),'')"
    bk_t2 = f"NULLIF(btrim(t2.{buyer_col}::text),'')"
    p["competitors"] = _all(cur, f"""
        SELECT COALESCE(c.canonical_name, a.winner_name_raw) AS competitor,
               c.country, count(*) AS shared_buyer_wins
        FROM awards a
        LEFT JOIN companies c ON c.id=a.company_id
        WHERE a.company_id <> %s
          AND a.winner_name_raw IS NOT NULL AND a.winner_name_raw <> ''
          AND a.publication_number IN (
              SELECT t.publication_number FROM awards a2
              JOIN tenders t USING(publication_number)
              WHERE {bk_t} IN (
                  SELECT DISTINCT {bk_t2} FROM awards a3
                  JOIN tenders t2 USING(publication_number)
                  WHERE a3.company_id=%s AND {bk_t2} IS NOT NULL))
        GROUP BY 1,2 ORDER BY 3 DESC LIMIT 6""", (company_id, company_id))

    # recent award timeline
    p["recent"] = _all(cur, f"""
        SELECT {EFF_DATE} AS award_date, t.title, t.country, l.subsector,
               {buyer_expr} AS buyer,
               a.award_value_eur, a.value_suspect, a.num_bids, t.url
        FROM awards a JOIN tenders t USING(publication_number)
        LEFT JOIN lots l ON l.publication_number=a.publication_number AND l.lot_id=a.lot_id
        {buyer_join}
        WHERE a.company_id=%s
        ORDER BY {EFF_DATE} DESC NULLS LAST LIMIT 10""", (company_id,))

    return p


# ============================================================================
# RENDER
# ============================================================================
def _bars(rows, label_key, value_key, n=5):
    """A small inline bar chart as HTML: the top-n rows, each a labelled bar
    whose width is relative to the biggest value in the set."""
    rows = rows[:n]
    if not rows:
        return "<div class='muted'>—</div>"
    top = max(float(r[value_key] or 0) for r in rows) or 1
    out = []
    for r in rows:
        v = float(r[value_key] or 0)
        w = max(2, round(v / top * 100))
        out.append(
            f"<div class='bar'><span class='bar-l'>{esc(r[label_key] or '—')}</span>"
            f"<span class='bar-t'><span class='bar-f' style='width:{w}%'></span></span>"
            f"<span class='bar-v'>{eur(v) if v >= 1000 else int(v)}</span></div>")
    return "".join(out)


def render_company_profile(p):
    """Return an HTML fragment (a <div class='prof'>...</div>) for one profile."""
    c, t = p["company"], p["totals"]

    # ownership / identity line
    idbits = []
    if c.get("city"):        idbits.append(esc(c["city"]))
    if c.get("country"):     idbits.append(esc(c["country"]))
    if c.get("registered_as"): idbits.append("reg " + esc(c["registered_as"]))
    if c.get("lei"):         idbits.append("LEI " + esc(c["lei"]))
    ident = " · ".join(idbits)
    owner = (f"<span class='badge'>Subsidiary of {esc(c['parent_name'])}</span>"
             if c.get("is_subsidiary") and c.get("parent_name") else "")

    # KPI strip
    kpis = f"""
      <div class='pkpi'><div class='n'>{t['wins']}</div><div class='l'>Wins</div></div>
      <div class='pkpi'><div class='n'>{eur(t['total_eur'])}</div><div class='l'>Awarded (clean)</div></div>
      <div class='pkpi'><div class='n'>{t['avg_bids'] if t['avg_bids'] is not None else '—'}</div><div class='l'>Avg bidders faced</div></div>
      <div class='pkpi'><div class='n'>{_years_active(t['first_win'], t['last_win'])}</div><div class='l'>Active</div></div>"""

    # specialism + footprint as bars, side by side
    spec = _bars(p["by_subsector"], "subsector", "wins")
    geo  = _bars(p["by_country"],   "country",   "wins")

    # top buyers
    buyers = "".join(
        f"<tr><td>{esc(b['buyer'])}</td><td>{esc(b['country'])}</td>"
        f"<td>{b['wins']}</td><td>{eur(b['value_eur'])}</td><td class='muted'>{esc(b['last_win'] or '—')}</td></tr>"
        for b in p["top_buyers"]) or "<tr><td colspan=5 class='muted'>—</td></tr>"

    # competitors
    comps = "".join(
        f"<span class='chip'>{esc(x['competitor'])}"
        f"<span class='chip-n'>{x['shared_buyer_wins']}</span></span>"
        for x in p["competitors"]) or "<span class='muted'>None identified.</span>"

    # recent timeline
    rows = ""
    for r in p["recent"]:
        val = eur(r["award_value_eur"])
        if r["value_suspect"]:
            val = f"<span class='muted' title='flagged outlier'>{val}?</span>"
        title = esc((r["title"] or "")[:90])
        link = f"<a href='{esc(r['url'])}'>{title}</a>" if r.get("url") else title
        rows += (f"<tr><td class='muted'>{esc(r['award_date'] or '—')}</td>"
                 f"<td>{link}<br><span class='muted' style='font-size:11px'>{esc(r['buyer'])}</span></td>"
                 f"<td>{('<span class=pill>'+esc(r['subsector'])+'</span>') if r['subsector'] else ''}</td>"
                 f"<td>{esc(r['country'])}</td><td>{val}</td>"
                 f"<td>{r['num_bids'] if r['num_bids'] is not None else '—'}</td></tr>")
    rows = rows or "<tr><td colspan=6 class='muted'>No awards on record.</td></tr>"

    return f"""
    <div class='prof'>
      <div class='prof-head'>
        <div><span class='prof-name'>{esc(c['canonical_name'])}</span> {owner}</div>
        <div class='muted' style='font-size:12px'>{ident}</div>
      </div>
      <div class='pkpis'>{kpis}</div>
      <div class='prof-cols'>
        <div><div class='prof-sub'>Specialism (wins by sub-sector)</div>{spec}</div>
        <div><div class='prof-sub'>Footprint (wins by country)</div>{geo}</div>
      </div>
      <div class='prof-sub'>Top buyers</div>
      <table><thead><tr><th>Buyer</th><th>Country</th><th>Wins</th><th>Awarded</th><th>Last win</th></tr></thead>
        <tbody>{buyers}</tbody></table>
      <div class='prof-sub'>Competitors at the same buyers</div>
      <div class='chips'>{comps}</div>
      <div class='prof-sub'>Recent awards</div>
      <table><thead><tr><th>Date</th><th>Contract / buyer</th><th>Sector</th><th>Country</th><th>Value</th><th>Bids</th></tr></thead>
        <tbody>{rows}</tbody></table>
    </div>"""


# CSS for standalone viewing (the main report will carry its own copy)
PROFILE_CSS = """
:root{--ink:#12261f;--green:#1f8a4c;--soft:#e5f3ea;--muted:#6b7c74;--line:#e2e8e4}
*{box-sizing:border-box}body{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;
color:var(--ink);margin:0;background:#f4f7f5;padding:30px}
.prof{max-width:820px;margin:0 auto 26px;background:#fff;border:1px solid var(--line);
border-radius:12px;padding:22px 26px}
.prof-head{border-bottom:2px solid var(--green);padding-bottom:10px;margin-bottom:14px}
.prof-name{font-size:19px;font-weight:700}
.badge{background:#fff4e5;color:#b9691a;border:1px solid #f0d3ad;border-radius:20px;
padding:2px 9px;font-size:11px;margin-left:6px;white-space:nowrap}
.pkpis{display:flex;gap:12px;margin:4px 0 18px;flex-wrap:wrap}
.pkpi{flex:1;min-width:120px;background:var(--soft);border-radius:9px;padding:11px 13px}
.pkpi .n{font-size:19px;font-weight:700;color:var(--green)}.pkpi .l{font-size:11px;color:var(--muted)}
.prof-cols{display:flex;gap:26px;margin-bottom:6px}.prof-cols>div{flex:1;min-width:0}
.prof-sub{font-size:11px;text-transform:uppercase;letter-spacing:.4px;color:var(--muted);
font-weight:700;margin:16px 0 8px}
.bar{display:flex;align-items:center;gap:8px;margin:5px 0;font-size:12px}
.bar-l{width:38%;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bar-t{flex:1;background:var(--soft);border-radius:5px;height:12px;overflow:hidden}
.bar-f{display:block;height:100%;background:var(--green);border-radius:5px}
.bar-v{width:56px;text-align:right;color:var(--muted)}
table{width:100%;border-collapse:collapse;font-size:12.5px;margin-bottom:4px}
th{text-align:left;color:var(--muted);font-weight:600;font-size:10.5px;text-transform:uppercase;
padding:7px;border-bottom:1px solid var(--line)}
td{padding:8px 7px;border-bottom:1px solid var(--line);vertical-align:top}
td a{color:var(--green);text-decoration:none}
.pill{background:var(--soft);color:var(--green);border-radius:20px;padding:2px 8px;font-size:11px}
.chips{line-height:2}.chip{display:inline-block;background:#f0f6f2;border:1px solid var(--line);
border-radius:20px;padding:3px 6px 3px 11px;font-size:12px;margin:0 6px 6px 0}
.chip-n{background:var(--green);color:#fff;border-radius:20px;padding:1px 7px;font-size:10px;margin-left:7px}
.muted{color:var(--muted)}
"""


def find_company(cur, name):
    return _one(cur, """SELECT id, canonical_name FROM companies
                        WHERE canonical_name ILIKE %s ORDER BY id LIMIT 1""", (f"%{name}%",))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name", nargs="?", help="company name to search for")
    ap.add_argument("--id", type=int, help="exact company id")
    ap.add_argument("--html", action="store_true", help="write an HTML page instead of JSON")
    args = ap.parse_args()

    conn = core.get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    if args.id:
        cid = args.id
    elif args.name:
        row = find_company(cur, args.name)
        if not row:
            raise SystemExit(f"No company matching '{args.name}'")
        cid = row["id"]
        print(f"Matched: {row['canonical_name']} (id {cid})")
    else:
        raise SystemExit('Give a company name or --id, e.g. python profiles.py "NOFRAYANE"')

    p = company_profile(cur, cid)
    conn.commit(); conn.close()
    if not p:
        raise SystemExit("No such company id.")

    if args.html:
        import os
        os.makedirs("reports", exist_ok=True)
        out = os.path.join("reports", f"profile_{cid}.html")
        page = (f"<!doctype html><html><head><meta charset='utf-8'>"
                f"<title>{esc(p['company']['canonical_name'])}</title>"
                f"<style>{PROFILE_CSS}</style></head><body>"
                f"{render_company_profile(p)}</body></html>")
        with open(out, "w", encoding="utf-8") as f:
            f.write(page)
        print("Wrote", out)
    else:
        print(json.dumps(p, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()