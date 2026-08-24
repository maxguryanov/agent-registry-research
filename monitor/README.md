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

**Status: modules 1 and 2 (schema, indexer) are in place. Prober, metrics and publishing are still to come.**

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


---

## The indexer

```bash
python3 -m monitor.indexer            # catch up to the chain tip
python3 -m monitor.indexer --status   # report only, change nothing
```

It reads the cursor from `indexer_state`, asks Base for logs since then, stops
50 blocks short of the chain tip, and writes events, agent state and the new
cursor in one transaction per chunk. Interrupting it loses nothing: the next
run resumes from the last committed chunk.

### First run: loading the history

The registry has around 64,000 registrations spread over 8.9 million blocks.
Loading them takes roughly 50 minutes, and is best done in steps so that a
failure costs one step rather than the whole thing:

```bash
python3 -m monitor.indexer --max-seconds 900
```

Run that repeatedly until `--status` reports the cursor close to the tip. Each
call picks up where the last one stopped. After that, the hourly job has only
about 1,800 new blocks to fetch and finishes in seconds.

### What it indexes

The contract emits five kinds of event. Their signatures were read off the
chain and confirmed against a public signature database, because the draft EIP
text does not match what is deployed.

| Event | Kept | Why |
|---|---|---|
| `Registered(uint256,string,address)` | yes | the registration, and the URI at mint time |
| `URIUpdated(uint256,string,address)` | yes | the reason current and registration URIs differ |
| `Transfer(address,address,uint256)` | yes | ownership, needed for the owner-level funnel |
| `MetadataSet(uint256,string,string,bytes)` | yes | key/value; `agentWallet` dominates |
| `MetadataUpdate(uint256)` | no | ERC-4906 cache hint, carries nothing new |

Anything with an unrecognised signature is stored with `event_type = 'unknown'`
and its raw payload, rather than dropped.

### Why `agents` is rebuilt rather than updated

After writing a chunk of events, the indexer recomputes the affected agents
from the event table with a single statement. Incrementing counters in place
would be faster and would produce wrong numbers the moment a range is
processed twice. Recomputing from stored events gives the same answer however
many times it runs.

Verified by indexing a 6,000-block range twice: 257 events both times, zero
inserted on the second pass, and a byte-identical fingerprint of the resulting
agent state.

### Public RPC endpoints are not interchangeable

Measured against this contract in August 2026:

| Endpoint | `eth_getLogs` |
|---|---|
| `mainnet.base.org` | up to 10,000 blocks |
| `base.drpc.org` | up to 5,000 blocks on the free plan |
| `base-rpc.publicnode.com` | HTTP 403 — historical queries need an account token |
| `1rpc.io/base` | capped at 50 blocks |
| `base.llamarpc.com` | HTTP 521, dropped from the default list |

Only two of the five can carry the indexer. The pool handles this by itself:
an endpoint that refuses a method is dropped for that method and keeps serving
the others, an endpoint that refuses a block range causes the range to be
halved rather than the endpoint to be blamed, and rate limits get a longer
backoff than dropped connections. Tested with a pool where three of four
endpoints were broken; the range still indexed.

To use your own endpoint, put it first:

```bash
export BASE_RPC_URLS='https://base-mainnet.g.alchemy.com/v2/YOUR_KEY,https://mainnet.base.org'
```

No code change is needed. A free Alchemy or QuickNode key is worth having for
the initial history load; the hourly job runs fine on the public endpoints.

### Block timestamps are computed, not fetched

Base produces a block every two seconds. Checked across the 8.9 million blocks
from deployment to August 2026, the arithmetic prediction matches the chain
exactly at every sampled point. Asking the node for each block's timestamp
would add roughly a hundred thousand calls to the history load.

The indexer still verifies one real block per chunk and falls back to fetching
true timestamps if the prediction is ever wrong. Every run reports the largest
drift it saw, so the assumption cannot quietly rot.
