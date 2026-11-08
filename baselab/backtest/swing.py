"""Swing / mean-reversion on 15-minute grids (GO-NO-GO.md stage 1 sigma-tau).

Per pool: price series from the pool state (V2 reserves, V3 virtual reserves of the active range), sampled every 15 min.
    z_k = (r_k - mu_k) / sigma_k     r_k = ln(P_k / P_{k-12})  (3 h), mu/sigma = rolling stats of the PREVIOUS 7 days
Arms: M  z <= -2.5 and Hurst < 0.5   exit: price back at its level 3 h before the signal (consistent mean reversion)
      L  z <= -2.5 and Hurst > 0.55  exit: same (literal reading of the proposal)
      T  z >= +2.5 and Hurst > 0.55  exit: z <= 0 (trend continuation)
All: stop -1.5% of the entry mid price (checked on EVERY event), 4 h time exit, long only, fixed notional, executed
`exec_delay_s` after the signal against the real reserves at that moment (LP fee + haircut + gas).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..quant.indicators import hurst
from ..risk.slippage import V2Pool

PERIOD = 900
LOOKBACK = 12            # 3 h in 15-min periods
ROLL, MIN_ROLL = 672, 288  # 7 days / 3 days
HURST_N = 128


@dataclass(frozen=True, slots=True)
class SwingParams:
    arm: str = "M"
    z_entry: float = 2.5
    stop: float = 0.015
    max_hold_s: int = 4 * 3600
    notional: float = 0.05
    haircut: float = 0.001
    max_impact: float = 0.005
    exec_delay_s: int = 60
    fee_mult: float = 1.0
    gas_round_trip: float = 6e-6
    cost_multiple: float = 3.0
    hurst_gate: bool = True   # always True in the real backtest; False only to unit-test entry/exit mechanics


@dataclass(slots=True)
class PoolSeries:
    pool: str
    symbol: str
    fee: float
    ts: np.ndarray   # event times (s), ascending
    rw: np.ndarray   # WETH reserve after each event
    rt: np.ndarray   # token reserve after each event


@dataclass(slots=True)
class Counters:
    z_signals: int = 0
    hurst_ok: int = 0
    fee_rule_ok: int = 0
    impact_skips: int = 0
    trades: int = 0


def rolling_prev_stats(x: np.ndarray, w: int, min_periods: int) -> tuple[np.ndarray, np.ndarray]:
    """mean and std of the previous `w` finite values (current excluded -> no lookahead)."""
    n = len(x)
    valid = np.isfinite(x)
    xv = np.where(valid, x, 0.0)
    c1 = np.concatenate(([0.0], np.cumsum(xv)))
    c2 = np.concatenate(([0.0], np.cumsum(xv * xv)))
    cn = np.concatenate(([0], np.cumsum(valid)))
    idx = np.arange(n)
    lo = np.maximum(idx - w, 0)
    cnt = cn[idx] - cn[lo]
    s1, s2 = c1[idx] - c1[lo], c2[idx] - c2[lo]
    with np.errstate(all="ignore"):
        mean = s1 / cnt
        var = np.maximum(s2 / cnt - mean * mean, 0.0)
    std = np.sqrt(var)
    bad = cnt < min_periods
    return np.where(bad, np.nan, mean), np.where(bad, np.nan, std)


def zscores(price: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    with np.errstate(all="ignore"):
        r = np.full(len(price), np.nan)
        r[LOOKBACK:] = np.log(price[LOOKBACK:] / price[:-LOOKBACK])
    r[~np.isfinite(r)] = np.nan
    mu, sd = rolling_prev_stats(r, ROLL, MIN_ROLL)
    with np.errstate(all="ignore"):
        z = (r - mu) / sd
    z[~np.isfinite(z)] = np.nan
    return z, r


def run_arm(s: PoolSeries, p: SwingParams) -> tuple[list[dict], Counters, np.ndarray]:
    """Simulate one arm on one pool. Returns (trades, counters, grid times)."""
    c = Counters()
    trades: list[dict] = []
    if len(s.ts) < 10:
        return trades, c, np.empty(0)
    grid = np.arange(np.ceil(s.ts[0] / PERIOD) * PERIOD, s.ts[-1], PERIOD)
    ev_idx = np.searchsorted(s.ts, grid, side="right") - 1
    ok = ev_idx >= 0
    price = np.full(len(grid), np.nan)
    price[ok] = s.rw[ev_idx[ok]] / s.rt[ev_idx[ok]]
    z, r = zscores(price)
    fee = s.fee * p.fee_mult
    cost = 2 * fee + 2 * p.haircut + p.gas_round_trip / p.notional
    long_mean_rev = p.arm in ("M", "L")
    k, n = LOOKBACK, len(grid)
    while k < n - 1:
        zk = z[k]
        trig = np.isfinite(zk) and ((zk <= -p.z_entry) if long_mean_rev else (zk >= p.z_entry))
        if not trig:
            k += 1
            continue
        c.z_signals += 1
        h = float("nan")
        if p.hurst_gate and k >= HURST_N:
            seg = price[k - HURST_N:k + 1]
            if np.all(np.isfinite(seg)) and np.all(seg > 0):
                h = hurst(np.diff(np.log(seg)))
        gate_ok = (not p.hurst_gate) or (np.isfinite(h) and ((h < 0.5) if p.arm == "M" else (h > 0.55)))
        if not gate_ok:
            k += 1
            continue
        c.hurst_ok += 1
        if 0.5 * abs(r[k]) < p.cost_multiple * cost:
            k += 1
            continue
        c.fee_rule_ok += 1
        # ---- entry against the real reserves `exec_delay_s` after the candle close
        te = grid[k] + p.exec_delay_s
        ie = int(np.searchsorted(s.ts, te, side="right") - 1)
        rw, rt = float(s.rw[ie]), float(s.rt[ie])
        amm = V2Pool(rw, rt, fee)
        tokens = amm.out(p.notional) * (1 - p.haircut)
        if tokens <= 0 or (1 - (amm.out(p.notional) / p.notional) / amm.marginal) > p.max_impact:
            c.impact_skips += 1
            k += 1
            continue
        mid_e = rw / rt
        stop_level = mid_e * (1 - p.stop)
        p_ref = price[k - LOOKBACK]
        # ---- exit scan
        exit_t, reason, kx = None, "end", n - 1
        for kk in range(k + 1, n):
            i0, i1 = int(np.searchsorted(s.ts, grid[kk - 1], side="right")), int(np.searchsorted(s.ts, grid[kk], side="right"))
            if i1 > i0:
                pe = s.rw[i0:i1] / s.rt[i0:i1]
                hit = np.nonzero((pe <= stop_level) & (s.ts[i0:i1] > te))[0]
                if len(hit):
                    exit_t, reason, kx = float(s.ts[i0 + hit[0]]), "stop", kk
                    break
            if long_mean_rev:
                # "back to the mean" = the price recovered to where it was 3 h before the signal. A z-score of a
                # rolling 3 h return returns to 0 once the drop ages out of the window WITHOUT any recovery.
                if np.isfinite(p_ref) and price[kk] >= p_ref:
                    exit_t, reason, kx = float(grid[kk]), "recovered", kk
                    break
            else:
                zz = z[kk]
                if np.isfinite(zz) and zz <= 0:
                    exit_t, reason, kx = float(grid[kk]), "z_exit", kk
                    break
            if grid[kk] - te >= p.max_hold_s:
                exit_t, reason, kx = float(grid[kk]), "time", kk
                break
        if exit_t is None:
            exit_t = float(s.ts[-1])
        ix = int(np.searchsorted(s.ts, exit_t + p.exec_delay_s, side="right") - 1)
        out = V2Pool(float(s.rt[ix]), float(s.rw[ix]), fee).out(tokens) * (1 - p.haircut)
        net = out - p.notional - p.gas_round_trip
        trades.append({"pool": s.pool, "symbol": s.symbol, "entry_ts": te, "exit_ts": exit_t + p.exec_delay_s,
                       "hold_s": exit_t - te, "reason": reason, "gross_ret": out / p.notional - 1,
                       "net_ret": net / p.notional, "net": net})
        c.trades += 1
        k = kx + 1  # no overlap, one period of cool-down
    return trades, c, grid
