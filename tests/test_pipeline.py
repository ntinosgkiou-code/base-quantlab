import asyncio
from datetime import datetime, timezone

import pytest

from baselab.backtest.replay import SimClock, run_backtest
from baselab.backtest.synthetic import SyntheticMarket
from baselab.config import Config, LatencyParams, Mode, RiskParams
from baselab.domain import BlockHead, Candle, Fill, Position, Side, Signal, Swap
from baselab.execution.paper import PaperSender
from baselab.execution.router import ExecutionRouter, Portfolio
from baselab.ingestion.candles import CandleAggregator
from baselab.ingestion.reorg import DeepReorgError, ReorgGuard
from baselab.latency import ConnectionWatchdog, RttMonitor
from baselab.quant.strategy import StrategyMatrix
from baselab.risk.exits import ExitPolicy
from baselab.risk.guards import CircuitBreaker, DailyLossGuard
from baselab.risk.sizing import PositionSizer
from baselab.state import Phase, SystemState

run = asyncio.run


def head(n, ts, h=None, parent=None):
    return BlockHead(n, h or f"0x{n}", parent or f"0x{n - 1}", ts)


def swap(n, ts, price, side=Side.BUY, li=0, h=None):
    return Swap("p", n, h or f"0x{n}", ts, li, side, price, 1.0)


# ------------------------------------------------------------------ state machine
def test_state_transitions_and_isolated_handlers():
    async def go():
        st = SystemState(handler_timeout_s=0.1)
        calls = []

        async def bad(ph, why):
            raise RuntimeError("boom")

        async def slow(ph, why):
            await asyncio.sleep(1)

        async def good(ph, why):
            calls.append((ph, why))

        for n, h in (("bad", bad), ("slow", slow), ("good", good)):
            st.on_safety_event(n, h)
        assert await st.arm()
        assert await st.freeze("latency")
        assert st.phase is Phase.FROZEN and calls == [(Phase.FROZEN, "latency")]
        assert await st.arm("recovered")
        assert await st.halt("2 losses")
        assert not await st.arm("nope")  # HALTED is terminal
        assert st.phase is Phase.HALTED
        await st.manual_reset("op")
        assert st.phase is Phase.BOOTING
    run(go())


def test_sleep_until_next_utc_day():
    async def go():
        now = [datetime(2026, 11, 8, 15, 0, tzinfo=timezone.utc)]
        st = SystemState(wall_clock=lambda: now[0])
        await st.arm()
        await st.sleep_until_next_utc_day("daily loss")
        assert st.sleep_until == datetime(2026, 11, 9, tzinfo=timezone.utc)
        assert not await st.maybe_wake()
        now[0] = datetime(2026, 11, 9, 0, 0, 1, tzinfo=timezone.utc)
        assert await st.maybe_wake() and st.phase is Phase.ARMED
    run(go())


# ------------------------------------------------------------------ candles / reorg
def test_candle_not_emitted_before_minute_closes():
    agg = CandleAggregator()
    agg.add_swap(swap(1, 0, 1.0))
    agg.add_swap(swap(2, 30, 2.0))
    assert agg.on_head(head(2, 30)) == []           # still inside minute 0: no lookahead
    out = agg.on_head(head(3, 62))
    assert len(out) == 1 and out[0].close == 2.0 and out[0].high == 2.0
    out = agg.on_head(head(40, 185))                 # minutes 60 and 120 had no swaps
    assert [c.synthetic for c in out] == [True, True]
    assert out[0].close == 2.0 and out[0].volume == 0


def test_reorg_rebuilds_candles():
    async def go():
        g, agg = ReorgGuard(), CandleAggregator()
        for n, ts in ((1, 0), (2, 10), (3, 20)):
            await g.on_head(head(n, ts))
        agg.add_swap(swap(2, 10, 1.0))
        agg.add_swap(swap(3, 20, 5.0))
        agg.on_head(head(4, 61))
        assert agg.candles("p")[0].close == 5.0
        alt3 = head(3, 20, h="0x3b", parent="0x2")
        alt4 = head(4, 61, h="0x4b", parent="0x3b")

        async def fetch(h):
            return {"0x3b": alt3}[h]
        r = await g.on_head(alt4, fetch)
        assert r is not None and r.fork_block == 3
        revs = agg.on_reorg(r.fork_block, None)
        assert len(revs) == 1 and revs[0].close == 1.0 and revs[0].revision == 1
        assert g.is_canonical(3, "0x3b")
    run(go())


def test_reconnect_gap_is_backfilled():
    from baselab.bus import EventBus
    from baselab.ingestion.engine import IngestionEngine

    class GapReader:
        def __init__(self):
            self.gap_calls = []

        async def block_by_hash(self, h):
            raise NotImplementedError

        async def swaps_in_block(self, head, pools):
            return [swap(head.number, head.timestamp, 1.0, li=0)]

        async def swaps_in_range(self, from_block, anchor):
            self.gap_calls.append((from_block, anchor.number))
            return [swap(n, n, 2.0, li=0) for n in range(from_block, anchor.number + 1)]

    async def go():
        r = GapReader()
        eng = IngestionEngine(r, EventBus(), ConnectionWatchdog(LatencyParams()), lambda why: asyncio.sleep(0),
                              pools={"p"})
        await eng.process_heads([head(1, 1)])
        await eng.process_heads([head(5, 5, parent="0x4")])   # blocks 2..4 missed during a reconnect
        assert r.gap_calls == [(2, 5)]
        await eng.process_heads([head(6, 61, parent="0x5")])
        c = eng.aggregator.candles("p")[0]
        assert c.volume == 5.0  # swaps of blocks 1..5, including the 3 recovered ones
    run(go())


def test_deep_reorg_raises():
    async def go():
        g = ReorgGuard(max_depth=2)
        for n in range(1, 6):
            await g.on_head(head(n, n))
        with pytest.raises(DeepReorgError):
            await g.on_head(head(6, 6, h="0x6x", parent="0x5x"))  # no fetch available
    run(go())


# ------------------------------------------------------------------ latency
def test_rtt_gate_and_ws_watchdog():
    t = [0.0]
    lat = RttMonitor(LatencyParams(), clock=lambda: t[0])
    for _ in range(10):
        lat.record("wss", 40)
    assert lat.all_healthy()
    lat.record("wss", 151)
    assert not lat.all_healthy()   # one spike invalidates immediately
    wd = ConnectionWatchdog(LatencyParams(), clock=lambda: t[0])
    wd.mark_connected()
    wd.mark_disconnected()
    t[0] = 0.4
    assert not wd.breached()
    t[0] = 0.51
    assert wd.breached()


# ------------------------------------------------------------------ router / risk
def _rig(market=None, risk=None):
    cfg = Config(mode=Mode.BACKTEST, risk=risk or RiskParams())
    market = market or SyntheticMarket(n_pools=1, seed=1)
    clock = SimClock(1000.0)
    st = SystemState(wall_clock=clock.wall)
    lat = RttMonitor(cfg.latency, clock=clock)
    for ch in ("wss", "private_rpc"):
        lat.record(ch, 30)
    strat = StrategyMatrix(cfg.strategy, clock=clock)
    sender = PaperSender(market, adverse_sigma=0.0)
    breaker = CircuitBreaker(cfg.risk, st)
    daily = DailyLossGuard(cfg.risk, st, wall_clock=clock.wall)
    router = ExecutionRouter(cfg, st, lat, strat, market, sender, PositionSizer(cfg.risk, cfg.mode),
                             breaker, daily, Portfolio(cash=1.0), clock=clock)
    st.on_safety_event("router", router.on_safety_event)
    return cfg, market, clock, st, lat, strat, sender, router, breaker, daily


def _signal(strat, clock, pool="0xpool00", price=1e-6):
    sig = Signal(pool, "0xtoken00", Side.BUY, clock(), 0, price, price * 0.01, 0.6, 0.5, 1.5, 0.1,
                 price * 1.02, price * 0.99)
    strat._live[pool] = sig  # registered as current, as evaluate() would
    return sig


def test_entry_toctou_gates_and_ephemeral_allowance():
    async def go():
        cfg, market, clock, st, lat, strat, sender, router, *_ = _rig()
        await st.arm()
        p = market.price("0xpool00")
        stale = _signal(strat, clock, price=p)
        clock.t += 10
        assert await router.enter(stale) is None and router.aborts["stale_signal"] == 1
        lat.record("wss", 500)
        assert await router.enter(_signal(strat, clock, price=p)) is None and "latency" in router.aborts
        for _ in range(40):
            lat.record("wss", 30)
        fill = await router.enter(_signal(strat, clock, price=p))
        assert fill is not None and fill.side is Side.BUY
        assert all(v == 0 for v in sender.allowances.values())  # approve(exact) -> swap -> approve(0)
        assert "0xpool00" in router.portfolio.positions
    run(go())


def test_live_hard_caps_notional_and_open_positions():
    async def go():
        risk = RiskParams(max_notional_quote=0.001, max_open_positions=1)
        cfg, market, clock, st, lat, strat, sender, router, *_ = _rig(SyntheticMarket(n_pools=2, seed=1), risk)
        await st.arm()
        f = await router.enter(_signal(strat, clock, "0xpool00", market.price("0xpool00")))
        assert f is not None and f.quote_amount <= 0.001 + 1e-12      # capped regardless of the sizer
        sig2 = Signal("0xpool01", "0xtoken01", Side.BUY, clock(), 0, market.price("0xpool01"), 1e-8, 0.6,
                      0.5, 1.5, 0.1, 2e-6, 0.5e-6)
        strat._live["0xpool01"] = sig2
        assert await router.enter(sig2) is None and router.aborts["max_open_positions"] == 1
    run(go())


def test_round_trip_pnl_includes_all_gas():
    async def go():
        cfg, market, clock, st, lat, strat, sender, router, *_ = _rig()
        await st.arm()
        await router.enter(_signal(strat, clock, price=market.price("0xpool00")))
        await router.exit("0xpool00", "test")
        t = router.closed_trades[-1]
        assert router.portfolio.cash == pytest.approx(1.0 + float(t["net"]))  # trade P&L == cash change
    run(go())


def test_circuit_breaker_two_losses_halts_and_flattens():
    async def go():
        cfg, market, clock, st, lat, strat, sender, router, breaker, _ = _rig()
        await st.arm()
        await breaker.on_trade_closed(-1.0)
        assert st.phase is Phase.ARMED
        await router.enter(_signal(strat, clock, price=market.price("0xpool00")))
        assert router.portfolio.positions
        await breaker.on_trade_closed(-1.0)
        assert st.phase is Phase.HALTED
        assert not router.portfolio.positions  # emergency flatten ran in the halt handler
    run(go())


def test_slippage_over_3pct_halts():
    async def go():
        _, _, _, st, *_rest = _rig()
        breaker = _rest[-2]
        await st.arm()
        f = Fill("i", "p", "t", Side.BUY, 1, 1, expected_out=100, actual_out=96, block_number=1, tx_hash="x",
                 timestamp=0)
        await breaker.on_fill(f, tolerance=0.01)
        assert st.phase is Phase.HALTED
    run(go())


def test_daily_loss_puts_system_to_sleep():
    async def go():
        _, _, _, st, *_rest = _rig()
        daily = _rest[-1]
        await st.arm()
        await daily.update(1.0)
        assert not await daily.update(0.975)
        assert await daily.update(0.969)
        assert st.phase is Phase.SLEEPING
    run(go())


def test_exit_policy_trailing_and_time():
    ex = ExitPolicy(trail_atr_mult=1.0, max_hold_s=180)
    pos = Position("p", "t", 100.0, 1.0, 100.0, opened_at=0.0, target=103.0, stop=99.0, peak=100.0)
    assert ex.update(pos, 101.0, 1.0, 10) is None
    assert pos.stop == pytest.approx(100.0)
    assert ex.update(pos, 99.9, 1.0, 20).reason == "trailing_stop"
    pos2 = Position("p", "t", 100.0, 1.0, 100.0, opened_at=0.0, target=103.0, stop=98.0, peak=100.0)
    assert ex.update(pos2, 100.2, 1.0, 181).reason == "time_exit_180s"


# ------------------------------------------------------------------ end to end
def test_backtest_runs_and_security_rejects_scams():
    res = run(run_backtest(blocks=6_000, seed=3))
    rej = res.security_rejections
    assert any("honeypot:sell_reverts" in r for r in rej.values())
    assert any(any(x.startswith("sell_tax") for x in r) for r in rej.values())
    assert any("upgradeable_proxy" in r for r in rej.values())
    assert any(any(x.startswith("top10") for x in r) for r in rej.values())
    traded = {t["pool"] for t in res.trades}
    assert not traded & set(rej)  # never traded a rejected pool
