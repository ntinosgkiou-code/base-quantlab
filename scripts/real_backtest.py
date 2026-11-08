"""Stage 1 of GO-NO-GO.md: backtest the UNCHANGED spec strategy on real historical Base swaps.

    python scripts/real_backtest.py --data data/hist3d [--notional 0.05] [--haircut 0.001]

Prints per-pool results, the in-sample / out-of-sample statistics, the gate verdict and the shadow
ledger (value of each filter). Writes <data>/results.json and <data>/trades.csv.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from baselab.app import GAS_ORACLE, GAS_ORACLE_ABI, GAS_UNITS  # noqa: E402
from baselab.backtest.real_replay import Dataset, load_dataset, simulate_pool  # noqa: E402
from baselab.config import Config, Mode  # noqa: E402
from baselab.research.shadow import ShadowLedger  # noqa: E402
from baselab.research.stats import evaluate_gate  # noqa: E402


async def measure_gas_round_trip(rpc: str) -> float:
    """Current cost of entry + exit (each: approve + swap + revoke) incl. the OP-stack L1 data fee."""
    from web3 import AsyncWeb3
    from web3.providers.rpc import AsyncHTTPProvider
    w3 = AsyncWeb3(AsyncHTTPProvider(rpc))
    oracle = w3.eth.contract(address=w3.to_checksum_address(GAS_ORACLE), abi=GAS_ORACLE_ABI)
    gas_price = await w3.eth.gas_price
    l1 = await oracle.functions.getL1Fee(b"\x01" * 180).call()
    return 2 * (gas_price * GAS_UNITS + 3 * l1) / 1e18


def _job(args: tuple) -> tuple:
    sub, pm, cfg, notional, gas, haircut, kind = args
    sh = ShadowLedger(horizon_s=180)
    r = simulate_pool(sub, pm, cfg, notional, gas, haircut, sh, kind)
    return r, dict(sh.resolved)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--notional", type=float, default=0.05, help="WETH per trade (fixed, edge measurement)")
    ap.add_argument("--haircut", type=float, default=0.001, help="extra adverse slippage per fill")
    ap.add_argument("--gas", type=float, default=None, help="WETH per round trip; default = measured now")
    ap.add_argument("--rpc", default="https://base-rpc.publicnode.com")
    ap.add_argument("--max-cost-to-atr", type=float, default=None, help="v2 cost filter (GO-NO-GO stage 1c: 0.5)")
    ap.add_argument("--strategy", default="matrix", choices=["matrix", "rsi", "breakout"])
    ap.add_argument("--alpha", type=float, default=0.05, help="CI level (stage 1d: 0.025)")
    ap.add_argument("--workers", type=int, default=1, help="parallel pools (each worker holds one pool's logs)")
    a = ap.parse_args()

    cfg = Config(mode=Mode.BACKTEST)
    if a.max_cost_to_atr is not None:
        cfg = replace(cfg, strategy=replace(cfg.strategy, max_cost_to_atr=a.max_cost_to_atr))
        print(f"strategy v2: max_cost_to_atr = {a.max_cost_to_atr}")
    gas = a.gas if a.gas is not None else asyncio.run(measure_gas_round_trip(a.rpc))
    ds = load_dataset(a.data, require_block_times=True)
    t_start, t_end = ds.ts_of(ds.from_block), ds.ts_of(ds.done_block)
    print(f"strategy: {a.strategy}  CI alpha={a.alpha}")
    print(f"data: {(t_end - t_start) / 3600:.1f} h, {sum(len(v) for v in ds.logs.values())} logs, "
          f"{len(ds.pools)} pools | notional {a.notional} WETH | gas/round trip {gas:.8f} WETH "
          f"({gas / a.notional:.3%} of notional) | haircut {a.haircut:.2%}/fill\n")

    shadow = ShadowLedger(horizon_s=180)
    trades = []
    print(f"{'pool':<12}{'kind':<5}{'swaps':>7}{'evals':>7}{'signals':>8}{'trades':>7}{'skip_imp':>9}"
          f"{'mean_net':>10}  top rejection reasons")
    jobs = [(Dataset(ds.anchor_block, ds.anchor_ts, ds.block_time, ds.from_block, ds.done_block, [pm],
                     {pm["pool"]: ds.logs.get(pm["pool"], [])}, ds.knot_blocks, ds.knot_ts),
             pm, cfg, a.notional, gas, a.haircut, a.strategy) for pm in ds.pools]
    with ProcessPoolExecutor(max_workers=max(1, a.workers)) as ex:
        results = list(ex.map(_job, jobs))
    for pm, (r, resolved) in zip(ds.pools, results):
        for group, xs in resolved.items():
            shadow.resolved[group].extend(xs)
        trades += r.trades
        mean = sum(t["net_ret"] for t in r.trades) / len(r.trades) if r.trades else float("nan")
        top = sorted(r.reject_reasons.items(), key=lambda kv: -kv[1])[:3]
        print(f"{r.symbol[:11]:<12}{'v3' if pm['is_v3'] else 'v2':<5}{r.swaps:>7}{r.evaluations:>7}{r.signals:>8}"
              f"{len(r.trades):>7}{r.skipped_impact:>9}{mean:>10.3%}  {top}")

    g = evaluate_gate(trades, t_start, t_end, alpha=a.alpha)
    print("\n== STAGE 1 GATE (GO-NO-GO.md) ==")
    for name, s in (("in-sample", g["in_sample"]), ("out-of-sample", g["out_of_sample"])):
        print(f"{name:<14} n={s['n']:<5} mean_net={s['mean_net_ret']:+.3%}  95%CI=[{s['ci_lo']:+.3%}, {s['ci_hi']:+.3%}]"
              f"  win={s['win_rate']:.0%}  PF={s['profit_factor']:.2f}  mean_gross={s['mean_gross_ret']:+.3%}")
    print(f"circuit-breaker halts/day: {g['halts_per_day']:.1f}")
    for k, v in g["gates"].items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    print(f"VERDICT: {g['verdict']}\n")
    print("== SHADOW LEDGER (3-min forward return of a hypothetical buy at each evaluation, gross) ==")
    print(shadow.report())

    (a.data / "results.json").write_text(json.dumps({"gate": g, "shadow": shadow.summary(), "gas": gas,
                                                     "notional": a.notional, "haircut": a.haircut}, indent=2,
                                                    default=str))
    if trades:
        with (a.data / "trades.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(trades[0]))
            w.writeheader()
            w.writerows(trades)


if __name__ == "__main__":
    main()
