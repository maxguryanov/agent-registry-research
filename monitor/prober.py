#!/usr/bin/env python3
"""
Check whether agents are alive, and keep every measurement.

    python3 -m monitor.prober                      # the daily panel run
    python3 -m monitor.prober --all                # full sweep, for a report
    python3 -m monitor.prober --limit 50 --dry-run # try it, write nothing
    python3 -m monitor.prober --resume 12          # continue an interrupted run

Each run inserts one row per agent into liveness_checks and never touches an
earlier row. Survival is computed from that history, so a measurement taken
ninety days ago has to still be there, exactly as it was recorded.

Liveness is checked against `agents.current_uri`, which the indexer derives
from the most recent URIUpdated event. Checking the URI from the registration
event instead overstates the dead population by roughly a third.

Politeness is not one setting but four: overall concurrency, one request at a
time per host, a minimum gap between requests to the same host, and robots.txt.
The contact address in the User-Agent is backed by the excluded_hosts table,
so a request to stop can be honoured rather than promised.

Deps: httpx, psycopg[binary]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time

import httpx

from . import db, liveness

VERSION = "1.0"
PROJECT_URL = "https://github.com/maxguryanov/agent-registry-research"
DEFAULT_CONTACT = os.environ.get("PROBER_CONTACT", "")
MAX_CONCURRENCY = 10        # a hard ceiling, not a default

INSERT_CHECK = """
INSERT INTO liveness_checks (
    run_id, agent_id, checked_at, uri_checked, uri_source, uri_scheme,
    resolved_url, s1_uri_present, s2_resolved, s3_valid_json, s4_schema_match,
    s5_has_services, s6_endpoint_alive, live_strict, funnel_stage,
    type_field_raw, type_is_canonical, registry_field_present,
    registry_field_matches, failure_stage, failure_category, failure_detail,
    http_status, latency_ms, content_type, content_bytes, services_count,
    endpoints_total, endpoints_checked, endpoints_ok, endpoints_ok_specific,
    generic_only, endpoint_details, doc_sha256
) VALUES (
    %(run_id)s, %(agent_id)s, now(), %(uri_checked)s, %(uri_source)s,
    %(uri_scheme)s, %(resolved_url)s, %(s1_uri_present)s, %(s2_resolved)s,
    %(s3_valid_json)s, %(s4_schema_match)s, %(s5_has_services)s,
    %(s6_endpoint_alive)s, %(live_strict)s, %(funnel_stage)s,
    %(type_field_raw)s, %(type_is_canonical)s, %(registry_field_present)s,
    %(registry_field_matches)s, %(failure_stage)s, %(failure_category)s,
    %(failure_detail)s, %(http_status)s, %(latency_ms)s, %(content_type)s,
    %(content_bytes)s, %(services_count)s, %(endpoints_total)s,
    %(endpoints_checked)s, %(endpoints_ok)s, %(endpoints_ok_specific)s,
    %(generic_only)s, %(endpoint_details)s, %(doc_sha256)s
)
ON CONFLICT (run_id, agent_id) DO NOTHING
"""

UPDATE_AGENT_HOST = """
UPDATE agents SET uri_host = %s, uri_root_domain = %s, updated_at = now()
 WHERE agent_id = %s AND (uri_host IS DISTINCT FROM %s
                          OR uri_root_domain IS DISTINCT FROM %s)
"""


def load_agents(conn, *, everything: bool, limit: int, run_id: int | None):
    where = "TRUE" if everything else "in_panel"
    skip = ""
    params: list = []
    if run_id is not None:
        skip = ("AND NOT EXISTS (SELECT 1 FROM liveness_checks c "
                "WHERE c.run_id = %s AND c.agent_id = a.agent_id)")
        params.append(run_id)
    sql = f"SELECT agent_id, current_uri FROM agents a WHERE {where} {skip} ORDER BY agent_id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return db.fetch_all(conn, sql, params or None)


def load_excluded(conn) -> set[str]:
    return {r["host"].lower()
            for r in db.fetch_all(conn, "SELECT host FROM excluded_hosts")}


def open_run(conn, kind: str, planned: int, args, user_agent: str) -> int:
    run_id = db.scalar(conn, """
        INSERT INTO check_runs (kind, status, agents_planned, concurrency,
                                timeout_seconds, user_agent, prober_version, notes)
        VALUES (%s, 'running', %s, %s, %s, %s, %s, %s) RETURNING id
    """, (kind, planned, args.concurrency, args.timeout, user_agent, VERSION,
          args.notes))
    conn.commit()
    return run_id


def close_run(conn, run_id: int, checked: int, status: str, notes: str) -> None:
    db.execute(conn, """
        UPDATE check_runs SET status = %s, finished_at = now(),
               agents_checked = %s,
               notes = COALESCE(notes || ' | ', '') || %s
         WHERE id = %s
    """, (status, checked, notes[:500], run_id))
    conn.commit()


async def run(args) -> int:
    started = time.time()

    contact = args.contact or DEFAULT_CONTACT
    if not contact and not args.no_contact:
        print("error: no contact address.\n"
              "  Pass --contact you@example.com, set PROBER_CONTACT, or pass\n"
              "  --no-contact to identify with the project URL alone.\n"
              "  The prober requests documents from other people's servers and\n"
              "  says who it is; refusing to say is not an option here.")
        return 2

    user_agent = liveness.build_user_agent(contact, PROJECT_URL, VERSION)
    concurrency = min(args.concurrency, MAX_CONCURRENCY)

    with db.connect() as conn:
        db.ensure_schema(conn)

        run_id = args.resume
        if run_id:
            row = db.fetch_one(conn, "SELECT * FROM check_runs WHERE id = %s",
                               (run_id,))
            if row is None:
                print(f"no run with id {run_id}")
                return 1
            kind = row["kind"]
            print(f"resuming run {run_id} ({kind}), started {row['started_at']}")
        else:
            kind = "sweep" if args.all else "panel"

        agents = load_agents(conn, everything=args.all, limit=args.limit,
                             run_id=run_id)
        if not agents:
            if args.resume:
                print(f"run {run_id} has no agents left to check; "
                      f"it is already complete")
            elif args.all:
                print("nothing to probe: there are no agents. Run the indexer first.")
            else:
                print("nothing to probe: the panel is empty.\n"
                      "  Run: python3 -m monitor.panel --target 2000")
            return 0

        excluded = load_excluded(conn)
        if not run_id:
            run_id = open_run(conn, kind, len(agents), args, user_agent)

        print(f"run id         : {run_id}  ({kind})")
        print(f"agents to check: {len(agents):,}")
        print(f"concurrency    : {concurrency}"
              + (f" (requested {args.concurrency}, capped at {MAX_CONCURRENCY})"
                 if args.concurrency > MAX_CONCURRENCY else ""))
        print(f"per-host gap   : {args.host_interval}s, one request at a time")
        print(f"timeout        : {args.timeout}s")
        print(f"robots.txt     : {'respected' if not args.ignore_robots else 'IGNORED'}")
        print(f"excluded hosts : {len(excluded)}")
        print(f"user-agent     : {user_agent}")
        print()

        limits = httpx.Limits(max_connections=concurrency,
                              max_keepalive_connections=concurrency)
        headers = {"User-Agent": user_agent,
                   "Accept": "application/json, text/plain, */*"}

        counters = {"done": 0, "live": 0, "generic_only": 0, "written": 0}
        categories: dict[str, int] = {}
        stage_counts = [0] * 7

        async with httpx.AsyncClient(timeout=args.timeout, limits=limits,
                                     headers=headers, follow_redirects=True) as client:
            limiter = liveness.HostLimiter(min_interval=args.host_interval)
            robots = liveness.RobotsCache(client, user_agent,
                                          enabled=not args.ignore_robots)
            prober = liveness.Prober(client, limiter, robots, excluded)
            gate = asyncio.Semaphore(concurrency)

            async def one(agent) -> dict:
                async with gate:
                    return await prober.check(agent["agent_id"],
                                              agent["current_uri"] or "")

            tasks = [asyncio.create_task(one(a)) for a in agents]
            for completed in asyncio.as_completed(tasks):
                row = await completed
                counters["done"] += 1
                stage_counts[row["funnel_stage"]] += 1
                if row["live_strict"]:
                    counters["live"] += 1
                if row["generic_only"]:
                    counters["generic_only"] += 1
                if row["failure_category"]:
                    categories[row["failure_category"]] = \
                        categories.get(row["failure_category"], 0) + 1

                if not args.dry_run:
                    row["run_id"] = run_id
                    # Everything here that is text came from a document on
                    # someone else's server, which is under no obligation to
                    # produce something Postgres will store. A single NUL byte
                    # in a type field would otherwise abort the whole run.
                    payload = {k: db.strip_nulls(v) for k, v in row.items()
                               if k not in ("uri_host", "uri_root_domain")}
                    counters["written"] += db.execute(conn, INSERT_CHECK, payload)
                    if row["uri_host"]:
                        db.execute(conn, UPDATE_AGENT_HOST,
                                   (row["uri_host"], row["uri_root_domain"],
                                    row["agent_id"], row["uri_host"],
                                    row["uri_root_domain"]))
                    if counters["done"] % 25 == 0:
                        conn.commit()

                if counters["done"] % 50 == 0 or counters["done"] == len(agents):
                    rate = counters["done"] / max(0.001, time.time() - started)
                    print(f"  {counters['done']:>6,}/{len(agents):,}  "
                          f"live {counters['live']:>5,}  "
                          f"{rate:>5.1f}/s  "
                          f"{time.time() - started:>5.0f}s")

            if not args.dry_run:
                conn.commit()

        elapsed = time.time() - started
        note = (f"{counters['done']} checked in {elapsed:.0f}s, "
                f"robots fetched {robots.stats['fetched']}, "
                f"disallowed {robots.stats['disallowed']}")
        if not args.dry_run:
            close_run(conn, run_id, counters["done"], "completed", note)

        summarise(agents, counters, stage_counts, categories, robots, elapsed,
                  args.dry_run)
    return 0


def summarise(agents, counters, stage_counts, categories, robots, elapsed,
              dry_run) -> None:
    total = len(agents)
    reached = [sum(stage_counts[i:]) for i in range(7)]
    labels = ["registered", "non-empty URI", "URI resolves", "valid JSON",
              "matches schema", "declares services", "endpoint responds"]

    print()
    print(f"{'stage':<22}{'n':>8}{'% of total':>13}{'% of previous':>15}")
    print("-" * 58)
    previous = total
    for i, label in enumerate(labels):
        n = total if i == 0 else reached[i]
        print(f"{label:<22}{n:>8,}{100 * n / total:>12.1f}%"
              f"{(100 * n / previous if previous else 0):>14.1f}%")
        previous = n

    print()
    print(f"{'strictly live':<22}{counters['live']:>8,}"
          f"{100 * counters['live'] / total:>12.1f}%")
    print(f"{'generic hosts only':<22}{counters['generic_only']:>8,}"
          f"{100 * counters['generic_only'] / total:>12.1f}%"
          "   <- would count as live without the strict rule")

    if categories:
        print()
        print("failure categories:")
        for name, count in sorted(categories.items(), key=lambda kv: -kv[1])[:15]:
            print(f"  {name:<28}{count:>7,}")

    print()
    print(f"robots.txt fetched : {robots.stats['fetched']} hosts, "
          f"{robots.stats['disallowed']} requests disallowed")
    print(f"elapsed            : {elapsed:.0f}s")
    if not dry_run:
        print(f"rows written       : {counters['written']:,}")
    else:
        print("dry run: nothing written")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--contact", default=None,
                    help="contact address for the User-Agent (or set PROBER_CONTACT)")
    ap.add_argument("--no-contact", action="store_true",
                    help="identify with the project URL only")
    ap.add_argument("--all", action="store_true",
                    help="probe every agent, not just the panel")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--resume", type=int, default=None, metavar="RUN_ID")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--host-interval", type=float, default=0.5,
                    help="minimum seconds between requests to the same host")
    ap.add_argument("--ignore-robots", action="store_true",
                    help="do not fetch robots.txt (say why in --notes)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--notes", default=None)
    args = ap.parse_args()
    return db.guard(lambda: asyncio.run(run(args)))


if __name__ == "__main__":
    raise SystemExit(main())
