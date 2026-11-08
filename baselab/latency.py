"""Layer 5: RTT heartbeat per network channel and WebSocket gap watchdog.

Health rule per channel: last RTT <= max_rtt AND p95(window) <= max_rtt. A single spike makes the
channel unhealthy immediately (last sample); the p95 keeps it unhealthy while jitter persists.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .config import LatencyParams

log = logging.getLogger(__name__)

Probe = Callable[[], Awaitable[object]]
BreachHandler = Callable[[str], Awaitable[object]]


def percentile(values: list[float], q: float) -> float:
    if not values:
        return float("inf")
    s = sorted(values)
    k = (len(s) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


@dataclass(slots=True)
class ChannelStats:
    samples: deque[float]
    last_ms: float = float("inf")
    healthy_since: float | None = None


class RttMonitor:
    def __init__(self, params: LatencyParams, clock: Callable[[], float] = time.monotonic) -> None:
        self._p = params
        self._clock = clock
        self._ch: dict[str, ChannelStats] = {}

    def channels(self) -> list[str]:
        return list(self._ch)

    def register(self, channel: str) -> None:
        self._ch.setdefault(channel, ChannelStats(deque(maxlen=self._p.window)))

    def record(self, channel: str, rtt_ms: float) -> bool:
        self.register(channel)
        st = self._ch[channel]
        st.samples.append(rtt_ms)
        st.last_ms = rtt_ms
        ok = self.healthy(channel)
        if ok and st.healthy_since is None:
            st.healthy_since = self._clock()
        elif not ok:
            st.healthy_since = None
        return ok

    def healthy(self, channel: str) -> bool:
        st = self._ch.get(channel)
        if st is None or not st.samples:
            return False
        return st.last_ms <= self._p.max_rtt_ms and percentile(list(st.samples), 0.95) <= self._p.max_rtt_ms

    def all_healthy(self) -> bool:
        return bool(self._ch) and all(self.healthy(c) for c in self._ch)

    def healthy_for(self, seconds: float) -> bool:
        now = self._clock()
        return self.all_healthy() and all(
            st.healthy_since is not None and now - st.healthy_since >= seconds for st in self._ch.values())

    def snapshot(self) -> dict[str, float]:
        return {c: st.last_ms for c, st in self._ch.items()}

    async def run_probe(self, channel: str, probe: Probe, on_breach: BreachHandler) -> None:
        """Probe forever. A timeout counts as an infinite RTT (fail closed)."""
        self.register(channel)
        timeout = self._p.max_rtt_ms * 4 / 1000
        while True:
            t0 = time.perf_counter()
            try:
                await asyncio.wait_for(probe(), timeout=timeout)
                rtt = (time.perf_counter() - t0) * 1000
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # timeout or transport error
                log.warning("probe %s failed: %r", channel, exc)
                rtt = float("inf")
            if not self.record(channel, rtt):
                await on_breach(f"latency:{channel}:{rtt:.0f}ms")
            await asyncio.sleep(self._p.probe_interval_s)


class ConnectionWatchdog:
    """Freezes if the WebSocket is disconnected (or silent on pings) longer than ws_gap_ms."""

    def __init__(self, params: LatencyParams, clock: Callable[[], float] = time.monotonic) -> None:
        self._gap_s = params.ws_gap_ms / 1000
        self._clock = clock
        self._down_since: float | None = self._clock()  # not connected yet
        self._last_alive = self._clock()

    def mark_connected(self) -> None:
        self._down_since = None
        self._last_alive = self._clock()

    def mark_disconnected(self) -> None:
        if self._down_since is None:
            self._down_since = self._clock()

    def mark_alive(self) -> None:
        self._last_alive = self._clock()

    def breached(self) -> bool:
        now = self._clock()
        if self._down_since is not None:
            return now - self._down_since > self._gap_s
        return now - self._last_alive > self._gap_s

    async def run(self, on_breach: BreachHandler, interval_s: float = 0.05) -> None:
        while True:
            if self.breached():
                await on_breach("ws_gap")
            await asyncio.sleep(interval_s)
