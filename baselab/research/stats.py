"""Statistics for the GO/NO-GO gate (see GO-NO-GO.md). Pure functions over trade records."""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np


def bootstrap_mean_ci(x: Sequence[float], n: int = 10_000, alpha: float = 0.05, seed: int = 0) -> tuple[float, float]:
    """Percentile bootstrap CI of the mean. With fewer than 2 points the CI is undefined (nan, nan)."""
    arr = np.asarray(x, dtype=float)
    if len(arr) < 2:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    means = arr[rng.integers(0, len(arr), size=(n, len(arr)))].mean(axis=1)
    return float(np.quantile(means, alpha / 2)), float(np.quantile(means, 1 - alpha / 2))


def profit_factor(x: Sequence[float]) -> float:
    gains = sum(v for v in x if v > 0)
    losses = -sum(v for v in x if v < 0)
    if losses == 0:
        return float("inf") if gains > 0 else float("nan")
    return gains / losses


def breaker_halts(trades: Sequence[dict[str, Any]], max_losses: int = 2) -> int:
    """How many times the '2 consecutive losses' circuit breaker would have halted (reset after each)."""
    halts = streak = 0
    for t in sorted(trades, key=lambda t: t["exit_ts"]):
        streak = streak + 1 if t["net"] < 0 else 0
        if streak >= max_losses:
            halts += 1
            streak = 0
    return halts


def summarize(trades: Sequence[dict[str, Any]], alpha: float = 0.05) -> dict[str, float]:
    r = [t["net_ret"] for t in trades]
    lo, hi = bootstrap_mean_ci(r, alpha=alpha)
    return {"n": len(r), "mean_net_ret": float(np.mean(r)) if r else float("nan"),
            "ci_lo": lo, "ci_hi": hi, "win_rate": (sum(v > 0 for v in r) / len(r)) if r else float("nan"),
            "profit_factor": profit_factor(r), "mean_gross_ret": float(np.mean([t["gross_ret"] for t in trades])) if r else float("nan")}


def evaluate_gate(trades: Sequence[dict[str, Any]], t_start: float, t_end: float,
                  is_fraction: float = 0.6, alpha: float = 0.05) -> dict[str, Any]:
    split = t_start + (t_end - t_start) * is_fraction
    is_tr = [t for t in trades if t["entry_ts"] < split]
    oos_tr = [t for t in trades if t["entry_ts"] >= split]
    s_is, s_oos = summarize(is_tr, alpha), summarize(oos_tr, alpha)
    days = max((t_end - t_start) / 86400, 1e-9)
    halts_per_day = breaker_halts(trades) / days
    g = {
        "G1_oos_trades>=200": s_oos["n"] >= 200,
        "G2_oos_mean>0_and_ci_lo>0": bool(s_oos["n"] >= 2 and s_oos["mean_net_ret"] > 0 and s_oos["ci_lo"] > 0),
        "G3_oos_profit_factor>=1.2": bool(s_oos["profit_factor"] >= 1.2) if s_oos["n"] else False,
        "G4_is_oos_same_sign": bool(s_is["n"] and s_oos["n"] and np.sign(s_is["mean_net_ret"]) == np.sign(s_oos["mean_net_ret"])),
        "G5_halts_per_day<=1": halts_per_day <= 1,
    }
    if not g["G1_oos_trades>=200"]:
        verdict = "INSUFFICIENT DATA (not GO)"
    else:
        verdict = "GO to stage 2 (forward paper)" if all(g.values()) else "NO-GO"
    return {"in_sample": s_is, "out_of_sample": s_oos, "halts_per_day": halts_per_day, "days": days,
            "gates": g, "verdict": verdict}


def cluster_bootstrap_ci(values: Sequence[float], clusters: Sequence[int], alpha: float = 0.05, n: int = 10_000,
                         seed: int = 0) -> tuple[float, float]:
    """CI of the mean when observations are correlated inside a cluster (e.g. the same calendar day).

    Resamples whole clusters with replacement; the statistic is the trade-weighted mean (sum / count)."""
    v = np.asarray(values, dtype=float)
    c = np.asarray(clusters)
    if len(v) < 2:
        return float("nan"), float("nan")
    ids, inv = np.unique(c, return_inverse=True)
    sums = np.bincount(inv, weights=v)
    cnts = np.bincount(inv).astype(float)
    rng = np.random.default_rng(seed)
    pick = rng.integers(0, len(ids), size=(n, len(ids)))
    means = sums[pick].sum(axis=1) / cnts[pick].sum(axis=1)
    return float(np.quantile(means, alpha / 2)), float(np.quantile(means, 1 - alpha / 2))
