"""Two textbook minute strategies, exactly as pre-registered in GO-NO-GO.md stage 1d.

R  RSI mean reversion : RSI(14) < 20 -> buy; exit at SMA20 (take profit), -0.5% hard stop, 15 min time exit.
B  Volume breakout    : volume >= 3 x mean volume of the previous 30 minutes AND close > highest high of the
                        previous 20 minutes -> buy; +1.0% take profit, -0.5% hard stop, 5 min time exit.

Fee rule (both): enter only if round-trip cost < 0.5 x expected profit.
Pure functions over closed candles. Parameters are module constants so they cannot drift after results.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ..domain import Candle

RSI_N, RSI_ENTRY, RSI_SMA = 14, 20.0, 20
R_STOP, R_HOLD_S = 0.005, 15 * 60
B_VOL_MULT, B_VOL_WIN, B_BREAK_WIN = 3.0, 30, 20
B_TP, B_STOP, B_HOLD_S = 0.010, 0.005, 5 * 60
FEE_RULE = 0.5


@dataclass(frozen=True, slots=True)
class AltSignal:
    price: float
    target: float
    stop: float
    max_hold_s: float
    atr: float = 0.0


def rsi(closes: Sequence[float], n: int = RSI_N) -> float:
    """Wilder RSI of the last values; nan if not enough data."""
    if len(closes) < n + 1:
        return float("nan")
    diffs = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gain = sum(max(d, 0.0) for d in diffs[:n]) / n
    loss = sum(max(-d, 0.0) for d in diffs[:n]) / n
    for d in diffs[n:]:
        gain = (gain * (n - 1) + max(d, 0.0)) / n
        loss = (loss * (n - 1) + max(-d, 0.0)) / n
    if loss == 0:
        return 100.0 if gain > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + gain / loss)


def eval_rsi(candles: Sequence[Candle], round_trip_cost: float) -> tuple[AltSignal | None, tuple[str, ...]]:
    need = max(RSI_N * 3, RSI_SMA + 1)
    if len(candles) < need:
        return None, (f"warmup:{len(candles)}/{need}",)
    last = candles[-1]
    if last.synthetic:
        return None, ("no_trades_last_minute",)
    closes = [c.close for c in candles[-(RSI_N * 6):]]
    r = rsi(closes)
    if not r < RSI_ENTRY:  # also rejects nan
        return None, (f"rsi_not_oversold:{r:.0f}",)
    sma = sum(c.close for c in candles[-RSI_SMA:]) / RSI_SMA
    expected = sma / last.close - 1
    if expected <= 0:
        return None, ("price_above_mean",)
    if round_trip_cost >= FEE_RULE * expected:
        return None, (f"cost_exceeds_edge:{round_trip_cost:.2%}>={FEE_RULE * expected:.2%}",)
    return AltSignal(last.close, sma, last.close * (1 - R_STOP), R_HOLD_S), ()


def eval_breakout(candles: Sequence[Candle], round_trip_cost: float) -> tuple[AltSignal | None, tuple[str, ...]]:
    need = B_VOL_WIN + 1
    if len(candles) < need:
        return None, (f"warmup:{len(candles)}/{need}",)
    last = candles[-1]
    if last.synthetic:
        return None, ("no_trades_last_minute",)
    prev_vol = [c.volume for c in candles[-(B_VOL_WIN + 1):-1]]
    mean_vol = sum(prev_vol) / len(prev_vol)
    if mean_vol <= 0 or last.volume < B_VOL_MULT * mean_vol:
        return None, ("no_volume_spike",)
    resistance = max(c.high for c in candles[-(B_BREAK_WIN + 1):-1])
    if last.close <= resistance:
        return None, ("no_breakout",)
    if round_trip_cost >= FEE_RULE * B_TP:
        return None, (f"cost_exceeds_edge:{round_trip_cost:.2%}>={FEE_RULE * B_TP:.2%}",)
    return AltSignal(last.close, last.close * (1 + B_TP), last.close * (1 - B_STOP), B_HOLD_S), ()


EVALUATORS = {"rsi": eval_rsi, "breakout": eval_breakout}
