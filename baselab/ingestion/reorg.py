"""Chain re-organisation detection by parent-hash linkage.

Invariant: `_hashes[n]` is our belief of the canonical hash at height n. A new head whose parent
does not match `_hashes[n-1]` (or that replaces a known height) proves our belief is wrong from some
fork point. We walk the new chain backwards by parent hash until it meets our stored chain; every
height above the meeting point is orphaned.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from ..domain import BlockHead, Reorg

log = logging.getLogger(__name__)

FetchByHash = Callable[[str], Awaitable[BlockHead]]


class DeepReorgError(RuntimeError):
    """Reorg deeper than we track (or unresolvable): caller must freeze and resync."""


class ReorgGuard:
    def __init__(self, max_depth: int = 64) -> None:
        self._max_depth = max_depth
        self._hashes: dict[int, str] = {}
        self._timestamps: dict[int, int] = {}

    @property
    def tip(self) -> int | None:
        return max(self._hashes) if self._hashes else None

    def hash_at(self, number: int) -> str | None:
        return self._hashes.get(number)

    def timestamp_at(self, number: int) -> int | None:
        return self._timestamps.get(number)

    def is_canonical(self, number: int, block_hash: str) -> bool:
        return self._hashes.get(number) == block_hash

    async def on_head(self, head: BlockHead, fetch: FetchByHash | None = None) -> Reorg | None:
        stored_parent = self._hashes.get(head.number - 1)
        replaced = head.number in self._hashes and self._hashes[head.number] != head.hash
        if head.number in self._hashes and not replaced:
            return None  # duplicate delivery
        reorg: Reorg | None = None
        if replaced or (stored_parent is not None and stored_parent != head.parent_hash):
            reorg = await self._resolve(head, fetch)
        self._store(head)
        return reorg

    async def _resolve(self, head: BlockHead, fetch: FetchByHash | None) -> Reorg:
        old_tip = self.tip if self.tip is not None else head.number
        fork = head.number
        new_chain: list[BlockHead] = []
        cursor_parent, n = head.parent_hash, head.number - 1
        while n in self._hashes and self._hashes[n] != cursor_parent:
            if fetch is None or head.number - n > self._max_depth:
                raise DeepReorgError(f"cannot resolve reorg at height {n}")
            blk = await fetch(cursor_parent)
            new_chain.append(blk)
            fork = n
            cursor_parent, n = blk.parent_hash, n - 1
        for k in [k for k in self._hashes if k >= fork]:
            del self._hashes[k]
            self._timestamps.pop(k, None)
        for blk in new_chain:
            self._store(blk)
        depth = old_tip - fork + 1
        log.warning("reorg detected: fork=%d depth=%d", fork, depth)
        return Reorg(fork_block=fork, depth=depth)

    def _store(self, head: BlockHead) -> None:
        self._hashes[head.number] = head.hash
        self._timestamps[head.number] = head.timestamp
        floor = head.number - self._max_depth * 2
        for k in [k for k in self._hashes if k < floor]:
            del self._hashes[k]
            self._timestamps.pop(k, None)
