"""Typed async pub/sub. Bounded queues: overflow means a consumer is behind real time,
which makes its data stale, so we report it instead of silently dropping events."""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)

OverflowHandler = Callable[[str], Awaitable[None]]


class EventBus:
    def __init__(self, queue_size: int = 10_000, on_overflow: OverflowHandler | None = None) -> None:
        self._queue_size = queue_size
        self._subs: dict[str, list[asyncio.Queue[Any]]] = defaultdict(list)
        self._on_overflow = on_overflow
        self._overflow_tasks: set[asyncio.Task[None]] = set()

    def set_overflow_handler(self, handler: OverflowHandler) -> None:
        self._on_overflow = handler

    def subscribe(self, topic: str) -> asyncio.Queue[Any]:
        q: asyncio.Queue[Any] = asyncio.Queue(maxsize=self._queue_size)
        self._subs[topic].append(q)
        return q

    def publish(self, topic: str, event: Any) -> None:
        for q in self._subs.get(topic, ()):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                log.error("bus overflow on topic=%s", topic)
                if self._on_overflow is not None:
                    task = asyncio.get_running_loop().create_task(self._on_overflow(topic))
                    self._overflow_tasks.add(task)
                    task.add_done_callback(self._overflow_tasks.discard)

    def flush(self) -> None:
        for queues in self._subs.values():
            for q in queues:
                while not q.empty():
                    q.get_nowait()
