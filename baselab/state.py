"""System state machine. The only writer of the trading phase (guarded by asyncio.Lock).

Fail-safe handlers (cancel intents, revoke allowances, flush caches, flatten) are fanned out
concurrently with per-handler timeouts and `return_exceptions=True`, so one failing handler can
never block the others or leave the phase half-changed: the phase is committed *before* handlers run.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

log = logging.getLogger(__name__)


class Phase(str, Enum):
    BOOTING = "booting"
    ARMED = "armed"
    FROZEN = "frozen"
    SLEEPING = "sleeping"
    HALTED = "halted"


_ALLOWED: dict[Phase, frozenset[Phase]] = {
    Phase.BOOTING: frozenset({Phase.ARMED, Phase.FROZEN, Phase.HALTED}),
    Phase.ARMED: frozenset({Phase.FROZEN, Phase.SLEEPING, Phase.HALTED}),
    Phase.FROZEN: frozenset({Phase.ARMED, Phase.SLEEPING, Phase.HALTED}),
    Phase.SLEEPING: frozenset({Phase.ARMED, Phase.HALTED}),
    Phase.HALTED: frozenset(),  # terminal; manual_reset() only
}

Handler = Callable[[Phase, str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Transition:
    src: Phase
    dst: Phase
    reason: str
    at: float


def next_utc_midnight(now: datetime) -> datetime:
    day = now.astimezone(timezone.utc).date() + timedelta(days=1)
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


class SystemState:
    def __init__(self, handler_timeout_s: float = 5.0,
                 wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self._phase = Phase.BOOTING
        self._lock = asyncio.Lock()
        self._handlers: list[tuple[str, Handler]] = []
        self._timeout = handler_timeout_s
        self._wall_clock = wall_clock
        self.sleep_until: datetime | None = None
        self.history: list[Transition] = []

    @property
    def phase(self) -> Phase:
        return self._phase

    def can_enter(self) -> bool:
        return self._phase is Phase.ARMED

    def can_exit(self) -> bool:
        return self._phase in (Phase.ARMED, Phase.FROZEN, Phase.SLEEPING)

    def on_safety_event(self, name: str, handler: Handler) -> None:
        """Register a handler that runs whenever the system leaves ARMED for a safe state."""
        self._handlers.append((name, handler))

    async def _transition(self, dst: Phase, reason: str) -> bool:
        async with self._lock:
            src = self._phase
            if src is dst:
                return False
            if dst not in _ALLOWED[src]:
                log.warning("rejected transition %s -> %s (%s)", src.value, dst.value, reason)
                return False
            self._phase = dst
            self.history.append(Transition(src, dst, reason, time.monotonic()))
            log.warning("STATE %s -> %s: %s", src.value, dst.value, reason)
        if dst in (Phase.FROZEN, Phase.SLEEPING, Phase.HALTED):
            await self._run_handlers(dst, reason)
        return True

    async def _run_handlers(self, dst: Phase, reason: str) -> None:
        async def guarded(name: str, h: Handler) -> None:
            try:
                await asyncio.wait_for(h(dst, reason), timeout=self._timeout)
            except Exception:  # isolated: log and continue, never propagate
                log.exception("safety handler %s failed", name)

        await asyncio.gather(*(guarded(n, h) for n, h in self._handlers), return_exceptions=True)

    async def arm(self, reason: str = "checks passed") -> bool:
        return await self._transition(Phase.ARMED, reason)

    async def freeze(self, reason: str) -> bool:
        return await self._transition(Phase.FROZEN, reason)

    async def halt(self, reason: str) -> bool:
        return await self._transition(Phase.HALTED, reason)

    async def sleep_until_next_utc_day(self, reason: str) -> bool:
        self.sleep_until = next_utc_midnight(self._wall_clock())
        return await self._transition(Phase.SLEEPING, reason)

    async def maybe_wake(self) -> bool:
        if self._phase is Phase.SLEEPING and self.sleep_until is not None \
                and self._wall_clock() >= self.sleep_until:
            self.sleep_until = None
            return await self._transition(Phase.ARMED, "new UTC day")
        return False

    async def manual_reset(self, operator_note: str) -> None:
        async with self._lock:
            log.warning("MANUAL RESET from %s: %s", self._phase.value, operator_note)
            self.history.append(Transition(self._phase, Phase.BOOTING, operator_note, time.monotonic()))
            self._phase = Phase.BOOTING
