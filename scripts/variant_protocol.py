"""Stage 1b of GO-NO-GO.md: the ONE pre-registered variant test (discovery / validation / holdout).

    python scripts/variant_protocol.py --data data/hist4d

The variant table and the selection rule are fixed in GO-NO-GO.md; this script only executes them.
The holdout result is printed for the single selected variant and nothing else.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from baselab.backtest.real_replay import Dataset, load_dataset, simulate_pool  # noqa: E402
from baselab.config import Config, Mode  # noqa: E402
from baselab.research.shadow import ShadowLedger  # noqa: E402
from baselab.research.stats import bootstrap_mean_ci, breaker_halts, profit_factor  # noqa: E402
from scripts.real_backtest import measure_gas_round_trip  # noqa: E402

VARIANTS = {  # copied verbatim from GO-NO-GO.md stage 1b; do not edit after results
    "V1": {},
    "V2": {"hurst_margin": 0.0},
    "V3": {"hurst_margin": 0.0, "min_vol_expansion": 1.0},
    "V4": {"hurst_margin": 0.0, "min_vol_expansion": 1.0, "min_ofi": 0.1},
    "V5": {"hurst_margin": 0.0, "min_vol_expansion": 1.0, "min_ofi": 0.1, "dev_min": 0.0, "dev_max": 3.0},
    "V6": {"hurst_margin": 0.0, "min_vol_expansion": 1.0, "min_ofi": 0.1, "dev_min": 0.0, "dev_max": 3.0,
           "atr_floor": 0.001},
}


def mean(x: list[float]) -> float:
    return sum(x) / len(x) if x else float("nan")


def _job(args: tuple) -> tuple[str, list]:
    name, sub, pm, notional, gas, haircut = args
    base = Config(mode=Mode.BACKTEST)
    cfg = replace(base, strategy=replace(base.strategy, **VARIANTS[name]))
    return name, simulate_pool(sub, pm, cfg, notional, gas, haircut, ShadowLedger()).trades


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--notional", type=float, default=0.05)
    ap.add_argument("--haircut", type=float, default=0.001)
    ap.add_argument("--gas", type=float, default=None)
    ap.add_argument("--rpc", default="https://base-rpc.publicnode.com")
    a = ap.parse_args()
    gas = a.gas if a.gas is not None else asyncio.run(measure_gas_round_trip(a.rpc))
    ds = load_dataset(a.data, require_block_times=True)
    t0, t1 = ds.ts_of(ds.from_block), ds.ts_of(ds.done_block)
    d_end, v_end = t0 + 0.5 * (t1 - t0), t0 + 0.75 * (t1 - t0)
    # one job per (variant, pool); each worker receives ONLY that pool's logs (low RAM machine)
    jobs = []
    for name in VARIANTS:
        for pm in ds.pools:
            sub = Dataset(ds.anchor_block, ds.anchor_ts, ds.block_time, ds.from_block, ds.done_block, [pm],
                          {pm["pool"]: ds.logs.get(pm["pool"], [])}, ds.knot_blocks, ds.knot_ts)
            jobs.append((name, sub, pm, a.notional, gas, a.haircut))
    all_trades: dict[str, list] = {n: [] for n in VARIANTS}
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        for done, (name, trades) in enumerate(ex.map(_job, jobs), 1):
            all_trades[name] += trades
            if done % 16 == 0:
                print(f"  {done}/{len(jobs)} jobs", flush=True)
    rows, trades_by_v = [], {}
    for name in VARIANTS:
        trades = all_trades[name]
        trades_by_v[name] = trades
        disc = [t["net_ret"] for t in trades if t["entry_ts"] < d_end]
        val = [t["net_ret"] for t in trades if d_end <= t["entry_ts"] < v_end]
        rows.append((name, len(disc), mean(disc), len(val), mean(val)))
        print(f"{name}: discovery n={len(disc):<5} mean={mean(disc):+.3%} | validation n={len(val):<5} "
              f"mean={mean(val):+.3%}", flush=True)
    eligible = [r for r in rows if r[1] >= 50 and r[2] > 0 and r[3] > 0]
    if not eligible:
        print("\nNo variant qualifies (discovery n>=50 and mean>0, with validation trades). "
              "VERDICT: NO-GO for this strategy family.")
        return
    chosen = max(eligible, key=lambda r: r[4])[0]
    hold = [t for t in trades_by_v[chosen] if t["entry_ts"] >= v_end]
    r = [t["net_ret"] for t in hold]
    lo, hi = bootstrap_mean_ci(r, alpha=0.05 / 6)
    days = (t1 - v_end) / 86400
    halts = breaker_halts(hold) / days if days > 0 else float("inf")
    gates = {"H1_trades>=100": len(r) >= 100, "H2_mean>0": mean(r) > 0, "H3_ci99.2_lo>0": lo > 0,
             "H4_pf>=1.2": profit_factor(r) >= 1.2 if r else False, "H5_halts/day<=1": halts <= 1}
    print(f"\nselected (by validation, before looking at holdout): {chosen} {VARIANTS[chosen]}")
    print(f"HOLDOUT ({days:.1f} days): n={len(r)} mean={mean(r):+.3%} 99.2%CI=[{lo:+.3%}, {hi:+.3%}] "
          f"PF={profit_factor(r):.2f} halts/day={halts:.1f}")
    for k, v in gates.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    verdict = "GO to stage 2 with " + chosen if all(gates.values()) else "NO-GO (definitive for this family)"
    print(f"VERDICT: {verdict}")
    (a.data / "variant_results.json").write_text(json.dumps(
        {"rows": rows, "chosen": chosen, "holdout_gates": gates, "verdict": verdict,
         "holdout": {"n": len(r), "mean": mean(r), "ci": [lo, hi]}}, indent=2, default=str))


if __name__ == "__main__":
    main()
