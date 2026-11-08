"""Strategy Matrix: closed candles in, `Signal` out. Every gate is explicit and reported."""
from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ..config import StrategyParams
from ..domain import Candle, Side, Signal
from .indicators import atr, flow_imbalance, hurst, log_returns, vwap

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Evaluation:
    signal: Signal | None
    reasons: tuple[str, ...]
    metrics: dict[str, float]


class StrategyMatrix:
    def __init__(self, params: StrategyParams, clock: Callable[[], float] = time.monotonic) -> None:
        self._p = params
        self._clock = clock
        self._live: dict[str, Signal] = {}

    def evaluate(self, pool: str, token: str, candles: Sequence[Candle],
                 round_trip_cost: float = 0.0) -> Evaluation:
        """`round_trip_cost` = fraction of notional lost on entry + exit (2 x LP fee + 2 x expected slippage);
        used only by the v2 cost filter (`max_cost_to_atr`, off by default)."""
        p = self._p
        need = max(p.hurst_min_points + 1, p.atr_slow + 1, p.vwap_window)
        if len(candles) < need:
            return Evaluation(None, (f"warmup:{len(candles)}/{need}",), {})
        last = candles[-1]
        h = hurst(log_returns(candles[-(p.hurst_min_points + 1):]))
        a_fast = atr(candles[-(p.atr_fast * 3):], p.atr_fast)
        a_slow = atr(candles[-(p.atr_slow * 2):], p.atr_slow)
        vw = vwap(candles[-p.vwap_window:])
        ofi = flow_imbalance(candles[-p.ofi_window:])
        rho = a_fast / a_slow if a_slow > 0 else float("nan")
        dev = (last.close - vw) / a_fast if a_fast > 0 else float("nan")
        m = {"hurst": h, "atr_fast": a_fast, "atr_slow": a_slow, "vwap": vw, "ofi": ofi,
             "vol_expansion": rho, "vwap_dev_atr": dev}

        reasons: list[str] = []
        if not all(math.isfinite(v) for v in m.values()):
            reasons.append("non_finite_metric")
        else:
            if h < 0.5 + p.hurst_margin:
                reasons.append(f"regime_halt:H={h:.3f}")
            if rho < p.min_vol_expansion:
                reasons.append(f"no_vol_expansion:{rho:.2f}")
            if a_fast / last.close < p.atr_floor:
                reasons.append("dead_liquidity")
            if ofi < p.min_ofi:
                reasons.append(f"weak_flow:{ofi:.2f}")
            if not p.dev_min <= dev <= p.dev_max:
                reasons.append(f"vwap_dev_out_of_band:{dev:.2f}")
            if last.synthetic:
                reasons.append("no_trades_last_minute")
            # v2: the expected 1-minute move must pay for the round trip (cost >= k * ATR/price -> skip)
            if round_trip_cost >= p.max_cost_to_atr * a_fast / last.close:
                reasons.append(f"cost_exceeds_edge:{round_trip_cost:.2%}")
        if reasons:
            self._live.pop(pool, None)
            return Evaluation(None, tuple(reasons), m)

        sig = Signal(
            pool=pool, token=token, side=Side.BUY, created_at=self._clock(), candle_minute=last.minute,
            price=last.close, atr=a_fast, hurst=h, ofi=ofi, vol_expansion=rho,
            score=ofi * min(rho, 3.0) * (h - 0.5),
            target=last.close + p.target_atr_mult * a_fast,
            stop=last.close - p.trail_atr_mult * a_fast,
        )
        self._live[pool] = sig
        return Evaluation(sig, (), m)

    def on_revision(self, candle: Candle) -> None:
        """Any revised candle inside the signal's lookback invalidates it (reorg-safe signals)."""
        sig = self._live.get(candle.pool)
        lookback = 60 * max(self._p.hurst_min_points, self._p.atr_slow * 2, self._p.vwap_window)
        if sig is not None and candle.minute >= sig.candle_minute - lookback:
            log.warning("signal for %s invalidated by candle revision", candle.pool)
            self._live.pop(candle.pool, None)

    def is_current(self, sig: Signal) -> bool:
        return self._live.get(sig.pool) is sig

    def consume(self, sig: Signal) -> None:
        if self._live.get(sig.pool) is sig:
            del self._live[sig.pool]

    def flush(self) -> None:
        self._live.clear()
