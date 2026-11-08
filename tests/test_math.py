import math

import numpy as np
import pytest

from baselab.domain import BlockHead, Candle
from baselab.execution.gas import FeeOracle, next_base_fee
from baselab.quant.indicators import atr, expected_rs, flow_imbalance, hurst, vwap
from baselab.risk.slippage import (
    V2Pool, V3Pool, dynamic_tolerance, impact, max_size_under_impact, split_route, sqrt_price_at_tick, v3_out,
)
from baselab.risk.sizing import EdgeEstimator
from baselab.security.checks import (
    HolderLedger, OwnerInfo, RoundTrip, check_lp_lock, check_ownership, check_round_trip,
)


def c(o, h, l, cl, v=1.0, b=0.5, s=0.5, m=0):
    return Candle("p", m, o, h, l, cl, v, b, s)


# ------------------------------------------------------------------ indicators
def test_vwap_and_ofi():
    cs = [c(1, 2, 0, 1, v=1, b=1, s=0), c(1, 4, 2, 3, v=3, b=0, s=3)]
    assert vwap(cs) == pytest.approx((1 * 1 + 3 * 3) / 4)
    assert flow_imbalance(cs) == pytest.approx((1 - 3) / 4)


def test_atr_wilder_constant_range():
    cs = [c(10, 11, 9, 10) for _ in range(30)]
    assert atr(cs, 14) == pytest.approx(2.0)
    assert math.isnan(atr(cs[:5], 14))


def test_expected_rs_reasonable():
    # E[R/S] grows ~ sqrt(n) for iid noise
    assert expected_rs(16) < expected_rs(64) < expected_rs(256)
    assert expected_rs(256) / expected_rs(64) == pytest.approx(2.0, rel=0.15)


def _ar1(phi, n, seed):
    rng = np.random.default_rng(seed)
    x = np.zeros(n)
    e = rng.standard_normal(n)
    for i in range(1, n):
        x[i] = phi * x[i - 1] + e[i]
    return x


def test_hurst_separates_regimes():
    noise = np.mean([hurst(_ar1(0.0, 512, s)) for s in range(20)])
    trend = np.mean([hurst(_ar1(0.6, 512, s)) for s in range(20)])
    revert = np.mean([hurst(_ar1(-0.6, 512, s)) for s in range(20)])
    assert abs(noise - 0.5) < 0.07
    assert trend > 0.55
    assert revert < 0.45


def test_hurst_fails_closed_on_flat_or_short():
    assert math.isnan(hurst(np.zeros(200)))
    assert math.isnan(hurst(np.ones(10)))


# ------------------------------------------------------------------ slippage
def test_v2_out_matches_uniswap_formula():
    p = V2Pool(100.0, 1000.0)
    x = 1.0 * 0.997
    assert p.out(1.0) == pytest.approx(x * 1000 / (100 + x))
    assert impact(p.out(1.0), 1.0, p.marginal) == pytest.approx(1 - p.out(1.0) / (10.0 * 0.997))
    assert impact(p.out(1e-9), 1e-9, p.marginal) == pytest.approx(0.0, abs=1e-9)  # fee is not slippage


def test_v3_single_range_equals_v2_virtual_reserves():
    # In one range with liquidity L, V3 behaves like V2 with x = L/sqrtP, y = L*sqrtP (fee aside)
    L, sp = 1_000_000.0, 1.0
    pool = V3Pool(sp, L, 0, 0.0, 60, ticks=(), zero_for_one=True)
    out, unfilled = v3_out(pool, 1000.0)
    x, y = L / sp, L * sp
    assert unfilled == 0
    assert out == pytest.approx(1000.0 * y / (x + 1000.0), rel=1e-9)


def test_v3_crosses_ticks_and_reports_exhaustion():
    L = 1000.0
    # one position on [-600, 600]: crossing tick -600 downward removes it (liquidityNet=+L)
    pool = V3Pool(sqrt_price_at_tick(0), L, 0, 0.0, 60, ticks=((-600, L), (600, -L)), zero_for_one=True)
    cap = L * (1 / sqrt_price_at_tick(-600) - 1 / sqrt_price_at_tick(0))
    out, unfilled = v3_out(pool, cap * 2)
    assert unfilled == pytest.approx(cap, rel=1e-6)
    assert out == pytest.approx(L * (sqrt_price_at_tick(0) - sqrt_price_at_tick(-600)), rel=1e-9)


def test_split_route_beats_single_venue_and_cap():
    a, b = V2Pool(100, 1000), V2Pool(100, 1000)
    one = split_route({"a": a.out}, {"a": a.marginal}, 10.0)
    two = split_route({"a": a.out, "b": b.out}, {"a": a.marginal, "b": b.marginal}, 10.0)
    assert two.total_out > one.total_out
    assert two.allocation["a"] == pytest.approx(5.0)
    size = max_size_under_impact(lambda s: split_route({"a": a.out}, {"a": a.marginal}, s), 0.004, 50.0)
    assert split_route({"a": a.out}, {"a": a.marginal}, size).impact <= 0.004 + 1e-9
    assert 0 < size < 50


def test_dynamic_tolerance_scales_with_vol_and_caps():
    lo = dynamic_tolerance(0.001, 0.002, 2.0, 1, 2.0, 0.05)
    hi = dynamic_tolerance(0.001, 0.02, 2.0, 1, 2.0, 0.05)
    assert lo < hi
    assert dynamic_tolerance(0.2, 0.5, 2.0, 1, 2.0, 0.01) == 0.01
    assert dynamic_tolerance(0.0, 0.0, 2.0, 1, 2.0, 0.05, tick_spacing=60) == pytest.approx((1.0001**60 - 1) / 2)


# ------------------------------------------------------------------ gas
def test_next_base_fee_eip1559_ethereum_params():
    full = BlockHead(1, "h", "p", 0, base_fee=100_000_000_000, gas_used=30_000_000, gas_limit=30_000_000)
    assert next_base_fee(full, 8, 2) == 112_500_000_000  # +12.5% when full
    empty = BlockHead(1, "h", "p", 0, base_fee=100_000_000_000, gas_used=0, gas_limit=30_000_000)
    assert next_base_fee(empty, 8, 2) == 87_500_000_000


def test_fee_oracle_clamps_tip():
    o = FeeOracle(250, 6)
    head = BlockHead(1, "h", "p", 0, base_fee=1_000_000, gas_used=20_000_000, gas_limit=120_000_000)
    q = o.quote(head, [5_000, 6_000, 10**12])
    assert q.max_priority_fee_per_gas <= 2 * 10**9
    assert q.max_fee_per_gas > q.projected_base_fee + q.max_priority_fee_per_gas - 1


# ------------------------------------------------------------------ security
def test_round_trip_tax_and_honeypot():
    ok = RoundTrip(True, True, True, 1000, 970, 1000, 960)
    passed, reasons, bt, st = check_round_trip(ok, 0.05, 0.05)
    assert passed and bt == pytest.approx(0.03) and st == pytest.approx(0.04)
    hp = RoundTrip(True, True, False, 1000, 1000, 1000, 0)
    assert not check_round_trip(hp, 0.05, 0.05)[0]
    taxed = RoundTrip(True, True, True, 1000, 900, 1000, 1000)
    assert "buy_tax:10.00%" in check_round_trip(taxed, 0.05, 0.05)[1]
    blocked = RoundTrip(True, False, True, 1000, 1000, 1000, 1000)
    assert "honeypot:transfer_blocked" in check_round_trip(blocked, 0.05, 0.05)[1]


def test_lp_and_ownership():
    assert check_lp_lock(96, 100, 0.95)[0]
    assert not check_lp_lock(50, 100, 0.95)[0]
    assert check_ownership(OwnerInfo(None, None, 0), 3600, True)[0]
    assert not check_ownership(OwnerInfo("0xabc", None, 0), 3600, True)[0]
    assert check_ownership(OwnerInfo("0xabc", 7200, 0), 3600, True)[0]
    assert "upgradeable_proxy" in check_ownership(OwnerInfo(None, None, 5), 3600, True)[1]


def test_holder_concentration_excludes_lp():
    L = HolderLedger()
    z = "0x0000000000000000000000000000000000000000"
    L.apply(z, "0xpair", 900, 1)
    for i in range(100):
        L.apply(z, f"0x{i}", 1, 1)
    top10, hhi, circ = L.concentration(["0xpair"])
    assert circ == 100
    assert top10 == pytest.approx(0.10)
    assert hhi == pytest.approx(100 * 0.01**2)


# ------------------------------------------------------------------ sizing
def test_kelly_prior_is_no_edge_and_learns():
    e = EdgeEstimator(0.5, 1.0, 20)
    assert e.kelly() == pytest.approx(0.0)
    for _ in range(60):
        e.record(0.02)
    for _ in range(40):
        e.record(-0.01)
    assert e.kelly() > 0
    e2 = EdgeEstimator(0.5, 1.0, 20)
    for _ in range(50):
        e2.record(0.01)
        e2.record(-0.02)
    assert e2.kelly() < 0
