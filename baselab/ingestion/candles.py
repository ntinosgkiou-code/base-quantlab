"""Reorg-safe, lookahead-free 1-minute candle aggregation.

* A candle for minute m is emitted only once a canonical block with timestamp >= m+60 exists
  (no lookahead: nothing downstream can see a minute before it is over).
* Candle content is a pure function of the canonical swaps in that minute, so a reorg is handled by
  dropping orphaned swaps and rebuilding: changed candles are re-emitted as revisions.
* Minutes without swaps are emitted as synthetic flat candles (zero-gap series for ATR/Hurst).
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import replace

from ..domain import BlockHead, Candle, Side, Swap

MINUTE = 60


def minute_of(ts: int) -> int:
    return ts - ts % MINUTE


def build_candle(pool: str, minute: int, swaps: list[Swap], prev_close: float | None) -> Candle | None:
    if not swaps:
        if prev_close is None:
            return None
        return Candle(pool, minute, prev_close, prev_close, prev_close, prev_close, 0.0, 0.0, 0.0, synthetic=True)
    ordered = sorted(swaps, key=lambda s: (s.block_number, s.log_index))
    prices = [s.price for s in ordered]
    buy = sum(s.quote_volume for s in ordered if s.side is Side.BUY)
    sell = sum(s.quote_volume for s in ordered if s.side is Side.SELL)
    return Candle(pool, minute, prices[0], max(prices), min(prices), prices[-1], buy + sell, buy, sell)


class CandleAggregator:
    def __init__(self, history: int = 500, retain_minutes: int = 15) -> None:
        self._swaps: dict[str, dict[tuple[int, int], Swap]] = defaultdict(dict)
        self._candles: dict[str, deque[Candle]] = defaultdict(lambda: deque(maxlen=history))
        self._next_minute: dict[str, int] = {}
        self._retain = retain_minutes * MINUTE

    def candles(self, pool: str) -> list[Candle]:
        return list(self._candles[pool])

    def pools(self) -> list[str]:
        return list(self._candles.keys() | self._swaps.keys())

    def add_swap(self, swap: Swap) -> list[Candle]:
        """Returns revisions if the swap lands in an already-emitted minute (late canonical data)."""
        self._swaps[swap.pool][(swap.block_number, swap.log_index)] = swap
        m = minute_of(swap.timestamp)
        self._next_minute.setdefault(swap.pool, m)
        if m < self._next_minute[swap.pool]:
            return self._rebuild(swap.pool, m)
        return []

    def on_head(self, head: BlockHead) -> list[Candle]:
        """Close every minute strictly before the head's minute."""
        closed: list[Candle] = []
        head_minute = minute_of(head.timestamp)
        for pool in list(self._next_minute):
            m = self._next_minute[pool]
            while m < head_minute:
                c = self._build(pool, m)
                if c is not None:
                    self._candles[pool].append(c)
                    closed.append(c)
                m += MINUTE
            self._next_minute[pool] = m
            self._prune(pool, head.timestamp)
        return closed

    def on_reorg(self, fork_block: int, fork_timestamp: int | None) -> list[Candle]:
        revisions: list[Candle] = []
        for pool, swaps in self._swaps.items():
            orphaned = [k for k in swaps if k[0] >= fork_block]
            if not orphaned:
                continue
            earliest = min(minute_of(swaps[k].timestamp) for k in orphaned)
            for k in orphaned:
                del swaps[k]
            start = earliest if fork_timestamp is None else min(earliest, minute_of(fork_timestamp))
            revisions.extend(self._rebuild(pool, start))
        return revisions

    def _minute_swaps(self, pool: str, m: int) -> list[Swap]:
        return [s for s in self._swaps[pool].values() if minute_of(s.timestamp) == m]

    def _prev_close(self, pool: str, m: int) -> float | None:
        for c in reversed(self._candles[pool]):
            if c.minute < m:
                return c.close
        return None

    def _build(self, pool: str, m: int) -> Candle | None:
        return build_candle(pool, m, self._minute_swaps(pool, m), self._prev_close(pool, m))

    def _rebuild(self, pool: str, start_minute: int) -> list[Candle]:
        out: list[Candle] = []
        hist = self._candles[pool]
        for i, old in enumerate(hist):
            if old.minute < start_minute:
                continue
            new = self._build(pool, old.minute)
            if new is None:
                continue
            new = replace(new, revision=old.revision + 1)
            if (new.open, new.high, new.low, new.close, new.volume) != \
                    (old.open, old.high, old.low, old.close, old.volume):
                hist[i] = new
                out.append(new)
        return out

    def _prune(self, pool: str, now_ts: int) -> None:
        cutoff = now_ts - self._retain
        swaps = self._swaps[pool]
        for k in [k for k, s in swaps.items() if s.timestamp < cutoff]:
            del swaps[k]
