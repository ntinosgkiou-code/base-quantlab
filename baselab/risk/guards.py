"""Circuit breaker (2 consecutive losses / slippage > 3%) and daily 3% drawdown guard."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import date, datetime, timezone

from ..config import RiskParams
from ..domain import Fill
from ..state import SystemState

log = logging.getLogger(__name__)


class CircuitBreaker:
    def __init__(self, params: RiskParams, state: SystemState) -> None:
        self._p = params
        self._state = state
        self._lock = asyncio.Lock()
        self.consecutive_losses = 0

    async def on_trade_closed(self, net_pnl: float) -> None:
        async with self._lock:
            self.consecutive_losses = self.consecutive_losses + 1 if net_pnl < 0 else 0
            hit = self.consecutive_losses >= self._p.max_consecutive_losses
        if hit:
            await self._state.halt(f"circuit_breaker:{self.consecutive_losses}_consecutive_losses")

    async def on_fill(self, fill: Fill, tolerance: float) -> None:
        s = fill.realized_slippage
        if s > self._p.halt_slippage:
            await self._state.halt(f"circuit_breaker:slippage_{s:.2%}")
        elif s > tolerance:
            await self._state.freeze(f"unsimulated_slippage:{s:.2%}>tol_{tolerance:.2%}")


class DailyLossGuard:
    def __init__(self, params: RiskParams, state: SystemState,
                 wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self._p = params
        self._state = state
        self._clock = wall_clock
        self._lock = asyncio.Lock()
        self._day: date | None = None
        self.equity_open = 0.0

    async def update(self, equity: float) -> bool:
        """Returns True if the daily loss limit was hit (system is put to sleep)."""
        async with self._lock:
            today = self._clock().astimezone(timezone.utc).date()
            if today != self._day:
                self._day, self.equity_open = today, equity
            breached = self.equity_open > 0 and equity <= self.equity_open * (1 - self._p.max_daily_loss)
        if breached:
            await self._state.sleep_until_next_utc_day(
                f"daily_loss:{1 - equity / self.equity_open:.2%}>=max_{self._p.max_daily_loss:.0%}")
        return breached
