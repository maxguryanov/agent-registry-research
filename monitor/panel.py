#!/usr/bin/env python3
"""
Choose the panel of agents that gets re-probed every day.

    python3 -m monitor.panel --target 2000    # size the panel, then select
    python3 -m monitor.panel --status         # show what is in it
    python3 -m monitor.panel --rate 32        # select exactly 1 agent in 32

Why a panel and not everything
------------------------------
The registry holds around 64,000 agents. Probing all of them daily would mean
64,000 requests a day to other people's servers, roughly three hours per run,
and about 23 million rows a year. None of that buys precision worth having:
the 2026 report measured 1,204 agents and got a 95% interval of plus or minus
1.4 points. Two thousand narrows that to about 1.1. A full census would remove
the remaining 1.1 points and cost thirty times the data.

How members are chosen
----------------------
By a hash of the agent id, taking every agent whose hash falls below a
threshold. Three properties follow, and all three matter:

  It is reproducible. Anyone with the agent ids can recompute the exact panel
  and check the sample was not cherry-picked.

  It is stable. Membership depends only on the agent id, so re-running this
  never swaps one agent for another. Survival analysis needs the same agents
  measured over time.

  It stays representative as the registry grows. New registrations are hashed
  on the same rule, so the panel keeps pace without being reshuffled.

Membership is only ever added, never removed. An agent that leaves the panel
would leave a hole in every survival curve that already counted it.
"""

from __future__ import annotations

import argparse

from . import db

# md5 of the agent id, first 8 hex digits as an integer. md5 is used because
# its output is fixed across Postgres versions, unlike hashtext(). This is a
# sampling decision, not a security one.
HASH_EXPR = "(('x' || substr(md5(agent_id::text), 1, 8))::bit(32)::bigint & 4294967295)"
HASH_SPACE = 4294967296


SELECT_INTO_PANEL = f"""
UPDATE agents
   SET in_panel = TRUE,
       panel_added_at = COALESCE(panel_added_at, now()),
       panel_stratum = COALESCE(panel_stratum,
                                to_char(registered_at, 'YYYY-MM')),
       updated_at = now()
 WHERE NOT in_panel
   AND {HASH_EXPR} < %s
"""


def current_rate(conn) -> tuple[int, int]:
    total = db.scalar(conn, "SELECT count(*) FROM agents") or 0
    panel = db.scalar(conn, "SELECT count(*) FROM agents WHERE in_panel") or 0
    return total, panel


def status(conn) -> None:
    total, panel = current_rate(conn)
    print(f"agents in registry : {total:,}")
    print(f"agents in panel    : {panel:,}"
          + (f"   ({100 * panel / total:.2f}%)" if total else ""))
    if not panel:
        return

    print()
    print("panel by registration month (stratum):")
    rows = db.fetch_all(conn, """
        WITH panel AS (
            SELECT COALESCE(panel_stratum, 'unknown') AS stratum, count(*) AS n
              FROM agents WHERE in_panel GROUP BY 1
        ),
        registry AS (
            SELECT COALESCE(to_char(registered_at, 'YYYY-MM'), 'unknown') AS stratum,
                   count(*) AS n
              FROM agents GROUP BY 1
        )
        SELECT p.stratum, p.n AS in_panel, COALESCE(r.n, 0) AS in_registry
          FROM panel p LEFT JOIN registry r USING (stratum)
         ORDER BY p.stratum
    """)
    print(f"  {'month':<10}{'panel':>8}{'registry':>11}{'share':>9}")
    for r in rows:
        share = (100 * r["in_panel"] / r["in_registry"]) if r["in_registry"] else 0
        print(f"  {r['stratum']:<10}{r['in_panel']:>8,}{r['in_registry']:>11,}"
              f"{share:>8.2f}%")

    print()
    uris = db.fetch_one(conn, """
        SELECT count(*) FILTER (WHERE current_uri <> '')          AS with_uri,
               count(*) FILTER (WHERE uri_change_count > 0)       AS changed,
               count(DISTINCT owner)                              AS owners
          FROM agents WHERE in_panel
    """)
    print(f"  with a URI       : {uris['with_uri']:,}")
    print(f"  changed URI      : {uris['changed']:,}")
    print(f"  distinct owners  : {uris['owners']:,}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--target", type=int,
                       help="desired panel size; the rate is derived from it")
    group.add_argument("--rate", type=int,
                       help="include one agent in every N")
    ap.add_argument("--status", action="store_true", help="report only")
    ap.add_argument("--dry-run", action="store_true",
                    help="say how many would be added, add nothing")
    args = ap.parse_args()

    with db.connect() as conn:
        if args.status or (args.target is None and args.rate is None):
            status(conn)
            return 0

        total, already = current_rate(conn)
        if total == 0:
            print("no agents yet. Run the indexer first.")
            return 1

        if args.rate:
            rate = args.rate
        else:
            rate = max(1, round(total / args.target))
        threshold = HASH_SPACE // rate

        would_add = db.scalar(
            conn,
            f"SELECT count(*) FROM agents WHERE NOT in_panel "
            f"AND {HASH_EXPR} < %s", (threshold,))

        print(f"agents in registry : {total:,}")
        print(f"already in panel   : {already:,}")
        print(f"sampling rate      : 1 in {rate}")
        print(f"would add          : {would_add:,}")
        print(f"panel would become : {already + would_add:,}")

        if args.dry_run:
            print("\ndry run, nothing changed")
            return 0

        added = db.execute(conn, SELECT_INTO_PANEL, (threshold,))
        conn.commit()
        print(f"\nadded {added:,} agents to the panel")
        print()
        status(conn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
