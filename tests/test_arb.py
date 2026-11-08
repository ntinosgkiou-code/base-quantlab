import numpy as np
import pytest

from baselab.backtest.arb import legs, optimal_size, out_of, track_pair
from baselab.risk.slippage import V2Pool


def brute_best(rw_c, rt_c, f_c, rw_e, rt_e, f_e, hi=50.0):
    best, bx = 0.0, 0.0
    for x in np.linspace(1e-6, hi, 200_000):
        y = V2Pool(rw_c, rt_c, f_c).out(x)
        w = V2Pool(rt_e, rw_e, f_e).out(y)
        if w - x > best:
            best, bx = w - x, x
    return bx, best


def test_closed_form_matches_brute_force_and_pool_math():
    rw_c, rt_c, rw_e, rt_e, fc, fe = 100.0, 1_000_000.0, 100.0, 960_000.0, 0.003, 0.003
    A, B, C = (np.asarray([v]) for v in legs(np.asarray(rw_c), np.asarray(rt_c), fc, np.asarray(rw_e), np.asarray(rt_e), fe))
    x = optimal_size(A, B, C, cap=1e9)[0]
    bx, best = brute_best(rw_c, rt_c, fc, rw_e, rt_e, fe)
    assert x == pytest.approx(bx, rel=1e-3)
    assert float(out_of(A, B, C, np.asarray([x]))[0] - x) == pytest.approx(best, rel=1e-4)
    # the composite agrees with chaining the two real pool formulas
    y = V2Pool(rw_c, rt_c, fc).out(3.0)
    assert float(out_of(A, B, C, np.asarray([3.0]))[0]) == pytest.approx(V2Pool(rt_e, rw_e, fe).out(y), rel=1e-12)


def test_no_trade_when_gap_is_inside_the_fees():
    # 0.5% price gap but 0.3% + 0.3% fees -> spread < 0 -> zero size
    A, B, C = (np.asarray([v]) for v in legs(np.asarray(100.0), np.asarray(1_000_000.0), 0.003,
                                             np.asarray(100.5), np.asarray(1_000_000.0), 0.003))
    assert optimal_size(A, B, C, 1e9)[0] == 0.0


def test_delay_closes_the_gap_and_reverts_cost_gas():
    blocks = np.arange(0, 20)
    n = len(blocks)
    uw, ut = np.full(n, 100.0), np.full(n, 1_000_000.0)
    aw, at = np.full(n, 100.0), np.full(n, 1_000_000.0)
    aw[5:8] = 102.0   # gap opens at block 5 on the aerodrome side, closes again at block 8
    U, A = (uw, ut, 0.0005), (aw, at, 0.0005)
    gas = 1e-5
    d0 = track_pair("t", blocks, U, A, gas, 2.0, delay=0)
    d1 = track_pair("t", blocks, U, A, gas, 2.0, delay=1)
    d5 = track_pair("t", blocks, U, A, gas, 2.0, delay=5)
    assert len(d0) == 1 and d0[0].success and d0[0].net > 0 and d0[0].block == 5
    assert d1[0].success and d1[0].net == pytest.approx(d0[0].net)          # gap still open one block later
    assert not d5[0].success and d5[0].net == pytest.approx(-gas)           # closed by then: revert, gas lost


def test_cooldown_prevents_double_counting_one_gap():
    blocks = np.arange(0, 30)
    n = len(blocks)
    uw, ut = np.full(n, 100.0), np.full(n, 1_000_000.0)
    aw, at = np.full(n, 100.0), np.full(n, 1_000_000.0)
    aw[5:25] = 102.0   # a long-lived gap is ONE opportunity (one episode), not 20
    res = track_pair("t", blocks, (uw, ut, 0.0005), (aw, at, 0.0005), 1e-5, 2.0, delay=2)
    assert len(res) == 1 and res[0].block == 5


def test_stress_options_only_make_results_worse():
    blocks = np.arange(0, 40)
    n = len(blocks)
    uw, ut = np.full(n, 100.0), np.full(n, 1_000_000.0)
    aw, at = np.full(n, 100.0), np.full(n, 1_000_000.0)
    aw[5:30] = 101.0
    aw[8:10] = 100.2            # the gap shrinks right where a 3-block-late tx would land
    U, A = (uw, ut, 0.0005), (aw, at, 0.0005)
    base = track_pair("t", blocks, U, A, 1e-5, 2.0, delay=3)
    bid = track_pair("t", blocks, U, A, 1e-5, 2.0, delay=3, bid_frac=0.5)
    worst = track_pair("t", blocks, U, A, 1e-5, 2.0, delay=3, intra="worst")
    assert base and bid and worst
    assert bid[0].net < base[0].net and bid[0].net == pytest.approx(base[0].net - 1e-5 * 0 - 0.5 * base[0].predicted, rel=1e-6)
    assert worst[0].net <= base[0].net + 1e-15


def test_episode_durations():
    from baselab.backtest.arb import episode_durations
    blocks = np.arange(0, 40)
    n = len(blocks)
    uw, ut = np.full(n, 100.0), np.full(n, 1_000_000.0)
    aw, at = np.full(n, 100.0), np.full(n, 1_000_000.0)
    aw[5:8] = 102.0     # lasts 3 blocks (5,6,7) -> gone at block 8
    aw[20:21] = 102.0   # lasts 1 block
    d = sorted(episode_durations(blocks, (uw, ut, 0.0005), (aw, at, 0.0005), 1e-5, 2.0))
    assert d == [1, 3]
