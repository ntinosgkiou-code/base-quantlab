"""Pure indicator math. Every function takes closed candles / returns only (no I/O, no clock).

See ARCHITECTURE.md §5 for the formulas.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from functools import lru_cache

import numpy as np

from ..domain import Candle


def vwap(candles: Sequence[Candle]) -> float:
    vol = sum(c.volume for c in candles)
    if vol <= 0:
        return float("nan")
    return sum((c.high + c.low + c.close) / 3 * c.volume for c in candles) / vol


def true_ranges(candles: Sequence[Candle]) -> list[float]:
    out: list[float] = []
    for i, c in enumerate(candles):
        if i == 0:
            out.append(c.high - c.low)
        else:
            pc = candles[i - 1].close
            out.append(max(c.high - c.low, abs(c.high - pc), abs(c.low - pc)))
    return out


def atr(candles: Sequence[Candle], n: int) -> float:
    """Wilder ATR, seeded with the SMA of the first n true ranges."""
    tr = true_ranges(candles)
    if len(tr) < n:
        return float("nan")
    value = sum(tr[:n]) / n
    for x in tr[n:]:
        value += (x - value) / n
    return value


def flow_imbalance(candles: Sequence[Candle]) -> float:
    """Signed swap-flow imbalance in [-1, 1]: the AMM substitute for order-book imbalance."""
    b = sum(c.buy_volume for c in candles)
    s = sum(c.sell_volume for c in candles)
    return 0.0 if b + s <= 0 else (b - s) / (b + s)


def liquidity_skew(liquidity_above: float, liquidity_below: float) -> float:
    """V3 tick-liquidity skew within a +/- window of ticks, in [-1, 1]."""
    tot = liquidity_above + liquidity_below
    return 0.0 if tot <= 0 else (liquidity_above - liquidity_below) / tot


def log_returns(candles: Sequence[Candle]) -> np.ndarray:
    closes = np.asarray([c.close for c in candles], dtype=float)
    if len(closes) < 2 or np.any(closes <= 0):
        return np.asarray([], dtype=float)
    return np.diff(np.log(closes))


@lru_cache(maxsize=None)
def expected_rs(n: int) -> float:
    """Anis-Lloyd expected R/S for iid increments, with Peters' (n - 1/2)/n correction."""
    front = math.exp(math.lgamma((n - 1) / 2) - math.lgamma(n / 2)) / math.sqrt(math.pi)
    tail = sum(math.sqrt((n - i) / i) for i in range(1, n))
    return (n - 0.5) / n * front * tail


def _rs(x: np.ndarray, n: int) -> float:
    k = len(x) // n
    chunks = x[: k * n].reshape(k, n)
    dev = chunks - chunks.mean(axis=1, keepdims=True)
    y = np.cumsum(dev, axis=1)
    r = y.max(axis=1) - y.min(axis=1)
    s = chunks.std(axis=1)
    ok = s > 0
    if not np.any(ok):
        return float("nan")
    return float(np.mean(r[ok] / s[ok]))


def hurst(returns: np.ndarray, min_chunk: int = 8, num_sizes: int = 8) -> float:
    """Bias-corrected rescaled-range Hurst exponent. NaN when not estimable (caller fails closed).

    H = 0.5 + slope of [log R/S(n) - log E[R/S](n)] against log n.
    """
    x = np.asarray(returns, dtype=float)
    if len(x) < 4 * min_chunk:
        return float("nan")
    sizes = np.unique(np.floor(np.geomspace(min_chunk, len(x) // 2, num=num_sizes)).astype(int))
    xs, ys = [], []
    for n in sizes:
        rs = _rs(x, int(n))
        if math.isfinite(rs) and rs > 0:
            xs.append(math.log(n))
            ys.append(math.log(rs) - math.log(expected_rs(int(n))))
    if len(xs) < 3:
        return float("nan")
    slope = float(np.polyfit(xs, ys, 1)[0])
    return 0.5 + slope
