"""Ingestion Engine: one deterministic pipeline per block.

    newHead -> ReorgGuard -> (reorg? drop orphans, emit revisions) -> logs of *that block hash*
            -> CandleAggregator.add_swap -> CandleAggregator.on_head -> publish closed candles

Logs are fetched by block hash (not a separate log subscription), so a block's swaps can never be
attributed to a different fork and there is no ordering race between heads and logs.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Protocol

from ..bus import EventBus
from ..domain import BlockHead, Swap
from ..latency import ConnectionWatchdog
from .candles import CandleAggregator
from .reorg import DeepReorgError, ReorgGuard

log = logging.getLogger(__name__)


class ChainReader(Protocol):
    def heads(self) -> AsyncIterator[BlockHead]: ...
    async def block_by_hash(self, block_hash: str) -> BlockHead: ...
    async def swaps_in_block(self, head: BlockHead, pools: set[str]) -> list[Swap]: ...
    async def ping(self) -> None: ...


class FreezeFn(Protocol):
    async def __call__(self, reason: str) -> object: ...


class IngestionEngine:
    def __init__(self, reader: ChainReader, bus: EventBus, watchdog: ConnectionWatchdog,
                 freeze: FreezeFn, pools: set[str] | None = None,
                 guard: ReorgGuard | None = None, aggregator: CandleAggregator | None = None) -> None:
        self._reader = reader
        self._bus = bus
        self._watchdog = watchdog
        self._freeze = freeze
        self.pools: set[str] = pools or set()
        self.guard = guard or ReorgGuard()
        self.aggregator = aggregator or CandleAggregator()

    async def process_head(self, head: BlockHead) -> None:
        await self.process_heads([head])

    async def process_heads(self, heads: list[BlockHead]) -> None:
        """Process consecutive heads as one batch: reorg checks per head, ONE log query for the batch.

        Base produces 5 blocks/s; one get_logs per block exceeds free-RPC rate limits."""
        tip = self.guard.tip
        gap_fn = getattr(self._reader, "swaps_in_range", None)
        if tip is not None and heads[0].number > tip + 1 and gap_fn is not None:
            # heads missed during a reconnect: fetch their swaps, otherwise those candles are incomplete
            missed = await gap_fn(tip + 1, heads[0])
            log.warning("filled ingestion gap: blocks %d..%d (%d swaps)", tip + 1, heads[0].number - 1, len(missed))
            for s in missed:
                if s.block_number < heads[0].number:
                    self._bus.publish("swap", s)
                    for c in self.aggregator.add_swap(s):
                        self._bus.publish("candle_revision", c)
        for head in heads:
            try:
                reorg = await self.guard.on_head(head, self._reader.block_by_hash)
            except DeepReorgError as exc:
                await self._freeze(f"deep_reorg:{exc}")
                return
            if reorg is not None:
                self._bus.publish("reorg", reorg)
                for c in self.aggregator.on_reorg(reorg.fork_block, None):
                    self._bus.publish("candle_revision", c)
        batch_fn = getattr(self._reader, "swaps_in_blocks", None)
        if batch_fn is not None:
            swaps = await batch_fn(heads, self.pools)
        else:
            swaps = [s for h in heads for s in await self._reader.swaps_in_block(h, self.pools)]
        for s in swaps:
            self._bus.publish("swap", s)
            for c in self.aggregator.add_swap(s):
                self._bus.publish("candle_revision", c)
        self._bus.publish("head", heads[-1])
        for c in self.aggregator.on_head(heads[-1]):
            self._bus.publish("candle", c)

    async def backfill(self, swaps: list[Swap], last_head: BlockHead) -> int:
        """Warm the aggregator with history so indicators are ready at start (Hurst needs ~2 h).

        Historical candles are NOT published: the strategy must never trade on a backfilled minute.
        """
        for s in sorted(swaps, key=lambda s: (s.block_number, s.log_index)):
            self.aggregator.add_swap(s)
        closed = self.aggregator.on_head(last_head)
        await self.guard.on_head(last_head)
        return len(closed)

    async def run(self, backoff_s: float = 0.2, max_backoff_s: float = 5.0,
                  batch_s: float = 1.0, lag_blocks: int = 3) -> None:
        """`lag_blocks`: the newest heads are held back one batch, because a load-balanced RPC may
        answer a by-number log query from a backend that has not reached that block yet."""
        delay = backoff_s
        loop = asyncio.get_running_loop()
        while True:
            pending: list[BlockHead] = []
            last_flush = loop.time()
            try:
                self._watchdog.mark_connected()
                async for head in self._reader.heads():
                    self._watchdog.mark_alive()
                    if pending and head.number <= pending[-1].number:
                        pending = [h for h in pending if h.number < head.number]  # replaced height
                    pending.append(head)
                    if loop.time() - last_flush >= batch_s and len(pending) > lag_blocks:
                        ready, pending = pending[:-lag_blocks], pending[-lag_blocks:]
                        await self.process_heads(ready)
                        last_flush = loop.time()
                    delay = backoff_s
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # free RPCs drop sockets routinely (e.g. close 1013): one line, not a traceback
                log.warning("ingestion stream dropped (%s: %s); reconnecting in %.1fs",
                            type(exc).__name__, str(exc)[:120], delay)
            self._watchdog.mark_disconnected()
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_backoff_s)
            reconnect = getattr(self._reader, "reconnect", None)
            if reconnect is not None:
                try:
                    await reconnect()
                except Exception:
                    log.exception("reconnect failed")

    async def keepalive(self, interval_s: float = 0.1) -> None:
        """WS ping loop for chains with slow blocks (not needed on Base: heads arrive every 0.2 s)."""
        while True:
            try:
                await asyncio.wait_for(self._reader.ping(), timeout=0.5)
                self._watchdog.mark_alive()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._watchdog.mark_disconnected()
            await asyncio.sleep(interval_s)
