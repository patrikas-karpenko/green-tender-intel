"""
News section — curated RSS, blended sector + procurement.

Pulls recent items from a small set of feeds, balances them across sources
(round-robin so a high-volume feed can't dominate), dedups, and renders a
compact list. Feeds come from the niche config if it defines `news_feeds`,
else the defaults below.

Per-feed filtering: each feed is (name, url, tag) or (name, url, tag, regex).
If a feed carries a regex, only its items matching that regex (in title+summary)
are kept — used here to EU-focus the globally-scoped procurement feed while
leaving the already-on-topic sector feed unfiltered.

Needs feedparser:  pip install feedparser

Public:
    fetch_news(feeds=None, days=60, limit=6, keyword_re=None)
        -> list[dict] | None   (None means feedparser isn't installed)
    render_news(items) -> html fragment
    feeds_for(niche_name) -> list of feed tuples

Standalone preview:
    python news.py --niche green
"""
from dotenv import load_dotenv
load_dotenv()

import argparse, os, re, datetime as dt
from profiles import esc
from niches import get_niche

# Matches EU institutions, EU-level policy terms, and member states — used to
# keep a globally-scoped feed focused on Europe.
EU_RE = re.compile(
    r"\b("
    r"EU|Europe\w*|Brussels|Eurozone|"
    r"European Commission|European Parliament|member states?|"
    r"eForms|CPV|TED|REPowerEU|Fit for 55|Green Deal|"
    r"Renewable Energy Directive|Horizon Europe|cohesion|"
    r"German\w*|France|French|Pol(and|ish)|Spa(in|nish)|Ital(y|ian)|"
    r"Netherlands|Dutch|Belgi\w*|Portug\w*|Gree(ce|k)|Czech\w*|"
    r"Swed\w*|Austria\w*|Ireland|Irish|Denmark|Danish|Finl\w*|Finnish|"
    r"Romania\w*|Hungar\w*|Slovak\w*|Croatia\w*|Bulgaria\w*|"
    r"Lithuania\w*|Latvia\w*|Estonia\w*|Sloveni\w*|Luxembourg|Cyprus|Malta"
    r")\b", re.I)

# (display name, feed url, tag[, per-feed regex]).
# pv-magazine is already green-sector on-topic → no filter.
# Open Contracting is global → EU-filter it. Euractiv Energy is EU-focused already.
DEFAULT_FEEDS = [
    ("pv magazine",      "https://www.pv-magazine.com/feed/",                        "sector"),
    ("Open Contracting", "https://www.open-contracting.org/feed/",                   "procurement", EU_RE),
    ("Euractiv Energy",  "https://www.euractiv.com/sections/energy-environment/feed/", "policy"),
]


def _unpack(feed):
    """Accept 3- or 4-tuples: (name, url, tag) or (name, url, tag, feed_regex)."""
    name, url, tag = feed[0], feed[1], feed[2]
    feed_re = feed[3] if len(feed) > 3 else None
    return name, url, tag, feed_re


def feeds_for(niche_name):
    """Feed list for a niche: its own `news_feeds` if defined, else the defaults."""
    try:
        n = get_niche(niche_name)
        return n.get("news_feeds") or DEFAULT_FEEDS
    except Exception:
        return DEFAULT_FEEDS


def _entry_date(e):
    pp = getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None)
    if pp:
        try:
            return dt.date(pp.tm_year, pp.tm_mon, pp.tm_mday)
        except Exception:
            return None
    return None


def fetch_news(feeds=None, days=60, limit=6, keyword_re=None, per_feed=15):
    """Recent items across the feeds, balanced across sources (round-robin) so a
    high-volume feed can't crowd out the others, then displayed newest-first.
    Applies each feed's own regex (if any) and the optional global keyword_re.
    Returns None if feedparser isn't installed."""
    try:
        import feedparser
    except ImportError:
        return None

    feeds = feeds or DEFAULT_FEEDS
    cutoff = dt.date.today() - dt.timedelta(days=days)

    # collect per source, newest first within each
    per_source = []
    for feed in feeds:
        name, url, tag, feed_re = _unpack(feed)
        bucket = []
        try:
            parsed = feedparser.parse(url)
        except Exception:
            per_source.append(bucket); continue
        for e in parsed.entries[:per_feed]:
            title = (getattr(e, "title", "") or "").strip()
            if not title:
                continue
            date = _entry_date(e)
            if date and date < cutoff:
                continue
            text = f"{title} {getattr(e, 'summary', '') or ''}"
            if feed_re and not feed_re.search(text):        # per-feed EU focus
                continue
            if keyword_re and not keyword_re.search(text):  # optional global filter
                continue
            bucket.append({"title": title, "link": getattr(e, "link", "") or "",
                           "date": date, "source": name, "tag": tag})
        bucket.sort(key=lambda x: (x["date"] or dt.date.min), reverse=True)
        per_source.append(bucket)

    # round-robin one from each source in turn until we hit the limit
    seen, picked, i = set(), [], 0
    while len(picked) < limit and any(i < len(b) for b in per_source):
        for b in per_source:
            if i < len(b):
                it = b[i]; k = it["title"].lower()
                if k not in seen:
                    seen.add(k); picked.append(it)
                    if len(picked) >= limit:
                        break
        i += 1

    picked.sort(key=lambda x: (x["date"] or dt.date.min), reverse=True)
    return picked


def render_news(items):
    if items is None:
        return ("<div class='prof-sub'>Industry news</div>"
                "<div class='muted' style='font-size:12px'>News feed unavailable "
                "(install feedparser: <code>pip install feedparser</code>).</div>")
    if not items:
        return ("<div class='prof-sub'>Industry news</div>"
                "<div class='muted' style='font-size:12px'>No recent items.</div>")
    rows = ""
    for it in items:
        d = f"{it['date']} · " if it["date"] else ""
        title = esc(it["title"])
        link = f"<a href='{esc(it['link'])}'>{title}</a>" if it["link"] else title
        rows += (f"<div class='news'><span class='news-tag'>{esc(it['tag'])}</span>"
                 f"<span class='news-t'>{link}</span>"
                 f"<div class='news-m'>{d}{esc(it['source'])}</div></div>")
    return f"<div class='prof-sub'>Industry news</div>{rows}"


NEWS_CSS = """
.news{border-bottom:1px solid var(--line);padding:9px 0}
.news-tag{display:inline-block;background:var(--soft);color:var(--green);border-radius:20px;
padding:1px 8px;font-size:10px;text-transform:uppercase;letter-spacing:.3px;margin-right:8px;vertical-align:middle}
.news-t{font-size:13.5px}.news-t a{color:var(--green);text-decoration:none}
.news-m{font-size:11px;color:var(--muted);margin-top:2px}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--niche", default="green")
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--limit", type=int, default=8)
    args = ap.parse_args()

    items = fetch_news(feeds_for(args.niche), days=args.days, limit=args.limit)
    from profiles import PROFILE_CSS
    os.makedirs("reports", exist_ok=True)
    out = os.path.join("reports", "news_preview.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(f"<!doctype html><html><head><meta charset='utf-8'><title>Industry news</title>"
                f"<style>{PROFILE_CSS}{NEWS_CSS}</style></head><body><div class='prof'>"
                f"{render_news(items)}</div></body></html>")
    n = "n/a (feedparser missing)" if items is None else len(items)
    print("Wrote", out, f"— {n} items")


if __name__ == "__main__":
    main()