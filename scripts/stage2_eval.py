"""Stage 2 of GO-NO-GO.md: judge the forward paper run from its logs (no opinions, only the P-criteria).

    python scripts/stage2_eval.py --runs runs --stage1 data/hist4d/results.json

Reads every runs/<ts>/events.jsonl, merges them, and checks:
  P0 elapsed >= 14 days of paper data   P1 trades >= 100
  P2 mean net > 0 and inside the stage-1 95% CI   P3 ARMED share of heartbeats >= 80%
  P4 no traded token that is now a honeypot / has tax > 5% / lost > 90% of its price (rug)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from baselab.research.stats import bootstrap_mean_ci  # noqa: E402


def load_events(runs: Path) -> list[dict]:
    ev = []
    for p in sorted(runs.glob("*/events.jsonl")):
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                ev.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return ev


async def rug_check(pools: set[str], rpc: str) -> dict[str, str]:
    """Re-run the honeypot/tax simulator NOW on every traded token (P4)."""
    if not pools:
        return {}
    from web3 import AsyncWeb3
    from web3.providers.rpc import AsyncHTTPProvider

    from baselab.config import Config, Mode
    from baselab.execution.chain import Web3SecurityReader
    from baselab.security.checks import check_round_trip
    pm = {p["pool"].lower(): p for p in json.loads((ROOT / "pools.json").read_text())}
    w3 = AsyncWeb3(AsyncHTTPProvider(rpc))
    reader = Web3SecurityReader(Config(mode=Mode.PAPER), w3, ROOT / "baselab" / "security" / "simulator.bin")
    head = await w3.eth.block_number
    out = {}
    for pool in pools:
        tok = pm.get(pool, {}).get("token")
        if tok is None:
            out[pool] = "unknown pool"
            continue
        try:
            ok, reasons, _, _ = check_round_trip(await reader.simulate_round_trip(tok, 10**15, head), 0.05, 0.05)
            out[pool] = "ok" if ok else ",".join(reasons)
        except Exception as exc:
            out[pool] = f"sim_error:{type(exc).__name__}"
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=Path, default=ROOT / "runs")
    ap.add_argument("--stage1", type=Path, required=True, help="results.json of the stage-1 backtest")
    ap.add_argument("--rpc", default="https://base-rpc.publicnode.com")
    a = ap.parse_args()
    ev = load_events(a.runs)
    if not ev:
        print("no paper events yet")
        return
    t0 = datetime.fromisoformat(ev[0]["ts"]).timestamp()
    t1 = datetime.fromisoformat(ev[-1]["ts"]).timestamp()
    days = (t1 - t0) / 86400
    exits = [e for e in ev if e["kind"] == "exit"]
    beats = [e for e in ev if e["kind"] == "heartbeat"]
    rets = [float(e["ret"]) for e in exits]
    mean = sum(rets) / len(rets) if rets else float("nan")
    lo, hi = bootstrap_mean_ci(rets)
    s1 = json.loads(a.stage1.read_text())["gate"]["out_of_sample"]
    armed = sum(b["phase"] == "armed" for b in beats) / len(beats) if beats else 0.0
    rugs = asyncio.run(rug_check({e["pool"] for e in exits}, a.rpc))
    bad = {p: r for p, r in rugs.items() if r != "ok"}
    gates = {
        "P0_paper_days>=14": days >= 14,
        "P1_trades>=100": len(rets) >= 100,
        "P2_mean>0_and_within_stage1_CI": bool(rets) and mean > 0 and s1["ci_lo"] <= mean <= s1["ci_hi"],
        "P3_armed_uptime>=80%": armed >= 0.8,
        "P4_no_rugged_or_honeypot_trades": not bad,
    }
    print(f"paper data: {days:.2f} days, {len(ev)} events, {len(beats)} heartbeats")
    print(f"trades={len(rets)} mean_net={mean:+.3%} 95%CI=[{lo:+.3%}, {hi:+.3%}]  "
          f"stage-1 OOS CI=[{s1['ci_lo']:+.3%}, {s1['ci_hi']:+.3%}]  armed_uptime={armed:.0%}")
    if bad:
        print(f"P4 problems: {bad}")
    for k, v in gates.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    if not gates["P0_paper_days>=14"] or not gates["P1_trades>=100"]:
        print("VERDICT: NOT FINISHED (keep the paper run going)")
    else:
        print("VERDICT: stage 3 may be considered (YOUR decision)" if all(gates.values()) else "VERDICT: NO-GO")


if __name__ == "__main__":
    main()
