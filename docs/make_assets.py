"""Regenerates the README graphics (docs/assets/*.svg). Plain Python, no dependencies.

    python docs/make_assets.py

The results chart is drawn from the numbers in docs/RESULTS.md: edit RESULTS below if you re-run an experiment.
"""
from __future__ import annotations

import html
from pathlib import Path

OUT = Path(__file__).resolve().parent / "assets"
OUT.mkdir(exist_ok=True)
FONT = "Inter, 'Segoe UI', Helvetica, Arial, sans-serif"
MONO = "'JetBrains Mono', 'SFMono-Regular', Consolas, monospace"


def banner() -> str:
    chips = [("6", "strategy families"), ("14", "pre-registered configs"), ("0", "passed the gate"), ("55", "tests"), ("13/13", "fork checks")]
    x, parts = 64, []
    for big, small in chips:
        w = 36 + 11 * len(small) * 0.62 + 18 * len(big) * 0.5
        w = max(w, 150)
        parts.append(f'<rect x="{x}" y="206" width="{w:.0f}" height="74" rx="12" fill="#ffffff" fill-opacity="0.07" stroke="#ffffff" stroke-opacity="0.16"/>'
                     f'<text x="{x + 18}" y="244" font-family="{FONT}" font-size="30" font-weight="700" fill="#ffffff">{big}</text>'
                     f'<text x="{x + 18}" y="266" font-family="{FONT}" font-size="13" fill="#9fb3d1">{small}</text>')
        x += w + 14
    # decorative price path (deterministic)
    pts = [(900, 130), (930, 112), (960, 150), (990, 96), (1020, 138), (1050, 84), (1080, 124), (1110, 70), (1140, 108), (1170, 60), (1200, 92)]
    line = " ".join(f"{a},{b}" for a, b in pts)
    candles = "".join(
        f'<line x1="{a}" y1="{b - 18}" x2="{a}" y2="{b + 22}" stroke="{"#34d399" if i % 2 == 0 else "#f87171"}" stroke-width="2" stroke-opacity="0.85"/>'
        f'<rect x="{a - 6}" y="{b - 8}" width="12" height="20" rx="2" fill="{"#34d399" if i % 2 == 0 else "#f87171"}" fill-opacity="0.85"/>'
        for i, (a, b) in enumerate(pts))
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 320" width="1280" height="320" role="img" aria-label="base-strategy-lab">
<defs><linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#0b1220"/><stop offset="1" stop-color="#16294d"/></linearGradient></defs>
<rect width="1280" height="320" rx="18" fill="url(#bg)"/>
<polyline points="{line}" fill="none" stroke="#60a5fa" stroke-width="2" stroke-opacity="0.5"/>{candles}
<text x="64" y="84" font-family="{MONO}" font-size="14" fill="#60a5fa" letter-spacing="2">BASE · UNISWAP · AERODROME</text>
<text x="64" y="138" font-family="{FONT}" font-size="54" font-weight="800" fill="#ffffff">base-strategy-lab</text>
<text x="64" y="176" font-family="{FONT}" font-size="21" fill="#c7d6ee">Pre-registered testing and execution infrastructure for on-chain trading strategies.</text>
{"".join(parts)}
</svg>'''


def box(x, y, w, h, title, lines, fill="#eef4ff", stroke="#3b6fd8"):
    t = "".join(f'<text x="{x + 14}" y="{y + 46 + 18 * i}" font-family="{FONT}" font-size="12.5" fill="#33415c">{l}</text>' for i, l in enumerate(lines))
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>'
            f'<text x="{x + 14}" y="{y + 26}" font-family="{FONT}" font-size="15" font-weight="700" fill="#14233f">{title}</text>{t}')


def architecture() -> str:
    arrows = ""
    for x1, y1, x2, y2 in ((250, 150, 292, 150), (542, 150, 584, 150), (834, 150, 876, 150), (710, 230, 710, 268), (460, 230, 460, 268)):
        arrows += f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="#14233f" stroke-width="2" marker-end="url(#a)"/>'
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1130 560" width="1130" height="560" role="img" aria-label="architecture">
<defs><marker id="a" markerWidth="10" markerHeight="8" refX="9" refY="4" orient="auto"><path d="M0,0 L10,4 L0,8 z" fill="#14233f"/></marker></defs>
<rect width="1130" height="560" rx="14" fill="#ffffff" stroke="#d5dded"/>
<text x="24" y="36" font-family="{FONT}" font-size="18" font-weight="800" fill="#14233f">How the pieces fit</text>
{box(24, 70, 226, 160, "Ingestion", ["WSS new heads, batched logs", "parent-hash reorg guard", "no-lookahead 1-min candles", "zero-gap fill, gap backfill"])}
{box(292, 70, 250, 160, "Event bus", ["typed topics, bounded queues", "overflow = stale data", "=> freeze (never drop)"], fill="#f4f1ff", stroke="#7c5cd6")}
{box(584, 70, 250, 160, "Strategy matrix", ["Hurst (R/S, bias-corrected)", "VWAP / ATR / flow imbalance", "pure functions, unit-tested"])}
{box(876, 70, 230, 160, "Execution router", ["re-checks at time of use", "approve exact, swap, approve 0", "eth_simulateV1 before send"], fill="#eafaf1", stroke="#2f9e6b")}
{box(334, 268, 250, 130, "Security validator", ["honeypot / tax by eth_call", "state-override simulator", "LP lock, owner, proxy, holders", "fail closed on any doubt"], fill="#fff4e8", stroke="#e08a2d")}
{box(626, 268, 250, 130, "Risk", ["fractional Kelly, prior = no edge", "circuit breaker, daily 3% sleep", "kill switch, max notional"], fill="#fff0f0", stroke="#d64545")}
{box(24, 268, 280, 130, "System state machine", ["BOOTING / ARMED / FROZEN", "SLEEPING / HALTED", "isolated, timed safety handlers", "RTT gate 150 ms, WS gap 500 ms"], fill="#f3f4f6", stroke="#6b7280")}
{box(24, 424, 1082, 112, "Research lab (what the results come from)", ["pre-registered gates (GO-NO-GO) · replay on real Base logs with exact block timestamps · cross-DEX arbitrage tracker (closed form)",
     "15-min swing backtester · day-clustered bootstrap · shadow ledger (value of each filter) · funding-carry measurement · local mainnet-fork test with fake ETH"], fill="#eef4ff", stroke="#3b6fd8")}
{arrows}
</svg>'''


# (label, mean net return per trade, n, note) ; out-of-sample or validation window; see docs/RESULTS.md
RESULTS = [
    ("1-min multi-factor (spec)", -2.14, 35, ""),
    ("variant V2", -2.22, 21, ""), ("variant V3", -2.22, 22, ""), ("variant V4", -2.17, 23, ""), ("variant V5", -2.18, 34, ""),
    ("variant V6 (loosest)", -1.55, 76, ""),
    ("RSI(14) < 20 reversion", -0.66, 51, "CI touches 0"),
    ("volume breakout", -0.32, 94, ""),
    ("15-min swing, arm M", 2.13, 16, "CI spans 0, ex-top-3: -2.5%"),
    ("15-min swing, arm L", -1.51, 8, ""), ("15-min swing, arm T", -1.36, 17, ""),
]


def results() -> str:
    left, right, top, row = 250, 930, 92, 34
    scale = (right - left) / 6.0   # -3% .. +3%
    zero = left + 3.0 * scale
    h = top + row * len(RESULTS) + 74
    rows = ""
    for i, (label, val, n, note) in enumerate(RESULTS):
        y = top + i * row
        bar_w = abs(val) * scale
        x = zero if val >= 0 else zero - bar_w
        sig = note.startswith("CI spans")
        fill = "#9aa5b8" if sig else ("#d64545" if val < 0 else "#2f9e6b")
        opacity = ' fill-opacity="0.55"' if sig else ""
        rows += (f'<text x="{left - 14}" y="{y + 20}" text-anchor="end" font-family="{FONT}" font-size="13.5" fill="#14233f">{html.escape(label)}</text>'
                 f'<rect x="{x:.1f}" y="{y + 5}" width="{bar_w:.1f}" height="22" rx="4" fill="{fill}"{opacity}/>'
                 f'<text x="{(x + bar_w + 8) if val >= 0 else (x - 8):.1f}" y="{y + 21}" text-anchor="{"start" if val >= 0 else "end"}" font-family="{MONO}" font-size="12.5" fill="#33415c">{val:+.2f}%  n={n}</text>')
        if note:
            rows += f'<text x="{right + 12}" y="{y + 21}" font-family="{FONT}" font-size="12" fill="#6b7280">{html.escape(note)}</text>'
    grid = "".join(f'<line x1="{left + k * scale:.1f}" y1="{top - 8}" x2="{left + k * scale:.1f}" y2="{top + row * len(RESULTS)}" stroke="#e3e8f2"/>'
                   f'<text x="{left + k * scale:.1f}" y="{top + row * len(RESULTS) + 20}" text-anchor="middle" font-family="{MONO}" font-size="11.5" fill="#6b7280">{k - 3:+d}%</text>' for k in range(0, 7))
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1130 {h}" width="1130" height="{h}" role="img" aria-label="results">
<rect width="1130" height="{h}" rx="14" fill="#ffffff" stroke="#d5dded"/>
<text x="24" y="34" font-family="{FONT}" font-size="18" font-weight="800" fill="#14233f">Net return per trade, out-of-sample (real Base data, fees, delay and gas included)</text>
<text x="24" y="56" font-family="{FONT}" font-size="13" fill="#6b7280">Variants V2-V6 are shown on their validation window. Grey = not statistically different from zero.</text>
{grid}<line x1="{zero:.1f}" y1="{top - 8}" x2="{zero:.1f}" y2="{top + row * len(RESULTS)}" stroke="#14233f" stroke-width="1.5"/>
{rows}
<text x="24" y="{h - 16}" font-family="{FONT}" font-size="12.5" fill="#6b7280">No configuration passed its pre-registered gate. The uncontested arbitrage and funding-carry measurements are in docs/RESULTS.md.</text>
</svg>'''


if __name__ == "__main__":
    for name, fn in (("banner.svg", banner), ("architecture.svg", architecture), ("results.svg", results)):
        (OUT / name).write_text(fn(), encoding="utf-8")
        print("wrote", OUT / name)
