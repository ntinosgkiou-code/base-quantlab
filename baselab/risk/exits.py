"""ATR trailing stop + 180 s time exit.

stop_t = max(initial_stop, peak_t - k * ATR). Once the momentum target is reached the trail tightens
to k/2 * ATR and the time exit no longer applies (winner is allowed to run, but never past 3x max hold).
"""
from __future__ import annotations

from dataclasses import dataclass

from ..domain import Position


@dataclass(frozen=True, slots=True)
class ExitDecision:
    reason: str
    price: float


class ExitPolicy:
    def __init__(self, trail_atr_mult: float, max_hold_s: float) -> None:
        self._k = trail_atr_mult
        self._max_hold = max_hold_s

    def update(self, pos: Position, price: float, atr_value: float, now: float) -> ExitDecision | None:
        initial_stop = pos.meta.setdefault("initial_stop", pos.stop)
        pos.peak = max(pos.peak or pos.entry_price, price)
        target_hit = pos.meta.get("target_hit", 0.0) > 0 or price >= pos.target
        if target_hit:
            pos.meta["target_hit"] = 1.0
        k = self._k / 2 if target_hit else self._k
        pos.stop = max(pos.stop, pos.peak - k * atr_value)
        held = now - pos.opened_at
        if price <= pos.stop:
            return ExitDecision("trailing_stop" if pos.stop > initial_stop else "stop", price)
        if not target_hit and held >= self._max_hold:
            return ExitDecision("time_exit_180s", price)
        if held >= 3 * self._max_hold:
            return ExitDecision("max_hold", price)
        return None
