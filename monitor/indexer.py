#!/usr/bin/env python3
"""
Pull new registry events from Base into Postgres. Runs hourly; resumes from a
cursor kept in the database.

    python3 -m monitor.indexer                    # catch up to the chain tip
    python3 -m monitor.indexer --max-blocks 500000  # bounded backfill step
    python3 -m monitor.indexer --max-seconds 900    # stop cleanly after 15 min
    python3 -m monitor.indexer --status             # report, change nothing

Three guarantees, each one enforced somewhere it cannot be undone by a bug
in this file:

  Re-running a block range creates no duplicates. The uniqueness constraint on
  (tx_hash, log_index) rejects them in the database.

  `agents` is recomputed from the event table rather than incremented in
  place. Processing the same events twice therefore produces the same state,
  which an incrementing counter would not.

  The cursor advances only when the events of a chunk are committed, in the
  same transaction. A crash mid-chunk loses no events and skips none.

Deps: httpx, psycopg[binary]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time
from typing import Any

from . import chain, db

STREAM = "identity_registry_base"
ZERO_ADDRESS = "0x" + "0" * 40

# Two runs overlapping would do the same work twice and fight over the cursor.
# An advisory lock is held for the length of the run and released when the
# connection closes, including on a crash.
LOCK_KEY = 8004_0001

# How many times one chunk may fail before the run gives up on it. A long
# backfill crosses hours of public-RPC weather; losing the whole run to one
# bad minute means starting the wait again.
CHUNK_RETRIES = 4

STORE_RAW_DATA = os.environ.get("INDEXER_STORE_RAW_DATA", "").lower() in ("1", "true", "yes")

INSERT_EVENT = """
INSERT INTO registration_events
    (event_type, agent_id, block_number, block_time, tx_hash, log_index,
     topic0, owner, from_address, agent_uri, metadata_key, metadata_value,
     raw_topics, raw_data)
VALUES (%s, %s, %s, to_timestamp(%s), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (tx_hash, log_index) DO NOTHING
"""

# Rebuild the current state of the touched agents from the event log.
# Written as one statement so the whole recomputation is atomic, and derived
# purely from stored events so that it is idempotent.
REBUILD_AGENTS = """
WITH affected AS (
    SELECT unnest(%(ids)s::numeric[]) AS agent_id
),
ev AS (
    SELECT e.* FROM registration_events e JOIN affected a USING (agent_id)
),
reg AS (                       -- the registration event, earliest wins
    SELECT DISTINCT ON (agent_id)
           agent_id, block_number, block_time, tx_hash, owner, agent_uri
    FROM ev WHERE event_type = 'Registered'
    ORDER BY agent_id, block_number, log_index
),
uri AS (                       -- whatever set the URI most recently
    SELECT DISTINCT ON (agent_id)
           agent_id, agent_uri, block_number, log_index
    FROM ev WHERE event_type IN ('Registered', 'URIUpdated')
    ORDER BY agent_id, block_number DESC, log_index DESC
),
hold AS (                      -- current holder: the most recent Transfer
    SELECT DISTINCT ON (agent_id) agent_id, owner
    FROM ev WHERE event_type = 'Transfer'
    ORDER BY agent_id, block_number DESC, log_index DESC
),
tally AS (
    SELECT agent_id,
           count(*) FILTER (WHERE event_type = 'URIUpdated') AS uri_changes,
           count(*) FILTER (WHERE event_type = 'Transfer'
                              AND from_address <> %(zero)s)  AS transfers
    FROM ev GROUP BY agent_id
)
INSERT INTO agents AS a (
    agent_id, owner, minted_to, transfer_count,
    registered_block, registered_at, registered_tx,
    uri_at_registration, current_uri, current_uri_block, current_uri_log_index,
    uri_change_count, updated_at
)
SELECT reg.agent_id,
       COALESCE(hold.owner, reg.owner),
       reg.owner,
       COALESCE(tally.transfers, 0),
       reg.block_number,
       reg.block_time,
       reg.tx_hash,
       COALESCE(reg.agent_uri, ''),
       COALESCE(uri.agent_uri, ''),
       uri.block_number,
       uri.log_index,
       COALESCE(tally.uri_changes, 0),
       now()
FROM reg
LEFT JOIN uri   USING (agent_id)
LEFT JOIN hold  USING (agent_id)
LEFT JOIN tally USING (agent_id)
ON CONFLICT (agent_id) DO UPDATE SET
    owner                 = EXCLUDED.owner,
    minted_to             = EXCLUDED.minted_to,
    transfer_count        = EXCLUDED.transfer_count,
    registered_block      = EXCLUDED.registered_block,
    registered_at         = EXCLUDED.registered_at,
    registered_tx         = EXCLUDED.registered_tx,
    uri_at_registration   = EXCLUDED.uri_at_registration,
    current_uri           = EXCLUDED.current_uri,
    current_uri_block     = EXCLUDED.current_uri_block,
    current_uri_log_index = EXCLUDED.current_uri_log_index,
    uri_change_count      = EXCLUDED.uri_change_count,
    updated_at            = now()
"""


# ---------------------------------------------------------------------------

def load_cursor(conn, stream: str):
    """Returns the cursor row, or None if the schema is not created yet."""
    if not db.schema_ready(conn):
        return None
    row = db.fetch_one(
        conn, "SELECT * FROM indexer_state WHERE stream = %s", (stream,))
    if row is None:
        raise SystemExit(
            f"stream {stream!r} is not in indexer_state. "
            f"Run: python3 -m monitor.init_db")
    return row


def take_lock(conn) -> bool:
    return bool(db.scalar(conn, "SELECT pg_try_advisory_lock(%s)", (LOCK_KEY,)))


async def resolve_block_times(pool: chain.RpcPool, clock: chain.BlockClock,
                              rows: list[dict]) -> dict[int, int]:
    """
    Timestamp per block, predicted arithmetically and checked against one real
    block. If the check fails, every block in the chunk is fetched for real.
    """
    numbers = sorted({r["block_number"] for r in rows})
    if not numbers:
        return {}

    try:
        drift = await clock.verify(pool, numbers[-1])
    except chain.RpcError as exc:
        # Verification is a safety check on an assumption that has held at
        # every sampled point across nine million blocks. Losing the check for
        # one chunk is worth far less than losing the run, so carry on with
        # the prediction and say so.
        print(f"    ! could not verify block times this chunk ({exc}); "
              f"using the predicted times")
        clock.unverified_chunks += 1
        return {n: clock.predict(n) for n in numbers}

    if drift == 0:
        return {n: clock.predict(n) for n in numbers}

    print(f"    ! block clock off by {drift}s at block {numbers[-1]}; "
          f"fetching {len(numbers)} real timestamps")
    actual = await chain.block_times(pool, numbers)
    return {n: actual.get(n, clock.predict(n)) for n in numbers}


def store_chunk(conn, rows: list[dict], times: dict[int, int],
                stream: str, new_cursor: int) -> tuple[int, int]:
    """
    Write one chunk and move the cursor, in a single transaction.

    Returns (events_inserted, agents_touched).
    """
    inserted = 0
    with conn.cursor() as cur:
        for r in rows:
            cur.execute(INSERT_EVENT, (
                r["event_type"], r["agent_id"], r["block_number"],
                times.get(r["block_number"]), r["tx_hash"], r["log_index"],
                r["topic0"], r["owner"], r["from_address"], r["agent_uri"],
                r["metadata_key"], r["metadata_value"], r["raw_topics"],
                r["raw_data"] if (STORE_RAW_DATA or r["event_type"] == "unknown"
                                  or r.get("sanitized")) else None,
            ))
            inserted += cur.rowcount

        sanitized = sum(1 for r in rows if r.get("sanitized"))
        ids = sorted({r["agent_id"] for r in rows if r["agent_id"] is not None})
        if ids:
            cur.execute(REBUILD_AGENTS, {"ids": ids, "zero": ZERO_ADDRESS})

        cur.execute(
            "UPDATE indexer_state SET last_block = %s, last_run_at = now(), "
            "last_advance_at = now(), "
            "last_run_status = 'ok', last_error = NULL, "
            "events_ingested = events_ingested + %s, updated_at = now() "
            "WHERE stream = %s",
            (new_cursor, inserted, stream))
    conn.commit()
    return inserted, len(ids) if rows else 0, sanitized


def hours_since_advance(conn, stream: str) -> float | None:
    """
    How long since the cursor last moved. None if it never has.

    Used to tell a passing network failure from a stall. The chain being
    briefly unreachable is normal and self-correcting; the same failure every
    hour for a day is not, and the difference is not visible in any single run.
    """
    value = db.scalar(
        conn,
        "SELECT EXTRACT(EPOCH FROM (now() - last_advance_at)) / 3600.0 "
        "FROM indexer_state WHERE stream = %s", (stream,))
    return None if value is None else float(value)


def record_failure(conn, stream: str, message: str) -> None:
    try:
        conn.rollback()
        db.execute(conn,
                   "UPDATE indexer_state SET last_run_at = now(), "
                   "last_run_status = 'error', last_error = %s, updated_at = now() "
                   "WHERE stream = %s", (message[:1000], stream))
        conn.commit()
    except Exception:  # noqa: BLE001 - reporting must not mask the real error
        pass


# ---------------------------------------------------------------------------

async def report_unreachable(conn, pool, args, exc) -> int:
    """
    The chain could not be reached. Decide whether that is a hiccup or a stall.

    The cursor has not moved, so the next run picks up from the same block and
    nothing is lost. An hourly job that goes red every time a public RPC
    hiccups teaches you to ignore red runs, which is worse than the hiccup. So
    this is a warning while it is plausibly temporary, and an error once the
    cursor has been stuck long enough that it plainly is not.
    """
    record_failure(conn, args.stream, f"{type(exc).__name__}: {exc}")
    stalled = hours_since_advance(conn, args.stream)
    stuck = stalled is None or stalled > args.fail_on_stall_hours
    how_long = "never" if stalled is None else f"{stalled:.1f} hours ago"
    detail = (f"Could not reach the chain: {exc}. The cursor has not moved; "
              f"the next run resumes from the same block. "
              f"Last advance: {how_long}.")
    print()
    if stuck:
        print(f"::error title=Indexer stalled::{detail} That is longer than "
              f"the {args.fail_on_stall_hours}h threshold, so it is reported "
              f"as a failure rather than a hiccup.")
    else:
        print(f"::warning title=Chain unreachable::{detail}")
    await pool.aclose()
    return 1 if stuck else 0


async def run(args) -> int:
    started = time.time()
    pool = chain.RpcPool()
    clock = chain.BlockClock()

    # Hosts only, never the key. Secrets are masked in the log, so without
    # this there is no way to tell a one-endpoint pool from a four-endpoint
    # one, or a Base endpoint from an Ethereum one.
    hosts = pool.describe()
    print(f"rpc endpoints  : {len(hosts)}")
    for host in hosts:
        print(f"                 {host}")
    if not any("base" in h for h in hosts):
        print("  ! none of these look like a Base endpoint. This indexer reads "
              "the Base chain;\n    an Ethereum endpoint will answer, and "
              "find nothing.")
    print()

    with db.connect() as conn:
        cursor = load_cursor(conn, args.stream)
        if cursor is None:
            print(db.NOT_CREATED)
            await pool.aclose()
            return 0 if args.status else 1

        if args.status:
            try:
                tip = await pool.block_number()
            except chain.RpcError as exc:
                tip = None
                print(f"note: the chain is unreachable right now ({exc}).\n"
                      f"      Everything below comes from the database.\n")
            report(conn, cursor, tip)
            await pool.aclose()
            return 0

        if not take_lock(conn):
            print("another indexer run holds the lock; exiting without doing work")
            await pool.aclose()
            return 0

        confirmations = args.confirmations \
            if args.confirmations is not None else cursor["confirmations"]
        start = cursor["last_block"] + 1
        totals = {"events": 0, "inserted": 0, "agents": 0, "chunks": 0,
                  "chunk_retries": 0, "sanitized": 0}
        stopped_early = None

        try:
            tip = await pool.block_number()
        except chain.RpcError as exc:
            return await report_unreachable(conn, pool, args, exc)

        target = tip - confirmations
        if args.max_blocks:
            target = min(target, start + args.max_blocks - 1)

        if target < start:
            print(f"nothing to do: cursor at {cursor['last_block']}, "
                  f"tip {tip} minus {confirmations} confirmations = {target}")
            await pool.aclose()
            return 0

        print(f"chain tip      : {tip}")
        print(f"confirmations  : {confirmations}")
        print(f"indexing       : {start} .. {target}  "
              f"({target - start + 1:,} blocks)")
        print(f"chunk size     : {args.chunk:,} blocks")
        print()

        try:
            lo = start
            while lo <= target:
                hi = min(target, lo + args.chunk - 1)

                for retry in range(CHUNK_RETRIES):
                    try:
                        logs, unreadable = await chain.get_logs(
                            pool, lo, hi, topics=chain.ALL_TOPICS)
                        break
                    except chain.RpcError as exc:
                        if retry == CHUNK_RETRIES - 1:
                            raise
                        pause = 5 * (retry + 1)
                        print(f"    ! chunk {lo}-{hi} failed ({exc}); "
                              f"retrying in {pause}s")
                        totals["chunk_retries"] += 1
                        await asyncio.sleep(pause)

                rows = [chain.decode_log(l) for l in logs]

                # Never advance past a range we could not read.
                commit_to = hi if unreadable is None else unreadable - 1
                if unreadable is not None:
                    rows = [r for r in rows if r["block_number"] <= commit_to]

                times = await resolve_block_times(pool, clock, rows)
                inserted, agents, sanitized = store_chunk(
                    conn, rows, times, args.stream, commit_to)

                totals["events"] += len(rows)
                totals["inserted"] += inserted
                totals["agents"] += agents
                totals["chunks"] += 1
                totals["sanitized"] += sanitized

                if rows or totals["chunks"] % 20 == 0:
                    pct = 100 * (hi - start + 1) / max(1, target - start + 1)
                    print(f"  {lo:>9}-{hi:<9} {len(rows):>4} events "
                          f"({inserted:>4} new, {agents:>4} agents)  "
                          f"[{pct:5.1f}%  {time.time() - started:.0f}s]")

                if unreadable is not None:
                    stopped_early = f"unreadable range starting at block {unreadable}"
                    break

                lo = hi + 1

                if args.max_seconds and time.time() - started > args.max_seconds:
                    stopped_early = f"time budget of {args.max_seconds}s reached"
                    break

        except chain.RpcError as exc:
            return await report_unreachable(conn, pool, args, exc)

        except Exception as exc:  # noqa: BLE001
            record_failure(conn, args.stream, f"{type(exc).__name__}: {exc}")
            print(f"\nFAILED: {type(exc).__name__}: {exc}")
            print("The cursor still points at the last fully committed chunk, "
                  "so the next run resumes from there.")
            await pool.aclose()
            return 1

        cursor = load_cursor(conn, args.stream)
        print()
        if stopped_early:
            print(f"stopped early: {stopped_early}")
        print(f"chunks         : {totals['chunks']}")
        print(f"events seen    : {totals['events']:,}  "
              f"({totals['inserted']:,} new, "
              f"{totals['events'] - totals['inserted']:,} already known)")
        print(f"cursor now at  : {cursor['last_block']}  "
              f"({tip - cursor['last_block']:,} blocks behind tip)")
        print(f"rpc calls      : {pool.stats['calls']}  "
              f"(retries {pool.stats['retries']}, "
              f"rate-limited {pool.stats['rate_limited']})")
        if totals["chunk_retries"]:
            print(f"chunk retries  : {totals['chunk_retries']}")
        if totals["sanitized"]:
            print(f"nul bytes      : {totals['sanitized']} event(s) contained a "
                  f"NUL byte; it was stripped and the raw payload kept")
        print(f"block clock    : {clock.verifications} checks, "
              f"max drift {clock.max_drift_seconds}s"
              + (f", {clock.unverified_chunks} chunks unverified"
                 if clock.unverified_chunks else ""))
        print(f"elapsed        : {time.time() - started:.0f}s")

    await pool.aclose()
    return 0


def report(conn, cursor: dict, tip: int | None) -> None:
    print(f"stream         : {cursor['stream']}")
    print(f"cursor         : {cursor['last_block']:,}")
    if tip is None:
        print("chain tip      : unknown (chain unreachable)")
    else:
        print(f"chain tip      : {tip:,}  "
              f"({tip - cursor['last_block']:,} blocks behind)")
    print(f"last run       : {cursor['last_run_at'] or 'never'}  "
          f"{cursor['last_run_status'] or ''}")
    print(f"last advance   : {cursor.get('last_advance_at') or 'never'}")
    if cursor["last_error"]:
        print(f"last error     : {cursor['last_error']}")
    print()
    for row in db.fetch_all(conn,
                            "SELECT event_type, count(*) AS n FROM registration_events "
                            "GROUP BY event_type ORDER BY n DESC"):
        print(f"  {row['event_type']:<14}{row['n']:>10,}")
    agents = db.scalar(conn, "SELECT count(*) FROM agents")
    with_uri = db.scalar(
        conn, "SELECT count(*) FROM agents WHERE current_uri <> ''")
    changed = db.scalar(
        conn, "SELECT count(*) FROM agents WHERE uri_change_count > 0")
    print()
    print(f"  {'agents':<14}{agents:>10,}")
    print(f"  {'with a URI':<14}{with_uri:>10,}")
    print(f"  {'URI changed':<14}{changed:>10,}"
          + (f"   ({100 * changed / agents:.1f}% of agents)" if agents else ""))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stream", default=STREAM)
    ap.add_argument("--chunk", type=int, default=5000,
                    help="blocks per eth_getLogs call before adaptive halving")
    ap.add_argument("--max-blocks", type=int, default=0,
                    help="stop after this many blocks (0 = run to the tip)")
    ap.add_argument("--max-seconds", type=int, default=0,
                    help="stop cleanly after this many seconds (0 = no limit)")
    ap.add_argument("--confirmations", type=int, default=None,
                    help="override how far behind the tip to stop")
    ap.add_argument("--fail-on-stall-hours", type=float, default=6.0,
                    help="how long the cursor may stay stuck before an "
                         "unreachable chain is reported as a failure")
    ap.add_argument("--status", action="store_true",
                    help="report state and exit")
    args = ap.parse_args()
    return db.guard(lambda: asyncio.run(run(args)))


if __name__ == "__main__":
    raise SystemExit(main())
