"""CLI.
    python -m baselab backtest [--blocks N] [--seed S]
    python -m baselab paper --pools pools.json --wss wss://...       (read-only chain, simulated fills)
    python -m baselab live  --pools pools.json --i-understand-risk    (PRIVATE_KEY in env)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import replace
from pathlib import Path

from .config import Config, Mode


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="baselab")
    ap.add_argument("mode", choices=[m.value for m in Mode])
    ap.add_argument("--blocks", type=int, default=20_000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--pools", type=Path)
    ap.add_argument("--wss", default="", help="overrides BASE_WSS_URL")
    ap.add_argument("--http", default="", help="HTTP RPC for heavy reads (security scans); overrides BASE_HTTP_URL")
    ap.add_argument("--equity", type=float, default=0.05, help="WETH allocated to the bot")
    ap.add_argument("--log-dir", type=Path, default=Path("runs"))
    ap.add_argument("--backfill-min", type=int, default=130)
    ap.add_argument("--report-every", type=float, default=600.0, help="seconds between console reports")
    ap.add_argument("--max-rtt-ms", type=float, default=150.0,
                    help="latency gate (spec: 150). A home connection to a free RPC is often 150-200 ms")
    ap.add_argument("--max-notional", type=float, default=None,
                    help="hard cap in WETH per trade; MANDATORY in live mode")
    ap.add_argument("--i-understand-risk", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    mode = Mode(a.mode)
    if mode is Mode.BACKTEST:
        from .backtest.replay import main as bt
        bt(a.blocks, a.seed)
        return 0
    if a.pools is None:
        ap.error("--pools is required for paper/live")
    if mode is Mode.LIVE and not a.i_understand_risk:
        ap.error("live mode requires --i-understand-risk")
    if mode is Mode.LIVE and (a.max_notional is None or a.max_notional <= 0):
        ap.error("live mode requires --max-notional <WETH per trade>")
    if mode is Mode.LIVE and not a.http:
        ap.error("live mode requires --http")
    cfg = Config.from_env(mode)
    chain = replace(cfg.chain, wss_url=a.wss or cfg.chain.wss_url, http_url=a.http or cfg.chain.http_url)
    if not chain.wss_url:
        ap.error("set --wss or BASE_WSS_URL")
    cfg = replace(cfg, chain=chain, security=replace(cfg.security, timeout_s=60.0),
                  latency=replace(cfg.latency, max_rtt_ms=a.max_rtt_ms),
                  risk=replace(cfg.risk, max_notional_quote=a.max_notional or float("inf")))
    from .app import run_live
    try:
        asyncio.run(run_live(cfg, a.pools, a.equity, a.log_dir, a.backfill_min, a.report_every))
    except KeyboardInterrupt:
        print("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
