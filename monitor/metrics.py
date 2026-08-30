#!/usr/bin/env python3
"""
Turn the stored history into the numbers that get published.

    python3 -m monitor.metrics                 # print a report
    python3 -m monitor.metrics --json out.json # write the aggregate document
    python3 -m monitor.metrics --run 12        # use a specific probe run

Everything here reads. Nothing in this module writes to the database.

Four things it is careful about:

  Proportions carry Wilson intervals. A funnel stage counted on 2,000 agents
  and one counted on 30 are not the same measurement, and a bare percentage
  hides that.

  Agents we chose not to probe are not counted as dead. A robots.txt refusal
  or an exclusion request is a fact about our access. Those observations are
  censored: reported, and kept out of the funnel denominator.

  Owners and projects are counted separately from agents. One operator running
  thirty-five wallets that all point at the same bucket is one project, not
  thirty-five participants.

  Survival needs history that does not exist yet on day one. Until it does,
  this reports that fact rather than a zero, and offers the cross-sectional
  decay curve instead, which is available immediately.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from . import db
from .hosts import is_shared_hosting

# Outcomes that mean "we did not measure this agent", as opposed to "we
# measured it and it was dead". Two of them are our own choice; the third is
# an agent that answered 429 or 403 on both a normal and a deliberately slow
# pass, where the result turned on our access rather than on the agent.
#
# These are reported and kept out of every denominator. Counting them as dead
# would let the registry look worse the harder someone defends their server.
CENSORED_CATEGORIES = {"excluded_by_request", "robots_disallowed",
                       "undetermined"}

STAGES = [
    ("s1_uri_present", "non-empty URI"),
    ("s2_resolved", "URI resolves"),
    ("s3_valid_json", "valid JSON"),
    ("s4_schema_match", "matches ERC-8004 schema"),
    ("s5_has_services", "declares services"),
    ("s6_endpoint_alive", "endpoint responds"),
    ("live_strict", "endpoint responds (strict)"),
]

SURVIVAL_HORIZONS = [30, 90]
SURVIVAL_TOLERANCE_DAYS = 7


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def wilson(successes: int, total: int, z: float = 1.96) -> dict:
    """
    Proportion with a Wilson score interval.

    Wilson rather than the normal approximation because these proportions sit
    near zero. At 6.8% of 1,204 the normal interval is passable; at 2 of 30 it
    reaches below zero, which is not a possible answer.
    """
    if total <= 0:
        return {"n": 0, "k": 0, "pct": None, "ci_low": None, "ci_high": None}
    p = successes / total
    denominator = 1 + z * z / total
    centre = p + z * z / (2 * total)
    spread = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    return {
        "n": total,
        "k": successes,
        "pct": round(100 * p, 2),
        "ci_low": round(100 * max(0.0, (centre - spread) / denominator), 2),
        "ci_high": round(100 * min(1.0, (centre + spread) / denominator), 2),
    }


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------

class UnionFind:
    def __init__(self):
        self.parent: dict = {}

    def find(self, item):
        self.parent.setdefault(item, item)
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:      # path compression
            self.parent[item], item = root, self.parent[item]
        return root

    def union(self, a, b) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def cluster_projects(rows: list[dict]) -> dict:
    """
    Group agents into projects.

    Two agents belong to the same project if they share an owner address or a
    root domain, and the relation is transitive: an owner linking two domains
    merges everything on both. This is what turns thirty-five wallets pointing
    at one bucket into one project.

    It is a lower bound on independence, not a truth. An operator using
    separate wallets and separate domains still counts as several projects.
    """
    uf = UnionFind()
    for row in rows:
        agent = ("agent", row["agent_id"])
        uf.find(agent)
        if row.get("owner"):
            uf.union(agent, ("owner", row["owner"].lower()))
        domain = (row.get("uri_root_domain") or "").lower()
        # A shared bucket, CDN or gateway is a filing cabinet, not an operator.
        # Joining agents on it produced "projects" like ipfs.io with 257 agents
        # across 254 unrelated wallets. Such an agent clusters by owner alone.
        if domain and not is_shared_hosting(domain):
            uf.union(agent, ("domain", domain))

    projects: dict = defaultdict(list)
    for row in rows:
        projects[uf.find(("agent", row["agent_id"]))].append(row)
    return projects


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def latest_run(conn, kind: str | None = None) -> dict | None:
    sql = ("SELECT * FROM check_runs WHERE status = 'completed' "
           + ("AND kind = %s " if kind else "")
           + "ORDER BY started_at DESC LIMIT 1")
    return db.fetch_one(conn, sql, (kind,) if kind else None)


def load_run_rows(conn, run_id: int) -> list[dict]:
    return db.fetch_all(conn, """
        SELECT c.agent_id, c.s1_uri_present, c.s2_resolved, c.s3_valid_json,
               c.s4_schema_match, c.s5_has_services, c.s6_endpoint_alive,
               c.live_strict, c.generic_only, c.failure_category,
               c.endpoints_total, c.endpoints_ok, c.endpoints_ok_specific,
               a.owner, a.uri_root_domain, a.registered_at, a.uri_change_count
          FROM liveness_checks c
          JOIN agents a USING (agent_id)
         WHERE c.run_id = %s
    """, (run_id,))


# ---------------------------------------------------------------------------
# Funnels
# ---------------------------------------------------------------------------

def funnel_over_agents(rows: list[dict]) -> dict:
    total = len(rows)
    stages = []
    previous = total
    for key, label in STAGES:
        reached = sum(1 for r in rows if r[key])
        entry = {"key": key, "label": label, **wilson(reached, total)}
        entry["pct_of_previous"] = round(100 * reached / previous, 2) if previous else None
        stages.append(entry)
        previous = reached
    return {"denominator": total, "stages": stages}


def _group_funnel(rows: list[dict], key_of) -> dict:
    """Funnel over groups: a group reaches a stage if any of its agents does."""
    groups: dict = defaultdict(list)
    for row in rows:
        group = key_of(row)
        if group is not None:
            groups[group].append(row)

    total = len(groups)
    stages = []
    previous = total
    for key, label in STAGES:
        reached = sum(1 for members in groups.values()
                      if any(m[key] for m in members))
        entry = {"key": key, "label": label, **wilson(reached, total)}
        entry["pct_of_previous"] = round(100 * reached / previous, 2) if previous else None
        stages.append(entry)
        previous = reached
    return {"denominator": total, "stages": stages}


def funnel_over_owners(rows: list[dict]) -> dict:
    return _group_funnel(rows, lambda r: (r["owner"] or "").lower() or None)


def funnel_over_projects(rows: list[dict]) -> dict:
    projects = cluster_projects(rows)
    membership = {}
    for project_id, members in projects.items():
        for member in members:
            membership[member["agent_id"]] = project_id
    return _group_funnel(rows, lambda r: membership.get(r["agent_id"]))


# ---------------------------------------------------------------------------
# Cohorts and survival
# ---------------------------------------------------------------------------

def cross_sectional_cohorts(rows: list[dict]) -> list[dict]:
    """
    Liveness by registration month, measured in one run.

    Available from the first day, unlike survival. It is not the same thing:
    this compares different agents of different ages at one moment, and
    anything that changed about who registers over time is mixed into it.
    """
    buckets: dict = defaultdict(list)
    for row in rows:
        if row["registered_at"]:
            buckets[row["registered_at"].strftime("%Y-%m")].append(row)
    out = []
    for month in sorted(buckets):
        members = buckets[month]
        live = sum(1 for m in members if m["live_strict"])
        out.append({"month": month, **wilson(live, len(members))})
    return out


def survival(conn, horizons=SURVIVAL_HORIZONS,
             tolerance_days: int = SURVIVAL_TOLERANCE_DAYS) -> dict:
    """
    Of the agents observed alive at some point, how many are still alive after
    N days.

    For each agent the anchor is its earliest strictly-live observation. The
    later measurement is the check closest to anchor + N days, accepted only
    if it lands within a tolerance window; otherwise that agent has no verdict
    yet and is counted as not-yet-evaluable rather than as dead.

    Only panel runs are used. Sweeps happen irregularly, and mixing them in
    would let the sampling schedule shape the curve.
    """
    rows = db.fetch_all(conn, """
        SELECT c.agent_id, c.checked_at, c.live_strict, a.registered_at
          FROM liveness_checks c
          JOIN check_runs r ON r.id = c.run_id
          JOIN agents a USING (agent_id)
         WHERE r.kind = 'panel' AND r.status = 'completed'
         ORDER BY c.agent_id, c.checked_at
    """)

    by_agent: dict = defaultdict(list)
    registered: dict = {}
    for row in rows:
        by_agent[row["agent_id"]].append((row["checked_at"], row["live_strict"]))
        registered[row["agent_id"]] = row["registered_at"]

    span_days = 0.0
    if rows:
        first = min(r["checked_at"] for r in rows)
        last = max(r["checked_at"] for r in rows)
        span_days = (last - first).total_seconds() / 86400

    results = []
    for horizon in horizons:
        evaluable = survived = 0
        per_cohort: dict = defaultdict(lambda: [0, 0])

        for agent_id, checks in by_agent.items():
            anchor = next((when for when, live in checks if live), None)
            if anchor is None:
                continue
            target = anchor + timedelta(days=horizon)
            candidates = [(abs((when - target).total_seconds()), when, live)
                          for when, live in checks
                          if abs((when - target).days) <= tolerance_days
                          and when > anchor]
            if not candidates:
                continue
            _, _, still_live = min(candidates)
            evaluable += 1
            survived += bool(still_live)
            reg = registered.get(agent_id)
            if reg:
                bucket = per_cohort[reg.strftime("%Y-%m")]
                bucket[0] += 1
                bucket[1] += bool(still_live)

        entry = {"horizon_days": horizon, **wilson(survived, evaluable)}
        entry["by_cohort"] = [
            {"month": month, **wilson(count[1], count[0])}
            for month, count in sorted(per_cohort.items())
        ]
        results.append(entry)

    enough = span_days >= min(horizons)
    return {
        "status": "ok" if enough else "insufficient_history",
        "history_span_days": round(span_days, 1),
        "days_until_first_horizon": max(0, round(min(horizons) - span_days, 1)),
        "agents_with_history": len(by_agent),
        "horizons": results,
        "note": (None if enough else
                 f"Survival at {min(horizons)} days needs {min(horizons)} days of "
                 f"measurements. There are {round(span_days, 1)}. The horizons "
                 f"below are computed on whatever qualifies so far and will be "
                 f"empty or unstable until then."),
    }


# ---------------------------------------------------------------------------
# Registry-wide figures
# ---------------------------------------------------------------------------

def registry_totals(conn) -> dict:
    row = db.fetch_one(conn, """
        SELECT count(*) AS agents,
               count(DISTINCT owner) AS owners,
               count(*) FILTER (WHERE current_uri <> '') AS with_uri,
               count(*) FILTER (WHERE uri_at_registration = '') AS empty_at_registration,
               count(*) FILTER (WHERE uri_change_count > 0) AS uri_changed,
               count(*) FILTER (WHERE transfer_count > 0) AS transferred,
               min(registered_at) AS first_registration,
               max(registered_at) AS last_registration
          FROM agents
    """)
    cursor = db.fetch_one(conn, "SELECT * FROM indexer_state LIMIT 1")
    events = db.fetch_all(conn, "SELECT event_type, count(*) AS n "
                                "FROM registration_events GROUP BY 1 ORDER BY 2 DESC")
    agents = row["agents"] or 0
    return {
        "agents": agents,
        "distinct_owners": row["owners"],
        "agents_with_uri": row["with_uri"],
        "empty_uri_at_registration": row["empty_at_registration"],
        "uri_changed_after_registration": wilson(row["uri_changed"], agents),
        "transferred": row["transferred"],
        "first_registration": iso(row["first_registration"]),
        "last_registration": iso(row["last_registration"]),
        "events": {e["event_type"]: e["n"] for e in events},
        "indexed_through_block": cursor["last_block"] if cursor else None,
        "indexer_last_run": iso(cursor["last_run_at"]) if cursor else None,
        "indexer_last_status": cursor["last_run_status"] if cursor else None,
        "indexer_last_error": cursor["last_error"] if cursor else None,
    }


def registrations_monthly(conn) -> list[dict]:
    rows = db.fetch_all(conn, """
        SELECT to_char(registered_at, 'YYYY-MM') AS month, count(*) AS n
          FROM agents WHERE registered_at IS NOT NULL
         GROUP BY 1 ORDER BY 1
    """)
    out, running = [], 0
    for row in rows:
        running += row["n"]
        out.append({"month": row["month"], "registered": row["n"],
                    "cumulative": running})
    return out


def failure_breakdown(rows: list[dict]) -> list[dict]:
    counts: dict = defaultdict(int)
    for row in rows:
        if row["failure_category"]:
            counts[row["failure_category"]] += 1
    total = len(rows)
    return [{"category": name, "n": count,
             "pct": round(100 * count / total, 2) if total else None}
            for name, count in sorted(counts.items(), key=lambda kv: -kv[1])]


def top_projects(rows: list[dict], limit: int = 20) -> list[dict]:
    projects = cluster_projects(rows)
    out = []
    for members in projects.values():
        seen = sorted({m["uri_root_domain"] for m in members
                       if m["uri_root_domain"]})
        own_domains = [d for d in seen if not is_shared_hosting(d)]
        hosting = [d for d in seen if is_shared_hosting(d)]
        owners = sorted({(m["owner"] or "").lower() for m in members if m["owner"]})
        if own_domains:
            label = own_domains[0]
        elif hosting:
            label = f"{len(owners)} wallet(s) on {hosting[0][:40]}"
        else:
            label = f"{len(owners)} wallet(s), no domain"
        out.append({
            "agents": len(members),
            "owners": len(owners),
            "domains": own_domains[:5],
            "hosted_on": hosting[:3],
            "live_agents": sum(1 for m in members if m["live_strict"]),
            "label": label,
        })
    out.sort(key=lambda p: -p["agents"])
    return out[:limit]


def iso(value) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def build(conn, run_id: int | None = None) -> dict:
    run = (db.fetch_one(conn, "SELECT * FROM check_runs WHERE id = %s", (run_id,))
           if run_id else latest_run(conn))

    document = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "registry": {
            "chain_id": 8453,
            "chain": "Base",
            "contract": "0x8004a169fb4a3325136eb29fa0ceb6d2e539a432",
            **registry_totals(conn),
        },
        "registrations_monthly": registrations_monthly(conn),
    }

    if run is None:
        document["liveness"] = {"status": "no_completed_runs"}
        document["survival"] = survival(conn)
        return document

    rows = load_run_rows(conn, run["id"])
    censored = [r for r in rows if r["failure_category"] in CENSORED_CATEGORIES]
    analysed = [r for r in rows if r["failure_category"] not in CENSORED_CATEGORIES]

    document["liveness"] = {
        "status": "ok",
        "run": {
            "id": run["id"], "kind": run["kind"],
            "started_at": iso(run["started_at"]),
            "finished_at": iso(run["finished_at"]),
            "agents_checked": run["agents_checked"],
            "concurrency": run["concurrency"],
            "timeout_seconds": float(run["timeout_seconds"])
                               if run["timeout_seconds"] is not None else None,
            "prober_version": run["prober_version"],
        },
        "censored": {
            "n": len(censored),
            "note": ("not measured: excluded by request, disallowed by "
                     "robots.txt, or throttled on both passes. Kept out of "
                     "every denominator rather than counted as dead."),
            "by_category": failure_breakdown(censored),
        },
        "by_agent": funnel_over_agents(analysed),
        "by_owner": funnel_over_owners(analysed),
        "by_project": funnel_over_projects(analysed),
        "generic_hosts_only": wilson(
            sum(1 for r in analysed if r["generic_only"]), len(analysed)),
        "endpoints": {
            "declared": sum(r["endpoints_total"] or 0 for r in analysed),
            "responding": sum(r["endpoints_ok"] or 0 for r in analysed),
            "responding_specific": sum(r["endpoints_ok_specific"] or 0
                                       for r in analysed),
        },
        "failures": failure_breakdown(analysed),
        "cohorts_cross_sectional": cross_sectional_cohorts(analysed),
        "top_projects": top_projects(analysed),
    }
    document["survival"] = survival(conn)
    return document


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _pct(entry: dict) -> str:
    if entry.get("pct") is None:
        return "n/a"
    return (f"{entry['pct']:>6.1f}%  "
            f"[{entry['ci_low']:.1f}-{entry['ci_high']:.1f}]")


def print_funnel(title: str, funnel: dict) -> None:
    print(f"\n{title}  (n = {funnel['denominator']:,})")
    if not funnel["denominator"]:
        print("  no observations")
        return
    print(f"  {'stage':<28}{'n':>8}{'% of total':>12}"
          f"{'95% CI':>16}{'% of prev':>12}")
    print("  " + "-" * 76)
    print(f"  {'registered':<28}{funnel['denominator']:>8,}{100.0:>11.1f}%"
          f"{'':>16}{'':>12}")
    for stage in funnel["stages"]:
        previous = (f"{stage['pct_of_previous']:.1f}%"
                    if stage["pct_of_previous"] is not None else "")
        ci = (f"{stage['ci_low']:.1f}-{stage['ci_high']:.1f}"
              if stage["pct"] is not None else "")
        print(f"  {stage['label']:<28}{stage['k']:>8,}{stage['pct']:>11.1f}%"
              f"{ci:>16}{previous:>12}")


def report(document: dict) -> None:
    registry = document["registry"]
    print("=" * 74)
    print(f"ERC-8004 Identity Registry on Base — {document['generated_at'][:19]}Z")
    print("=" * 74)
    print(f"agents indexed          : {registry['agents']:,}")
    print(f"distinct owners         : {registry['distinct_owners']:,}")
    print(f"indexed through block   : {registry['indexed_through_block']:,}")
    if registry["first_registration"]:
        print(f"registration window     : {registry['first_registration'][:10]}"
              f" .. {registry['last_registration'][:10]}")
    churn = registry["uri_changed_after_registration"]
    print(f"changed URI after mint  : {churn['k']:,} of {churn['n']:,}"
          f"   {_pct(churn)}")
    print(f"empty URI at mint       : {registry['empty_uri_at_registration']:,}")
    print("events                  : "
          + ", ".join(f"{k} {v:,}" for k, v in registry["events"].items()))

    if registry.get("indexer_last_status") == "error" and registry.get("indexer_last_error"):
        print()
        print("!! THE LAST INDEXER RUN FAILED")
        print(f"   when  : {registry['indexer_last_run']}")
        print(f"   error : {registry['indexer_last_error']}")
        print("   The cursor above is where it stopped. Nothing was lost; the "
              "next run resumes there.")

    liveness = document["liveness"]
    if liveness.get("status") != "ok":
        print("\nno completed probe run yet — run: python3 -m monitor.prober")
    else:
        run = liveness["run"]
        print(f"\nprobe run {run['id']} ({run['kind']}), "
              f"{run['started_at'][:19]}Z, "
              f"concurrency {run['concurrency']}, timeout {run['timeout_seconds']}s, "
              f"prober {run['prober_version']}")
        if liveness["censored"]["n"]:
            print(f"not measured: {liveness['censored']['n']:,}"
                  f"  — excluded from every denominator below")
            for c in liveness["censored"]["by_category"]:
                print(f"    {c['category']:<28}{c['n']:>7,}")

        print_funnel("FUNNEL BY AGENT", liveness["by_agent"])
        print_funnel("FUNNEL BY OWNER", liveness["by_owner"])
        print_funnel("FUNNEL BY PROJECT (shared owner or shared root domain)",
                     liveness["by_project"])

        generic = liveness["generic_hosts_only"]
        print(f"\nlive only via a generic host : {generic['k']:,}"
              f"  {_pct(generic)}")
        print("  these would count as live without the strict rule")

        endpoints = liveness["endpoints"]
        print(f"\ndeclared endpoints           : {endpoints['declared']:,}")
        print(f"  responding                 : {endpoints['responding']:,}")
        print(f"  responding, not generic    : {endpoints['responding_specific']:,}")

        if liveness["failures"]:
            print("\nfailure categories:")
            for failure in liveness["failures"][:12]:
                print(f"  {failure['category']:<30}{failure['n']:>7,}"
                      f"{failure['pct']:>8.1f}%")

        cohorts = liveness["cohorts_cross_sectional"]
        if len(cohorts) > 1:
            print("\nLIVENESS BY REGISTRATION MONTH (cross-sectional, not survival)")
            print(f"  {'month':<10}{'agents':>9}{'live':>8}{'%':>9}{'95% CI':>16}")
            print("  " + "-" * 52)
            for cohort in cohorts:
                ci = f"{cohort['ci_low']:.1f}-{cohort['ci_high']:.1f}"
                print(f"  {cohort['month']:<10}{cohort['n']:>9,}{cohort['k']:>8,}"
                      f"{cohort['pct']:>8.1f}%{ci:>16}")

        if liveness["top_projects"]:
            print("\nLARGEST PROJECTS")
            print(f"  {'agents':>7}{'owners':>8}{'live':>6}  label")
            for project in liveness["top_projects"][:10]:
                print(f"  {project['agents']:>7,}{project['owners']:>8,}"
                      f"{project['live_agents']:>6,}  {project['label'][:44]}")

    surv = document["survival"]
    print("\nSURVIVAL")
    print(f"  history span        : {surv['history_span_days']} days")
    print(f"  agents with history : {surv['agents_with_history']:,}")
    if surv["status"] != "ok":
        print(f"  status              : {surv['status']}")
        print(f"  {surv['note']}")
    for horizon in surv["horizons"]:
        label = f"live at T, still live at T+{horizon['horizon_days']}"
        if horizon["n"]:
            print(f"  {label:<34}{horizon['k']:>6,}/{horizon['n']:<6,}"
                  f"{_pct(horizon)}")
        else:
            print(f"  {label:<34}   no agent has been observed that long yet")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", type=int, default=None,
                    help="probe run to report on (default: the latest completed)")
    ap.add_argument("--json", dest="json_path", default=None,
                    help="also write the aggregate document to this file")
    ap.add_argument("--quiet", action="store_true", help="write JSON, print nothing")
    args = ap.parse_args()

    started = time.time()
    with db.connect() as conn:
        if not db.schema_ready(conn):
            print(db.NOT_CREATED)
            return 0
        document = build(conn, args.run)

    if not args.quiet:
        report(document)
        print(f"\ncomputed in {time.time() - started:.1f}s")

    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, ensure_ascii=False)
        if not args.quiet:
            print(f"wrote {args.json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(db.guard(main))
