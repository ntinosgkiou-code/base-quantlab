"""Point-in-time replay through the REAL pipeline (ingestion -> security -> strategy -> router -> risk).

Anti-lookahead: a signal produced while processing block t executes against the pool state AFTER
block t+1's swaps (we never fill at the candle close that generated the signal). Exit decisions made
on block t also execute on block t+1. Anti-survivorship: the universe is every generated pool,
including rugs and honeypots.
"""
from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..bus import EventBus
from ..config import ChainConfig, Config, Mode
from ..domain import BlockHead, Candle, Swap
from ..execution.paper import PaperSender
from ..execution.router import ExecutionRouter, Portfolio
from ..ingestion.engine import IngestionEngine
from ..latency import ConnectionWatchdog, RttMonitor
from ..quant.indicators import atr
from ..quant.strategy import StrategyMatrix
from ..risk.exits import ExitPolicy
from ..risk.guards import CircuitBreaker, DailyLossGuard
from ..risk.sizing import PositionSizer
from ..security.validator import SecurityValidator, Target
from ..state import Phase, SystemState
from .synthetic import SyntheticMarket, SyntheticSecurityReader

log = logging.getLogger(__name__)


class SimClock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def wall(self) -> datetime:
        return datetime.fromtimestamp(self.t, tz=timezone.utc)


class _BlockReader:
    """ChainReader fed one block at a time by the replay loop."""

    def __init__(self) -> None:
        self.pending: list[Swap] = []

    def heads(self) -> AsyncIterator[BlockHead]:
        raise NotImplementedError

    async def block_by_hash(self, block_hash: str) -> BlockHead:
        raise NotImplementedError

    async def swaps_in_block(self, head: BlockHead, pools: set[str]) -> list[Swap]:
        out, self.pending = self.pending, []
        return out

    async def ping(self) -> None:
        return None


@dataclass(slots=True)
class BacktestResult:
    blocks: int
    trades: list[dict[str, float | str]]
    start_equity: float
    end_equity: float
    max_drawdown: float
    halts: int
    sleeps: int
    freezes: int
    aborts: dict[str, int]
    security_rejections: dict[str, tuple[str, ...]]
    edge_n: int
    kelly: float
    expectancy: float
    equity_curve: list[float] = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        return sum(1 for t in self.trades if float(t["net"]) > 0) / len(self.trades) if self.trades else 0.0

    def summary(self) -> str:
        reasons: dict[str, int] = {}
        for t in self.trades:
            reasons[str(t["reason"])] = reasons.get(str(t["reason"]), 0) + 1
        ret = self.end_equity / self.start_equity - 1
        lines = [
            f"blocks={self.blocks}  ({self.blocks * 2 / 3600:.1f} h simulated)",
            f"trades={len(self.trades)}  win_rate={self.win_rate:.1%}  expectancy/trade={self.expectancy:.3%}",
            f"equity {self.start_equity:.4f} -> {self.end_equity:.4f} WETH  ({ret:+.2%})  max_dd={self.max_drawdown:.2%}",
            f"halts={self.halts} sleeps={self.sleeps} freezes={self.freezes}  kelly(f*)={self.kelly:.3f} n={self.edge_n}",
            f"exit reasons={reasons}",
            f"aborts={self.aborts}",
            "security rejections:",
            *[f"  {k}: {', '.join(v)}" for k, v in self.security_rejections.items()],
        ]
        return "\n".join(lines)


async def run_backtest(cfg: Config | None = None, blocks: int = 20_000, seed: int = 42,
                       equity: float = 1.0, reset_halts: bool = True,
                       latency_spike_prob: float = 0.002) -> BacktestResult:
    # the synthetic market emits one block every 2 s
    cfg = cfg or Config(mode=Mode.BACKTEST, chain=ChainConfig(block_time_s=2.0))
    market = SyntheticMarket(seed=seed)
    clock = SimClock(float(market.ts))
    state = SystemState(wall_clock=clock.wall)
    bus = EventBus(cfg.bus_queue_size)
    latency = RttMonitor(cfg.latency, clock=clock)
    watchdog = ConnectionWatchdog(cfg.latency, clock=clock)
    strategy = StrategyMatrix(cfg.strategy, clock=clock)
    sizer = PositionSizer(cfg.risk, cfg.mode)
    breaker = CircuitBreaker(cfg.risk, state)
    daily = DailyLossGuard(cfg.risk, state, wall_clock=clock.wall)
    portfolio = Portfolio(cash=equity)
    sender = PaperSender(market, seed=seed)
    router = ExecutionRouter(cfg, state, latency, strategy, market, sender, sizer, breaker, daily,
                             portfolio, clock=clock)
    exits = ExitPolicy(cfg.strategy.trail_atr_mult, cfg.risk.max_hold_s)
    validator = SecurityValidator(SyntheticSecurityReader(market), cfg.security)
    reader = _BlockReader()
    engine = IngestionEngine(reader, bus, watchdog, state.freeze, pools=set(market.pools))
    candles_q = bus.subscribe("candle")
    revisions_q = bus.subscribe("candle_revision")
    bus.set_overflow_handler(lambda topic: state.freeze(f"bus_overflow:{topic}"))
    state.on_safety_event("router", router.on_safety_event)
    state.on_safety_event("caches", lambda ph, why: _flush(strategy, validator))

    await daily.update(equity)
    await state.arm("backtest start")
    pending_signals: list = []
    pending_exits: list[tuple[str, str]] = []
    rejections: dict[str, tuple[str, ...]] = {}
    halts = sleeps = freezes = 0
    peak, max_dd, curve = equity, 0.0, []

    for _ in range(blocks):
        head, swaps = market.step()
        clock.t = float(head.timestamp)
        rtt = 400.0 if market.rng.random() < latency_spike_prob else 30.0 + market.rng.random() * 40
        for ch in ("wss", "private_rpc"):
            latency.record(ch, rtt)
        if not latency.all_healthy() and state.phase is Phase.ARMED:
            await state.freeze("latency_spike")
        # 1) execute decisions made on the previous block against THIS block's state (no lookahead)
        for pool, why in pending_exits:
            await router.exit(pool, why)
        pending_exits.clear()
        for sig in pending_signals:
            await router.enter(sig)
        pending_signals.clear()
        # 2) ingest the block through the real pipeline
        reader.pending = swaps
        await engine.process_head(head)
        while not revisions_q.empty():
            strategy.on_revision(revisions_q.get_nowait())
        closed: list[Candle] = []
        while not candles_q.empty():
            closed.append(candles_q.get_nowait())
        # 3) strategy on closed candles of security-approved tokens
        for c in closed:
            p = market.pools[c.pool]
            rep = await validator.check(Target(p.token, p.address), head.number)
            if not rep.passed:
                rejections[p.address] = rep.reasons
                continue
            if c.pool in portfolio.positions:
                continue
            ev = strategy.evaluate(c.pool, p.token, engine.aggregator.candles(c.pool))
            if ev.signal is not None:
                pending_signals.append(ev.signal)
        # 4) exits on every block (trailing stop is intra-minute; ATR from closed candles only)
        for pool, pos in list(portfolio.positions.items()):
            hist = engine.aggregator.candles(pool)
            a = atr(hist[-15:], cfg.strategy.atr_fast) if len(hist) >= 15 else pos.meta.get("atr", 0.0)
            if not math.isfinite(a):
                a = pos.meta.get("atr", 0.0)
            d = exits.update(pos, market.price(pool), a, clock.t)
            if d is not None:
                pending_exits.append((pool, d.reason))
        # 5) supervisor: recovery and day roll
        if state.phase is Phase.FROZEN and latency.healthy_for(cfg.latency.recovery_s):
            await state.arm("recovered")
        await state.maybe_wake()
        if state.phase is Phase.HALTED and reset_halts:
            halts += 1
            await state.manual_reset("backtest auto-reset (research only)")
            breaker.consecutive_losses = 0
            await state.arm("post-reset")
        eq = portfolio.equity(market.price)
        curve.append(eq)
        await daily.update(eq)
        peak = max(peak, eq)
        max_dd = max(max_dd, 1 - eq / peak if peak > 0 else 0.0)

    for t in state.history:
        if t.dst is Phase.SLEEPING:
            sleeps += 1
        elif t.dst is Phase.FROZEN:
            freezes += 1
    if not reset_halts:
        halts = sum(1 for t in state.history if t.dst is Phase.HALTED)
    return BacktestResult(blocks, router.closed_trades, equity, portfolio.equity(market.price), max_dd,
                          halts, sleeps, freezes, router.aborts, rejections, sizer.edge.n,
                          sizer.edge.kelly(), sizer.edge.expectancy(), curve)


async def _flush(strategy: StrategyMatrix, validator: SecurityValidator) -> None:
    strategy.flush()
    validator.flush()


def main(blocks: int = 20_000, seed: int = 42) -> None:
    res = asyncio.run(run_backtest(blocks=blocks, seed=seed))
    print(res.summary())
