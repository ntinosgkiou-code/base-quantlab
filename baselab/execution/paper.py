"""Paper sender: same interface as the live sender, fills against the market view with random adverse
slippage, and models allowances so the ephemeral approve->swap->revoke cycle is exercised (and asserted)
exactly like live.

Realism hooks (used by paper mode on real chain data):
* `pre_fill`  - awaited after "broadcast": waits for the NEXT block and refreshes reserves, so the fill
                price is the post-inclusion price, never the price that produced the signal.
* `gas_fn`    - returns the current cost of approve + swap + revoke in quote units.
"""
from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable

from ..domain import Side
from .router import ExecResult, Intent, MarketView

ROUTER = "router"


class PaperSender:
    def __init__(self, market: MarketView, seed: int = 7, adverse_sigma: float = 0.001,
                 gas_cost_quote: float = 0.00002,
                 pre_fill: Callable[[Intent], Awaitable[None]] | None = None,
                 gas_fn: Callable[[], Awaitable[float]] | None = None) -> None:
        self._market = market
        self._rng = random.Random(seed)
        self._sigma = adverse_sigma
        self._gas = gas_cost_quote
        self._pre_fill = pre_fill
        self._gas_fn = gas_fn
        self.allowances: dict[tuple[str, str], float] = {}
        self._block = 0

    async def execute(self, intent: Intent) -> ExecResult:
        token_in = "WETH" if intent.side is Side.BUY else intent.token
        self.allowances[(token_in, ROUTER)] = intent.amount_in     # approve(exact)
        intent.sent = True
        gas = await self._gas_fn() if self._gas_fn is not None else 3 * self._gas
        if self._pre_fill is not None:
            await self._pre_fill(intent)                            # inclusion delay: next block's state
        else:
            await asyncio.sleep(0)
        route = self._market.route(intent.pool, intent.side, intent.amount_in)  # state at execution time
        actual = route.total_out * (1 - abs(self._rng.gauss(0.0, self._sigma)))
        self._block += 1
        if actual < intent.min_out:
            self.allowances[(token_in, ROUTER)] = 0.0              # revoke still lands
            # on-chain the swap reverts (minOut) -> gas spent, nothing received
            raise RuntimeError(f"swap reverted: out {actual:.8f} < minOut {intent.min_out:.8f}")
        self.allowances[(token_in, ROUTER)] -= intent.amount_in    # router pulls exactly amount_in
        self.allowances[(token_in, ROUTER)] = 0.0                  # approve(0), same tx batch
        apply = getattr(self._market, "apply_trade", None)
        if apply is not None:
            apply(intent.pool, intent.side, intent.amount_in)       # our own price impact
        return ExecResult(actual, gas, self._block, f"paper-{intent.id}")

    async def cancel_pending(self) -> None:
        return None

    async def revoke_all(self) -> int:
        n = sum(1 for v in self.allowances.values() if v != 0)
        for k in self.allowances:
            self.allowances[k] = 0.0
        return n
