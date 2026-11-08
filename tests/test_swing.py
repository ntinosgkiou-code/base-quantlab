import numpy as np
import pytest

from baselab.backtest.swing import PERIOD, PoolSeries, SwingParams, rolling_prev_stats, run_arm, zscores
from baselab.research.stats import cluster_bootstrap_ci


def make_series(price_fn, days=30, fee=0.003, rw0=200.0, seed=0, step_s=60):
    """Reserves series whose WETH/token price follows price_fn(t_seconds); constant product k."""
    t = np.arange(0, days * 86400, step_s, dtype=float)
    price = np.asarray([price_fn(x) for x in t])
    k = rw0 * rw0 / 1.0
    rt = np.sqrt(k / price)
    rw = price * rt
    return PoolSeries("0xpool", "TST", fee, t, rw, rt)


def test_rolling_stats_exclude_current_and_need_history():
    x = np.arange(10, dtype=float)
    mu, sd = rolling_prev_stats(x, 4, 3)
    assert np.isnan(mu[2]) and mu[3] == pytest.approx(1.0)       # mean of x[0:3] = 1, current (3) excluded
    assert mu[9] == pytest.approx(np.mean(x[5:9]))


def test_zscore_has_no_lookahead():
    rng = np.random.default_rng(1)
    p = np.exp(np.cumsum(rng.normal(0, 0.01, 2000)))
    z1, _ = zscores(p)
    p2 = p.copy()
    p2[1500:] *= 3                 # future change must not alter earlier z values
    z2, _ = zscores(p2)
    assert np.allclose(z1[:1500], z2[:1500], equal_nan=True)


def test_mean_reverting_shocks_are_profitable_for_the_reversion_arm_and_accounting_is_exact():
    def f(t):
        h = (t / 3600.0) % 24.0
        if 12.0 <= h < 12.5:
            return 0.80                          # sudden -20%, then flat
        if 12.5 <= h < 13.5:
            return 0.80 + 0.20 * (h - 12.5)      # linear recovery back to exactly 1.0 at 13:30
        return 1.0
    s = make_series(f, days=20)
    p = SwingParams(arm="L", cost_multiple=0.0, gas_round_trip=0.0, haircut=0.0, hurst_gate=False)
    tr, c, _ = run_arm(s, p)
    assert c.trades >= 10 and len(tr) == c.trades
    for t in tr:
        assert t["exit_ts"] > t["entry_ts"]
        assert t["net_ret"] == pytest.approx(t["net"] / p.notional)
        assert t["net_ret"] == pytest.approx(t["gross_ret"], abs=1e-12)   # no gas -> net == gross
    assert np.mean([t["net_ret"] for t in tr]) > 0.15                      # bought the bottom of a dip that really reverts
    assert all(t["reason"] == "recovered" for t in tr)


def test_no_fake_recovery_exit_when_the_drop_simply_ages_out_of_the_3h_window():
    def f(t):
        day = t / 86400
        return 1.0 + 0.002 * np.sin(t / 3000.0) if day < 10 else 0.90   # permanent -10%: it never recovers
    s = make_series(f, days=12)
    tr, c, _ = run_arm(s, SwingParams(arm="M", cost_multiple=0.0, hurst_gate=False))
    assert tr, "the permanent drop must trigger an entry"
    assert all(t["reason"] != "recovered" for t in tr)                    # price never got back to its old level
    assert all(t["reason"] in ("time", "stop", "end") for t in tr)


def test_stop_is_hit_on_an_event_and_costs_make_the_loss_larger_than_the_price_move():
    def f(t):
        day = t / 86400
        if day < 10:
            return 1.0 + 0.002 * np.sin(t / 3000.0)
        if day < 10.1:
            return 0.90        # sudden -10% -> z << -2.5, entry
        return 0.80            # falls further 2.4 h later -> -1.5% stop from the entry mid
    s = make_series(f, days=12)
    tr, c, _ = run_arm(s, SwingParams(arm="M", cost_multiple=0.0, hurst_gate=False))
    stops = [t for t in tr if t["reason"] == "stop"]
    assert stops and stops[0]["net_ret"] < -0.1                           # sold into the lower pool state
    flat = make_series(lambda t: 1.0 + 0.0005 * np.sin(t / 5000.0), days=15)
    tr2, _, _ = run_arm(flat, SwingParams(arm="M", z_entry=0.5, cost_multiple=0.0, hurst_gate=False))
    assert all(t["net_ret"] < 0 for t in tr2)                             # fees + gas + haircut on a flat price


def test_cluster_bootstrap_wider_than_naive_when_days_are_correlated():
    from baselab.research.stats import bootstrap_mean_ci
    rng = np.random.default_rng(0)
    days = np.repeat(np.arange(20), 15)
    day_effect = rng.normal(0, 0.02, 20)[days]
    v = day_effect + rng.normal(0, 0.002, len(days))
    lo_c, hi_c = cluster_bootstrap_ci(v, days, alpha=0.05)
    lo_n, hi_n = bootstrap_mean_ci(list(v), alpha=0.05)
    assert (hi_c - lo_c) > 2 * (hi_n - lo_n)
