#!/usr/bin/env python3
"""
ERC-8004 Identity Registry (Base) -> registered.csv

Contract : 0x8004A169FB4a3325136EB29fA0ceB6D2e539a432  (proxy, impl IdentityRegistryUpgradeable)
Event    : Registered(uint256 indexed agentId, string agentURI, address indexed owner)
topic0   : 0xca52e62c367d81bb2e328eb795f7c7ba24afb478408a26c0e201d155c449bc4a

Modes:
  --mode recent --count 300     scan backwards from tip until N events collected (fast)
  --mode full                   scan the whole history from deploy block (slow, ~1-2h on public RPC)

Options:
  --resolve-current             additionally eth_call tokenURI(agentId) at latest block,
                                because agentURI can be changed later via URIUpdated

Deps: httpx
Usage:
  python fetch_registered.py --mode recent --count 300 --resolve-current
  python fetch_registered.py --mode full --out registered_full.csv
"""

import argparse
import asyncio
import csv
import json
import sys
import time

import httpx

CONTRACT = "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432"
TOPIC_REGISTERED = "0xca52e62c367d81bb2e328eb795f7c7ba24afb478408a26c0e201d155c449bc4a"
DEPLOY_BLOCK = 41453265  # contract creation, 2026-01-29T14:11:17Z
SEL_TOKEN_URI = "0xc87b56dd"  # tokenURI(uint256)

DEFAULT_RPCS = [
    "https://mainnet.base.org",
    "https://base.llamarpc.com",
    "https://base-rpc.publicnode.com",
]


class Rpc:
    def __init__(self, urls, timeout=30.0):
        self.urls = list(urls)
        self.i = 0
        self.client = httpx.AsyncClient(timeout=timeout)
        self._id = 0

    async def call(self, method, params, retries=4):
        last = None
        for attempt in range(retries):
            url = self.urls[self.i % len(self.urls)]
            self._id += 1
            body = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
            try:
                r = await self.client.post(url, json=body)
                data = r.json()
                if "error" in data:
                    last = RuntimeError(f"{method}: {data['error']}")
                    # range-too-large / rate limit -> rotate node and let caller shrink range
                    self.i += 1
                    await asyncio.sleep(0.3 * (attempt + 1))
                    continue
                return data["result"]
            except Exception as e:  # noqa: BLE001
                last = e
                self.i += 1
                await asyncio.sleep(0.5 * (attempt + 1))
        raise last if last else RuntimeError("rpc failed")

    async def aclose(self):
        await self.client.aclose()


def decode_string_arg(data_hex):
    """Decode a single dynamic `string` from ABI-encoded log data."""
    try:
        raw = bytes.fromhex(data_hex[2:] if data_hex.startswith("0x") else data_hex)
        if len(raw) < 64:
            return ""
        offset = int.from_bytes(raw[0:32], "big")
        length = int.from_bytes(raw[offset:offset + 32], "big")
        return raw[offset + 32: offset + 32 + length].decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""


def parse_log(log):
    agent_id = int(log["topics"][1], 16)
    owner = "0x" + log["topics"][2][-40:]
    uri = decode_string_arg(log.get("data", "0x"))
    return {
        "agent_id": agent_id,
        "owner": owner,
        "agent_uri_at_registration": uri,
        "block_number": int(log["blockNumber"], 16),
        "tx_hash": log["transactionHash"],
        "log_index": int(log["logIndex"], 16),
    }


async def get_logs_range(rpc, from_b, to_b):
    """eth_getLogs with automatic range halving on provider limits."""
    stack = [(from_b, to_b)]
    out = []
    while stack:
        a, b = stack.pop()
        try:
            res = await rpc.call("eth_getLogs", [{
                "address": CONTRACT,
                "topics": [TOPIC_REGISTERED],
                "fromBlock": hex(a),
                "toBlock": hex(b),
            }], retries=2)
            out.extend(res)
        except Exception as e:  # noqa: BLE001
            if b - a < 32:
                print(f"  ! giving up on {a}-{b}: {e}", file=sys.stderr)
                continue
            mid = (a + b) // 2
            stack.append((mid + 1, b))
            stack.append((a, mid))
    return out


async def block_timestamps(rpc, blocks, concurrency=10):
    """Fetch timestamps for a set of block numbers."""
    sem = asyncio.Semaphore(concurrency)
    result = {}

    async def one(bn):
        async with sem:
            try:
                blk = await rpc.call("eth_getBlockByNumber", [hex(bn), False])
                result[bn] = int(blk["timestamp"], 16)
            except Exception:  # noqa: BLE001
                result[bn] = None

    await asyncio.gather(*(one(b) for b in blocks))
    return result


async def resolve_current_uris(rpc, agent_ids, concurrency=10):
    """eth_call tokenURI(agentId) at latest block."""
    sem = asyncio.Semaphore(concurrency)
    out = {}

    async def one(aid):
        async with sem:
            data = SEL_TOKEN_URI + hex(aid)[2:].rjust(64, "0")
            try:
                res = await rpc.call("eth_call", [{"to": CONTRACT, "data": data}, "latest"])
                out[aid] = decode_string_arg(res) if res and res != "0x" else ""
            except Exception:  # noqa: BLE001
                out[aid] = None

    await asyncio.gather(*(one(a) for a in agent_ids))
    return out


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["recent", "full"], default="recent")
    ap.add_argument("--count", type=int, default=300)
    ap.add_argument("--chunk", type=int, default=5000, help="blocks per eth_getLogs call")
    ap.add_argument("--out", default="registered.csv")
    ap.add_argument("--monthly-out", default="monthly.csv")
    ap.add_argument("--resolve-current", action="store_true")
    ap.add_argument("--rpc", action="append", default=None)
    args = ap.parse_args()

    rpc = Rpc(args.rpc or DEFAULT_RPCS)
    tip = int(await rpc.call("eth_blockNumber", []), 16)
    print(f"tip block: {tip}")

    logs = []
    t0 = time.time()

    if args.mode == "recent":
        hi = tip
        while hi > DEPLOY_BLOCK and len(logs) < args.count:
            lo = max(DEPLOY_BLOCK, hi - args.chunk + 1)
            batch = await get_logs_range(rpc, lo, hi)
            logs = batch + logs
            print(f"  {lo}-{hi}: +{len(batch)} (total {len(logs)})")
            hi = lo - 1
        logs = logs[-args.count:]
    else:
        lo = DEPLOY_BLOCK
        while lo <= tip:
            hi = min(tip, lo + args.chunk - 1)
            batch = await get_logs_range(rpc, lo, hi)
            logs.extend(batch)
            if batch:
                print(f"  {lo}-{hi}: +{len(batch)} (total {len(logs)}) "
                      f"[{(lo - DEPLOY_BLOCK) / max(1, tip - DEPLOY_BLOCK):.1%}]")
            lo = hi + 1

    rows = [parse_log(l) for l in logs]
    rows.sort(key=lambda r: (r["block_number"], r["log_index"]))
    print(f"parsed {len(rows)} Registered events in {time.time() - t0:.0f}s")

    ts = await block_timestamps(rpc, sorted({r["block_number"] for r in rows}))
    for r in rows:
        r["timestamp"] = ts.get(r["block_number"])
        r["datetime_utc"] = (
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(r["timestamp"]))
            if r["timestamp"] else ""
        )

    if args.resolve_current:
        cur = await resolve_current_uris(rpc, [r["agent_id"] for r in rows])
        for r in rows:
            r["agent_uri_current"] = cur.get(r["agent_id"])
            r["uri_changed_after_registration"] = (
                r["agent_uri_current"] is not None
                and r["agent_uri_current"] != r["agent_uri_at_registration"]
            )
    else:
        for r in rows:
            r["agent_uri_current"] = ""
            r["uri_changed_after_registration"] = ""

    cols = ["agent_id", "owner", "agent_uri_at_registration", "agent_uri_current",
            "uri_changed_after_registration", "block_number", "timestamp",
            "datetime_utc", "tx_hash", "log_index"]
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {args.out}")

    if rows:
        print(f"first: agentId {rows[0]['agent_id']} @ {rows[0]['datetime_utc']}")
        print(f"last : agentId {rows[-1]['agent_id']} @ {rows[-1]['datetime_utc']}")

    if args.mode == "full":
        monthly = {}
        for r in rows:
            if not r["timestamp"]:
                continue
            m = time.strftime("%Y-%m", time.gmtime(r["timestamp"]))
            monthly[m] = monthly.get(m, 0) + 1
        with open(args.monthly_out, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["month", "registered", "cumulative"])
            cum = 0
            for m in sorted(monthly):
                cum += monthly[m]
                w.writerow([m, monthly[m], cum])
        print(f"wrote {args.monthly_out}")
        for m in sorted(monthly):
            print(f"  {m}: {monthly[m]}")

    await rpc.aclose()


if __name__ == "__main__":
    asyncio.run(main())
