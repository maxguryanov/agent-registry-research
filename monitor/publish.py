#!/usr/bin/env python3
"""
Build the public page and the JSON API into docs/.

    python3 -m monitor.publish                 # write docs/
    python3 -m monitor.publish --out site      # somewhere else
    python3 -m monitor.publish --from agg.json # from a saved document

The output is static files. Nothing on the page fetches anything, runs any
script, or depends on a host staying up: charts are inline SVG generated here,
styling is inline CSS, and the API is a directory of JSON files. A page like
that cannot go down, cannot be rate-limited, and still opens in ten years.

The cost is that the numbers are as fresh as the last run, which is what the
daily schedule is for.
"""

from __future__ import annotations

import argparse
import json
import pathlib
from datetime import datetime, timezone

from . import charts, db, metrics
from .hosts import hosting_list
from .page_style import STYLE

REPO_URL = "https://github.com/maxguryanov/agent-registry-research"
SITE_URL = "https://maxguryanov.github.io/agent-registry-research/"
esc = charts.esc


# ---------------------------------------------------------------------------
# Small builders
# ---------------------------------------------------------------------------

def card(key: str, value: str, caption: str = "") -> str:
    caption_html = f'<div class="c">{esc(caption)}</div>' if caption else ""
    return (f'<div class="card"><div class="k">{esc(key)}</div>'
            f'<div class="v">{esc(value)}</div>{caption_html}</div>')


def pct_cell(entry: dict) -> str:
    if entry.get("pct") is None:
        return "n/a"
    return f'{entry["pct"]:.1f}%'


def ci_cell(entry: dict) -> str:
    if entry.get("pct") is None:
        return ""
    return f'{entry["ci_low"]:.1f}–{entry["ci_high"]:.1f}'


def funnel_table(funnel: dict) -> str:
    rows = [
        '<tr class="total"><td>registered</td>'
        f'<td>{funnel["denominator"]:,}</td><td>100.0%</td><td class="ci"></td>'
        '<td></td></tr>'
    ]
    for stage in funnel["stages"]:
        previous = ("" if stage.get("pct_of_previous") is None
                    else f'{stage["pct_of_previous"]:.1f}%')
        rows.append(
            f'<tr><td>{esc(stage["label"])}</td><td>{stage["k"]:,}</td>'
            f'<td>{pct_cell(stage)}</td><td class="ci">{ci_cell(stage)}</td>'
            f'<td>{previous}</td></tr>')
    return (
        '<div class="scroll"><table><thead><tr><th>stage</th><th>n</th>'
        '<th>% of total</th><th>95% CI</th><th>% of previous</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div>')


def comparison_table(liveness: dict) -> str:
    rows = []
    for label, key, note in [
        ("per agent", "by_agent", "every registration counted separately"),
        ("per owner", "by_owner", "an owner counts once, however many agents"),
        ("per project", "by_project", "agents linked by shared owner or domain"),
    ]:
        funnel = liveness[key]
        final = funnel["stages"][-1]
        rows.append(
            f'<tr><td>{esc(label)}</td><td>{final["k"]:,}</td>'
            f'<td>{funnel["denominator"]:,}</td><td>{pct_cell(final)}</td>'
            f'<td class="ci">{ci_cell(final)}</td>'
            f'<td style="text-align:left;white-space:normal">'
            f'<span class="note">{esc(note)}</span></td></tr>')
    return ('<div class="scroll"><table><thead><tr><th>counted</th><th>live</th>'
            '<th>of</th><th>rate</th><th>95% CI</th><th></th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def failures_table(failures: list[dict], limit: int = 12) -> str:
    if not failures:
        return '<p class="empty">No failures recorded.</p>'
    rows = "".join(
        f'<tr><td><code>{esc(f["category"])}</code></td><td>{f["n"]:,}</td>'
        f'<td>{f["pct"]:.1f}%</td></tr>' for f in failures[:limit])
    return ('<div class="scroll"><table><thead><tr><th>category</th><th>n</th>'
            f'<th>share</th></tr></thead><tbody>{rows}</tbody></table></div>')


def projects_table(projects: list[dict], limit: int = 12) -> str:
    if not projects:
        return '<p class="empty">No projects yet.</p>'
    rows = []
    for project in projects[:limit]:
        rows.append(
            f'<tr><td style="white-space:normal"><code>'
            f'{esc(project["label"][:56])}</code></td>'
            f'<td>{project["agents"]:,}</td><td>{project["owners"]:,}</td>'
            f'<td>{project["live_agents"]:,}</td></tr>')
    return ('<div class="scroll"><table><thead><tr><th>project</th><th>agents</th>'
            '<th>wallets</th><th>live</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def survival_section(survival: dict) -> str:
    if survival["status"] != "ok":
        remaining = survival["days_until_first_horizon"]
        return (
            '<div class="panel warn"><h3>Not measurable yet</h3>'
            f'<p>Survival at 30 days needs 30 days of measurements. There are '
            f'<strong>{survival["history_span_days"]}</strong>. '
            f'The first 30-day figure is due in about '
            f'<strong>{remaining:.0f} days</strong>.</p>'
            '<p class="note">This says "not yet" rather than showing a zero, '
            'because a zero here would read as "nobody survived".</p></div>')
    rows = []
    for horizon in survival["horizons"]:
        if not horizon["n"]:
            rows.append(f'<tr><td>T+{horizon["horizon_days"]} days</td>'
                        f'<td colspan="4" style="text-align:left">'
                        '<span class="note">no agent observed that long yet'
                        '</span></td></tr>')
            continue
        rows.append(
            f'<tr><td>T+{horizon["horizon_days"]} days</td>'
            f'<td>{horizon["k"]:,}</td><td>{horizon["n"]:,}</td>'
            f'<td>{pct_cell(horizon)}</td>'
            f'<td class="ci">{ci_cell(horizon)}</td></tr>')
    return ('<div class="scroll"><table><thead><tr><th>horizon</th>'
            '<th>still live</th><th>evaluable</th><th>rate</th>'
            f'<th>95% CI</th></tr></thead><tbody>{"".join(rows)}</tbody>'
            '</table></div>')


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

# A tab icon, as an inline data URI so the page still fetches nothing.
FAVICON = ("data:image/svg+xml,"
           "%3Csvg xmlns='http%3A//www.w3.org/2000/svg' viewBox='0 0 32 32'%3E"
           "%3Crect width='32' height='32' rx='7' fill='%232f6fd0'/%3E"
           "%3Crect x='7' y='7' width='18' height='4' rx='2' fill='white'/%3E"
           "%3Crect x='7' y='14' width='12' height='4' rx='2' fill='white' "
           "opacity='.72'/%3E"
           "%3Crect x='7' y='21' width='5' height='4' rx='2' fill='white' "
           "opacity='.45'/%3E%3C/svg%3E")


def headline(doc: dict) -> tuple[str, str]:
    """
    The one sentence this page exists to say, and the number in it.

    Also used as the description a link preview shows, so it has to stand on
    its own with no page around it.
    """
    registry = doc["registry"]
    liveness = doc["liveness"]
    total = f"{registry['agents']:,}"

    if liveness.get("status") != "ok":
        return ("", f"{total} agents are registered in the ERC-8004 Identity "
                    f"Registry on Base. How many of them work is measured here "
                    f"daily; the first measurement has not run yet.")

    agent = liveness["by_agent"]["stages"][-1]
    owner = liveness["by_owner"]["stages"][-1]
    if agent.get("pct") is None:
        return ("", f"{total} agents registered. No liveness measurement yet.")

    return (f'{agent["pct"]:.1f}%',
            f'of the {total} agents registered in the ERC-8004 Identity Registry '
            f'on Base expose an endpoint that answers. Counted per owner rather '
            f'than per agent, it is {owner["pct"]:.1f}%.')


def build_page(doc: dict) -> str:
    registry = doc["registry"]
    liveness = doc["liveness"]
    generated = doc["generated_at"][:16].replace("T", " ")
    number, sentence = headline(doc)

    cards = [
        card("agents registered", f'{registry["agents"]:,}',
             f'through block {registry["indexed_through_block"]:,}'),
        card("distinct owners", f'{registry["distinct_owners"]:,}'),
    ]
    churn = registry["uri_changed_after_registration"]
    if churn["pct"] is not None:
        cards.append(card("changed URI after mint", f'{churn["pct"]:.1f}%',
                          f'{churn["k"]:,} agents'))

    body = []
    if number:
        body.append(
            '<div class="headline">'
            f'<div class="num">{esc(number)}</div>'
            f'<div class="say">{esc(sentence)}'
            '<span class="qual">A responding endpoint is not a working agent: '
            'nothing here completes a handshake or exercises a task. '
            'The figure is an upper bound.</span></div></div>')
    else:
        body.append(f'<div class="headline"><div class="say">{esc(sentence)}'
                    '</div></div>')
    body.append(f'<div class="cards">{"".join(cards)}</div>')

    if liveness.get("status") != "ok":
        body.append(
            '<div class="panel warn"><h3>No liveness measurement yet</h3>'
            '<p>The registry has been indexed, but no probe run has completed. '
            'The funnel appears here after the first run.</p></div>')
    else:
        run = liveness["run"]

        # The three liveness rates are already in the headline and in the
        # table immediately below. A row of cards repeating them would be the
        # third time on one screen.
        body.append('<h2>The same registry, three denominators</h2>')
        body.append(
            '<p class="note">One operator running many wallets is many agents '
            'and one participant. Which number is right depends on the question, '
            'so all three are published.</p>')
        body.append(comparison_table(liveness))

        body.append('<h2>Funnel, per agent</h2>')
        body.append('<p class="note">Bars are the share of all registrations '
                    'reaching each stage. The line across each bar is the 95% '
                    'Wilson interval.</p>')
        body.append(charts.funnel_chart(liveness["by_agent"]["stages"]))
        body.append(funnel_table(liveness["by_agent"]))

        generic = liveness["generic_hosts_only"]
        endpoints = liveness["endpoints"]
        body.append('<h2>Strict liveness</h2>')
        body.append(
            f'<p class="note">An agent whose only responding endpoint is a '
            f'generic host — a code repository, a social network, a public IPFS '
            f'gateway — is not counted as live. Those hosts answer for anyone. '
            f'In this run that rule excluded <strong>{generic["k"]:,}</strong> '
            f'agents ({pct_cell(generic)}). Of '
            f'<strong>{endpoints["declared"]:,}</strong> declared endpoints, '
            f'{endpoints["responding"]:,} responded and '
            f'{endpoints["responding_specific"]:,} were not on a generic host.'
            f'</p>')

        body.append('<h2>Where agents fail</h2>')
        body.append(
            '<p class="note">Categories are kept apart on purpose. Raising the '
            'timeout does not remove failures, it relabels them: a read timeout '
            'becomes an HTTP 504 from a gateway. This run used a '
            f'{run["timeout_seconds"]:.0f}s timeout at concurrency '
            f'{run["concurrency"]}.</p>')
        body.append(failures_table(liveness["failures"]))

        if liveness["censored"]["n"]:
            body.append(
                f'<p class="note"><strong>{liveness["censored"]["n"]:,}</strong> '
                f'agents were not probed because robots.txt disallowed it or '
                f'their operator asked to be excluded. They are left out of '
                f'every denominator above rather than counted as dead.</p>')

    body.append('<h2>Registrations over time</h2>')
    body.append('<p class="note">Bars: registrations per month. '
                'Line: cumulative total.</p>')
    body.append(charts.registrations_chart(doc["registrations_monthly"]))

    if liveness.get("status") == "ok":
        cohorts = liveness["cohorts_cross_sectional"]
        body.append('<h2>Liveness by registration month</h2>')
        body.append(
            '<p class="note">Measured in a single run, so this compares '
            'different agents of different ages at one moment. It is a '
            'cross-section, not survival: anything that changed about who '
            'registers over time is mixed into it. Shaded band is the 95% '
            'interval.</p>')
        body.append(charts.cohort_chart(cohorts))

    body.append('<h2>Survival</h2>')
    body.append(
        '<p class="note">Of the agents observed alive at a time T, the share '
        'still alive at T+30 and T+90 days. Measured on a fixed panel, so the '
        'same agents are followed rather than resampled.</p>')
    body.append(survival_section(doc["survival"]))

    if liveness.get("status") == "ok" and liveness["top_projects"]:
        body.append('<h2>Largest projects</h2>')
        body.append(
            '<p class="note">Agents grouped by shared owner address or shared '
            'root domain, transitively. A lower bound on concentration: an '
            'operator using separate wallets <em>and</em> separate domains '
            'still counts as several projects.</p>')
        body.append(
            '<p class="note">Domains that are shared hosting — object storage, '
            'CDNs, public IPFS gateways, naming services, platforms where the '
            'subdomain is a tenant — do not group anything. Agents on them '
            'share a filing cabinet, not an operator, and joining them produced '
            'a "project" called ipfs.io holding 257 agents across 254 unrelated '
            'wallets. Such an agent is grouped by its owner alone. '
            f'{len(hosting_list())} domains are treated this way:</p>')
        body.append(
            '<details class="excl"><summary>show the excluded domains</summary>'
            '<p class="domlist">'
            + ", ".join(f"<code>{esc(d)}</code>" for d in hosting_list())
            + '</p><p class="note">A domain carrying many agents across almost '
            'as many wallets is the signature of shared hosting, but it is a '
            'guide for adding to this list by hand, not an automatic rule: '
            '<code>voltplayground.xyz</code> shows thirteen agents on thirteen '
            'wallets and all thirteen respond.</p></details>')
        body.append(projects_table(liveness["top_projects"]))

    body.append('<h2>API</h2>')
    body.append(
        '<p class="note">The same numbers as files. No key, no rate limit.</p>'
        '<ul>'
        '<li><code><a href="api/summary.json">api/summary.json</a></code>'
        ' — everything on this page</li>'
        '<li><code><a href="api/funnel.json">api/funnel.json</a></code>'
        ' — the three funnels and failure categories</li>'
        '<li><code><a href="api/registrations.json">api/registrations.json</a>'
        '</code> — registrations per month</li>'
        '<li><code><a href="api/survival.json">api/survival.json</a></code>'
        ' — survival and cohorts</li>'
        '<li><code><a href="api/projects.json">api/projects.json</a></code>'
        ' — project clustering</li>'
        '<li><code><a href="api/meta.json">api/meta.json</a></code>'
        ' — freshness and run settings</li>'
        '</ul>')

    body.append('<h2>How to read this</h2>')
    body.append(
        '<ul>'
        '<li>Liveness is measured against each agent\'s <em>current</em> '
        'on-chain URI, not the one in its registration event. Agents that mint '
        'with an empty URI and set it later are common enough to move the '
        'result by about a third.</li>'
        '<li>A responding endpoint is not a working agent. Nothing here '
        'completes an MCP handshake or exercises an A2A task. '
        '<strong>These figures are an upper bound.</strong></li>'
        '<li>Owner counts treat one wallet as one participant. An operator '
        'using many wallets is understated, not overstated.</li>'
        '<li>Intervals are Wilson score intervals at 95%, which stay inside '
        '0–100% for the small proportions this measures.</li>'
        '</ul>')

    footer = (
        f'<footer><p>Generated {esc(generated)} UTC from on-chain state on Base. '
        f'Method, code and corrections: <a href="{REPO_URL}">'
        f'{esc(REPO_URL.split("//")[1])}</a>. '
        f'Per-agent records and owner addresses are not published.</p></footer>')

    title = "ERC-8004 agents on Base: how many actually work"
    share = (f"{number} {sentence}" if number else sentence)

    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{esc(title)}</title>"
        f'<meta name="description" content="{esc(share)}">'
        # What a shared link shows. Without these it unfurls as a bare URL,
        # which is the difference between a link people open and one they
        # scroll past. No image: the platforms that matter will not render an
        # SVG, and this page has no way to make a raster one without pulling in
        # a dependency it otherwise does not need.
        f'<meta property="og:title" content="{esc(title)}">'
        f'<meta property="og:description" content="{esc(share)}">'
        '<meta property="og:type" content="website">'
        f'<meta property="og:url" content="{esc(SITE_URL)}">'
        '<meta name="twitter:card" content="summary">'
        f'<meta name="twitter:title" content="{esc(title)}">'
        f'<meta name="twitter:description" content="{esc(share)}">'
        f'<link rel="icon" href="{FAVICON}">'
        f"<style>{STYLE}</style></head><body><div class=\"wrap\">"
        "<header><h1>ERC-8004 agents on Base: how many actually work</h1>"
        '<p class="sub">Continuous measurement of the Identity Registry. '
        "Registrations are counted from chain state; liveness is re-checked "
        "daily against each agent's current URI.</p>"
        f'<p class="stamp">updated {esc(generated)} UTC</p></header>'
        + "".join(body) + footer +
        "</div></body></html>\n")


# ---------------------------------------------------------------------------
# API files
# ---------------------------------------------------------------------------

def api_documents(doc: dict) -> dict[str, dict]:
    liveness = doc["liveness"]
    meta = {
        "generated_at": doc["generated_at"],
        "chain": doc["registry"]["chain"],
        "chain_id": doc["registry"]["chain_id"],
        "contract": doc["registry"]["contract"],
        "indexed_through_block": doc["registry"]["indexed_through_block"],
        "indexer_last_run": doc["registry"]["indexer_last_run"],
        "probe_run": liveness.get("run"),
        "source": REPO_URL,
        "license": "CC BY 4.0",
    }
    return {
        "summary.json": doc,
        "meta.json": meta,
        "registrations.json": {"generated_at": doc["generated_at"],
                               "monthly": doc["registrations_monthly"],
                               "totals": doc["registry"]},
        "funnel.json": {"generated_at": doc["generated_at"],
                        "run": liveness.get("run"),
                        "by_agent": liveness.get("by_agent"),
                        "by_owner": liveness.get("by_owner"),
                        "by_project": liveness.get("by_project"),
                        "generic_hosts_only": liveness.get("generic_hosts_only"),
                        "endpoints": liveness.get("endpoints"),
                        "censored": liveness.get("censored"),
                        "failures": liveness.get("failures")},
        "survival.json": {"generated_at": doc["generated_at"],
                          "survival": doc["survival"],
                          "cohorts_cross_sectional":
                              liveness.get("cohorts_cross_sectional")},
        "projects.json": {"generated_at": doc["generated_at"],
                          "top_projects": liveness.get("top_projects")},
        "index.json": {"endpoints": ["summary.json", "meta.json",
                                     "registrations.json", "funnel.json",
                                     "survival.json", "projects.json"],
                       "source": REPO_URL, "license": "CC BY 4.0"},
    }


def write_site(doc: dict, out_dir: pathlib.Path) -> list[pathlib.Path]:
    api_dir = out_dir / "api"
    api_dir.mkdir(parents=True, exist_ok=True)
    written = []

    index = out_dir / "index.html"
    index.write_text(build_page(doc), encoding="utf-8")
    written.append(index)

    # Tells GitHub Pages not to run the files through Jekyll.
    nojekyll = out_dir / ".nojekyll"
    nojekyll.write_text("", encoding="utf-8")
    written.append(nojekyll)

    for name, payload in api_documents(doc).items():
        path = api_dir / name
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
        written.append(path)
    return written


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="docs", help="output directory")
    ap.add_argument("--from", dest="source", default=None,
                    help="build from a saved metrics document instead of the database")
    ap.add_argument("--run", type=int, default=None)
    args = ap.parse_args()

    if args.source:
        doc = json.loads(pathlib.Path(args.source).read_text(encoding="utf-8"))
    else:
        with db.connect() as conn:
            doc = metrics.build(conn, args.run)

    out_dir = pathlib.Path(args.out)
    written = write_site(doc, out_dir)

    total = sum(p.stat().st_size for p in written)
    print(f"wrote {len(written)} files into {out_dir}/  ({total / 1024:.0f} KB)")
    for path in written:
        print(f"  {path}  ({path.stat().st_size:,} bytes)")
    print(f"\nopen {out_dir / 'index.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(db.guard(main))
