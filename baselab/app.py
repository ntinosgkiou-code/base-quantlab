"""Composition root for paper/live: wires every layer and runs them under one TaskGroup.

Decoupling: layers talk only through the bus, the SystemState and the router's public methods.
A crash in any task cancels the group and the `finally` block freezes the system (allowances revoked).

Paper mode realism (what a demo account usually gets wrong):
* fills happen on the NEXT block's reserves (inclusion delay), never at the signal price
* gas = current Base gas price x real gas units + the OP-stack L1 data fee from GasPriceOracle
* every evaluation goes to the shadow ledger, so each filter's value is measured on real data
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

from .bus import EventBus
from .config import Config, Mode
from .domain import Candle, Side
from .latency import ConnectionWatchdog, RttMonitor
from .quant.indicators import atr
from .quant.strategy import StrategyMatrix
from .research.shadow import ShadowLedger
from .risk.exits import ExitPolicy
from .risk.guards import CircuitBreaker, DailyLossGuard
from .risk.sizing import PositionSizer
from .risk.slippage import Route, V2Pool, split_route
from .security.validator import SecurityValidator, Target
from .state import Phase, SystemState

log = logging.getLogger(__name__)

GAS_UNITS = 46_000 + 160_000 + 30_000  # approve(exact) + fee-on-transfer swap + approve(0)
GAS_ORACLE = "0x420000000000000000000000000000000000000F"
GAS_ORACLE_ABI = [{"name": "getL1Fee", "type": "function", "stateMutability": "view",
                   "inputs": [{"type": "bytes"}], "outputs": [{"type": "uint256"}]}]
PAIR_ABI = [{"name": "getReserves", "type": "function", "stateMutability": "view", "inputs": [],
             "outputs": [{"type": "uint112"}, {"type": "uint112"}, {"type": "uint32"}]}]


class ReserveMarket:
    """MarketView backed by V2 reserves refreshed from chain right before use."""

    def __init__(self) -> None:
        self.reserves: dict[str, tuple[float, float]] = {}  # pool -> (weth, token)

    def route(self, pool: str, side: Side, amount_in: float) -> Route:
        w, t = self.reserves[pool]
        amm = V2Pool(w, t) if side is Side.BUY else V2Pool(t, w)
        return split_route({"uni_v2": amm.out}, {"uni_v2": amm.marginal}, amount_in, chunks=8)

    def price(self, pool: str) -> float:
        w, t = self.reserves.get(pool, (0.0, 1.0))
        return w / t

    def tick_spacing(self, pool: str) -> int | None:
        return None


class JsonlLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, kind: str, **fields: object) -> None:
        rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "kind": kind, **fields}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")


async def run_live(cfg: Config, pools_file: Path, equity_weth: float, log_dir: Path,
                   backfill_minutes: int = 130, report_every_s: float = 600.0) -> None:
    from web3 import AsyncWeb3
    from web3.providers.rpc import AsyncHTTPProvider

    from .execution.chain import LiveSender, PoolMeta, Web3ChainReader, Web3SecurityReader
    from .execution.gas import FeeOracle
    from .execution.paper import PaperSender
    from .execution.router import ExecutionRouter, Intent, Portfolio
    from .ingestion.engine import IngestionEngine

    def quiet_dead_socket(loop: asyncio.AbstractEventLoop, ctx: dict) -> None:
        # web3.py leaves an un-awaited task when it closes a socket the server already dropped; harmless
        exc = ctx.get("exception")
        if exc is not None and "disconnect" in repr(ctx.get("future", "")):
            log.warning("websocket closed by server during disconnect (%s)", type(exc).__name__)
            return
        loop.default_exception_handler(ctx)

    asyncio.get_running_loop().set_exception_handler(quiet_dead_socket)
    run_dir = log_dir / datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    events = JsonlLog(run_dir / "events.jsonl")
    metas = {m["pool"].lower(): PoolMeta(**{**m, "pool": m["pool"].lower()})
             for m in json.loads(pools_file.read_text())}
    state = SystemState()
    bus = EventBus(cfg.bus_queue_size)
    bus.set_overflow_handler(lambda topic: state.freeze(f"bus_overflow:{topic}"))
    latency = RttMonitor(cfg.latency)
    watchdog = ConnectionWatchdog(cfg.latency)
    reader = Web3ChainReader(cfg, metas)
    w3 = await reader.connect()
    bt = await reader.measure_block_time()
    cfg = replace(cfg, chain=replace(cfg.chain, block_time_s=bt))
    reader = Web3ChainReader(cfg, metas)
    w3 = await reader.connect()
    print(f"measured block time: {bt:.3f} s/block")
    http_w3 = AsyncWeb3(AsyncHTTPProvider(cfg.chain.http_url)) if cfg.chain.http_url else None
    priv = AsyncWeb3(AsyncHTTPProvider(cfg.chain.private_rpc_url)) if cfg.chain.private_rpc_url else None

    def rpc() -> AsyncWeb3:
        """HTTP for request/response reads; the WS object is replaced on every reconnect."""
        assert reader.w3 is not None
        return http_w3 or reader.w3

    http = rpc()
    market = ReserveMarket()
    strategy = StrategyMatrix(cfg.strategy)
    sizer = PositionSizer(cfg.risk, cfg.mode)
    breaker = CircuitBreaker(cfg.risk, state)
    daily = DailyLossGuard(cfg.risk, state)
    fee = FeeOracle(cfg.chain.base_fee_change_denominator, cfg.chain.elasticity_multiplier)
    shadow = ShadowLedger(path=run_dir / "shadow.jsonl")
    new_head = asyncio.Event()

    async def refresh(pool: str) -> None:
        m = metas[pool]
        c = rpc().eth.contract(address=rpc().to_checksum_address(pool), abi=PAIR_ABI)
        r0, r1, _ = await c.functions.getReserves().call()
        w, t = (r0, r1) if m.weth_is_token0 else (r1, r0)
        market.reserves[pool] = (w / 1e18, t / 10**m.token_decimals)

    async def next_block_then_refresh(intent: Intent) -> None:
        new_head.clear()
        try:
            await asyncio.wait_for(new_head.wait(), timeout=10)
        except TimeoutError:
            raise RuntimeError("no new block within 10s: inclusion unknown")
        await refresh(intent.pool)

    async def paper_gas() -> float:
        oracle = rpc().eth.contract(address=rpc().to_checksum_address(GAS_ORACLE), abi=GAS_ORACLE_ABI)
        gas_price = await rpc().eth.gas_price
        l1 = await oracle.functions.getL1Fee(b"\x01" * 180).call()  # ~ size of one signed swap tx
        return (gas_price * GAS_UNITS + 3 * l1) / 1e18

    if cfg.mode is Mode.LIVE:
        if http_w3 is None:
            raise SystemExit("live mode requires --http (a WS connection can be replaced mid-trade)")
        sender = LiveSender(cfg, http_w3, os.environ["PRIVATE_KEY"], metas, fee, os.environ.get("FLASHBOTS_AUTH_KEY"))
    else:
        sender = PaperSender(market, pre_fill=next_block_then_refresh, gas_fn=paper_gas)
    portfolio = Portfolio(cash=equity_weth)
    router = ExecutionRouter(cfg, state, latency, strategy, market, sender, sizer, breaker, daily, portfolio)
    validator = SecurityValidator(Web3SecurityReader(cfg, http, Path(__file__).parent / "security" / "simulator.bin"),
                                  cfg.security,
                                  ttl_blocks=int(60 / bt),          # a PASS is trusted for 1 minute
                                  reject_ttl_blocks=int(600 / bt))  # a REJECT is not re-scanned for 10 minutes
    engine = IngestionEngine(reader, bus, watchdog, state.freeze, pools=set(metas))
    exits = ExitPolicy(cfg.strategy.trail_atr_mult, cfg.risk.max_hold_s)
    state.on_safety_event("router", router.on_safety_event)

    async def flush_caches(ph: Phase, why: str) -> None:
        strategy.flush()
        validator.flush()
        events.write("safety", phase=ph.value, reason=why)
    state.on_safety_event("caches", flush_caches)

    # ---------------------------------------------------------------- warm start
    anchor = await reader.latest_head()
    t0 = time.perf_counter()
    hist = await reader.swaps_in_range(anchor.number - int(backfill_minutes * 60 / bt), anchor)
    n_closed = await engine.backfill(hist, anchor)
    for pool in metas:
        await refresh(pool)
    events.write("backfill", swaps=len(hist), candles=n_closed, seconds=round(time.perf_counter() - t0, 1),
                 per_pool={p: len(engine.aggregator.candles(p)) for p in metas})
    print(f"[{run_dir.name}] backfilled {len(hist)} swaps -> {n_closed} candles; logs in {run_dir}")

    # ---------------------------------------------------------------- tasks
    async def strategy_loop() -> None:
        q = bus.subscribe("candle")
        rq = bus.subscribe("candle_revision")
        while True:
            c: Candle = await q.get()
            while not rq.empty():
                strategy.on_revision(rq.get_nowait())
            shadow.on_candle(c)
            m = metas[c.pool]
            if c.pool in portfolio.positions:
                continue
            # the strategy is evaluated (and shadow-recorded) for EVERY pool, so filter value is measured
            # on real data even when security blocks trading; only security-approved signals may trade
            # conservative cost estimate before sizing: 2 x LP fee + 2 x the max allowed impact
            ev = strategy.evaluate(c.pool, m.token, engine.aggregator.candles(c.pool),
                                   round_trip_cost=2 * m.fee_ppm / 1e6 + 2 * cfg.risk.max_impact)
            shadow.record(c.pool, c.minute, c.close, ev.reasons, ev.metrics)
            events.write("eval", pool=c.pool, minute=c.minute, passed=ev.signal is not None,
                         reasons=ev.reasons, metrics={k: v for k, v in ev.metrics.items() if math.isfinite(v)})
            rep = await validator.check(Target(m.token, m.pool, m.is_v3), engine.guard.tip or 0)
            if not rep.passed:
                events.write("security_reject", pool=c.pool, reasons=rep.reasons)
                if ev.signal is not None:
                    strategy.consume(ev.signal)
                continue
            if ev.signal is not None:
                await refresh(c.pool)
                fill = await router.enter(ev.signal)
                lat_ms = (time.monotonic() - ev.signal.created_at) * 1000
                events.write("entry" if fill else "entry_aborted", pool=c.pool, signal_to_fill_ms=round(lat_ms),
                             fill=asdict(fill) if fill else None, aborts=dict(router.aborts),
                             rtt=latency.snapshot())

    async def exit_loop() -> None:
        hq = bus.subscribe("head")
        while True:
            await hq.get()
            new_head.set()
            for pool, pos in list(portfolio.positions.items()):
                await refresh(pool)
                hist_c = engine.aggregator.candles(pool)
                a = atr(hist_c[-15:], cfg.strategy.atr_fast) if len(hist_c) >= 15 else pos.meta["atr"]
                d = exits.update(pos, market.price(pool), a if math.isfinite(a) else pos.meta["atr"], time.monotonic())
                if d is not None:
                    n_before = len(router.closed_trades)
                    await router.exit(pool, d.reason)
                    if len(router.closed_trades) > n_before:
                        events.write("exit", **router.closed_trades[-1], equity=portfolio.equity(market.price))

    kill_file = Path("STOP")

    async def supervisor() -> None:
        last_beat = 0.0
        while True:
            if time.monotonic() - last_beat >= 60:  # stage-2 uptime metric (P3) = share of ARMED heartbeats
                last_beat = time.monotonic()
                events.write("heartbeat", phase=state.phase.value, open=len(portfolio.positions))
            if kill_file.exists() and state.phase is not Phase.HALTED:
                # operator kill switch: create a file named STOP -> halt, flatten, revoke allowances
                await state.halt("kill switch: STOP file present")
                events.write("kill_switch", path=str(kill_file.resolve()))
            if state.phase is Phase.FROZEN and latency.healthy_for(cfg.latency.recovery_s) \
                    and not watchdog.breached():
                await state.arm("recovered")
            await state.maybe_wake()
            await asyncio.sleep(0.25)

    async def reporter() -> None:
        while True:
            await asyncio.sleep(report_every_s)
            trades = router.closed_trades
            net = sum(float(t["net"]) for t in trades)
            rtt = {k: (round(v) if math.isfinite(v) else "timeout") for k, v in latency.snapshot().items()}
            freezes = sum(1 for t in state.history if t.dst is Phase.FROZEN)
            txt = "\n".join([
                f"== {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC  phase={state.phase.value}  freezes={freezes}",
                f"trades={len(trades)} net={net:+.6f} WETH equity={portfolio.equity(market.price):.6f} "
                f"open={list(portfolio.positions)} kelly={sizer.edge.kelly():+.3f} n={sizer.edge.n}",
                f"aborts={router.aborts} rtt_ms={rtt}",
                "shadow ledger (forward 3-min return of a hypothetical buy, gross):",
                shadow.report(),
            ])
            print(txt, flush=True)
            with (run_dir / "report.txt").open("a", encoding="utf-8") as f:
                f.write(txt + "\n\n")

    async def private_rpc_probe() -> None:
        assert priv is not None
        await priv.eth.chain_id

    await daily.update(equity_weth)
    await state.arm("boot")
    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(engine.run())
            tg.create_task(watchdog.run(state.freeze))
            tg.create_task(latency.run_probe("wss", reader.ping, state.freeze))
            if priv is not None:  # without a private RPC there is no second channel to watch
                tg.create_task(latency.run_probe("private_rpc", private_rpc_probe, state.freeze))
            tg.create_task(strategy_loop())
            tg.create_task(exit_loop())
            tg.create_task(supervisor())
            tg.create_task(reporter())
    finally:
        await state.freeze("shutdown")
