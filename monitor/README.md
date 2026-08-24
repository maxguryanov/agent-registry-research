# Continuous monitoring

The scripts in the repository root take one measurement. This directory turns
that into a system that keeps measuring and keeps the history.

| Piece | Runs | Job |
|---|---|---|
| `indexer` | hourly | pull new `Registered` / `URIUpdated` / `Transfer` logs from Base |
| `prober` | daily | re-check agent liveness, store every measurement |
| `metrics` + `publish` | daily | compute aggregates, write the public page and JSON API |

Nothing runs continuously. GitHub Actions starts each script on a schedule, it
finishes in minutes, and it exits. There is no server to keep alive.

**Status: module 1 of 6 (database schema) is in place. The rest is being built.**

---

## Tables

| Table | Contains | Written by |
|---|---|---|
| `registration_events` | raw chain logs, append-only | indexer |
| `agents` | current state per agent, overwritten in place | indexer |
| `blocks` | block number to timestamp cache | indexer |
| `indexer_state` | the cursor: last fully ingested block | indexer |
| `check_runs` | one row per probe run, with its settings | prober |
| `liveness_checks` | one row per agent per run, never overwritten | prober |

Three properties this schema is built around:

**Re-running the indexer cannot create duplicates.** `registration_events` has a
uniqueness constraint on `(tx_hash, log_index)` and inserts use
`ON CONFLICT DO NOTHING`. The guarantee is enforced by the database, so it holds
even if the indexer crashes mid-range or is pointed at the same blocks twice.

**Liveness history is never overwritten.** `liveness_checks` gets a new row per
agent per run. Survival rates are computed from this table, so a measurement
taken 90 days ago has to still be there.

**The measurement settings are recorded, not just the result.** `check_runs`
stores concurrency, timeout and prober version. Raising the timeout does not
remove failures, it reclassifies them — `ReadTimeout` becomes HTTP 504 from an
IPFS gateway. Two runs are only comparable if you can see how each was
configured.

---

## Setup

### 1. Create the database

Sign up at [neon.com](https://neon.com), create a project, and copy the
connection string. It looks like:

```
postgresql://user:password@ep-something.eu-central-1.aws.neon.tech/neondb?sslmode=require
```

The free tier is enough. The data is small.

### 2. Install

```bash
python3 -m pip install -r monitor/requirements.txt
```

### 3. Point the code at the database and create the tables

```bash
export DATABASE_URL='paste-your-connection-string-here'
```

```bash
python3 -m monitor.init_db
```

Expected output ends with a table listing and a cursor sitting at the deploy
block, meaning nothing has been ingested yet:

```
indexer stream : identity_registry_base
  start block  : 41453265
  last block   : 41453264  (0 blocks ingested)
```

To check the state later without changing anything:

```bash
python3 -m monitor.init_db --status
```

`init_db` only creates what is missing. Running it against a populated database
does not touch the data.
