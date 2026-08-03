"""
Richer opportunity cards.

A plain opportunity is a title + buyer + value. This turns each one into a
briefing a bidder can act on, by adding context mined from history:

  - a deadline countdown,
  - "who usually wins this kind of contract" (top suppliers in the same
    sub-sector + country),
  - how contested it typically is (average bidders on similar awards),
  - how active this buyer is in the niche.

Public functions:
    enrich_opportunity(cur, niche, o) -> dict of the extra context
    render_opportunity(cur, niche, o, title=None, desc=None) -> html fragment

Standalone preview (renders the latest few green opportunities to HTML):
    python opportunity.py --niche green --days 30 --limit 6
"""
from dotenv import load_dotenv
load_dotenv()

import argparse, os, re, datetime as dt
import core
from psycopg2.extras import RealDictCursor
from profiles import _one, _all, eur, esc          # reuse the shared helpers
from niches import get_niche


# ---------- small helpers ----------
def days_to(deadline):
    """Whole days from today to a deadline (negative if past, None if unknown)."""
    if not deadline:
        return None
    try:
        d = deadline if isinstance(deadline, dt.date) else dt.date.fromisoformat(str(deadline)[:10])
    except Exception:
        return None
    return (d - dt.date.today()).days


# TED gives unnamed lots placeholder titles like "Default lot" / "Lot 1" / "Lote 1".
_GENERIC_TITLE = re.compile(r"^\s*(default lot|lot[e]?\s*\d*|partie\s*\d+|part\s*\d+)\s*$", re.I)

def _is_generic(s):
    return not s or bool(_GENERIC_TITLE.match(s))

def pick_title(o):
    """Choose the most useful title: a translation if we have one, else the
    lot's own title (unless it's a placeholder), else the descriptive notice title."""
    lot = None if _is_generic(o.get("lot_title")) else o.get("lot_title")
    for cand in (o.get("title_en"), lot, o.get("notice_title"), o.get("lot_title")):
        if cand and cand.strip():
            return cand
    return ""


def snippet(text, limit=300):
    """A clean lead-in: collapse whitespace, then end on a sentence or word
    boundary with an ellipsis rather than slicing mid-word."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if end >= limit * 0.5:
        return cut[:end + 1]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > 0 else cut).rstrip() + "…"

# ---------- context queries ----------
def typical_winners(cur, niche, subsector, country, days=730, limit=3):
    """Top suppliers by wins in this sub-sector (and country, if known)."""
    params = [niche, subsector, days]
    cc = ""
    if country:
        params.append(country); cc = " AND t.country=%s"
    return _all(cur, f"""
        SELECT COALESCE(c.canonical_name, a.winner_name_raw) AS winner, count(*) AS wins
        FROM awards a
        JOIN lots l    ON l.publication_number=a.publication_number AND l.lot_id=a.lot_id
        JOIN tenders t ON t.publication_number=a.publication_number
        LEFT JOIN companies c ON c.id=a.company_id
        WHERE a.niche=%s AND l.subsector=%s
          AND COALESCE(a.award_date, t.publication_date) >= current_date - make_interval(days => %s)
          AND a.winner_name_raw IS NOT NULL AND a.winner_name_raw <> ''{cc}
        GROUP BY 1 ORDER BY 2 DESC LIMIT {int(limit)}""", params)


def competition_stats(cur, niche, subsector, country, days=730):
    """Average bidders and sample size on similar past awards."""
    params = [niche, subsector, days]
    cc = ""
    if country:
        params.append(country); cc = " AND t.country=%s"
    return _one(cur, f"""
        SELECT round(avg(a.num_bids),1) AS avg_bids, count(*) AS awards
        FROM awards a
        JOIN lots l    ON l.publication_number=a.publication_number AND l.lot_id=a.lot_id
        JOIN tenders t ON t.publication_number=a.publication_number
        WHERE a.niche=%s AND l.subsector=%s AND a.num_bids IS NOT NULL
          AND COALESCE(a.award_date, t.publication_date) >= current_date - make_interval(days => %s){cc}""",
        params)


def buyer_activity(cur, niche, buyer_name, days=730):
    """How many notices this buyer has published in the niche recently."""
    if not buyer_name:
        return None
    return _one(cur, """
        SELECT count(*) AS notices
        FROM tenders
        WHERE niche=%s AND buyer_name=%s
          AND publication_date >= current_date - make_interval(days => %s)""",
        (niche, buyer_name, days))


def enrich_opportunity(cur, niche, o):
    """Bundle all the context for one opportunity into a dict."""
    sub, country = o.get("subsector"), o.get("country")
    return {
        "days_to_deadline": days_to(o.get("deadline")),
        "typical_winners":  typical_winners(cur, niche, sub, country),
        "competition":      competition_stats(cur, niche, sub, country),
        "buyer":            buyer_activity(cur, niche, o.get("buyer_name")),
    }


# ---------- render ----------
def render_opportunity(cur, niche, o, title=None, desc=None):
    """One opportunity as an HTML card. `title`/`desc` let the caller pass in
    already-translated text; otherwise we use whatever the row carries."""
    title = title or pick_title(o)
    desc  = desc  or o.get("description_en") or o.get("description_original") or ""
    ctx   = enrich_opportunity(cur, niche, o)

    # deadline chip
    d = ctx["days_to_deadline"]
    if d is None:      deadline_chip = ""
    elif d < 0:        deadline_chip = "<span class='chip warn'>deadline passed</span>"
    elif d <= 7:       deadline_chip = f"<span class='chip warn'>{d} days left</span>"
    else:              deadline_chip = f"<span class='chip'>{d} days left</span>"

    loc = " · ".join(x for x in [esc(o.get("country")), esc(o.get("region_nuts"))]
                     if x and x != "None")

    # facts strip: competition + buyer activity
    facts = []
    comp = ctx["competition"]
    if comp and comp["avg_bids"] is not None:
        facts.append(f"Typically <b>{comp['avg_bids']}</b> bidders "
                     f"<span class='muted'>({comp['awards']} similar awards)</span>")
    b = ctx["buyer"]
    if b and b["notices"]:
        facts.append(f"This buyer: <b>{b['notices']}</b> notices in {get_niche(niche)['label'].lower()} (2y)")
    facts_html = " &nbsp;·&nbsp; ".join(facts)

    # recent winners in this segment (honest label — small samples are common)
    tw = ctx["typical_winners"]
    if tw:
        names = ", ".join(f"{esc(w['winner'])} <span class='muted'>({w['wins']})</span>" for w in tw)
        winners_html = f"<div class='opp-win'><span class='opp-lbl'>Recent winners in this segment:</span> {names}</div>"
    else:
        winners_html = ""

    crit = f" · {esc(o['award_criteria'])}" if o.get("award_criteria") else ""

    return f"""
    <div class='opp'>
      <div class='opp-t'><a href='{esc(o.get('url'))}'>{esc(title)}</a></div>
      <div class='opp-meta'>{esc(o.get('buyer_name'))} · {loc}
        · <span class='pill'>{esc(o.get('subsector'))}</span>
        · {('est. ' + eur(o.get('value_eur'))) if o.get('value_eur') else '—'}{crit} {deadline_chip}</div>
      <div class='opp-desc'>{esc(snippet(desc, 300))}</div>
      {f"<div class='opp-facts'>{facts_html}</div>" if facts_html else ""}
      {winners_html}
    </div>"""


# extra CSS for standalone viewing (main report carries its own copy)
OPP_CSS = """
:root{--ink:#12261f;--green:#1f8a4c;--soft:#e5f3ea;--muted:#6b7c74;--line:#e2e8e4;--warn:#b9691a}
*{box-sizing:border-box}body{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;
color:var(--ink);margin:0;background:#f4f7f5;padding:30px}
.wrap{max-width:820px;margin:0 auto;background:#fff;border:1px solid var(--line);
border-radius:12px;padding:22px 26px}
.opp{border-bottom:1px solid var(--line);padding:14px 0}
.opp-t{font-weight:600}.opp-t a{color:var(--green);text-decoration:none}
.opp-meta{color:var(--muted);font-size:12px;margin:4px 0}
.opp-desc{font-size:13px;margin-top:5px}
.opp-facts{font-size:12px;margin-top:7px;background:var(--soft);border-radius:7px;padding:7px 10px}
.opp-win{font-size:12px;margin-top:6px}
.opp-lbl{color:var(--muted);text-transform:uppercase;font-size:10.5px;letter-spacing:.4px;font-weight:700}
.pill{background:var(--soft);color:var(--green);border-radius:20px;padding:2px 8px;font-size:11px}
.chip{background:var(--soft);color:var(--green);border-radius:20px;padding:2px 9px;font-size:11px;margin-left:4px}
.chip.warn{background:#fff4e5;color:var(--warn);border:1px solid #f0d3ad}
.muted{color:var(--muted)}
"""


def recent_opportunities(cur, niche, days, limit):
    """Latest open-call opportunities in a niche, ONE card per notice.
    DISTINCT ON collapses a notice's multiple (often identical) lots to a single
    representative — its highest-value lot — so we don't show triplicate cards."""
    return _all(cur, """
        SELECT * FROM (
          SELECT DISTINCT ON (t.publication_number)
                 l.id, t.publication_number, t.title AS notice_title, t.buyer_name,
                 t.country, t.publication_date, t.deadline, t.url,
                 l.title AS lot_title, l.title_en, l.description_original, l.description_en,
                 l.subsector, l.value_eur, l.region_nuts, l.award_criteria
          FROM lots l JOIN tenders t ON t.publication_number=l.publication_number
          WHERE t.niche=%s AND t.notice_type='contract_notice'
            AND l.subsector NOT IN ('Other','Other green')
            AND t.publication_date >= current_date - make_interval(days => %s)
          ORDER BY t.publication_number, l.value_eur DESC NULLS LAST
        ) q
        ORDER BY value_eur DESC NULLS LAST, publication_date DESC
        LIMIT %s""", (niche, days, limit))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--niche", default="green")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--limit", type=int, default=6)
    args = ap.parse_args()

    conn = core.get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    ops = recent_opportunities(cur, args.niche, args.days, args.limit)
    cards = "".join(render_opportunity(cur, args.niche, o) for o in ops) \
            or "<p class='muted'>No opportunities in this window.</p>"
    conn.commit(); conn.close()

    os.makedirs("reports", exist_ok=True)
    out = os.path.join("reports", "opportunities_preview.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(f"<!doctype html><html><head><meta charset='utf-8'><title>Opportunities</title>"
                f"<style>{OPP_CSS}</style></head><body><div class='wrap'>{cards}</div></body></html>")
    print("Wrote", out, f"({len(ops)} cards)")


if __name__ == "__main__":
    main()