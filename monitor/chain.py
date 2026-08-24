#!/usr/bin/env python3
"""
Talking to Base: RPC pool, ABI decoding, and the event definitions.

Two problems this file exists to solve.

Public RPC endpoints rate-limit, disagree about how many blocks eth_getLogs
may cover, and go down individually. `RpcPool` rotates across endpoints,
backs off, and treats a rate-limit answer differently from a dead endpoint.
`get_logs` halves its block range whenever a provider refuses, so the caller
never has to know any provider's limit.

The event signatures below were confirmed against live chain data rather than
taken from the draft EIP text, which does not match what is deployed. Each
topic hash is recomputed from its signature at import time and checked against
the hash observed on chain, so a typo here fails loudly instead of silently
indexing nothing.

Deps: httpx
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
from typing import Any, Iterable, Sequence

import httpx

from .keccak import event_topic, function_selector

CHAIN_ID = 8453
CONTRACT = "0x8004a169fb4a3325136eb29fa0ceb6d2e539a432"
DEPLOY_BLOCK = 41453265

SEL_TOKEN_URI = function_selector("tokenURI(uint256)")   # 0xc87b56dd

# Public endpoints, used unless BASE_RPC_URLS says otherwise. Putting a paid
# key first in that variable is the whole upgrade path: no code change.
#
# Measured against this contract in August 2026, they are not interchangeable:
#
#   mainnet.base.org         eth_getLogs up to 10,000 blocks
#   base.drpc.org            eth_getLogs up to  5,000 blocks on the free plan
#   base-rpc.publicnode.com  historical log queries need an account token
#   1rpc.io/base             eth_getLogs capped at 50 blocks
#   base.llamarpc.com        returning HTTP 521, dropped from the list
#
# So only two of them can carry the indexer. The pool below keeps the weaker
# endpoints because they still serve eth_blockNumber and eth_call, and
# demotes them per method the first time they refuse.
DEFAULT_RPC_URLS = [
    "https://mainnet.base.org",
    "https://base.drpc.org",
    "https://base-rpc.publicnode.com",
    "https://1rpc.io/base",
]


def rpc_urls() -> list[str]:
    raw = os.environ.get("BASE_RPC_URLS", "").strip()
    if not raw:
        return list(DEFAULT_RPC_URLS)
    return [u.strip() for u in raw.split(",") if u.strip()]


# ---------------------------------------------------------------------------
# ABI decoding
#
# Only the four types this registry actually uses. A general ABI decoder would
# be longer than the rest of this module and would not be exercised.
# ---------------------------------------------------------------------------

class DecodeError(ValueError):
    pass


def _word(raw: bytes, index: int) -> int:
    start = index * 32
    if start + 32 > len(raw):
        raise DecodeError(f"data too short for word {index}")
    return int.from_bytes(raw[start:start + 32], "big")


def _dynamic_at(raw: bytes, offset: int) -> bytes:
    if offset + 32 > len(raw):
        raise DecodeError("dynamic offset past end of data")
    length = int.from_bytes(raw[offset:offset + 32], "big")
    end = offset + 32 + length
    if end > len(raw):
        raise DecodeError(f"dynamic field claims {length} bytes, data has "
                          f"{len(raw) - offset - 32}")
    return raw[offset + 32:end]


def decode_data(data_hex: str, types: Sequence[str]) -> list[Any]:
    """
    Decode the non-indexed arguments of a log.

    Supported types: uint256, address, string, bytes.
    Returns python ints, lowercase hex addresses, str, and bytes.
    """
    raw = bytes.fromhex(data_hex[2:] if data_hex.startswith("0x") else data_hex)
    out: list[Any] = []
    for i, typ in enumerate(types):
        if typ == "uint256":
            out.append(_word(raw, i))
        elif typ == "address":
            out.append("0x" + f"{_word(raw, i):040x}"[-40:])
        elif typ in ("string", "bytes"):
            payload = _dynamic_at(raw, _word(raw, i))
            out.append(payload.decode("utf-8", errors="replace")
                       if typ == "string" else payload)
        else:  # pragma: no cover - guarded by the definitions below
            raise DecodeError(f"unsupported type {typ}")
    return out


def topic_to_address(topic: str) -> str:
    return "0x" + topic[-40:].lower()


def topic_to_int(topic: str) -> int:
    return int(topic, 16)


# ---------------------------------------------------------------------------
# Events
#
# `observed_topic` is the topic0 seen in the contract's logs on Base. It is
# compared against the hash computed from `signature` at import time.
# ---------------------------------------------------------------------------

class EventDef:
    def __init__(self, name: str, signature: str, observed_topic: str,
                 indexed: Sequence[str], data_types: Sequence[str],
                 note: str = ""):
        self.name = name
        self.signature = signature
        self.indexed = list(indexed)
        self.data_types = list(data_types)
        self.note = note
        self.topic0 = event_topic(signature)
        if self.topic0 != observed_topic:
            raise RuntimeError(
                f"{name}: signature {signature!r} hashes to {self.topic0}, "
                f"but the chain emits {observed_topic}. One of the two is wrong."
            )


EVENTS: dict[str, EventDef] = {}


def _define(*args, **kwargs) -> EventDef:
    ev = EventDef(*args, **kwargs)
    EVENTS[ev.topic0] = ev
    return ev


REGISTERED = _define(
    "Registered", "Registered(uint256,string,address)",
    "0xca52e62c367d81bb2e328eb795f7c7ba24afb478408a26c0e201d155c449bc4a",
    indexed=["agentId", "owner"], data_types=["string"],
    note="agentURI at mint time; may be empty and set later",
)

URI_UPDATED = _define(
    "URIUpdated", "URIUpdated(uint256,string,address)",
    "0x3a2c7fffc2cba7582c690e3b82c453ea02a308326a98a3ad7576c606336409fb",
    indexed=["agentId", "owner"], data_types=["string"],
    note="the event that makes current_uri differ from the registration URI",
)

TRANSFER = _define(
    "Transfer", "Transfer(address,address,uint256)",
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
    indexed=["from", "to", "agentId"], data_types=[],
    note="ERC-721. from == 0x0 is a mint, paired with Registered.",
)

METADATA_SET = _define(
    "MetadataSet", "MetadataSet(uint256,string,string,bytes)",
    "0x2c149ed548c6d2993cd73efe187df6eccabe4538091b33adbd25fafdb8a1468b",
    indexed=["agentId", "keyHash"], data_types=["string", "bytes"],
    note="key/value. 'agentWallet' dominates: an address bound to the agent.",
)

# Deliberately not indexed: MetadataUpdate(uint256), topic0
# 0xf8e1a15aba9398e019f0b49df1a4fde98ee17ae345cb5f6b5e2c27f5033e8ce7.
# It is the ERC-4906 "refresh your cache" notification. It fires alongside the
# events above and carries only an agentId those events already give us, so
# storing it would add tens of thousands of rows and no information.
IGNORED_TOPICS = {
    "0xf8e1a15aba9398e019f0b49df1a4fde98ee17ae345cb5f6b5e2c27f5033e8ce7":
        "MetadataUpdate(uint256)",
}

ALL_TOPICS = list(EVENTS.keys())


def decode_log(log: dict) -> dict:
    """
    Turn a raw log into a flat row.

    Never raises on bad data: a log that cannot be decoded comes back with
    event_type 'unknown' and its raw payload intact, so it lands in the
    database and can be looked at rather than being silently dropped.
    """
    topics = log.get("topics") or []
    topic0 = (topics[0] if topics else "").lower()
    row: dict[str, Any] = {
        "event_type": "unknown",
        "agent_id": None,
        "owner": None,
        "from_address": None,
        "agent_uri": None,
        "metadata_key": None,
        "metadata_value": None,
        "block_number": int(log["blockNumber"], 16),
        "tx_hash": log["transactionHash"].lower(),
        "log_index": int(log["logIndex"], 16),
        "topic0": topic0,
        "raw_topics": json.dumps(topics),
        "raw_data": log.get("data", "0x"),
        "decode_error": None,
    }

    ev = EVENTS.get(topic0)
    if ev is None:
        row["decode_error"] = "unknown topic0"
        return row

    try:
        row["event_type"] = ev.name
        if ev is TRANSFER:
            row["from_address"] = topic_to_address(topics[1])
            row["owner"] = topic_to_address(topics[2])
            row["agent_id"] = topic_to_int(topics[3])
        elif ev in (REGISTERED, URI_UPDATED):
            row["agent_id"] = topic_to_int(topics[1])
            row["owner"] = topic_to_address(topics[2])
            (row["agent_uri"],) = decode_data(log.get("data", "0x"), ev.data_types)
        elif ev is METADATA_SET:
            row["agent_id"] = topic_to_int(topics[1])
            key, value = decode_data(log.get("data", "0x"), ev.data_types)
            row["metadata_key"] = key
            # Values are usually raw bytes (an address), not text. Store hex
            # unless the bytes are plainly printable.
            try:
                text = value.decode("utf-8")
                printable = text.isprintable() and text != ""
            except UnicodeDecodeError:
                printable = False
            row["metadata_value"] = text if printable else "0x" + value.hex()
    except Exception as exc:  # noqa: BLE001 - a bad log must not stop the run
        row["event_type"] = "unknown"
        row["decode_error"] = f"{type(exc).__name__}: {exc}"

    return row


# ---------------------------------------------------------------------------
# RPC
# ---------------------------------------------------------------------------

class RpcError(RuntimeError):
    pass


class RpcRangeError(RpcError):
    """Every endpoint refused the block range. The caller should ask for less."""


class RpcPool:
    """
    A rotating pool of JSON-RPC endpoints.

    Three kinds of failure, handled differently, because retrying them the
    same way wastes the run:

      Transport errors and rate limits are temporary. Rotate, back off, retry.
      A rate limit gets a longer sleep than a dropped connection, since
      hammering a throttled endpoint is what produced the throttling.

      Refusing a block range is not a failure of the endpoint, it is a
      statement about the request. Rotate immediately with no sleep, and once
      every endpoint has refused, tell the caller to halve the range.

      Refusing a method outright ("archive requests require a token",
      "upgrade to a paid plan") will not change during this run. That endpoint
      is dropped for that method and keeps serving the others.
    """

    RATE_LIMIT_MARKERS = ("rate limit", "too many requests", "429",
                          "throttl", "capacity", "exceeded quota")
    RANGE_MARKERS = ("limited to", "block range", "range is too", "too large",
                     "query returned more than", "response size", "too many results",
                     "not supported on free plan", "request timeout on the free plan")
    CAPABILITY_MARKERS = ("personal token", "upgrade to a paid", "requires a paid",
                          "method not found", "unsupported method", "not available on")

    def __init__(self, urls: Iterable[str] | None = None, timeout: float = 30.0):
        self.urls = list(urls) if urls else rpc_urls()
        if not self.urls:
            raise RpcError("no RPC endpoints configured")
        self._next = 0
        self._id = 0
        self._retired: dict[tuple[str, str], str] = {}
        self.stats = {"calls": 0, "retries": 0, "rate_limited": 0,
                      "failures": 0, "retired": 0}
        self.client = httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": "erc8004-registry-monitor/1.0",
                     "Content-Type": "application/json"},
        )

    def available(self, method: str) -> list[str]:
        live = [u for u in self.urls if (u, method) not in self._retired]
        # If everything has been retired for a method, the diagnosis was
        # probably wrong. Better to try them all again than to stop.
        return live or list(self.urls)

    def _rotate(self, method: str) -> str:
        options = self.available(method)
        url = options[self._next % len(options)]
        self._next += 1
        return url

    def _retire(self, url: str, method: str, reason: str) -> None:
        if (url, method) not in self._retired:
            self._retired[(url, method)] = reason
            self.stats["retired"] += 1
            print(f"    ! {url} cannot serve {method}: {reason[:110]}")

    @staticmethod
    def _matches(text: str, markers: tuple[str, ...]) -> bool:
        low = text.lower()
        return any(m in low for m in markers)

    async def call(self, method: str, params: list, attempts: int = 5) -> Any:
        last = "no attempt made"
        range_refusals = 0
        endpoint_count = len(self.available(method))

        for attempt in range(attempts):
            url = self._rotate(method)
            self._id += 1
            self.stats["calls"] += 1
            if attempt:
                self.stats["retries"] += 1
            body = {"jsonrpc": "2.0", "id": self._id,
                    "method": method, "params": params}
            try:
                resp = await self.client.post(url, json=body)

                if resp.status_code == 429:
                    last = f"HTTP 429 from {url}"
                    self.stats["rate_limited"] += 1
                    await self._sleep(attempt, rate_limited=True)
                    continue
                if resp.status_code in (401, 403, 404, 405):
                    # An endpoint that answers eth_blockNumber but returns 403
                    # for eth_getLogs is refusing the method, not failing at
                    # it. Retrying costs attempts that a working endpoint
                    # needs, so drop it for this method and move on.
                    last = f"HTTP {resp.status_code} from {url}"
                    self._retire(url, method, f"HTTP {resp.status_code}")
                    continue
                if resp.status_code >= 400:
                    last = f"HTTP {resp.status_code} from {url}"
                    await self._sleep(attempt)
                    continue

                try:
                    payload = resp.json()
                except ValueError:
                    last = f"non-JSON reply from {url}: {resp.text[:80]}"
                    await self._sleep(attempt)
                    continue

                if "error" in payload:
                    message = str(payload["error"])
                    last = f"{method} at {url}: {message[:200]}"
                    if self._matches(message, self.CAPABILITY_MARKERS):
                        self._retire(url, method, message)
                        continue                       # no sleep, try another
                    if self._matches(message, self.RANGE_MARKERS):
                        range_refusals += 1
                        if range_refusals >= endpoint_count:
                            raise RpcRangeError(last)
                        continue                       # no sleep, try another
                    limited = self._matches(message, self.RATE_LIMIT_MARKERS)
                    if limited:
                        self.stats["rate_limited"] += 1
                    await self._sleep(attempt, rate_limited=limited)
                    continue

                return payload.get("result")

            except RpcRangeError:
                raise
            except Exception as exc:  # noqa: BLE001 - transport errors are expected
                last = f"{type(exc).__name__} at {url}: {str(exc)[:140]}"
                await self._sleep(attempt)

        self.stats["failures"] += 1
        if range_refusals:
            raise RpcRangeError(f"{method}: range refused; last was {last}")
        raise RpcError(f"{method} failed after {attempts} attempts: {last}")

    async def _sleep(self, attempt: int, rate_limited: bool = False) -> None:
        base = 2.0 if rate_limited else 0.5
        delay = min(30.0, base * (2 ** attempt)) + random.uniform(0, 0.4)
        await asyncio.sleep(delay)

    async def block_number(self) -> int:
        return int(await self.call("eth_blockNumber", []), 16)

    async def aclose(self) -> None:
        await self.client.aclose()


async def get_logs(pool: RpcPool, from_block: int, to_block: int,
                   topics: Sequence[str] | None = None,
                   min_span: int = 8, on_progress=None
                   ) -> tuple[list[dict], int | None]:
    """
    eth_getLogs over a block range, halving the range whenever a provider
    refuses it.

    Providers cap either the number of blocks or the number of results, and
    the caps differ between them, so the range that works is discovered rather
    than configured. A range that fails even at `min_span` blocks is reported
    and skipped: one unreadable window must not stall the whole backfill.
    """
    topic_filter = [list(topics)] if topics else None
    # Enough attempts to reach every endpoint at least once, plus room for one
    # transient failure. With fewer, a run can exhaust its attempts on the
    # endpoints that refuse the method and never reach the ones that serve it.
    attempts = max(3, len(pool.urls) + 2)
    pending = [(from_block, to_block)]
    out: list[dict] = []
    skipped: list[tuple[int, int]] = []

    while pending:
        lo, hi = pending.pop()
        params: dict[str, Any] = {
            "address": CONTRACT,
            "fromBlock": hex(lo),
            "toBlock": hex(hi),
        }
        if topic_filter:
            params["topics"] = topic_filter
        try:
            result = await pool.call("eth_getLogs", [params], attempts=attempts)
            out.extend(result or [])
            if on_progress:
                on_progress(lo, hi, len(result or []))
        except RpcError as exc:
            if hi - lo < min_span:
                skipped.append((lo, hi))
                print(f"    ! unreadable range {lo}-{hi}: {exc}")
                continue
            mid = (lo + hi) // 2
            pending.append((mid + 1, hi))
            pending.append((lo, mid))

    if skipped:
        print(f"    ! {len(skipped)} range(s) skipped, cursor will not advance "
              f"past the first of them")
    out.sort(key=lambda l: (int(l["blockNumber"], 16), int(l["logIndex"], 16)))
    return out, (min(lo for lo, _ in skipped) if skipped else None)


async def block_times(pool: RpcPool, blocks: Sequence[int],
                      concurrency: int = 8) -> dict[int, int]:
    """Block number -> unix timestamp, fetched concurrently."""
    sem = asyncio.Semaphore(concurrency)
    result: dict[int, int] = {}

    async def one(number: int) -> None:
        async with sem:
            try:
                block = await pool.call("eth_getBlockByNumber", [hex(number), False])
                if block and block.get("timestamp"):
                    result[number] = int(block["timestamp"], 16)
            except RpcError as exc:
                print(f"    ! block {number} timestamp unavailable: {exc}")

    await asyncio.gather(*(one(b) for b in blocks))
    return result


# ---------------------------------------------------------------------------
# Block timestamps
# ---------------------------------------------------------------------------

class BlockClock:
    """
    Block number to wall-clock time, without an RPC call per block.

    Base produces a block every two seconds. Measured across the 8.9 million
    blocks between the registry deployment and August 2026, the arithmetic
    prediction matches the chain exactly, with zero drift at every sampled
    point. Asking the node for each block's timestamp instead would cost
    roughly a hundred thousand extra calls on a full backfill.

    An assumption this convenient has to be checked rather than trusted, so
    the indexer verifies one real block per chunk and falls back to fetching
    true timestamps for that chunk if the prediction is ever wrong.
    """

    ANCHOR_BLOCK = 41453265        # registry deployment
    ANCHOR_TIME = 1769695877       # its block timestamp, read from chain
    SECONDS_PER_BLOCK = 2

    def __init__(self) -> None:
        self.verifications = 0
        self.max_drift_seconds = 0

    def predict(self, block_number: int) -> int:
        return (self.ANCHOR_TIME
                + (block_number - self.ANCHOR_BLOCK) * self.SECONDS_PER_BLOCK)

    async def verify(self, pool: "RpcPool", block_number: int) -> int:
        """Return drift in seconds: actual minus predicted. 0 means exact."""
        block = await pool.call("eth_getBlockByNumber", [hex(block_number), False])
        if not block or not block.get("timestamp"):
            raise RpcError(f"block {block_number} has no timestamp")
        actual = int(block["timestamp"], 16)
        drift = actual - self.predict(block_number)
        self.verifications += 1
        self.max_drift_seconds = max(self.max_drift_seconds, abs(drift))
        return drift


async def token_uri(pool: RpcPool, agent_id: int) -> str | None:
    """Current tokenURI(agentId). None means the call could not be made."""
    data = SEL_TOKEN_URI + f"{agent_id:064x}"
    try:
        result = await pool.call("eth_call", [{"to": CONTRACT, "data": data}, "latest"])
    except RpcError:
        return None
    if not result or result == "0x":
        return ""
    try:
        (value,) = decode_data(result, ["string"])
        return value
    except DecodeError:
        return ""


if __name__ == "__main__":
    print("event definitions (topic hash recomputed from signature):\n")
    for topic, ev in EVENTS.items():
        print(f"  {ev.name:<13} {topic}")
        print(f"  {'':<13} {ev.signature}")
        print(f"  {'':<13} indexed: {', '.join(ev.indexed) or 'none'}"
              f" | data: {', '.join(ev.data_types) or 'none'}")
        if ev.note:
            print(f"  {'':<13} {ev.note}")
        print()
    for topic, sig in IGNORED_TOPICS.items():
        print(f"  {'(ignored)':<13} {topic}\n  {'':<13} {sig}\n")
    print(f"RPC endpoints: {len(rpc_urls())}")
    for u in rpc_urls():
        print(f"  {u}")
