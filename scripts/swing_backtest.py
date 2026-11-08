"""Stage 1 sigma-tau of GO-NO-GO.md: swing / mean-reversion on 15-minute grids over the contiguous ~42 days.

    python scripts/swing_backtest.py --data <oldest dir> <next dir> <newest dir>

Runs the three pre-registered arms (M, L, T) once each, plus the execution stress run (G3), and prints the gate.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from baselab.backtest.arb import pool_states  # noqa: E402
from baselab.backtest.real_replay import load_dataset  # noqa: E402
from baselab.backtest.swing import PoolSeries, SwingParams, run_arm  # noqa: E402
from baselab.research.stats import breaker_halts, cluster_bootstrap_ci, profit_factor  # noqa: E402

ALPHA = 0.01 / 3   # 99.67% CI: Bonferroni for 3 arms on top of a 1% level (13th hypothesis of the project)


def load_series(dirs: list[Path]) -> dict[str, PoolSeries]:
    parts: dict[str, list[tuple]] = {}
    meta: dict[str, dict] = {}
    for d in dirs:
        ds = load_dataset(d, require_block_times=True)
        for pm in ds.pools:
            blocks, rw, rt = pool_states(ds, pm)
            if len(blocks) == 0:
                continue
            ts = np.interp(blocks, ds.knot_blocks, ds.knot_ts)
            parts.setdefault(pm["pool"], []).append((ts, rw, rt))
            meta[pm["pool"]] = pm
        del ds
    out: dict[str, PoolSeries] = {}
    for addr, lst in parts.items():
        lst.sort(key=lambda t: t[0][0])
        ts = np.concatenate([t[0] for t in lst])
        rw = np.concatenate([t[1] for t in lst])
        rt = np.concatenate([t[2] for t in lst])
        keep = (rw > 0) & (rt > 0)
        order = np.argsort(ts, kind="stable")
        ts, rw, rt, keep = ts[order], rw[order], rt[order], keep[order]
        pm = meta[addr]
        out[addr] = PoolSeries(addr, pm.get("symbol", "?"), pm["fee_ppm"] / 1e6, ts[keep], rw[keep], rt[keep])
    return out


def mean(x: list[float]) -> float:
    return float(np.mean(x)) if x else float("nan")


def evaluate(trades: list[dict], t0: float, t1: float, stress_trades: list[dict]) -> dict:
    split = t0 + 0.6 * (t1 - t0)
    oos = [t for t in trades if t["entry_ts"] >= split]
    is_ = [t for t in trades if t["entry_ts"] < split]
    r = [t["net_ret"] for t in oos]
    days = [int(t["entry_ts"] // 86400) for t in oos]
    lo, hi = cluster_bootstrap_ci(r, days, alpha=ALPHA) if len(r) >= 2 else (float("nan"), float("nan"))
    oos_days = max((t1 - split) / 86400, 1e-9)
    by_pool: dict[str, float] = {}
    for t in oos:
        by_pool[t["symbol"] + t["pool"][-4:]] = by_pool.get(t["symbol"] + t["pool"][-4:], 0.0) + t["net"]
    tot = sum(by_pool.values())
    top_share = max(by_pool.values()) / tot if tot > 0 and by_pool else float("inf")
    ex3 = sorted(r, reverse=True)[3:]
    s_oos = [t["net_ret"] for t in stress_trades if t["entry_ts"] >= split]
    gates = {
        "G1_oos_trades>=100": len(oos) >= 100,
        "G2_oos_mean>0_and_ci99.67_lo>0": bool(len(r) >= 2 and mean(r) > 0 and lo > 0),
        "G3_stress_5min_delay_fees_x1.5_mean>0": bool(len(s_oos) >= 2 and mean(s_oos) > 0),
        "G4_oos_profit_factor>=1.3": bool(r and profit_factor(r) >= 1.3),
        "G5_halts/day<=0.5": breaker_halts(oos) / oos_days <= 0.5,
        "G6_concentration_and_ex_top3": bool(top_share <= 0.40 and len(ex3) >= 2 and mean(ex3) > 0),
    }
    return {"n_all": len(trades), "n_is": len(is_), "n_oos": len(oos), "mean_is": mean([t["net_ret"] for t in is_]),
            "mean_oos": mean(r), "ci": (lo, hi), "pf": profit_factor(r) if r else float("nan"),
            "win": (sum(v > 0 for v in r) / len(r)) if r else float("nan"), "stress_mean": mean(s_oos),
            "stress_n": len(s_oos), "top_pool_share": top_share, "mean_ex_top3": mean(ex3),
            "halts_per_day": breaker_halts(oos) / oos_days, "gates": gates,
            "reasons": {k: sum(1 for t in trades if t["reason"] == k) for k in ("recovered", "z_exit", "stop", "time", "end")}}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, nargs="+", required=True, help="dataset dirs (any order)")
    ap.add_argument("--notional", type=float, default=0.05)
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args()
    series = load_series(a.data)
    t0 = min(s.ts[0] for s in series.values())
    t1 = max(s.ts[-1] for s in series.values())
    print(f"{len(series)} pools, {(t1 - t0) / 86400:.1f} days, "
          f"{sum(len(s.ts) for s in series.values())} state points | alpha={ALPHA:.4f} (99.67% CI, clustered by day)\n", flush=True)
    results = {}
    for arm in ("M", "L", "T"):
        base = SwingParams(arm=arm, notional=a.notional)
        stress = replace(base, exec_delay_s=360, fee_mult=1.5)
        trades, strade = [], []
        cnt = {"z_signals": 0, "hurst_ok": 0, "fee_rule_ok": 0, "impact_skips": 0, "trades": 0}
        for s in series.values():
            tr, c, _ = run_arm(s, base)
            trades += tr
            for k in cnt:
                cnt[k] += getattr(c, k)
            strade += run_arm(s, stress)[0]
        res = evaluate(trades, t0, t1, strade)
        res["funnel"] = cnt
        results[arm] = res
        g = res["gates"]
        print(f"=== ARM {arm} ===  funnel: z-signals {cnt['z_signals']} -> hurst ok {cnt['hurst_ok']} -> fee rule ok "
              f"{cnt['fee_rule_ok']} -> trades {cnt['trades']} (impact skips {cnt['impact_skips']})")
        print(f"all={res['n_all']} IS n={res['n_is']} mean={res['mean_is']:+.3%} | OOS n={res['n_oos']} mean={res['mean_oos']:+.3%} "
              f"CI99.67=[{res['ci'][0]:+.3%}, {res['ci'][1]:+.3%}] win={res['win']:.0%} PF={res['pf']:.2f} "
              f"stress mean={res['stress_mean']:+.3%} (n={res['stress_n']}) exits={res['reasons']}")
        print(f"top pool share {res['top_pool_share']:.0%} | mean ex-top3 {res['mean_ex_top3']:+.3%} | halts/day {res['halts_per_day']:.2f}")
        for k, v in g.items():
            print(f"   {'PASS' if v else 'FAIL'}  {k}")
        verdict = "GO to survivorship-free re-test" if all(g.values()) else ("INSUFFICIENT DATA (not GO)" if not g["G1_oos_trades>=100"] else "NO-GO")
        res["verdict"] = verdict
        print(f"   VERDICT {arm}: {verdict}\n", flush=True)
    if a.out:
        a.out.write_text(json.dumps(results, indent=2, default=str))


if __name__ == "__main__":
    main()
