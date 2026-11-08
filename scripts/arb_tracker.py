"""Stage 1e of GO-NO-GO.md: cross-DEX (Uniswap <-> Aerodrome) arbitrage measured on real history.

    python scripts/arb_tracker.py --root data/arb

<root>/<token>/ = a collect_history.py output for ONE token (its Uniswap + Aerodrome pools, same block window).
No parameter is tuned: delays, caps and gas model are fixed in GO-NO-GO.md.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from baselab.app import GAS_ORACLE, GAS_ORACLE_ABI  # noqa: E402
from baselab.backtest.arb import Attempt, aligned, episode_durations, pool_states, track_pair  # noqa: E402
from baselab.backtest.real_replay import load_dataset  # noqa: E402
from baselab.research.stats import bootstrap_mean_ci, profit_factor  # noqa: E402

GAS_UNITS_ATOMIC = 350_000   # two swaps + transfers inside one contract call (pre-registered)
TX_BYTES = 300
DELAYS = (0, 1, 2, 3, 5)
CAPS = (0.01, 0.05, 2.0)


async def measure_gas(rpc: str) -> float:
    from web3 import AsyncWeb3
    from web3.providers.rpc import AsyncHTTPProvider
    w3 = AsyncWeb3(AsyncHTTPProvider(rpc))
    oracle = w3.eth.contract(address=w3.to_checksum_address(GAS_ORACLE), abi=GAS_ORACLE_ABI)
    gas_price = await w3.eth.gas_price
    l1 = await oracle.functions.getL1Fee(b"\x01" * TX_BYTES).call()
    return (gas_price * GAS_UNITS_ATOMIC + l1) / 1e18


def summarize(net: list[float]) -> dict[str, float]:
    if not net:
        return {"n": 0, "mean": float("nan"), "lo": float("nan"), "hi": float("nan"), "total": 0.0,
                "pf": float("nan"), "hit": float("nan")}
    lo, hi = bootstrap_mean_ci(net)
    return {"n": len(net), "mean": float(np.mean(net)), "lo": lo, "hi": hi, "total": float(np.sum(net)),
            "pf": profit_factor(net), "hit": sum(v > 0 for v in net) / len(net)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--gas", type=float, default=None, help="WETH per atomic tx (default: measured now)")
    ap.add_argument("--rpc", default="https://base-rpc.publicnode.com")
    a = ap.parse_args()
    gas = a.gas if a.gas is not None else asyncio.run(measure_gas(a.rpc))
    all_att: dict[tuple[float, int], list[Attempt]] = {(c, d): [] for c in CAPS for d in DELAYS}
    lo_blk, hi_blk, block_time = None, None, 0.2
    stress: dict[str, list[Attempt]] = {"S3_priority_fee_50%": [], "S4_worst_intra_block": [], "S5_aero_cl_fee_x1.5": []}
    durations: list[int] = []
    for tok_dir in sorted(p for p in a.root.iterdir() if (p / "meta.json").exists()):
        ds = load_dataset(tok_dir)
        lo_blk = ds.from_block if lo_blk is None else min(lo_blk, ds.from_block)
        hi_blk = ds.done_block if hi_blk is None else max(hi_blk, ds.done_block)
        block_time = ds.block_time
        pools = {p["pool"]: p for p in ds.pools}
        states = {addr: pool_states(ds, pm) for addr, pm in pools.items()}
        union = np.unique(np.concatenate([s[0] for s in states.values() if len(s[0])] or [np.empty(0, dtype=np.int64)]))
        if len(union) == 0:
            continue
        al = {addr: aligned(union, *s) for addr, s in states.items()}
        unis = [p for p in pools.values() if p["dex"] == "uniswap"]
        aeros = [p for p in pools.values() if p["dex"] == "aerodrome"]
        for pu in unis:
            for pa in aeros:
                name = f"{pu['symbol']}:{pu['kind']}{pu['fee_ppm'] / 1e4:g}%/{pa['kind']}{pa['fee_ppm'] / 1e4:g}%"
                U = (al[pu["pool"]][0], al[pu["pool"]][1], pu["fee_ppm"] / 1e6)
                A = (al[pa["pool"]][0], al[pa["pool"]][1], pa["fee_ppm"] / 1e6)
                for c in CAPS:
                    for d in DELAYS:
                        all_att[(c, d)] += track_pair(name, union, U, A, gas, c, d)
                # stress scenarios (GO-NO-GO.md S3-S5), cap 2 WETH, delay 2
                aero_fee_mult = 1.5 if pa["kind"] == "cl" else 1.0
                A_hi = (A[0], A[1], A[2] * aero_fee_mult)
                stress["S3_priority_fee_50%"] += track_pair(name, union, U, A, gas, 2.0, 2, bid_frac=0.5)
                stress["S4_worst_intra_block"] += track_pair(name, union, U, A, gas, 2.0, 2, intra="worst")
                stress["S5_aero_cl_fee_x1.5"] += track_pair(name, union, U, A_hi, gas, 2.0, 2)
                durations += episode_durations(union, U, A, gas, 2.0)
        print(f"{tok_dir.name}: {len(union)} event blocks, {len(unis)} uniswap x {len(aeros)} aerodrome pools", flush=True)

    days = (hi_blk - lo_blk) * block_time / 86400
    split = lo_blk + 0.6 * (hi_blk - lo_blk)
    print(f"\nwindow {days:.2f} days | gas per atomic tx {gas:.9f} WETH | blocks {lo_blk}..{hi_blk}\n")
    print(f"{'cap WETH':>8} {'delay':>5} {'attempts':>9} {'success':>8} {'mean net (WETH)':>16} {'95% CI':>26} "
          f"{'total net':>11} {'per day':>10} {'PF':>6}")
    for c in CAPS:
        for d in DELAYS:
            att = all_att[(c, d)]
            s = summarize([x.net for x in att])
            succ = sum(x.success for x in att)
            print(f"{c:>8} {d:>5} {s['n']:>9} {succ:>8} {s['mean']:>16.3e} [{s['lo']:>10.2e}, {s['hi']:>10.2e}] "
                  f"{s['total']:>11.5f} {s['total'] / days:>10.5f} {s['pf']:>6.2f}")

    # ---- the pre-registered gate: cap 2.0, primary delay 2, robustness delay 3 -------------------
    def split_stats(att: list[Attempt]) -> tuple[dict, dict]:
        return (summarize([x.net for x in att if x.block < split]), summarize([x.net for x in att if x.block >= split]))

    att2 = all_att[(2.0, 2)]
    full2 = summarize([x.net for x in att2])
    is2, oos2 = split_stats(att2)
    oos3 = split_stats(all_att[(2.0, 3)])[1]
    survival = (sum(x.success for x in att2) / len(att2)) if att2 else 0.0   # gap still profitable 2 blocks later
    gate = {
        "G1_episodes>=200": full2["n"] >= 200,
        "G2_mean>0_and_ci_lo>0": bool(full2["n"] >= 2 and full2["mean"] > 0 and full2["lo"] > 0),
        "G3_survival_2_blocks>=40%": survival >= 0.40,
        "G4_oos_profit_factor>=1.2": bool(oos2["n"] and oos2["pf"] >= 1.2),
        "S1_oos_mean>0_and_oos_n>=50": bool(oos2["n"] >= 50 and oos2["mean"] > 0),
        "S2_mean>0_at_delay3": bool(oos3["n"] >= 2 and summarize([x.net for x in all_att[(2.0, 3)]])["mean"] > 0),
    }
    print("\n== STRESS (cap 2 WETH, delay 2) ==")
    for tag, att in stress.items():
        st = summarize([x.net for x in att])
        gate[tag + "_mean>0"] = bool(st["n"] >= 2 and st["mean"] > 0)
        print(f"{tag:<24} n={st['n']:<6} mean={st['mean']:+.3e} total={st['total']:+.5f} success={sum(x.success for x in att)}")
    if durations:
        dur = np.asarray(durations)
        print(f"\n== EPISODE-LENGTH AUDIT ({len(dur)} profitable-gap episodes, blocks of {block_time:g}s) ==\n"
              f"median {np.median(dur):g} blocks | p90 {np.percentile(dur, 90):g} | longest {dur.max()} | "
              f"lasting >=3 blocks: {np.mean(dur >= 3):.0%} (SUSPECT: possible wrong fee/liquidity, not real arbitrage) | "
              f">=1 s: {np.mean(dur * block_time >= 1):.0%}")
    print("\n== STAGE 1e GATE (cap 2 WETH, delay 2) ==")
    print(f"ALL: n={full2['n']} mean={full2['mean']:+.3e} CI=[{full2['lo']:+.2e}, {full2['hi']:+.2e}] total={full2['total']:+.5f} "
          f"PF={full2['pf']:.2f} survival(2 blocks)={survival:.0%}")
    print(f"IS : n={is2['n']} mean={is2['mean']:+.3e} total={is2['total']:+.5f}")
    print(f"OOS: n={oos2['n']} mean={oos2['mean']:+.3e} CI=[{oos2['lo']:+.2e}, {oos2['hi']:+.2e}] total={oos2['total']:+.5f} "
          f"PF={oos2['pf']:.2f}")
    for k, v in gate.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    verdict = ("INSUFFICIENT DATA (not GO)" if not gate["G1_episodes>=200"]
               else "GO to stage 1e-2 (tick-level re-simulation, then canary decision)" if all(gate.values()) else "NO-GO")
    print(f"VERDICT: {verdict}")

    best = max(all_att[(2.0, 0)], key=lambda x: x.predicted, default=None)
    zero = all_att[(2.0, 0)]
    print(f"\n[context] zero-latency theoretical pie (delay 0, cap 2 WETH, no competition): "
          f"{sum(x.net for x in zero):.4f} WETH over {days:.1f} days = {sum(x.net for x in zero) / days:.4f} WETH/day "
          f"across {len(zero)} opportunities" + (f"; largest single {best.predicted:.4f} WETH ({best.pair})" if best else ""))
    out = a.root / "arb_results.json"
    out.write_text(json.dumps({"gas": gas, "days": days, "gate": gate, "verdict": verdict,
                               "all_d2": full2, "oos_d2": oos2, "is_d2": is2, "survival_2_blocks": survival}, indent=2, default=str))
    with (a.root / "arb_attempts_cap2_d2.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["pair", "block", "direction", "size_weth", "predicted", "net", "success"])
        for x in all_att[(2.0, 2)]:
            w.writerow([x.pair, x.block, x.direction, x.size, x.predicted, x.net, x.success])


if __name__ == "__main__":
    main()
