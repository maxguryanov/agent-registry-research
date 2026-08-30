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

VERSION = "1.1"

# Outcomes that are facts about our access, not about the agent. Kept out of
# every rate. The same set is censored in the metrics stage.
NOT_MEASURED = {"excluded_by_request", "robots_disallowed", "undetermined"}
PROJECT_URL = "https://github.com/maxguryanov/agent-registry-research"
DEFAULT_CONTACT = os.environ.get("PROBER_CONTACT", "")
MAX_CONCURRENCY = 10        # a hard ceiling, not a default

REPLACE_CHECK = """
DELETE FROM liveness_checks WHERE run_id = %s AND agent_id = %s
"""

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


async def retry_throttled(conn, run_id: int, rows: list[dict], args,
                          user_agent: str, excluded: set) -> dict:
    """
    Check again, slowly, the agents whose result turned on being throttled.

    A 429 is our own rate limit and a 403 is usually bot protection. Either one
    recorded as a dead agent measures the crawler rather than the registry. So
    they get a second pass at a crawl: two at a time, seconds apart.

    What answers this time is taken at face value, alive or dead. What is
    throttled twice is recorded as `undetermined`, which the metrics stage
    keeps out of the denominator instead of counting against the agent.
    """
    if not rows:
        return {"retried": 0, "recovered": 0, "undetermined": 0}

    print()
    print(f"second pass: {len(rows)} agent(s) looked throttled rather than dead")
    print(f"  concurrency {args.retry_concurrency}, "
          f"{args.retry_host_interval}s between requests to a host")

    limits = httpx.Limits(max_connections=args.retry_concurrency,
                          max_keepalive_connections=args.retry_concurrency)
    counters = {"retried": len(rows), "recovered": 0, "undetermined": 0}

    async with httpx.AsyncClient(
            timeout=args.timeout * 1.5, limits=limits, follow_redirects=True,
            headers={"User-Agent": user_agent,
                     "Accept": "application/json, text/plain, */*"}) as client:
        limiter = liveness.HostLimiter(min_interval=args.retry_host_interval)
        robots = liveness.RobotsCache(client, user_agent,
                                      enabled=not args.ignore_robots)
        prober = liveness.Prober(client, limiter, robots, excluded)
        gate = asyncio.Semaphore(args.retry_concurrency)

        async def one(row):
            async with gate:
                return await prober.check(row["agent_id"], row["uri_checked"])

        for done, coro in enumerate(
                asyncio.as_completed([asyncio.create_task(one(r)) for r in rows]), 1):
            fresh = await coro
            if liveness.looks_throttled(fresh):
                fresh["failure_category"] = "undetermined"
                fresh["failure_detail"] = (
                    "throttled on both passes; not counted for or against "
                    "the agent")
                counters["undetermined"] += 1
            elif fresh["live_strict"]:
                counters["recovered"] += 1

            fresh["run_id"] = run_id
            payload = {k: db.strip_nulls(v) for k, v in fresh.items()
                       if k not in ("uri_host", "uri_root_domain")}
            with conn.cursor() as cur:
                cur.execute(REPLACE_CHECK, (run_id, fresh["agent_id"]))
                cur.execute(INSERT_CHECK, payload)
            if done % 20 == 0:
                conn.commit()
                print(f"  {done}/{len(rows)}")
    conn.commit()

    print(f"  answered on the second pass : {counters['recovered']} now live")
    print(f"  throttled again             : {counters['undetermined']} "
          f"recorded as undetermined")
    return counters


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

            throttled: list[dict] = []
            tasks = [asyncio.create_task(one(a)) for a in agents]
            for completed in asyncio.as_completed(tasks):
                row = await completed
                if liveness.looks_throttled(row):
                    throttled.append(row)
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

        retry_stats = {"retried": 0, "recovered": 0, "undetermined": 0}
        if throttled and not args.dry_run and args.retry_throttled:
            retry_stats = await retry_throttled(
                conn, run_id, throttled, args, user_agent, excluded)
            counters["live"] += retry_stats["recovered"]
        elif throttled:
            print(f"\n{len(throttled)} agent(s) looked throttled; second pass "
                  f"skipped")

        elapsed = time.time() - started
        note = (f"{counters['done']} checked in {elapsed:.0f}s, "
                f"robots fetched {robots.stats['fetched']}, "
                f"disallowed {robots.stats['disallowed']}, "
                f"retried {retry_stats['retried']}, "
                f"undetermined {retry_stats['undetermined']}")
        if not args.dry_run:
            close_run(conn, run_id, counters["done"], "completed", note)

        if args.dry_run:
            print(f"\ndry run: {counters['done']} checked, "
                  f"{counters['live']} strictly live, nothing written")
        else:
            summarise(conn, run_id, robots, elapsed, retry_stats)
    return 0


def summarise(conn, run_id: int, robots, elapsed: float,
              retry_stats: dict | None = None) -> None:
    """
    Report the run by reading back what was stored.

    Not from the counters kept while probing: the second pass rewrites some of
    those rows, and a summary built from memory said 46% live in the same
    breath that the metrics said 100%. One of them was counting agents the
    other had excluded. Reading the table cannot drift from it.
    """
    rows = db.fetch_all(conn, """
        SELECT s1_uri_present, s2_resolved, s3_valid_json, s4_schema_match,
               s5_has_services, s6_endpoint_alive, live_strict, generic_only,
               failure_category
          FROM liveness_checks WHERE run_id = %s
    """, (run_id,))
    if not rows:
        print("\nnothing was stored for this run")
        return

    not_measured = [r for r in rows if r["failure_category"] in NOT_MEASURED]
    measured = [r for r in rows if r["failure_category"] not in NOT_MEASURED]
    total = len(measured)

    stages = [("s1_uri_present", "non-empty URI"), ("s2_resolved", "URI resolves"),
              ("s3_valid_json", "valid JSON"), ("s4_schema_match", "matches schema"),
              ("s5_has_services", "declares services"),
              ("s6_endpoint_alive", "endpoint responds"),
              ("live_strict", "endpoint responds (strict)")]

    print()
    print(f"{'stage':<28}{'n':>8}{'% of measured':>16}{'% of previous':>15}")
    print("-" * 67)
    print(f"{'registered':<28}{total:>8,}{100.0:>15.1f}%")
    previous = total
    for key, label in stages:
        n = sum(1 for r in measured if r[key])
        share = 100 * n / total if total else 0
        prev = f"{100 * n / previous:.1f}%" if previous else ""
        print(f"{label:<28}{n:>8,}{share:>15.1f}%{prev:>15}")
        previous = n

    generic = sum(1 for r in measured if r["generic_only"])
    print()
    print(f"{'generic hosts only':<28}{generic:>8,}"
          f"{(100 * generic / total if total else 0):>15.1f}%"
          "   <- live without the strict rule")

    if not_measured:
        print()
        print(f"not measured: {len(not_measured):,} of {len(rows):,} "
              f"— kept out of every rate above")
        counts: dict = {}
        for r in not_measured:
            counts[r["failure_category"]] = counts.get(r["failure_category"], 0) + 1
        for name, count in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"  {name:<28}{count:>7,}")

    failures: dict = {}
    for r in measured:
        if r["failure_category"]:
            failures[r["failure_category"]] = failures.get(r["failure_category"], 0) + 1
    if failures:
        print()
        print("failure categories:")
        for name, count in sorted(failures.items(), key=lambda kv: -kv[1])[:15]:
            print(f"  {name:<28}{count:>7,}")

    if retry_stats and retry_stats.get("retried"):
        print()
        print(f"throttled, re-checked slowly : {retry_stats['retried']}")
        print(f"  answered second time       : {retry_stats['recovered']}")
        print(f"  throttled again            : {retry_stats['undetermined']}")

    print()
    print(f"robots.txt fetched : {robots.stats['fetched']} hosts, "
          f"{robots.stats['disallowed']} requests disallowed")
    print(f"elapsed            : {elapsed:.0f}s")
    print(f"rows stored        : {len(rows):,}")


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
    ap.add_argument("--retry-throttled", action="store_true", default=True,
                    help="re-check agents that looked throttled, slowly")
    ap.add_argument("--no-retry-throttled", dest="retry_throttled",
                    action="store_false")
    ap.add_argument("--retry-concurrency", type=int, default=2)
    ap.add_argument("--retry-host-interval", type=float, default=3.0)
    ap.add_argument("--ignore-robots", action="store_true",
                    help="do not fetch robots.txt (say why in --notes)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--notes", default=None)
    args = ap.parse_args()
    return db.guard(lambda: asyncio.run(run(args)))


if __name__ == "__main__":
    raise SystemExit(main())
