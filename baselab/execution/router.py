"""Execution Router: intent lifecycle with time-of-use re-checks and ephemeral allowances.

Entry pipeline (every gate re-evaluated immediately before broadcast, P5):
    phase ARMED -> all RTT channels healthy -> signal fresh & not revised -> route impact <= max
    -> dynamic tolerance -> minOut -> sender.execute(approve exact -> swap -> approve 0)
    -> realized-slippage check -> position booked

Intents not yet broadcast can be cancelled at any time (task.cancel). Broadcast intents are never
cancelled mid-flight (that would lose track of real on-chain state); the sender replaces them instead.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from ..config import Config
from ..domain import Fill, Position, Side, Signal
from ..latency import RttMonitor
from ..quant.strategy import StrategyMatrix
from ..risk.guards import CircuitBreaker, DailyLossGuard
from ..risk.sizing import PositionSizer
from ..risk.slippage import Route, dynamic_tolerance, max_size_under_impact
from ..state import Phase, SystemState

log = logging.getLogger(__name__)


class MarketView(Protocol):
    def route(self, pool: str, side: Side, amount_in: float) -> Route: ...
    def price(self, pool: str) -> float: ...
    def tick_spacing(self, pool: str) -> int | None: ...


@dataclass(slots=True)
class Intent:
    id: str
    pool: str
    token: str
    side: Side
    amount_in: float
    expected_out: float
    min_out: float
    tolerance: float
    sent: bool = False
    created_at: float = field(default_factory=time.monotonic)


@dataclass(frozen=True, slots=True)
class ExecResult:
    actual_out: float
    gas_cost_quote: float
    block_number: int
    tx_hash: str


class TxSender(Protocol):
    async def execute(self, intent: Intent) -> ExecResult: ...
    async def cancel_pending(self) -> None: ...
    async def revoke_all(self) -> int: ...


@dataclass(slots=True)
class Portfolio:
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    realized: float = 0.0

    def equity(self, price_of: Callable[[str], float]) -> float:
        return self.cash + sum(p.token_amount * price_of(p.pool) for p in self.positions.values())


class AbortError(RuntimeError):
    pass


class ExecutionRouter:
    def __init__(self, cfg: Config, state: SystemState, latency: RttMonitor, strategy: StrategyMatrix,
                 market: MarketView, sender: TxSender, sizer: PositionSizer, breaker: CircuitBreaker,
                 daily: DailyLossGuard, portfolio: Portfolio,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._cfg = cfg
        self._state = state
        self._lat = latency
        self._strategy = strategy
        self._market = market
        self._sender = sender
        self.sizer = sizer
        self._breaker = breaker
        self._daily = daily
        self.portfolio = portfolio
        self._clock = clock
        self._ids = itertools.count(1)
        self._inflight: dict[str, tuple[Intent, asyncio.Task[Fill | None]]] = {}
        self._pos_lock = asyncio.Lock()
        self.closed_trades: list[dict[str, float | str]] = []
        self.aborts: dict[str, int] = {}

    # ------------------------------------------------------------------ gates
    def _abort(self, why: str) -> None:
        self.aborts[why.split(":")[0]] = self.aborts.get(why.split(":")[0], 0) + 1
        raise AbortError(why)

    def _check_entry_gates(self, sig: Signal) -> None:
        if not self._state.can_enter():
            self._abort(f"phase:{self._state.phase.value}")
        if not self._lat.all_healthy():
            self._abort(f"latency:{self._lat.snapshot()}")
        if self._clock() - sig.created_at > self._cfg.latency.max_signal_age_s:
            self._abort("stale_signal")
        if not self._strategy.is_current(sig):
            self._abort("signal_revoked")
        if sig.pool in self.portfolio.positions:
            self._abort("already_in_position")
        if len(self.portfolio.positions) >= self._cfg.risk.max_open_positions:
            self._abort("max_open_positions")

    def _tolerance(self, pool: str, route: Route, atr_over_price: float) -> float:
        r = self._cfg.risk
        return dynamic_tolerance(route.impact, atr_over_price, self._cfg.chain.block_time_s, 1,
                                 r.tolerance_z, r.max_tolerance, self._market.tick_spacing(pool))

    # ------------------------------------------------------------------ entry
    async def enter(self, sig: Signal) -> Fill | None:
        try:
            self._check_entry_gates(sig)
            r = self._cfg.risk
            price = self._market.price(sig.pool)
            equity = self.portfolio.equity(self._market.price)
            liq_cap = max_size_under_impact(lambda a: self._market.route(sig.pool, Side.BUY, a),
                                            r.max_impact, hi=self.portfolio.cash)
            stop_pct = max((price - sig.stop) / price, 1e-6)
            dec = self.sizer.size(equity, price, sig.atr, sig.vol_expansion, stop_pct, liq_cap,
                                  self._cfg.strategy.extreme_vol_expansion)
            if dec.quote_amount <= 0:
                self._abort(f"size_zero:{','.join(dec.reasons)}")
            amount = min(dec.quote_amount, r.max_notional_quote)  # hard cap, independent of the sizer
            route = self._market.route(sig.pool, Side.BUY, amount)
            if route.impact > r.max_impact:
                self._abort(f"impact:{route.impact:.4%}")
            tol = self._tolerance(sig.pool, route, sig.atr / price)
            intent = Intent(f"i{next(self._ids)}", sig.pool, sig.token, Side.BUY, amount,
                            route.total_out, route.total_out * (1 - tol), tol)
            self._check_entry_gates(sig)  # time-of-use re-check, right before broadcast
            self._strategy.consume(sig)
        except AbortError as exc:
            log.info("entry aborted %s: %s", sig.pool, exc)
            return None
        return await self._run_intent(intent, sig)

    async def _run_intent(self, intent: Intent, sig: Signal | None) -> Fill | None:
        task: asyncio.Task[Fill | None] = asyncio.create_task(self._execute(intent, sig))
        self._inflight[intent.id] = (intent, task)
        try:
            return await task
        except asyncio.CancelledError:
            if intent.sent:
                raise
            log.warning("intent %s cancelled before broadcast", intent.id)
            return None
        except Exception as exc:  # revert (minOut), RPC failure, simulation failure
            log.error("intent %s failed: %r", intent.id, exc)
            self.aborts["exec_failed"] = self.aborts.get("exec_failed", 0) + 1
            await self._state.freeze(f"execution_failed:{type(exc).__name__}")
            return None
        finally:
            self._inflight.pop(intent.id, None)

    async def _execute(self, intent: Intent, sig: Signal | None) -> Fill | None:
        res = await self._sender.execute(intent)
        fill = Fill(intent.id, intent.pool, intent.token, intent.side,
                    quote_amount=intent.amount_in if intent.side is Side.BUY else res.actual_out,
                    token_amount=res.actual_out if intent.side is Side.BUY else intent.amount_in,
                    expected_out=intent.expected_out, actual_out=res.actual_out,
                    block_number=res.block_number, tx_hash=res.tx_hash, timestamp=self._clock(),
                    gas_cost=res.gas_cost_quote)
        async with self._pos_lock:
            if intent.side is Side.BUY:
                self.portfolio.cash -= intent.amount_in + res.gas_cost_quote
                assert sig is not None
                entry = intent.amount_in / res.actual_out if res.actual_out > 0 else sig.price
                self.portfolio.positions[intent.pool] = Position(
                    intent.pool, intent.token, entry, res.actual_out, intent.amount_in + res.gas_cost_quote,
                    self._clock(), target=sig.target, stop=sig.stop, peak=entry, meta={"atr": sig.atr})
            else:
                self.portfolio.cash += res.actual_out - res.gas_cost_quote
        await self._breaker.on_fill(fill, intent.tolerance)
        return fill

    # ------------------------------------------------------------------ exit
    async def exit(self, pool: str, reason: str, emergency: bool = False) -> Fill | None:
        pos = self.portfolio.positions.get(pool)
        if pos is None:
            return None
        if not emergency and (not self._state.can_exit() or not self._lat.all_healthy()):
            log.warning("exit %s deferred: phase=%s latency_ok=%s", pool, self._state.phase.value,
                        self._lat.all_healthy())
            return None
        r = self._cfg.risk
        route = self._market.route(pool, Side.SELL, pos.token_amount)
        price = self._market.price(pool)
        atr_frac = max(pos.meta.get("atr", 0.0) / price, 0.0) if price > 0 else 0.0
        tol = r.emergency_max_slippage if emergency else max(self._tolerance(pool, route, atr_frac), route.impact)
        intent = Intent(f"i{next(self._ids)}", pool, pos.token, Side.SELL, pos.token_amount,
                        route.total_out, route.total_out * (1 - tol), tol)
        fill = await self._run_intent(intent, None)
        if fill is None:
            return None
        async with self._pos_lock:
            self.portfolio.positions.pop(pool, None)
            net = fill.actual_out - fill.gas_cost - pos.quote_spent  # entry gas is in quote_spent
            self.portfolio.realized += net
        ret = net / pos.quote_spent if pos.quote_spent > 0 else 0.0
        self.sizer.edge.record(ret)
        self.closed_trades.append({"pool": pool, "reason": reason, "net": net, "ret": ret})
        log.info("closed %s reason=%s net=%.6f ret=%.3f%%", pool, reason, net, ret * 100)
        await self._breaker.on_trade_closed(net)
        await self._daily.update(self.portfolio.equity(self._market.price))
        return fill

    # ------------------------------------------------------------------ fail-safes
    async def cancel_all_intents(self) -> int:
        n = 0
        for intent, task in list(self._inflight.values()):
            if not intent.sent:
                task.cancel()
                n += 1
        await self._sender.cancel_pending()
        return n

    async def on_safety_event(self, phase: Phase, reason: str) -> None:
        cancelled = await self.cancel_all_intents()
        if phase is Phase.HALTED:
            for pool in list(self.portfolio.positions):
                try:
                    await self.exit(pool, f"emergency:{reason}", emergency=True)
                except Exception:
                    log.exception("emergency flatten failed for %s", pool)
        revoked = await self._sender.revoke_all()
        log.warning("safety(%s): cancelled=%d revoked=%d", phase.value, cancelled, revoked)
