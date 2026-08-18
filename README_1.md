# agent-registry-research

Measuring how many agents registered in the ERC-8004 Identity Registry on Base actually expose a working service endpoint.

This repository contains the collection and measurement code behind **The State of Agent Registries 2026**. Everything here runs against public Base RPC endpoints. No API keys, no indexers, no third-party data providers, so the results are reproducible from chain state alone.

---

## Headline result

Complete event log of the ERC-8004 Identity Registry on Base, from deployment on 3 February 2026 through 16 August 2026: **63,832 registrations**.

Even sample of **1,204 agents** across that history, tested for liveness:

| Stage | n | % of total |
|---|---:|---:|
| Registered | 1,204 | 100.0 |
| Non-empty current URI | 760 | 63.1 |
| URI resolves | 646 | 53.7 |
| Valid JSON | 600 | 49.8 |
| Matches ERC-8004 schema | 341 | 28.3 |
| Declares services | 215 | 17.9 |
| **Endpoint responds (strict)** | **82** | **6.8** |

95% Wilson CI: 5.5–8.4%.

Two further figures: **10.2%** of unique owner addresses have at least one working agent, and of **371** declared service endpoints, **122** respond (32.9%).

Full report: forthcoming.

---

## Contracts

| Registry | Address | Chain |
|---|---|---|
| Identity | `0x8004A169FB4a3325136EB29fA0ceB6D2e539a432` | Base (8453) |
| Reputation | `0x8004BAa17C55a88189AE136b182e5fdA19dE9b63` | Base (8453) |

Deployment block on Base: 41453265. Measurement window ends at block 50089563.

`Registered` event signature used for log filtering:

```
Registered(uint256 indexed agentId, string agentURI, address indexed owner)
topic0 = 0xca52e62c367d81bb2e328eb795f7c7ba24afb478408a26c0e201d155c449bc4a
```

Note this differs from the draft EIP text. Verify against the revision you are measuring.

---

## Requirements

```
python3 -m pip install httpx
```

Python 3.9 or later. No other dependencies.

---

## Pipeline

Run in this order.

### 1. Collect registration events

```bash
python3 fetch_registered.py --mode full
```

Walks the full block range with `eth_getLogs`, splitting the range automatically when a provider refuses a request. Writes `registered.csv` and `monthly.csv`.

Roughly 16 minutes for the full history. Use `--mode recent --count 300` for a quick check.

### 2. Draw an even sample

```bash
head -1 registered.csv > sample.csv
awk 'NR>1 && (NR-1)%53==0' registered.csv >> sample.csv
```

Every 53rd agent, giving about 1,200 observations spread evenly across the history.

Do not sample the most recent N agents. Registration activity arrived in bursts, and the tail of the window is one burst rather than a cross-section.

### 3. Resolve current URIs

```bash
python3 resolve_current.py --in sample.csv --out sample_resolved.csv --concurrency 4
```

**This step is not optional and it changes the answer.** An agent's `agentURI` may be empty at mint and set later via `setAgentURI`. Measured by registration event, 43.9% of sampled agents have no URI; measured by current `tokenURI(agentId)`, 32.3% do. 16.5% of agents change their URI after registration. Reading only the event log overstates the dead population by roughly a third.

Use `--concurrency 4`. Higher values trigger rate limits on public RPCs: at concurrency 12, 256 of 1,204 calls failed to resolve; at concurrency 4, 81 failed.

### 4. Test liveness

```bash
python3 check_liveness.py \
  --in sample_resolved.csv \
  --out liveness.csv \
  --uri-column agent_uri_current \
  --contact you@example.com \
  --concurrency 5 \
  --timeout 30
```

`--uri-column agent_uri_current` matters. Without it the script reads the registration-time URI and step 3 is wasted.

`--contact` goes into the User-Agent header. Set a real address. You are contacting other people's servers.

### 5. Aggregate

```bash
python3 funnel.py liveness.csv
```

---

## Two things to check before trusting your output

### The canonical `type` value

`CANONICAL_TYPE_VALUES` in `check_liveness.py` is a candidate list, not gospel. In the data we collected, the only canonical value that appears is:

```
https://eips.ethereum.org/EIPS/eip-8004#registration-v1
```

present in 341 of 600 valid JSON documents. Note the `-v1` suffix and the uppercase `EIPS` path segment. Comparison is case-insensitive in the script, but the suffix must be in the list or you will undercount schema conformance to zero.

Verify against the specification revision you are measuring. The raw `type` value is written to the output CSV regardless, so you can always re-derive conformance after the fact.

The `registrations[].agentRegistry` field is the more robust conformance criterion, and the script accepts either. It is present in only 106 of 600 valid documents, so it cannot be the sole test.

### Generic hosts inflate liveness

Our first run counted 88 live agents. Six of them qualified only because they declared `github.com` or `facebook.com` as a service endpoint. Three declared OASF pointing at the specification repository, two declared MCP, one A2A. The host returned HTTP 200 and a naive check scored the agent as working.

Filter responding endpoints whose host is a code repository, social network, or public IPFS gateway. This moves the result from 7.3% to **6.8%**.

More generally: a responding endpoint is not a functioning agent. This pipeline does not invoke declared capabilities, complete MCP handshakes, or exercise A2A task lifecycles. **The figures it produces are an upper bound.**

---

## Reproducibility notes

**Run it twice with different settings.** We ran the liveness test at concurrency 15 / timeout 10s and at concurrency 5 / timeout 30s. The terminal figure was identical (82 strictly-live agents), but intermediate stages moved by up to 14 observations and network failures were reclassified rather than eliminated: `ReadTimeout` fell from 55 to 3 while HTTP 504 rose from 0 to 39. With a longer timeout, IPFS gateways return their own error instead of failing to answer.

Failures stable across both runs are server-confirmed absences: HTTP 404 (27), unsupported scheme (18), HTTP 500 (11).

**Both runs originated from the same network.** Blocking by IP is not distinguished from unavailability. Four failures (two HTTP 403, one 429, one 530) may reflect bot protection. Re-running from a different network is worthwhile.

**Ownership is measured by token holder address.** One operator may control many addresses, so concentration is more likely understated than overstated. Separately, eleven agents on eleven distinct wallets pointing at a single domain look like eleven participants when counted by owner and like one project when counted by service. Owner-level figures are themselves an upper bound on independent participation.

---

## What is not in this repository

Per-agent records and owner addresses are not published. The purpose of this work is to characterise the state of a registry, not to attribute behaviour to identifiable parties.

Aggregate data (per-stage counts, monthly registrations, ownership distribution, domain distribution) is published alongside the report under CC BY 4.0.

---

## Prior work

Xiong, Li, Wei, Wang, Knottenbelt, Wang. *Can Trustless Agents Be Trusted? An Empirical Study of the ERC-8004 Decentralized AI Agent Ecosystem.* arXiv:2606.26028, July 2026.

Their study covers Ethereum, BSC, and Base through 13 May 2026, including the Reputation Registry, which this work does not. Our ownership-concentration result on Base (Gini 0.701, measured in August on the full event log) independently reproduces theirs (0.708, measured in May by a different collection method).

---

## Corrections

Methodological challenges are welcome. Open an issue. Corrections will be published alongside the next edition of the report.

## License

MIT. See `LICENSE`.
