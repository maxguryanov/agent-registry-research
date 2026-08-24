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

**Status: modules 1 to 4 (schema, indexer, prober, metrics) are in place. Publishing is still to come.**

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


---

## The panel

```bash
python3 -m monitor.panel --target 2000    # size and select
python3 -m monitor.panel --status         # what is in it
```

Probing all 64,000 agents daily would mean 64,000 requests a day to other
people's servers, about three hours per run, and 23 million rows a year. It
would also buy almost nothing: the 2026 report measured 1,204 agents and got a
95% interval of ±1.4 points; 2,000 narrows that to ±1.1. A full census removes
the remaining 1.1 points at thirty times the cost.

Members are chosen by a hash of the agent id. That makes the selection
reproducible by anyone holding the ids, stable across runs, and automatically
representative as the registry grows. Membership is only ever added: an agent
dropped from the panel would leave a hole in every survival curve that already
counted it.

For a one-off census, `python3 -m monitor.prober --all` records a run of kind
`sweep` alongside the daily panel runs.

---

## The prober

```bash
python3 -m monitor.prober --contact you@example.com
python3 -m monitor.prober --limit 20 --dry-run     # try it, write nothing
python3 -m monitor.prober --resume 12              # continue an interrupted run
```

One row per agent per run, in `liveness_checks`, never overwritten.

### It checks the current URI

Liveness is measured against `agents.current_uri`, which the indexer derives
from the most recent `URIUpdated` event. The registration URI is stored too,
in `uri_at_registration`, but it is not what gets probed. Agents that mint with
an empty URI and set it afterwards are common enough to move the result by
about a third.

### Failure categories are recorded, not collapsed

Every check stores which stage it died at, a failure category, an HTTP status
and a latency, separately from the six pass/fail flags. Raising the timeout
does not remove failures, it relabels them: in the 2026 report, going from 10s
to 30s cut `ReadTimeout` from 55 to 3 and raised HTTP 504 from 0 to 39. The
agents were unchanged. `check_runs` therefore stores the concurrency, timeout
and prober version of each run, because two runs are only comparable if you can
see how each was configured.

Categories distinguish `timeout_connect`, `timeout_read`, `dns_failure`,
`tls_failure`, `http_404_not_found`, `http_504_gateway_timeout`, `not_json`,
`schema_mismatch`, `no_services`, `all_endpoints_dead`, `generic_hosts_only`,
`robots_disallowed` and `excluded_by_request`, among others.

### Strict liveness

An agent is counted live only if a responding endpoint is on a host that is not
generic. `github.com`, `facebook.com`, public IPFS gateways and the EIP
specification site all answer HTTP 200 for anyone; a response from them says
nothing about the agent. In the 2026 report this distinction moved the headline
figure from 7.3% to 6.8%.

Deliberately not treated as generic: `github.io`, `vercel.app`, `netlify.app`
and similar, where the subdomain belongs to one project and a response really
is about that project.

### Politeness is four things, not one

| Control | Default |
|---|---|
| overall concurrency | 8, hard-capped at 10 |
| requests to one host | one at a time |
| gap between requests to the same host | 0.5s |
| `robots.txt` | fetched once per host, respected |

Global concurrency alone is not enough, because agents cluster onto shared
hosts and IPFS gateways: a run limited only by a semaphore can still aim all
of its capacity at a single server.

A `robots.txt` refusal is recorded as `robots_disallowed`, not as a dead agent.
It is a fact about our access, not about the agent, and the two must not be
added together.

### The opt-out is a mechanism, not a promise

The contact address in the `User-Agent` is only worth something if a request to
stop can be acted on:

```sql
INSERT INTO excluded_hosts (host, reason, requested_by)
VALUES ('example.com', 'operator asked by email', 'ops@example.com');
```

Matching covers subdomains. Affected agents are still recorded, with
`excluded_by_request`, so they can be told apart from agents that are dead.

### Cost

Measured on 63 agents: 142 seconds. A 2,000-agent panel takes roughly 75 to 90
minutes, which fits comfortably in a daily scheduled job.


---

## Metrics

```bash
python3 -m monitor.metrics                  # a report on the terminal
python3 -m monitor.metrics --json out.json  # the aggregate document
```

Read-only. It writes nothing to the database.

### Three denominators, not one

The same run produces three different answers, and the gap between them is the
finding, not a rounding error. On a test slice of 63 recently registered
agents:

| Counted by | live | of | rate |
|---|---:|---:|---:|
| agent | 20 | 63 | 31.8% |
| owner | 1 | 40 | 2.5% |
| project | 1 | 6 | 16.7% |

Every one of the twenty live agents belonged to a single operator. Counted per
agent, a third of the registry looks alive. Counted per participant, one
participant is.

A project is a group of agents connected by a shared owner address or a shared
root domain, transitively. This is a lower bound on independence: an operator
using separate wallets *and* separate domains still counts as several projects.

### Censored observations are not deaths

An agent we chose not to probe — `robots_disallowed`, `excluded_by_request` —
is reported separately and kept out of every denominator. It is a fact about
our access, not about the agent, and adding the two together would make the
registry look worse each time someone asks us to leave them alone.

### Every proportion carries a Wilson interval

These proportions sit near zero, where the textbook normal interval starts
returning negative lower bounds. Wilson does not.

### Survival, and what to show before it exists

Survival is the share of agents observed alive at some time T that are still
alive at T+30 and T+90. For each agent the anchor is its earliest strictly-live
observation; the later reading is the check closest to the horizon, accepted
only within a seven-day window. An agent with no qualifying later check has no
verdict yet and is counted as not-yet-evaluable, never as dead. Only panel runs
are used, so the sampling schedule cannot shape the curve.

None of this can produce a number on day one: thirty days of survival needs
thirty days of measurements. Until then the output reports
`insufficient_history` and says how many days remain, rather than printing a
zero that would read as "nobody survived".

Meanwhile there is a figure available immediately — liveness by registration
month, measured in a single run. It is labelled cross-sectional because that
is what it is: different agents of different ages compared at one moment,
with any change in who registers over time mixed into it. It is a useful
proxy for decay, and it is not survival.

The survival arithmetic was verified against synthetic history with a known
answer: 50 agents alive at the anchor, 30 alive at day 30, 20 at day 90,
with decoy checks at days 27 and 33 to confirm the horizon picks the closest
reading, and 50 never-live agents to confirm they stay out of the denominator.
