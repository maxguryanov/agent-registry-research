#!/usr/bin/env python3
"""
Database access for the monitoring pipeline.

One place that knows the connection string, the retry behaviour and the
handful of query helpers every other module uses. Nothing else in the
pipeline imports psycopg directly.

Connection string comes from the DATABASE_URL environment variable, e.g.

    postgresql://user:pass@ep-xxx.eu-central-1.aws.neon.tech/monitoring?sslmode=require

Serverless Postgres suspends an idle database and takes a few seconds to wake
up, so the first connection of a run is retried rather than treated as an
outage.

Deps: psycopg[binary]
"""

from __future__ import annotations

import contextlib
import os
import pathlib
import random
import time
from typing import Any, Iterable, Iterator, Sequence

import psycopg
from psycopg.rows import dict_row

SCHEMA_PATH = pathlib.Path(__file__).with_name("schema.sql")

CONNECT_ATTEMPTS = 6
CONNECT_BASE_DELAY = 1.5   # seconds, doubled each attempt, plus jitter
STATEMENT_TIMEOUT_MS = 120_000


class ConfigError(RuntimeError):
    """The environment is not set up, as opposed to the database being down."""


def database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise ConfigError(
            "DATABASE_URL is not set.\n"
            "Locally:  export DATABASE_URL='postgresql://...?sslmode=require'\n"
            "In CI:    add it under Settings -> Secrets and variables -> Actions."
        )
    # Neon and most managed providers require TLS. Add it if the caller forgot.
    if "sslmode=" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"

    # Pooled endpoints run PgBouncer in transaction mode, where a session-level
    # advisory lock is not held for the length of the session. The indexer uses
    # one to stop two runs overlapping, and it would fail silently rather than
    # loudly. Nothing is corrupted if it does — the inserts are idempotent and
    # the cursor only moves on committed chunks — but the work gets done twice.
    if "-pooler." in url and not _WARNED:
        _warn_pooled()
    return url


_WARNED = False


def _warn_pooled() -> None:
    global _WARNED
    _WARNED = True
    print("  warning: this looks like a pooled connection string (host contains "
          "'-pooler').\n"
          "  The indexer's overlap protection needs a session-level advisory "
          "lock, which\n"
          "  a transaction pooler does not hold. Prefer the direct connection "
          "string:\n"
          "  in the Neon dashboard, switch 'Connection pooling' off when "
          "copying it.")


@contextlib.contextmanager
def connect(autocommit: bool = False) -> Iterator[psycopg.Connection]:
    """
    Yield a connection, retrying while the database wakes from suspend.

    Only connection-time failures are retried. Once a statement is running,
    an error is a real error and is raised to the caller.
    """
    url = database_url()
    last: Exception | None = None

    for attempt in range(CONNECT_ATTEMPTS):
        try:
            conn = psycopg.connect(url, autocommit=autocommit, row_factory=dict_row)
            break
        except psycopg.OperationalError as exc:
            last = exc
            if attempt == CONNECT_ATTEMPTS - 1:
                raise
            delay = CONNECT_BASE_DELAY * (2 ** attempt) + random.uniform(0, 0.5)
            print(f"  db: connection attempt {attempt + 1} failed "
                  f"({type(exc).__name__}), retrying in {delay:.1f}s")
            time.sleep(delay)
    else:  # pragma: no cover - the loop always breaks or raises
        raise last  # type: ignore[misc]

    try:
        # UTC everywhere. Timestamps in this dataset are compared across runs
        # and across machines; a session picking up the host's local zone is a
        # silent source of wrong survival intervals.
        conn.execute("SET timezone = 'UTC'")
        conn.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def fetch_all(conn: psycopg.Connection, sql: str,
              params: Sequence[Any] | None = None) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def fetch_one(conn: psycopg.Connection, sql: str,
              params: Sequence[Any] | None = None) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def scalar(conn: psycopg.Connection, sql: str,
           params: Sequence[Any] | None = None) -> Any:
    row = fetch_one(conn, sql, params)
    return None if row is None else next(iter(row.values()))


def execute(conn: psycopg.Connection, sql: str,
            params: Sequence[Any] | None = None) -> int:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.rowcount


def execute_many(conn: psycopg.Connection, sql: str,
                 rows: Iterable[Sequence[Any]]) -> int:
    rows = list(rows)
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
        return cur.rowcount


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def apply_schema(conn: psycopg.Connection) -> None:
    """Apply schema.sql. Every statement in it is idempotent."""
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


TABLES = [
    "schema_version", "blocks", "registration_events", "agents",
    "check_runs", "liveness_checks", "indexer_state", "excluded_hosts",
]


def status(conn: psycopg.Connection) -> list[tuple[str, int | str]]:
    """Row count per table, for a quick 'is it alive' check."""
    out: list[tuple[str, int | str]] = []
    for table in TABLES:
        try:
            out.append((table, scalar(conn, f"SELECT count(*) FROM {table}")))
        except psycopg.Error:
            conn.rollback()
            out.append((table, "missing"))
    return out


# ---------------------------------------------------------------------------
# Small conversions
# ---------------------------------------------------------------------------

def to_int(value: Any) -> int | None:
    """
    agent_id is NUMERIC(78,0) because a uint256 does not fit in a bigint,
    and psycopg hands NUMERIC back as Decimal. Callers that want a plain int
    go through here.
    """
    return None if value is None else int(value)
