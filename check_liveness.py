#!/usr/bin/env python3
"""
ERC-8004 agent liveness checker.

Input : registered.csv (from fetch_registered.py) or a plain text file with one URI per line
Output: liveness.csv  -- one row per agent, every intermediate stage as its own column
        raw/<agent_id>.json (optional) -- fetched documents, for manual inspection

No database. Files only. One broken URI never kills the run.

Deps: httpx
Usage:
  python check_liveness.py --in registered.csv --out liveness.csv \
      --contact "max@example.com" --concurrency 20 --timeout 10
  python check_liveness.py --in registered.csv --resume        # skip already-done agents
"""

import argparse
import asyncio
import base64
import csv
import json
import os
import sys
import time
import urllib.parse

import httpx

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

REGISTRY_CAIP = "eip155:8453:0x8004a169fb4a3325136eb29fa0ceb6d2e539a432"

# Values of the JSON "type" field that we count as an ERC-8004 registration
# document. Observed docs are A2A-style AgentCards; verify against the spec
# revision you are measuring and edit this list rather than trusting defaults.
CANONICAL_TYPE_VALUES = {
    "https://eips.ethereum.org/eips/eip-8004#registration",
    "https://eips.ethereum.org/eips/eip-8004#registration-v1",
    "https://eips.ethereum.org/eips/eip-8004",
    "erc8004:registration",
    "agentcard",
    "https://schema.org/profilepage",
}

IPFS_GATEWAYS = [
    "https://ipfs.io/ipfs/{cid}",
    "https://cloudflare-ipfs.com/ipfs/{cid}",
    "https://dweb.link/ipfs/{cid}",
]

# Keys under which a service list may live, in priority order
SERVICE_LIST_KEYS = ["services", "service", "endpoints", "skills", "capabilities"]
# Keys inside a service object that may hold the callable URL
ENDPOINT_URL_KEYS = ["serviceEndpoint", "endpoint", "url", "uri", "href", "address"]
# Keys inside a service object that may hold its declared type
ENDPOINT_TYPE_KEYS = ["type", "serviceType", "name", "id", "protocol"]

MAX_ENDPOINTS_PER_AGENT = 5
MAX_DOC_BYTES = 2_000_000

COLUMNS = [
    "agent_id", "owner", "uri_raw", "uri_source",
    "s1_uri_present", "uri_scheme", "resolved_url",
    "s2_resolved", "http_status", "latency_ms", "content_type", "content_bytes",
    "fetch_error",
    "s3_valid_json", "json_error",
    "type_field_raw", "s4_schema_match", "registry_field_present", "registry_matches",
    "s5_has_services", "services_count", "service_types",
    "endpoints_total", "endpoints_checked", "endpoints_ok",
    "s6_any_endpoint_alive", "endpoint_details",
    "funnel_stage", "checked_at",
]


# ---------------------------------------------------------------------------
# URI handling
# ---------------------------------------------------------------------------

def classify_uri(uri):
    if uri is None:
        return "", "empty"
    u = uri.strip()
    if not u:
        return "", "empty"
    low = u.lower()
    if low.startswith("https://"):
        return u, "https"
    if low.startswith("http://"):
        return u, "http"
    if low.startswith("ipfs://"):
        return u, "ipfs"
    if low.startswith("data:"):
        return u, "data"
    if low.startswith("ar://"):
        return u, "arweave"
    if low.startswith("/ipfs/") or (len(u) > 40 and u.startswith(("Qm", "bafy", "bafk"))):
        return u, "ipfs"
    return u, "other"


def ipfs_urls(uri):
    cid = uri[7:] if uri.lower().startswith("ipfs://") else uri.lstrip("/").removeprefix("ipfs/")
    cid = cid.lstrip("/")
    return [g.format(cid=cid) for g in IPFS_GATEWAYS]


def decode_data_uri(uri):
    """Return (text, content_type). Raises on malformed input."""
    head, _, payload = uri.partition(",")
    meta = head[5:]  # strip "data:"
    is_b64 = meta.endswith(";base64")
    ctype = meta[:-7] if is_b64 else meta
    if is_b64:
        pad = "=" * (-len(payload) % 4)
        text = base64.b64decode(payload + pad).decode("utf-8", errors="replace")
    else:
        text = urllib.parse.unquote(payload)
    return text, (ctype or "text/plain")


# ---------------------------------------------------------------------------
# Document analysis
# ---------------------------------------------------------------------------

def extract_type(doc):
    for k in ("type", "@type", "schemaVersion", "kind"):
        v = doc.get(k)
        if isinstance(v, str):
            return v
        if isinstance(v, list) and v and isinstance(v[0], str):
            return ",".join(x for x in v if isinstance(x, str))
    return ""


def check_registry_field(doc):
    """ERC-8004 docs observed on-chain carry registrations[].agentRegistry in CAIP-10 form."""
    regs = doc.get("registrations")
    if not isinstance(regs, list) or not regs:
        return False, False
    for r in regs:
        if isinstance(r, dict):
            val = str(r.get("agentRegistry", "")).lower()
            if REGISTRY_CAIP in val:
                return True, True
    return True, False


def extract_services(doc):
    """Return (count, [(type, url), ...])."""
    for key in SERVICE_LIST_KEYS:
        v = doc.get(key)
        if isinstance(v, list) and v:
            out = []
            for item in v:
                if isinstance(item, str):
                    out.append(("", item))
                elif isinstance(item, dict):
                    url = next((str(item[k]) for k in ENDPOINT_URL_KEYS
                                if isinstance(item.get(k), str) and item[k]), "")
                    typ = next((str(item[k]) for k in ENDPOINT_TYPE_KEYS
                                if isinstance(item.get(k), str) and item[k]), "")
                    out.append((typ, url))
            return len(v), out
        if isinstance(v, dict) and v:
            out = []
            for typ, val in v.items():
                url = val if isinstance(val, str) else (
                    next((str(val[k]) for k in ENDPOINT_URL_KEYS
                          if isinstance(val, dict) and isinstance(val.get(k), str)), "")
                )
                out.append((str(typ), url))
            return len(v), out
    return 0, []


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

async def fetch(client, url):
    """Return dict with status/latency/body/error. Never raises."""
    t0 = time.perf_counter()
    try:
        r = await client.get(url, follow_redirects=True)
        latency = (time.perf_counter() - t0) * 1000
        body = r.content[:MAX_DOC_BYTES]
        return {
            "status": r.status_code,
            "latency_ms": round(latency, 1),
            "content_type": r.headers.get("content-type", "").split(";")[0],
            "bytes": len(r.content),
            "text": body.decode("utf-8", errors="replace"),
            "error": "",
        }
    except Exception as e:  # noqa: BLE001
        return {
            "status": "", "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            "content_type": "", "bytes": "", "text": "",
            "error": f"{type(e).__name__}: {str(e)[:160]}",
        }


async def probe_endpoint(client, url):
    if not url.lower().startswith(("http://", "https://")):
        return {"url": url, "status": "unsupported_scheme", "latency_ms": None, "ok": False}
    t0 = time.perf_counter()
    try:
        r = await client.head(url, follow_redirects=True)
        if r.status_code in (405, 501) or r.status_code >= 400:
            r = await client.get(url, follow_redirects=True)
        latency = round((time.perf_counter() - t0) * 1000, 1)
        return {"url": url, "status": r.status_code, "latency_ms": latency,
                "ok": 200 <= r.status_code < 400}
    except Exception as e:  # noqa: BLE001
        return {"url": url, "status": f"{type(e).__name__}",
                "latency_ms": round((time.perf_counter() - t0) * 1000, 1), "ok": False}


# ---------------------------------------------------------------------------
# Per-agent pipeline
# ---------------------------------------------------------------------------

async def check_agent(client, agent, sem, raw_dir):
    row = {c: "" for c in COLUMNS}
    row["agent_id"] = agent.get("agent_id", "")
    row["owner"] = agent.get("owner", "")
    row["uri_raw"] = agent.get("uri", "")
    row["uri_source"] = agent.get("uri_source", "")
    row["checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for k in ("s1_uri_present", "s2_resolved", "s3_valid_json",
              "s4_schema_match", "s5_has_services", "s6_any_endpoint_alive"):
        row[k] = False
    row["funnel_stage"] = 0

    async with sem:
        try:
            uri, scheme = classify_uri(row["uri_raw"])
            row["uri_scheme"] = scheme
            if scheme == "empty":
                return row
            row["s1_uri_present"] = True
            row["funnel_stage"] = 1

            # --- stage 2: resolve -------------------------------------------------
            if scheme == "data":
                try:
                    text, ctype = decode_data_uri(uri)
                    res = {"status": "data", "latency_ms": 0.0, "content_type": ctype,
                           "bytes": len(text), "text": text, "error": ""}
                    row["resolved_url"] = "inline"
                except Exception as e:  # noqa: BLE001
                    res = {"status": "", "latency_ms": "", "content_type": "", "bytes": "",
                           "text": "", "error": f"data_uri_decode: {str(e)[:120]}"}
            elif scheme == "ipfs":
                res, tried = None, []
                for g in ipfs_urls(uri):
                    res = await fetch(client, g)
                    tried.append(g)
                    if res["error"] == "" and isinstance(res["status"], int) \
                            and 200 <= res["status"] < 300:
                        row["resolved_url"] = g
                        break
                if not row["resolved_url"]:
                    row["resolved_url"] = tried[-1] if tried else ""
            elif scheme in ("http", "https"):
                row["resolved_url"] = uri
                res = await fetch(client, uri)
            else:
                res = {"status": "", "latency_ms": "", "content_type": "", "bytes": "",
                       "text": "", "error": f"unsupported_scheme:{scheme}"}

            row["http_status"] = res["status"]
            row["latency_ms"] = res["latency_ms"]
            row["content_type"] = res["content_type"]
            row["content_bytes"] = res["bytes"]
            row["fetch_error"] = res["error"]

            ok = res["error"] == "" and (
                res["status"] == "data" or
                (isinstance(res["status"], int) and 200 <= res["status"] < 300)
            )
            if not ok or not res["text"]:
                return row
            row["s2_resolved"] = True
            row["funnel_stage"] = 2

            # --- stage 3: JSON ----------------------------------------------------
            try:
                doc = json.loads(res["text"])
            except Exception as e:  # noqa: BLE001
                row["json_error"] = f"{type(e).__name__}: {str(e)[:120]}"
                return row
            if not isinstance(doc, dict):
                row["json_error"] = f"top-level {type(doc).__name__}, expected object"
                return row
            row["s3_valid_json"] = True
            row["funnel_stage"] = 3

            if raw_dir:
                try:
                    with open(os.path.join(raw_dir, f"{row['agent_id']}.json"), "w",
                              encoding="utf-8") as f:
                        f.write(res["text"])
                except Exception:  # noqa: BLE001
                    pass

            # --- stage 4: schema --------------------------------------------------
            tval = extract_type(doc)
            row["type_field_raw"] = tval[:200]
            has_reg, reg_match = check_registry_field(doc)
            row["registry_field_present"] = has_reg
            row["registry_matches"] = reg_match
            type_ok = bool(tval) and tval.strip().lower() in CANONICAL_TYPE_VALUES
            row["s4_schema_match"] = bool(type_ok or reg_match)
            if not row["s4_schema_match"]:
                return row
            row["funnel_stage"] = 4

            # --- stage 5: services ------------------------------------------------
            count, services = extract_services(doc)
            row["services_count"] = count
            row["service_types"] = "|".join(t for t, _ in services if t)[:300]
            if count == 0:
                return row
            row["s5_has_services"] = True
            row["funnel_stage"] = 5

            # --- stage 6: endpoints -----------------------------------------------
            urls = [u for _, u in services if u][:MAX_ENDPOINTS_PER_AGENT]
            row["endpoints_total"] = len([u for _, u in services if u])
            row["endpoints_checked"] = len(urls)
            if not urls:
                return row
            probes = await asyncio.gather(*(probe_endpoint(client, u) for u in urls),
                                          return_exceptions=True)
            clean = [p for p in probes if isinstance(p, dict)]
            row["endpoints_ok"] = sum(1 for p in clean if p["ok"])
            row["endpoint_details"] = json.dumps(clean, ensure_ascii=False)[:900]
            if row["endpoints_ok"] > 0:
                row["s6_any_endpoint_alive"] = True
                row["funnel_stage"] = 6
            return row

        except Exception as e:  # noqa: BLE001  -- last line of defence
            row["fetch_error"] = f"UNHANDLED {type(e).__name__}: {str(e)[:160]}"
            return row


# ---------------------------------------------------------------------------
# IO
# ---------------------------------------------------------------------------

def load_agents(path, uri_column):
    agents = []
    if path.lower().endswith(".csv"):
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                uri = ""
                src = ""
                if uri_column and r.get(uri_column) is not None:
                    uri, src = r.get(uri_column, ""), uri_column
                else:
                    for c in ("agent_uri_current", "agent_uri_at_registration",
                              "agent_uri", "uri"):
                        if r.get(c):
                            uri, src = r[c], c
                            break
                agents.append({"agent_id": r.get("agent_id", ""),
                               "owner": r.get("owner", ""),
                               "uri": uri, "uri_source": src})
    else:
        with open(path, encoding="utf-8") as f:
            for i, line in enumerate(f, 1):
                agents.append({"agent_id": i, "owner": "",
                               "uri": line.strip(), "uri_source": "txt"})
    return agents


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", default="liveness.csv")
    ap.add_argument("--uri-column", default=None)
    ap.add_argument("--concurrency", type=int, default=20)
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--contact", default="research@example.com")
    ap.add_argument("--raw-dir", default="raw")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    agents = load_agents(args.inp, args.uri_column)
    if args.limit:
        agents = agents[-args.limit:]

    done = set()
    if args.resume and os.path.exists(args.out):
        with open(args.out, newline="", encoding="utf-8") as f:
            done = {r["agent_id"] for r in csv.DictReader(f)}
        agents = [a for a in agents if str(a["agent_id"]) not in done]
        print(f"resume: {len(done)} already done, {len(agents)} to go")

    if args.raw_dir:
        os.makedirs(args.raw_dir, exist_ok=True)

    ua = (f"ERC8004-liveness-research/1.0 (+contact: {args.contact}; "
          f"one-off academic crawl; respects robots-owner contact on request)")
    limits = httpx.Limits(max_connections=args.concurrency,
                          max_keepalive_connections=args.concurrency)
    sem = asyncio.Semaphore(args.concurrency)

    new_file = not (args.resume and os.path.exists(args.out))
    f_out = open(args.out, "w" if new_file else "a", newline="", encoding="utf-8")
    writer = csv.DictWriter(f_out, fieldnames=COLUMNS, extrasaction="ignore")
    if new_file:
        writer.writeheader()

    t0 = time.time()
    async with httpx.AsyncClient(timeout=args.timeout, limits=limits,
                                 headers={"User-Agent": ua, "Accept": "application/json,*/*"},
                                 follow_redirects=True) as client:
        tasks = [asyncio.create_task(check_agent(client, a, sem, args.raw_dir)) for a in agents]
        n = 0
        for coro in asyncio.as_completed(tasks):
            try:
                row = await coro
            except Exception as e:  # noqa: BLE001
                print(f"  task failed: {e}", file=sys.stderr)
                continue
            writer.writerow(row)
            n += 1
            if n % 25 == 0:
                f_out.flush()
                print(f"  {n}/{len(agents)}  ({time.time() - t0:.0f}s)")

    f_out.close()
    print(f"wrote {args.out} ({time.time() - t0:.0f}s)")


if __name__ == "__main__":
    asyncio.run(main())
