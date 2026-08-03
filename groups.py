"""
Group-level competitor intelligence — roll wins up to the corporate parent.

Once build_groups.py has clustered legal entities, this shows the *true*
competitive picture: VINCI as ONE competitor (NOFRAYANE + Cobra + SICE + CYMI
combined), not four scattered names. Standalone companies (no group) roll up to
themselves, so the leaderboard mixes groups and single firms fairly.

Public:
    group_leaderboard(cur, niche, days=365, limit=15, countries=None) -> rows
    render_group_leaderboard(rows) -> html table fragment

Standalone:
    python groups.py --niche green            # print the rolled-up leaderboard
"""
from dotenv import load_dotenv
load_dotenv()

import argparse
import core
from psycopg2.extras import RealDictCursor
from profiles import _all, eur, esc


def group_leaderboard(cur, niche, days=365, limit=15, countries=None):
    """Top competitors with wins rolled up to their corporate group.
    Rollup key: the group if the winner belongs to one, else the company, else
    the raw name — so a group's subsidiaries combine into a single row."""
    params = [niche, days]
    cc = ""
    if countries:
        params.append(countries); cc = " AND a.winner_country = ANY(%s)"
    return _all(cur, f"""
        SELECT COALESCE(g.name, c.canonical_name, a.winner_name_raw)              AS competitor,
               COALESCE('g' || g.id, 'c' || c.id::text, 'n' || a.winner_name_raw) AS gkey,
               max(COALESCE(g.member_count, 1))                                   AS entities,
               count(*)                                                           AS wins,
               COALESCE(sum(a.award_value_eur) FILTER (WHERE NOT a.value_suspect), 0) AS total,
               count(DISTINCT a.winner_country)                                   AS countries,
               max(a.award_date)                                                  AS last_win
        FROM awards a
        LEFT JOIN companies c       ON c.id = a.company_id
        LEFT JOIN company_groups g  ON g.id = c.group_id
        WHERE a.niche=%s AND a.award_date >= current_date - make_interval(days => %s)
          AND a.winner_name_raw IS NOT NULL AND a.winner_name_raw <> ''{cc}
        GROUP BY 1, 2
        ORDER BY total DESC, wins DESC
        LIMIT {int(limit)}""", params)


def render_group_leaderboard(rows):
    body = "".join(
        f"<tr><td class='rank'>{i}</td><td>{esc(r['competitor'])}"
        f"{(' <span class=pill>group · ' + str(r['entities']) + ' entities</span>') if r['entities'] > 1 else ''}"
        f"</td><td>{r['countries']}</td><td>{r['wins']}</td><td>{eur(r['total'])}</td>"
        f"<td class='muted'>{esc(r['last_win'] or '—')}</td></tr>"
        for i, r in enumerate(rows, 1)) or "<tr><td colspan=6 class='muted'>No award data.</td></tr>"
    return ("<h2>Top corporate groups (by awarded value, wins rolled up to parent)</h2>"
            "<table><thead><tr><th>#</th><th>Group / supplier</th><th>Countries</th>"
            "<th>Wins</th><th>Total awarded</th><th>Last win</th></tr></thead>"
            f"<tbody>{body}</tbody></table>")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--niche", default="green")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--limit", type=int, default=15)
    args = ap.parse_args()

    conn = core.get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    rows = group_leaderboard(cur, args.niche, args.days, args.limit)
    conn.close()

    print(f"\nTop {len(rows)} competitors (wins rolled up to corporate group):\n")
    print(f"  {'#':>2}  {'group / supplier':40} {'ent':>3} {'ctry':>4} {'wins':>5} {'awarded':>10}")
    for i, r in enumerate(rows, 1):
        tag = f"[{r['entities']}]" if r["entities"] > 1 else ""
        print(f"  {i:>2}  {(r['competitor'] or '')[:40]:40} {tag:>3} "
              f"{r['countries']:>4} {r['wins']:>5} {eur(r['total']):>10}")


if __name__ == "__main__":
    main()