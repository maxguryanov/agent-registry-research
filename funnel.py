#!/usr/bin/env python3
"""
Funnel over liveness.csv, absolute + percentages (of total and step-to-step),
plus a Wilson 95% CI on the final conversion so you can see whether N is enough.

Usage: python funnel.py liveness.csv
"""

import csv
import math
import sys

STAGES = [
    ("registered",        None),
    ("non-empty URI",     "s1_uri_present"),
    ("URI resolves",      "s2_resolved"),
    ("valid JSON",        "s3_valid_json"),
    ("matches ERC-8004",  "s4_schema_match"),
    ("has services",      "s5_has_services"),
    ("endpoint responds", "s6_any_endpoint_alive"),
]


def truthy(v):
    return str(v).strip().lower() in ("true", "1", "yes")


def wilson(k, n, z=1.96):
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    s = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (c - s) / d), min(1.0, (c + s) / d)


def main(path):
    rows = list(csv.DictReader(open(path, newline="", encoding="utf-8")))
    total = len(rows)
    if total == 0:
        print("empty input")
        return

    print(f"N = {total}\n")
    print(f"{'stage':<22}{'n':>7}{'% of total':>13}{'% of prev':>12}")
    print("-" * 54)

    prev = total
    counts = []
    for name, col in STAGES:
        n = total if col is None else sum(1 for r in rows if truthy(r.get(col)))
        counts.append((name, n))
        pt = 100 * n / total
        pp = 100 * n / prev if prev else 0
        print(f"{name:<22}{n:>7}{pt:>12.1f}%{pp:>11.1f}%")
        prev = n

    alive = counts[-1][1]
    lo, hi = wilson(alive, total)
    print("\nlive agents: "
          f"{alive}/{total} = {100 * alive / total:.1f}%  "
          f"(Wilson 95% CI: {100 * lo:.1f}% – {100 * hi:.1f}%, width {100 * (hi - lo):.1f} pp)")

    # biggest drop-off
    drops = []
    for i in range(1, len(counts)):
        drops.append((counts[i - 1][1] - counts[i][1], counts[i - 1][0], counts[i][0]))
    drops.sort(reverse=True)
    print("\nlargest drop-offs:")
    for d, a, b in drops[:3]:
        print(f"  {a} -> {b}: -{d} ({100 * d / total:.1f} pp of total)")

    # failure reasons at the resolve stage
    errs = {}
    for r in rows:
        if truthy(r.get("s1_uri_present")) and not truthy(r.get("s2_resolved")):
            key = (r.get("fetch_error", "").split(":")[0] or f"HTTP {r.get('http_status')}")
            errs[key] = errs.get(key, 0) + 1
    if errs:
        print("\nresolve failures by reason:")
        for k, v in sorted(errs.items(), key=lambda x: -x[1])[:10]:
            print(f"  {k}: {v}")

    # owner concentration - one wallet mass-minting distorts everything
    owners = {}
    for r in rows:
        o = (r.get("owner") or "").lower()
        if o:
            owners[o] = owners.get(o, 0) + 1
    if owners:
        top = sorted(owners.items(), key=lambda x: -x[1])[:5]
        print(f"\ndistinct owners: {len(owners)} / {total}")
        print("top owners:")
        for o, c in top:
            print(f"  {o}: {c} ({100 * c / total:.1f}%)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "liveness.csv")
