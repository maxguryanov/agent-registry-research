#!/usr/bin/env python3
"""
The liveness funnel: what counts as a working agent, and why a check failed.

Kept separate from the prober so the judgement calls in it can be read,
argued with, and tested without a network or a database. Every threshold in
this file is a methodological choice, not an implementation detail.

The six stages:

    1  the agent has a non-empty URI
    2  that URI resolves
    3  what comes back is a JSON object
    4  the object matches the ERC-8004 registration schema
    5  it declares services
    6  at least one declared endpoint responds

Strict liveness adds a seventh condition: the responding endpoint must not be
a generic host. An agent whose only "service endpoint" is github.com has
declared a link, not an interface.

Deps: httpx
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
import urllib.parse
import urllib.robotparser
from typing import Any

import httpx

REGISTRY_CAIP = "eip155:8453:0x8004a169fb4a3325136eb29fa0ceb6d2e539a432"

# The only canonical value observed in the data is the -v1 one. The others are
# kept because the specification revision being measured may change and the
# raw value is recorded either way, so conformance can be re-derived later.
CANONICAL_TYPE_VALUES = {
    "https://eips.ethereum.org/eips/eip-8004#registration-v1",
    "https://eips.ethereum.org/eips/eip-8004#registration",
    "https://eips.ethereum.org/eips/eip-8004",
    "erc8004:registration",
}

IPFS_GATEWAYS = [
    "https://ipfs.io/ipfs/{cid}",
    "https://dweb.link/ipfs/{cid}",
    "https://gateway.pinata.cloud/ipfs/{cid}",
]

SERVICE_LIST_KEYS = ["services", "service", "endpoints", "skills", "capabilities"]
ENDPOINT_URL_KEYS = ["serviceEndpoint", "endpoint", "url", "uri", "href", "address"]
ENDPOINT_TYPE_KEYS = ["type", "serviceType", "name", "id", "protocol"]

MAX_ENDPOINTS_PER_AGENT = 5
MAX_DOC_BYTES = 2_000_000

# ---------------------------------------------------------------------------
# Generic hosts
#
# A responding endpoint on one of these is not evidence that an agent works.
# The host would answer for anyone. Counting them inflated the liveness figure
# in the 2026 report from 6.8% to 7.3%: six of 88 apparently-live agents
# qualified only because they pointed at a code repository or a social network.
#
# Deliberately NOT on this list: github.io, vercel.app, netlify.app, and
# similar hosting subdomains, where the subdomain belongs to one project and a
# response really is about that project.
# ---------------------------------------------------------------------------

GENERIC_HOSTS = {
    # code hosting and package registries
    "github.com", "raw.githubusercontent.com", "gist.github.com",
    "gitlab.com", "bitbucket.org", "sourceforge.net", "npmjs.com", "pypi.org",
    # social and messaging
    "facebook.com", "twitter.com", "x.com", "t.me", "telegram.me",
    "discord.com", "discord.gg", "linkedin.com", "instagram.com",
    "youtube.com", "youtu.be", "reddit.com", "medium.com", "mirror.xyz",
    "warpcast.com", "farcaster.xyz",
    # public IPFS and Arweave gateways
    "ipfs.io", "cloudflare-ipfs.com", "dweb.link", "gateway.pinata.cloud",
    "nftstorage.link", "w3s.link", "4everland.io", "ipfs.filebase.io",
    "arweave.net", "gateway.irys.xyz",
    # specifications and documentation
    "eips.ethereum.org", "ethereum.org", "schema.org", "w3.org",
    "docs.google.com", "drive.google.com", "notion.so", "notion.site",
    # placeholders
    "example.com", "example.org", "localhost",
}

# Public suffixes that are two labels deep. Not the full Public Suffix List:
# that is a 15,000-line file updated weekly, and getting a handful of these
# wrong shifts a domain-clustering count by one, not a liveness verdict.
TWO_LABEL_SUFFIXES = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "co.jp", "or.jp", "ne.jp",
    "com.au", "net.au", "org.au", "co.nz", "com.br", "com.cn", "com.mx",
    "co.in", "co.kr", "com.tr", "com.sg", "com.hk", "co.za", "com.ar",
    "s3.amazonaws.com", "github.io", "gitlab.io", "pages.dev", "workers.dev",
    "vercel.app", "netlify.app", "web.app", "firebaseapp.com", "herokuapp.com",
    "onrender.com", "fly.dev", "railway.app", "ngrok.io", "ngrok-free.app",
}


def host_of(url: str) -> str:
    try:
        host = urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        return ""
    return host.lower().rstrip(".")


# Object-storage and CDN hosts, where the leftmost label identifies the tenant.
# Without this, every agent document parked in an S3 bucket clusters into one
# "project" called amazonaws.com, which is the provider, not the operator.
BUCKET_HOST_SUFFIXES = (
    "storage.googleapis.com", "blob.core.windows.net", "digitaloceanspaces.com",
    "r2.cloudflarestorage.com", "r2.dev", "supabase.co", "cloudfront.net",
    "b-cdn.net", "fastly.net", "akamaized.net", "azureedge.net",
)


def is_bucket_host(host: str) -> bool:
    host = (host or "").lower().rstrip(".")
    if host.endswith(".amazonaws.com") and (".s3." in host or ".s3-" in host):
        return True                      # bucket.s3.eu-west-1.amazonaws.com
    return any(host.endswith("." + suffix) for suffix in BUCKET_HOST_SUFFIXES)


def registrable_domain(host: str) -> str:
    """
    Approximate eTLD+1. 'a.b.example.co.uk' -> 'example.co.uk'.

    Used to cluster agents into projects by shared domain, where being one
    label off changes a grouping rather than a measurement.
    """
    host = (host or "").lower().strip(".")
    if not host or host.replace(".", "").isdigit():
        return host
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    if is_bucket_host(host):
        return host
    for suffix in TWO_LABEL_SUFFIXES:
        dotted = "." + suffix
        if host.endswith(dotted):
            head = host[: -len(dotted)].split(".")
            return f"{head[-1]}.{suffix}" if head and head[-1] else suffix
    return ".".join(parts[-2:])


def is_generic_host(host: str) -> bool:
    host = (host or "").lower().rstrip(".")
    if not host:
        return False
    if host in GENERIC_HOSTS:
        return True
    # A subdomain of a generic host is generic too (m.facebook.com), but only
    # when the parent is listed as generic in its own right.
    return any(host.endswith("." + generic) for generic in GENERIC_HOSTS)


# ---------------------------------------------------------------------------
# URIs
# ---------------------------------------------------------------------------

def classify_uri(uri: str | None) -> tuple[str, str]:
    if uri is None:
        return "", "empty"
    text = uri.strip()
    if not text:
        return "", "empty"
    low = text.lower()
    if low.startswith("https://"):
        return text, "https"
    if low.startswith("http://"):
        return text, "http"
    if low.startswith("ipfs://"):
        return text, "ipfs"
    if low.startswith("data:"):
        return text, "data"
    if low.startswith("ar://"):
        return text, "arweave"
    if low.startswith("/ipfs/") or (len(text) > 40
                                    and text.startswith(("Qm", "bafy", "bafk"))):
        return text, "ipfs"
    return text, "other"


def ipfs_urls(uri: str) -> list[str]:
    cid = uri[7:] if uri.lower().startswith("ipfs://") else uri.lstrip("/")
    if cid.startswith("ipfs/"):
        cid = cid[5:]
    cid = cid.lstrip("/")
    return [g.format(cid=cid) for g in IPFS_GATEWAYS]


def decode_data_uri(uri: str) -> tuple[str, str]:
    head, _, payload = uri.partition(",")
    meta = head[5:]
    is_b64 = meta.endswith(";base64")
    ctype = meta[:-7] if is_b64 else meta
    if is_b64:
        pad = "=" * (-len(payload) % 4)
        text = base64.b64decode(payload + pad).decode("utf-8", errors="replace")
    else:
        text = urllib.parse.unquote(payload)
    return text, (ctype.split(";")[0] or "text/plain")


# ---------------------------------------------------------------------------
# Document analysis
# ---------------------------------------------------------------------------

def extract_type(doc: dict) -> str:
    for key in ("type", "@type", "schemaVersion", "kind"):
        value = doc.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, list) and value and isinstance(value[0], str):
            return ",".join(v for v in value if isinstance(v, str))
    return ""


def check_registry_field(doc: dict) -> tuple[bool, bool]:
    """(registrations[].agentRegistry present, it points at this registry)."""
    regs = doc.get("registrations")
    if not isinstance(regs, list) or not regs:
        return False, False
    for entry in regs:
        if isinstance(entry, dict):
            value = str(entry.get("agentRegistry", "")).lower()
            if REGISTRY_CAIP in value:
                return True, True
    return True, False


def schema_verdict(doc: dict) -> dict:
    """
    Conformance, with both criteria kept apart.

    Neither test alone is enough. The canonical `type` value appears in a
    little over half of valid documents; registrations[].agentRegistry appears
    in well under a fifth. An agent is counted as conforming if either holds,
    and both signals are stored so the definition can be changed afterwards
    without re-probing anything.
    """
    raw_type = extract_type(doc)
    type_ok = bool(raw_type) and raw_type.strip().lower() in CANONICAL_TYPE_VALUES
    present, matches = check_registry_field(doc)
    return {
        "type_field_raw": raw_type[:400],
        "type_is_canonical": type_ok,
        "registry_field_present": present,
        "registry_field_matches": matches,
        "schema_match": bool(type_ok or matches),
    }


def extract_services(doc: dict) -> tuple[int, list[tuple[str, str]]]:
    for key in SERVICE_LIST_KEYS:
        value = doc.get(key)
        if isinstance(value, list) and value:
            out = []
            for item in value:
                if isinstance(item, str):
                    out.append(("", item))
                elif isinstance(item, dict):
                    url = next((str(item[k]) for k in ENDPOINT_URL_KEYS
                                if isinstance(item.get(k), str) and item[k]), "")
                    typ = next((str(item[k]) for k in ENDPOINT_TYPE_KEYS
                                if isinstance(item.get(k), str) and item[k]), "")
                    out.append((typ, url))
            return len(value), out
        if isinstance(value, dict) and value:
            out = []
            for typ, item in value.items():
                if isinstance(item, str):
                    url = item
                elif isinstance(item, dict):
                    url = next((str(item[k]) for k in ENDPOINT_URL_KEYS
                                if isinstance(item.get(k), str) and item[k]), "")
                else:
                    url = ""
                out.append((str(typ), url))
            return len(value), out
    return 0, []


# ---------------------------------------------------------------------------
# Failure classification
#
# Stored separately from the pass/fail flags, because raising the timeout does
# not remove failures: it moves them. In the 2026 report, going from a 10s to
# a 30s timeout cut ReadTimeout from 55 to 3 and raised HTTP 504 from 0 to 39.
# The agents did not change. Only the label did.
# ---------------------------------------------------------------------------

def classify_exception(exc: BaseException) -> str:
    name = type(exc).__name__
    text = str(exc).lower()
    if isinstance(exc, httpx.ConnectTimeout):
        return "timeout_connect"
    if isinstance(exc, httpx.ReadTimeout):
        return "timeout_read"
    if isinstance(exc, httpx.PoolTimeout):
        return "timeout_pool"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout_other"
    if isinstance(exc, httpx.ConnectError):
        if "name or service not known" in text or "getaddrinfo" in text \
                or "nodename nor servname" in text or "name resolution" in text:
            return "dns_failure"
        if "certificate" in text or "ssl" in text or "tls" in text:
            return "tls_failure"
        return "connect_failure"
    if "ssl" in name.lower() or "certificate" in text:
        return "tls_failure"
    if isinstance(exc, httpx.TooManyRedirects):
        return "too_many_redirects"
    if isinstance(exc, httpx.RemoteProtocolError):
        return "protocol_error"
    if isinstance(exc, httpx.UnsupportedProtocol):
        return "unsupported_scheme"
    if isinstance(exc, UnicodeError):
        return "bad_hostname"
    return f"transport_{name.lower()}"


def classify_http(status: int) -> str:
    named = {401: "http_401_unauthorized", 403: "http_403_forbidden",
             404: "http_404_not_found", 410: "http_410_gone",
             429: "http_429_rate_limited", 451: "http_451_blocked",
             500: "http_500", 502: "http_502_bad_gateway",
             503: "http_503_unavailable", 504: "http_504_gateway_timeout"}
    if status in named:
        return named[status]
    if 300 <= status < 400:
        return f"http_{status}_redirect"
    if 400 <= status < 500:
        return f"http_{status}_client_error"
    if status >= 500:
        return f"http_{status}_server_error"
    return f"http_{status}"


# ---------------------------------------------------------------------------
# Politeness
# ---------------------------------------------------------------------------

class HostLimiter:
    """
    One request at a time per host, with a minimum gap between them.

    Overall concurrency is not enough on its own. Agents cluster onto shared
    hosts, IPFS gateways above all, so a run limited only by a global
    semaphore can still aim its entire capacity at one server. This makes the
    limit per host, which is what the operator of that server experiences.
    """

    def __init__(self, min_interval: float = 0.5):
        self.min_interval = min_interval
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}

    def _lock(self, host: str) -> asyncio.Lock:
        if host not in self._locks:
            self._locks[host] = asyncio.Lock()
        return self._locks[host]

    class _Slot:
        def __init__(self, limiter: "HostLimiter", host: str):
            self.limiter, self.host = limiter, host

        async def __aenter__(self):
            await self.limiter._lock(self.host).acquire()
            wait = self.limiter.min_interval - (
                time.monotonic() - self.limiter._last.get(self.host, 0.0))
            if wait > 0:
                await asyncio.sleep(wait)
            return self

        async def __aexit__(self, *exc):
            self.limiter._last[self.host] = time.monotonic()
            self.limiter._lock(self.host).release()
            return False

    def slot(self, host: str) -> "HostLimiter._Slot":
        return self._Slot(self, host or "-")


class RobotsCache:
    """
    robots.txt per host, fetched once per run.

    A disallowed document is recorded as `robots_disallowed` rather than as a
    dead agent. It is a fact about our access, not about the agent, and the
    metrics stage needs to be able to tell those apart.
    """

    def __init__(self, client: httpx.AsyncClient, user_agent: str,
                 enabled: bool = True):
        self.client, self.user_agent, self.enabled = client, user_agent, enabled
        self._cache: dict[str, Any] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.stats = {"fetched": 0, "disallowed": 0, "unavailable": 0}

    async def allowed(self, url: str) -> bool:
        if not self.enabled:
            return True
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return True
        origin = f"{parts.scheme}://{parts.netloc}"

        if origin not in self._cache:
            lock = self._locks.setdefault(origin, asyncio.Lock())
            async with lock:
                if origin not in self._cache:
                    self._cache[origin] = await self._load(origin)

        parser = self._cache[origin]
        if parser is None:
            return True
        try:
            ok = parser.can_fetch(self.user_agent, url)
        except Exception:  # noqa: BLE001 - a malformed robots.txt is not a verdict
            return True
        if not ok:
            self.stats["disallowed"] += 1
        return ok

    async def _load(self, origin: str):
        try:
            resp = await self.client.get(f"{origin}/robots.txt",
                                         follow_redirects=True, timeout=10.0)
            self.stats["fetched"] += 1
            if resp.status_code != 200 or not resp.text.strip():
                return None
            parser = urllib.robotparser.RobotFileParser()
            parser.parse(resp.text.splitlines())
            return parser
        except Exception:  # noqa: BLE001 - no robots.txt means no restriction
            self.stats["unavailable"] += 1
            return None


def build_user_agent(contact: str, project_url: str, version: str = "1.0") -> str:
    return (f"ERC8004-registry-monitor/{version} (+{project_url}"
            + (f"; contact: {contact}" if contact else "") + ")")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


# ---------------------------------------------------------------------------
# The probe
# ---------------------------------------------------------------------------

def blank_result(agent_id, uri: str) -> dict:
    """A result row with every field at its 'nothing worked' value."""
    return {
        "agent_id": agent_id,
        "uri_checked": uri or "",
        "uri_source": "current_uri",
        "uri_scheme": None,
        "resolved_url": None,
        "s1_uri_present": False, "s2_resolved": False, "s3_valid_json": False,
        "s4_schema_match": False, "s5_has_services": False,
        "s6_endpoint_alive": False, "live_strict": False, "funnel_stage": 0,
        "type_field_raw": None, "type_is_canonical": False,
        "registry_field_present": False, "registry_field_matches": False,
        "failure_stage": None, "failure_category": None, "failure_detail": None,
        "http_status": None, "latency_ms": None,
        "content_type": None, "content_bytes": None,
        "services_count": 0, "endpoints_total": 0, "endpoints_checked": 0,
        "endpoints_ok": 0, "endpoints_ok_specific": 0, "generic_only": False,
        "endpoint_details": None, "doc_sha256": None,
        "uri_host": None, "uri_root_domain": None,
    }


def _fail(row: dict, stage: int, category: str, detail: str = "") -> dict:
    row["failure_stage"] = stage
    row["failure_category"] = category
    row["failure_detail"] = (detail or "")[:500] or None
    return row


class Prober:
    """Runs the funnel for one agent at a time, politely."""

    def __init__(self, client: httpx.AsyncClient, limiter: HostLimiter,
                 robots: RobotsCache, excluded_hosts: set[str] | None = None):
        self.client = client
        self.limiter = limiter
        self.robots = robots
        self.excluded = {h.lower() for h in (excluded_hosts or set())}

    def is_excluded(self, host: str) -> bool:
        host = (host or "").lower()
        return bool(host) and (host in self.excluded
                               or any(host.endswith("." + h) for h in self.excluded))

    async def _get(self, url: str, method: str = "GET") -> dict:
        """One HTTP request. Never raises."""
        host = host_of(url)
        started = time.perf_counter()
        async with self.limiter.slot(host):
            try:
                request = (self.client.get if method == "GET" else self.client.head)
                resp = await request(url, follow_redirects=True)
                body = resp.content[:MAX_DOC_BYTES]
                return {
                    "status": resp.status_code,
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                    "content_type": resp.headers.get("content-type", "").split(";")[0],
                    "bytes": len(resp.content),
                    "text": body.decode("utf-8", errors="replace"),
                    "error": None,
                }
            except Exception as exc:  # noqa: BLE001 - classified, not propagated
                return {
                    "status": None,
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                    "content_type": None, "bytes": None, "text": "",
                    "error": classify_exception(exc),
                    "error_detail": f"{type(exc).__name__}: {str(exc)[:200]}",
                }

    async def _resolve_document(self, row: dict, uri: str, scheme: str) -> dict | None:
        """Stage 2. Returns the fetch result, or None if the row already failed."""
        if scheme == "data":
            try:
                text, ctype = decode_data_uri(uri)
            except Exception as exc:  # noqa: BLE001
                _fail(row, 2, "data_uri_malformed", f"{type(exc).__name__}: {exc}")
                return None
            row["resolved_url"] = "inline"
            return {"status": None, "latency_ms": 0, "content_type": ctype,
                    "bytes": len(text), "text": text, "error": None}

        if scheme in ("http", "https"):
            targets = [uri]
        elif scheme == "ipfs":
            targets = ipfs_urls(uri)
        else:
            _fail(row, 2, "unsupported_scheme", scheme)
            return None

        last = None
        for target in targets:
            host = host_of(target)
            if self.is_excluded(host):
                _fail(row, 2, "excluded_by_request", host)
                return None
            if not await self.robots.allowed(target):
                _fail(row, 2, "robots_disallowed", host)
                return None

            row["resolved_url"] = target
            result = await self._get(target)
            row["http_status"] = result["status"]
            row["latency_ms"] = result["latency_ms"]
            row["content_type"] = result["content_type"]
            row["content_bytes"] = result["bytes"]

            if result["error"]:
                last = _fail(row, 2, result["error"], result.get("error_detail", ""))
                continue
            if not (200 <= result["status"] < 300):
                last = _fail(row, 2, classify_http(result["status"]),
                             f"{result['status']} from {host}")
                continue
            if not result["text"].strip():
                last = _fail(row, 2, "empty_body", host)
                continue
            row["failure_stage"] = None
            row["failure_category"] = None
            row["failure_detail"] = None
            return result

        if last is not None and scheme == "ipfs":
            row["failure_detail"] = (
                f"all {len(targets)} gateways failed; last: {row['failure_category']}")
        return None

    async def _probe_endpoints(self, urls: list[str]) -> list[dict]:
        async def one(url: str) -> dict:
            host = host_of(url)
            if not url.lower().startswith(("http://", "https://")):
                return {"url": url[:200], "host": host, "ok": False,
                        "status": "unsupported_scheme", "generic": False}
            if self.is_excluded(host):
                return {"url": url[:200], "host": host, "ok": False,
                        "status": "excluded_by_request", "generic": False}
            if not await self.robots.allowed(url):
                return {"url": url[:200], "host": host, "ok": False,
                        "status": "robots_disallowed", "generic": False}

            result = await self._get(url, method="HEAD")
            # Plenty of servers reject HEAD but serve GET.
            if result["error"] or (result["status"] is not None
                                   and result["status"] >= 400):
                result = await self._get(url, method="GET")

            ok = result["status"] is not None and 200 <= result["status"] < 400
            return {
                "url": url[:200], "host": host, "ok": ok,
                "status": result["status"] if result["status"] is not None
                          else result["error"],
                "latency_ms": result["latency_ms"],
                "generic": is_generic_host(host),
            }

        return list(await asyncio.gather(*(one(u) for u in urls)))

    async def check(self, agent_id, uri: str) -> dict:
        row = blank_result(agent_id, uri)
        try:
            cleaned, scheme = classify_uri(uri)
            row["uri_scheme"] = scheme

            # --- stage 1 ---
            if scheme == "empty":
                return _fail(row, 1, "empty_uri")
            row["s1_uri_present"] = True
            row["funnel_stage"] = 1
            if scheme in ("http", "https"):
                row["uri_host"] = host_of(cleaned)
                row["uri_root_domain"] = registrable_domain(row["uri_host"])

            # --- stage 2 ---
            fetched = await self._resolve_document(row, cleaned, scheme)
            if fetched is None:
                return row
            row["s2_resolved"] = True
            row["funnel_stage"] = 2

            # --- stage 3 ---
            row["doc_sha256"] = sha256_text(fetched["text"])
            try:
                doc = json.loads(fetched["text"])
            except Exception as exc:  # noqa: BLE001
                return _fail(row, 3, "not_json", f"{type(exc).__name__}: {exc}")
            if not isinstance(doc, dict):
                return _fail(row, 3, "json_not_object",
                             f"top level is {type(doc).__name__}")
            row["s3_valid_json"] = True
            row["funnel_stage"] = 3

            # --- stage 4 ---
            verdict = schema_verdict(doc)
            row.update({k: verdict[k] for k in
                        ("type_field_raw", "type_is_canonical",
                         "registry_field_present", "registry_field_matches")})
            row["s4_schema_match"] = verdict["schema_match"]
            if not verdict["schema_match"]:
                return _fail(row, 4, "schema_mismatch",
                             f"type={verdict['type_field_raw'][:120]!r}")
            row["funnel_stage"] = 4

            # --- stage 5 ---
            count, services = extract_services(doc)
            row["services_count"] = count
            if count == 0:
                return _fail(row, 5, "no_services")
            urls = [u for _, u in services if u]
            row["endpoints_total"] = len(urls)
            if not urls:
                return _fail(row, 5, "services_without_urls",
                             f"{count} service entries, none with a URL")
            row["s5_has_services"] = True
            row["funnel_stage"] = 5

            # --- stage 6 ---
            checked = urls[:MAX_ENDPOINTS_PER_AGENT]
            row["endpoints_checked"] = len(checked)
            probes = await self._probe_endpoints(checked)
            row["endpoint_details"] = json.dumps(probes, ensure_ascii=False)
            alive = [p for p in probes if p["ok"]]
            specific = [p for p in alive if not p["generic"]]
            row["endpoints_ok"] = len(alive)
            row["endpoints_ok_specific"] = len(specific)

            if not alive:
                statuses = ", ".join(str(p["status"]) for p in probes[:5])
                return _fail(row, 6, "all_endpoints_dead", statuses)
            row["s6_endpoint_alive"] = True
            row["funnel_stage"] = 6

            # --- strict liveness ---
            if not specific:
                row["generic_only"] = True
                hosts = ", ".join(sorted({p["host"] for p in alive}))
                return _fail(row, 6, "generic_hosts_only", hosts)
            row["live_strict"] = True
            return row

        except Exception as exc:  # noqa: BLE001 - one bad agent must not stop a run
            return _fail(row, row["funnel_stage"] or 0, "prober_error",
                         f"{type(exc).__name__}: {exc}")
