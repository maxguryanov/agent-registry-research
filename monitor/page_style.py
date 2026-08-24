"""The page's CSS, kept out of publish.py so neither file is mostly the other."""

STYLE = """
:root {
  --bg: #ffffff; --panel: #f7f8fa; --border: #e2e5ea;
  --ink: #14171c; --muted: #5d6470; --faint: #8a919e;
  --accent: #2f6fd0; --accent-soft: #d6e4f7;
  --good: #1c7a4a; --warn: #b4530a;
  --track: #e6e9ee; --band: rgba(47,111,208,0.16);
  --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #0f1218; --panel: #161b23; --border: #262d38;
    --ink: #e8ecf2; --muted: #9aa3b1; --faint: #6f7887;
    --accent: #6ba3f0; --accent-soft: #1d3352;
    --good: #46b47c; --warn: #e08a3c;
    --track: #232a35; --band: rgba(107,163,240,0.20);
  }
}
:root[data-theme="dark"] {
  --bg: #0f1218; --panel: #161b23; --border: #262d38;
  --ink: #e8ecf2; --muted: #9aa3b1; --faint: #6f7887;
  --accent: #6ba3f0; --good: #46b47c; --warn: #e08a3c;
  --track: #232a35; --band: rgba(107,163,240,0.20);
}

* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font: 16px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
        "Helvetica Neue", Arial, sans-serif;
  -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 860px; margin: 0 auto; padding: 40px 20px 80px; }

header { border-bottom: 1px solid var(--border); padding-bottom: 24px; margin-bottom: 8px; }
h1 { font-size: 30px; line-height: 1.25; margin: 0 0 8px; letter-spacing: -0.02em; }
.sub { color: var(--muted); margin: 0 0 14px; font-size: 17px; }
.stamp { color: var(--faint); font-size: 13px; font-family: var(--mono); }

h2 {
  font-size: 13px; text-transform: uppercase; letter-spacing: 0.09em;
  color: var(--muted); margin: 44px 0 4px; font-weight: 600;
}
h2 + .note { margin-top: 0; }
h3 { font-size: 16px; margin: 26px 0 6px; }
p { margin: 10px 0; }
.note { color: var(--muted); font-size: 14.5px; }
a { color: var(--accent); }

.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
         gap: 12px; margin: 22px 0 8px; }
.card { background: var(--panel); border: 1px solid var(--border);
        border-radius: 10px; padding: 14px 16px; }
.card .k { font-size: 12px; text-transform: uppercase; letter-spacing: 0.06em;
           color: var(--muted); }
.card .v { font-size: 27px; font-weight: 650; letter-spacing: -0.02em;
           margin-top: 4px; font-variant-numeric: tabular-nums; }
.card .c { font-size: 12.5px; color: var(--faint); font-family: var(--mono);
           margin-top: 2px; }

.panel { background: var(--panel); border: 1px solid var(--border);
         border-radius: 10px; padding: 16px 18px; margin: 16px 0; }
.panel.warn { border-color: var(--warn); }
.panel h3 { margin-top: 0; }

.scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; margin: 14px 0; }
table { border-collapse: collapse; width: 100%; font-size: 14.5px;
        font-variant-numeric: tabular-nums; }
th, td { text-align: right; padding: 7px 10px; border-bottom: 1px solid var(--border);
         white-space: nowrap; }
th:first-child, td:first-child { text-align: left; }
thead th { color: var(--muted); font-weight: 600; font-size: 12.5px;
           text-transform: uppercase; letter-spacing: 0.05em; }
tbody tr:last-child td { border-bottom: none; }
tr.total td { font-weight: 650; }
td.ci { color: var(--faint); font-family: var(--mono); font-size: 13px; }
code { font-family: var(--mono); font-size: 13.5px;
       background: var(--panel); border: 1px solid var(--border);
       border-radius: 4px; padding: 1px 5px; }

.chart { display: block; max-width: 100%; height: auto; margin: 8px 0 4px; }
.chart .lbl { fill: var(--ink); font-size: 13px; }
.chart .val { fill: var(--muted); font-size: 12.5px; font-family: var(--mono); }
.chart .axis { fill: var(--faint); font-size: 11.5px; font-family: var(--mono); }
.chart .axis.alt { fill: var(--accent); }
.chart .track { fill: var(--track); }
.chart .bar { fill: var(--accent); }
.chart .ci { stroke: var(--ink); stroke-width: 1.4; opacity: 0.65; }
.chart .grid { stroke: var(--border); stroke-width: 1; }
.chart .line { fill: none; stroke: var(--accent); stroke-width: 2;
               stroke-linejoin: round; }
.chart .band { fill: var(--band); stroke: none; }
.chart .dot { fill: var(--accent); }
.empty { color: var(--faint); font-style: italic; }

footer { margin-top: 60px; padding-top: 20px; border-top: 1px solid var(--border);
         color: var(--muted); font-size: 14px; }
ul { padding-left: 20px; }
li { margin: 6px 0; }
@media (max-width: 620px) {
  h1 { font-size: 24px; }
  .wrap { padding: 26px 16px 60px; }
}
"""
