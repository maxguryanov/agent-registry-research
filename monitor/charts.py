#!/usr/bin/env python3
"""
Charts as inline SVG, generated in Python.

No JavaScript, no charting library, no external requests. The page has to
survive being opened years from now, on a locked-down network, from a cached
copy, so everything it needs is in the file.

Colours come from CSS custom properties defined by the page, so the charts
follow the reader's light or dark theme without a second set of drawing code.
"""

from __future__ import annotations

import html


def esc(text) -> str:
    return html.escape(str(text), quote=True)


def _nice_ceiling(value: float) -> float:
    """Round an axis maximum up to something a human would have chosen."""
    if value <= 0:
        return 1
    magnitude = 10 ** (len(str(int(value))) - 1)
    for step in (1, 1.5, 2, 2.5, 3, 4, 5, 7.5, 10):
        if value <= step * magnitude:
            return step * magnitude
    return 10 * magnitude


def funnel_chart(stages: list[dict], width: int = 720,
                 row_height: int = 34, label_width: int = 200) -> str:
    """
    Horizontal bars with the confidence interval drawn on top of each bar.

    The interval is the point of the chart. A stage measured on sixty agents
    and one measured on two thousand look identical as bars and are not the
    same claim.
    """
    if not stages:
        return ""
    height = row_height * len(stages) + 46
    bar_width = width - label_width - 74
    parts = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'role="img" aria-label="Funnel" class="chart">'
    ]
    for index, stage in enumerate(stages):
        y = index * row_height + 10
        pct = stage.get("pct") or 0
        filled = bar_width * pct / 100
        parts.append(
            f'<text x="{label_width - 10}" y="{y + 15}" text-anchor="end" '
            f'class="lbl">{esc(stage["label"])}</text>')
        parts.append(
            f'<rect x="{label_width}" y="{y + 4}" width="{bar_width}" '
            f'height="16" rx="3" class="track"/>')
        parts.append(
            f'<rect x="{label_width}" y="{y + 4}" width="{filled:.1f}" '
            f'height="16" rx="3" class="bar"/>')
        if stage.get("ci_low") is not None:
            low = label_width + bar_width * stage["ci_low"] / 100
            high = label_width + bar_width * stage["ci_high"] / 100
            mid = y + 12
            parts.append(
                f'<line x1="{low:.1f}" y1="{mid}" x2="{high:.1f}" y2="{mid}" '
                f'class="ci"/>'
                f'<line x1="{low:.1f}" y1="{mid - 4}" x2="{low:.1f}" '
                f'y2="{mid + 4}" class="ci"/>'
                f'<line x1="{high:.1f}" y1="{mid - 4}" x2="{high:.1f}" '
                f'y2="{mid + 4}" class="ci"/>')
        value = "n/a" if stage.get("pct") is None else f'{stage["pct"]:.1f}%'
        parts.append(
            f'<text x="{label_width + bar_width + 8}" y="{y + 16}" '
            f'class="val">{value}</text>')
    parts.append(
        f'<text x="{label_width}" y="{height - 12}" class="axis">0%</text>'
        f'<text x="{label_width + bar_width}" y="{height - 12}" '
        f'text-anchor="end" class="axis">100%</text>')
    parts.append("</svg>")
    return "".join(parts)


def registrations_chart(months: list[dict], width: int = 720,
                        height: int = 260) -> str:
    """Monthly bars with the cumulative total as a line on a second scale."""
    if not months:
        return '<p class="empty">No registrations indexed yet.</p>'

    left, right, top, bottom = 52, 52, 16, 38
    plot_w = width - left - right
    plot_h = height - top - bottom
    max_month = _nice_ceiling(max(m["registered"] for m in months))
    max_total = _nice_ceiling(max(m["cumulative"] for m in months))
    slot = plot_w / len(months)
    bar_w = min(46, slot * 0.62)

    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
             f'role="img" aria-label="Registrations per month" class="chart">']

    for fraction in (0, 0.25, 0.5, 0.75, 1):
        y = top + plot_h * (1 - fraction)
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" '
                     f'y2="{y:.1f}" class="grid"/>')
        parts.append(f'<text x="{left - 8}" y="{y + 4:.1f}" text-anchor="end" '
                     f'class="axis">{int(max_month * fraction):,}</text>')

    for index, month in enumerate(months):
        x = left + slot * index + (slot - bar_w) / 2
        bar_h = plot_h * month["registered"] / max_month
        parts.append(
            f'<rect x="{x:.1f}" y="{top + plot_h - bar_h:.1f}" '
            f'width="{bar_w:.1f}" height="{bar_h:.1f}" rx="2" class="bar">'
            f'<title>{esc(month["month"])}: {month["registered"]:,} registered'
            f'</title></rect>')
        if len(months) <= 18 or index % 2 == 0:
            parts.append(
                f'<text x="{x + bar_w / 2:.1f}" y="{height - 14}" '
                f'text-anchor="middle" class="axis">'
                f'{esc(month["month"][2:])}</text>')

    points = " ".join(
        f'{left + slot * i + slot / 2:.1f},'
        f'{top + plot_h * (1 - m["cumulative"] / max_total):.1f}'
        for i, m in enumerate(months))
    parts.append(f'<polyline points="{points}" class="line"/>')
    for fraction in (0.5, 1):
        y = top + plot_h * (1 - fraction)
        parts.append(f'<text x="{left + plot_w + 8}" y="{y + 4:.1f}" '
                     f'class="axis alt">{int(max_total * fraction):,}</text>')
    parts.append("</svg>")
    return "".join(parts)


def cohort_chart(cohorts: list[dict], width: int = 720, height: int = 260,
                 label: str = "live") -> str:
    """Percentage per cohort, with the confidence band drawn behind the line."""
    usable = [c for c in cohorts if c.get("pct") is not None]
    if len(usable) < 2:
        return ('<p class="empty">Not enough cohorts to draw a curve yet.</p>')

    left, right, top, bottom = 52, 20, 16, 38
    plot_w = width - left - right
    plot_h = height - top - bottom
    ceiling = _nice_ceiling(max(c["ci_high"] for c in usable))
    slot = plot_w / max(1, len(usable) - 1)

    def x_of(i: int) -> float:
        return left + slot * i

    def y_of(pct: float) -> float:
        return top + plot_h * (1 - pct / ceiling)

    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
             f'role="img" aria-label="Liveness by cohort" class="chart">']
    for fraction in (0, 0.25, 0.5, 0.75, 1):
        y = top + plot_h * (1 - fraction)
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" '
                     f'y2="{y:.1f}" class="grid"/>')
        parts.append(f'<text x="{left - 8}" y="{y + 4:.1f}" text-anchor="end" '
                     f'class="axis">{ceiling * fraction:.0f}%</text>')

    upper = " ".join(f"{x_of(i):.1f},{y_of(c['ci_high']):.1f}"
                     for i, c in enumerate(usable))
    lower = " ".join(f"{x_of(i):.1f},{y_of(c['ci_low']):.1f}"
                     for i, c in reversed(list(enumerate(usable))))
    parts.append(f'<polygon points="{upper} {lower}" class="band"/>')
    parts.append('<polyline points="' + " ".join(
        f"{x_of(i):.1f},{y_of(c['pct']):.1f}" for i, c in enumerate(usable))
        + '" class="line"/>')

    for i, cohort in enumerate(usable):
        parts.append(
            f'<circle cx="{x_of(i):.1f}" cy="{y_of(cohort["pct"]):.1f}" r="3.5" '
            f'class="dot"><title>{esc(cohort["month"])}: {cohort["k"]:,} of '
            f'{cohort["n"]:,} {esc(label)} ({cohort["pct"]:.1f}%, CI '
            f'{cohort["ci_low"]:.1f}-{cohort["ci_high"]:.1f})</title></circle>')
        if len(usable) <= 18 or i % 2 == 0:
            parts.append(f'<text x="{x_of(i):.1f}" y="{height - 14}" '
                         f'text-anchor="middle" class="axis">'
                         f'{esc(cohort["month"][2:])}</text>')
    parts.append("</svg>")
    return "".join(parts)
