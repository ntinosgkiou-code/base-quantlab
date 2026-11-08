import pytest

from baselab.backtest.real_replay import simulate_pool
from baselab.config import Config
from baselab.domain import Candle
from baselab.quant.alt_strategies import B_TP, eval_breakout, eval_rsi, rsi
from baselab.research.shadow import ShadowLedger
from tests.test_real_replay import PM, _v2_dataset


def cd(i, close, vol=1.0, high=None, synthetic=False):
    return Candle("p", i * 60, close, high or close, close, close, vol, vol / 2, vol / 2, synthetic=synthetic)


def test_rsi_extremes():
    assert rsi([float(i) for i in range(1, 40)]) == pytest.approx(100.0)
    assert rsi([float(100 - i) for i in range(40)]) == pytest.approx(0.0, abs=1e-9)
    assert rsi([1.0] * 40) == pytest.approx(50.0)


def test_rsi_entry_and_fee_rule():
    fall = [cd(i, 100.0 - i * 0.5) for i in range(60)]          # steady fall -> RSI ~ 0
    sig, why = eval_rsi(fall, round_trip_cost=0.001)
    assert sig is not None and sig.stop == pytest.approx(fall[-1].close * 0.995) and sig.target > sig.price
    sig, why = eval_rsi(fall, round_trip_cost=0.5)               # fees larger than half the edge -> skip
    assert sig is None and why[0].startswith("cost_exceeds_edge")
    sig, why = eval_rsi([cd(i, 100.0 + i) for i in range(60)], 0.001)
    assert sig is None                                           # rising market is not oversold


def test_breakout_needs_spike_and_new_high():
    base = [cd(i, 100.0, vol=1.0) for i in range(40)]
    spike = cd(40, 101.0, vol=10.0, high=101.0)
    sig, _ = eval_breakout(base + [spike], 0.001)
    assert sig is not None and sig.target == pytest.approx(101.0 * (1 + B_TP))
    assert eval_breakout(base + [cd(40, 101.0, vol=2.0, high=101.0)], 0.001)[0] is None     # no spike
    assert eval_breakout(base + [cd(40, 99.0, vol=10.0, high=99.0)], 0.001)[0] is None      # no breakout
    sig, why = eval_breakout(base + [spike], 0.006)
    assert sig is None and why[0].startswith("cost_exceeds_edge")                           # fee rule


def test_replay_runs_alt_strategies_with_fixed_exits():
    cfg = Config()
    ds = _v2_dataset(minutes=400, seed=3)
    for kind, reasons in (("rsi", {"take_profit", "hard_stop", "time_exit"}),
                          ("breakout", {"take_profit", "hard_stop", "time_exit"})):
        r = simulate_pool(ds, PM, cfg, 0.05, 0.0, 0.001, ShadowLedger(), kind)
        assert r.evaluations > 100
        for t in r.trades:
            assert t["reason"] in reasons and t["exit_ts"] > t["entry_ts"]
