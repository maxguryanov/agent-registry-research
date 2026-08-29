#!/usr/bin/env python3
"""
Create or verify the monitoring schema.

    python3 -m monitor.init_db            # apply schema.sql, then show status
    python3 -m monitor.init_db --status   # show status only, change nothing

Safe to run repeatedly: schema.sql only creates things that do not exist and
never drops or alters anything. Running it against a populated database does
not touch the data.
"""

from __future__ import annotations

import argparse
import sys

from . import db


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--status", action="store_true",
                    help="report table row counts without applying the schema")
    args = ap.parse_args()

    try:
        url = db.database_url()
    except db.ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # Never print the password.
    host = url.split("@")[-1].split("/")[0] if "@" in url else ""
    print(f"database host: {host or '(local socket)'}")

    with db.connect() as conn:
        if not args.status:
            print("applying schema ...")
            db.apply_schema(conn)
            print("schema applied")

        print()
        print(f"{'table':<24}{'rows':>10}")
        print("-" * 34)
        for table, count in db.status(conn):
            print(f"{table:<24}{count:>10}")

        if not db.schema_ready(conn):
            print()
            print(db.NOT_CREATED)
            return 0

        cursor = db.fetch_one(
            conn,
            "SELECT stream, start_block, last_block, confirmations, "
            "       last_run_at, last_run_status "
            "FROM indexer_state ORDER BY stream LIMIT 1",
        )
        if cursor:
            behind = cursor["last_block"] - cursor["start_block"] + 1
            print()
            print(f"indexer stream : {cursor['stream']}")
            print(f"  start block  : {cursor['start_block']}")
            print(f"  last block   : {cursor['last_block']}"
                  f"  ({max(0, behind)} blocks ingested)")
            print(f"  confirmations: {cursor['confirmations']}")
            print(f"  last run     : {cursor['last_run_at'] or 'never'}"
                  f"  {cursor['last_run_status'] or ''}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
