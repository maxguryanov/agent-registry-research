#!/usr/bin/env python3
"""
Дорезолвливает текущий tokenURI(agentId) для агентов из готового CSV.

Использование:
    python3 resolve_current.py --in sample.csv --out sample_resolved.csv

Заполняет колонки agent_uri_current и uri_changed_after_registration.
Работает батчами через eth_call к публичным RPC Base, с ротацией
эндпоинтов при ошибках и ретраями. Пишет промежуточные результаты,
поэтому прерывание не теряет работу.
"""

import argparse
import asyncio
import csv
import sys
import time

import httpx

CONTRACT = "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432"

# tokenURI(uint256) селектор
SELECTOR = "0xc87b56dd"

RPCS = [
    "https://mainnet.base.org",
    "https://base.llamarpc.com",
    "https://base-rpc.publicnode.com",
    "https://base.drpc.org",
    "https://1rpc.io/base",
]


def encode_call(agent_id: int) -> str:
    return SELECTOR + hex(agent_id)[2:].rjust(64, "0")


def decode_string(hexdata: str) -> str:
    """Декодирует ABI-строку из ответа eth_call."""
    if not hexdata or hexdata in ("0x", "0x0"):
        return ""
    raw = bytes.fromhex(hexdata[2:])
    if len(raw) < 64:
        return ""
    try:
        offset = int.from_bytes(raw[0:32], "big")
        length = int.from_bytes(raw[offset:offset + 32], "big")
        data = raw[offset + 32: offset + 32 + length]
        return data.decode("utf-8", errors="replace")
    except Exception:
        return ""


class RpcPool:
    def __init__(self, urls):
        self.urls = list(urls)
        self.i = 0

    def next(self):
        u = self.urls[self.i % len(self.urls)]
        self.i += 1
        return u


async def fetch_one(client, pool, agent_id, sem, stats):
    async with sem:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [{"to": CONTRACT, "data": encode_call(agent_id)}, "latest"],
        }
        for attempt in range(4):
            url = pool.next()
            try:
                r = await client.post(url, json=payload, timeout=20.0)
                j = r.json()
                if "result" in j:
                    stats["ok"] += 1
                    return decode_string(j["result"])
                # revert (например, токен сожжён) считаем пустым
                if "error" in j:
                    msg = str(j["error"]).lower()
                    if "revert" in msg or "execution" in msg:
                        stats["revert"] += 1
                        return ""
            except Exception:
                pass
            await asyncio.sleep(0.4 * (attempt + 1))
        stats["fail"] += 1
        return None  # не удалось определить


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="infile", required=True)
    ap.add_argument("--out", dest="outfile", default="sample_resolved.csv")
    ap.add_argument("--concurrency", type=int, default=12)
    args = ap.parse_args()

    with open(args.infile, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        print("входной файл пуст")
        sys.exit(1)

    print(f"агентов на входе: {len(rows)}")

    pool = RpcPool(RPCS)
    sem = asyncio.Semaphore(args.concurrency)
    stats = {"ok": 0, "revert": 0, "fail": 0}
    t0 = time.time()

    async with httpx.AsyncClient(
        headers={"User-Agent": "agent-registry-research/1.0"}
    ) as client:
        tasks = [
            fetch_one(client, pool, int(r["agent_id"]), sem, stats) for r in rows
        ]
        results = []
        done = 0
        for coro in asyncio.as_completed(
            [asyncio.create_task(t) for t in tasks]
        ):
            results.append(await coro)
            done += 1
            if done % 100 == 0:
                el = time.time() - t0
                print(f"  {done}/{len(rows)}  ok={stats['ok']} "
                      f"revert={stats['revert']} fail={stats['fail']}  {el:.0f}s")

    # as_completed не сохраняет порядок, поэтому резолвим заново по порядку
    # (быстро, всё уже в кеше RPC) — вместо этого делаем прямой gather
    async with httpx.AsyncClient(
        headers={"User-Agent": "agent-registry-research/1.0"}
    ) as client:
        sem2 = asyncio.Semaphore(args.concurrency)
        ordered = await asyncio.gather(*[
            fetch_one(client, pool, int(r["agent_id"]), sem2, stats) for r in rows
        ])

    fields = list(rows[0].keys())
    for extra in ("agent_uri_current", "uri_changed_after_registration",
                  "resolve_status"):
        if extra not in fields:
            fields.append(extra)

    empty_at_reg = 0
    empty_now = 0
    changed = 0
    unresolved = 0

    for r, cur in zip(rows, ordered):
        at_reg = (r.get("agent_uri_at_registration") or "").strip()
        if at_reg == "":
            empty_at_reg += 1
        if cur is None:
            r["agent_uri_current"] = ""
            r["resolve_status"] = "unresolved"
            r["uri_changed_after_registration"] = ""
            unresolved += 1
            continue
        cur = cur.strip()
        r["agent_uri_current"] = cur
        r["resolve_status"] = "ok"
        r["uri_changed_after_registration"] = str(cur != at_reg).lower()
        if cur == "":
            empty_now += 1
        if cur != at_reg:
            changed += 1

    with open(args.outfile, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    n = len(rows)
    resolved = n - unresolved
    print("\n=== ИТОГ ===")
    print(f"всего агентов:            {n}")
    print(f"не удалось определить:    {unresolved}")
    print(f"пустой URI при регистрации: {empty_at_reg} ({empty_at_reg/n*100:.1f}%)")
    if resolved:
        print(f"пустой URI сейчас:        {empty_now} "
              f"({empty_now/resolved*100:.1f}% от определённых)")
        print(f"URI менялся после регистрации: {changed} "
              f"({changed/resolved*100:.1f}%)")
    print(f"\nзаписано в {args.outfile}")
    print(f"время: {time.time()-t0:.0f}s")


if __name__ == "__main__":
    asyncio.run(main())
