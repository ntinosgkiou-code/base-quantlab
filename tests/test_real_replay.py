import random

import pytest
from eth_abi import encode

from baselab.backtest.real_replay import Dataset, simulate_pool, v3_virtual_reserves
from baselab.config import Config, StrategyParams
from baselab.research.shadow import ShadowLedger
from baselab.research.stats import bootstrap_mean_ci, breaker_halts, evaluate_gate, profit_factor
from baselab.risk.slippage import V2Pool

POOL = "0xpool"
PM = {"pool": POOL, "token": "0xtoken", "weth_is_token0": True, "token_decimals": 18, "is_v3": False,
      "fee_ppm": 3000, "symbol": "TST"}


def _v2_dataset(minutes: int = 260, seed: int = 1) -> Dataset:
    rng = random.Random(seed)
    w, t = 50.0, 50_000_000.0
    logs, bt, start_block, start_ts = [], 0.2, 1_000_000, 1_800_000_000
    li = 0
    for step in range(minutes * 6):  # one swap every 10 s
        b = start_block + int(step * 10 / bt)
        buy = rng.random() < 0.55
        amt = 0.02 * (0.5 + rng.random())
        if buy:
            amm = V2Pool(w, t)
            out = amm.out(amt)
            w, t = w + amt, t - out
            data = encode(["uint256"] * 4, [int(amt * 1e18), 0, 0, int(out * 1e18)])
        else:
            tok_in = amt / (w / t)
            out = V2Pool(t, w).out(tok_in)
            w, t = w - out, t + tok_in
            data = encode(["uint256"] * 4, [0, int(tok_in * 1e18), int(out * 1e18), 0])
        logs.append((b, li, "sync", encode(["uint112", "uint112"], [int(w * 1e18), int(t * 1e18)])))
        logs.append((b, li + 1, "v2swap", data))
        li += 2
    last = logs[-1][0]
    return Dataset(anchor_block=last, anchor_ts=int(start_ts + (last - start_block) * bt), block_time=bt,
                   from_block=start_block, done_block=last, pools=[PM], logs={POOL: logs})


def test_replay_trades_and_accounting_with_permissive_filters():
    # filters wide open ONLY to exercise fills/exits/accounting; the real backtest uses the spec values
    sp = StrategyParams(hurst_margin=-1.0, min_vol_expansion=0.0, atr_floor=0.0, min_ofi=-1.0,
                        dev_min=-100.0, dev_max=100.0)
    cfg = Config(strategy=sp)
    gas, notional = 0.0001, 0.05
    r = simulate_pool(_v2_dataset(), PM, cfg, notional, gas, 0.001, ShadowLedger())
    assert r.swaps > 1000 and r.evaluations > 100
    assert len(r.trades) >= 5
    for tr in r.trades:
        assert tr["net_ret"] == pytest.approx(tr["gross_ret"] - gas / notional)
        assert tr["exit_ts"] > tr["entry_ts"]                      # never fills on the signal's own block
        assert tr["hold_s"] <= 3 * cfg.risk.max_hold_s + 60
        assert tr["reason"] in {"stop", "trailing_stop", "time_exit_180s", "max_hold"}
        assert -0.2 < tr["gross_ret"] < 0.2
    # a round trip pays ~2 x (0.3% fee + 0.1% haircut): the mean gross cannot hide that cost
    assert min(t["gross_ret"] for t in r.trades) < 0


def test_v2_cost_filter_blocks_trades_that_cannot_pay_their_fees():
    from dataclasses import replace
    sp = StrategyParams(hurst_margin=-1.0, min_vol_expansion=0.0, atr_floor=0.0, min_ofi=-1.0,
                        dev_min=-100.0, dev_max=100.0)
    open_cfg = Config(strategy=sp)
    strict = Config(strategy=replace(sp, max_cost_to_atr=0.5))
    pm_1pct = {**PM, "fee_ppm": 10_000}  # 1% pool: round trip ~2.2%, far above 0.5 x ATR on this data
    ds = _v2_dataset()
    base = simulate_pool(ds, pm_1pct, open_cfg, 0.05, 0.0, 0.001, ShadowLedger())
    filt = simulate_pool(ds, pm_1pct, strict, 0.05, 0.0, 0.001, ShadowLedger())
    assert len(base.trades) > 0
    assert len(filt.trades) == 0 and filt.reject_reasons["cost_exceeds_edge"] > 0


def test_block_time_knots_handle_regime_switch():
    import numpy as np
    # 2 s blocks until block 1000, then 0.2 s blocks
    ds = _v2_dataset(minutes=1)
    ds.knot_blocks = np.asarray([0.0, 1000.0, 2000.0])
    ds.knot_ts = np.asarray([0.0, 2000.0, 2200.0])
    assert ds.ts_of(500) == 1000            # 2 s regime
    assert ds.ts_of(1500) == 2100           # 0.2 s regime
    assert ds.block_at(2100) == 1500
    assert ds.ts_of(ds.block_at(1001)) >= 1001


def test_v3_virtual_reserves_price_matches_sqrtprice():
    # token0 = WETH, token1 = token (18 dec). price token1/token0 = 1000 -> 1 token = 0.001 WETH
    sqrt_x96 = int((1000 ** 0.5) * 2**96)
    w, t = v3_virtual_reserves(sqrt_x96, 10**21, True, 18)
    assert w / t == pytest.approx(0.001, rel=1e-9)


def test_gate_stats():
    assert profit_factor([0.02, -0.01, 0.01]) == pytest.approx(3.0)
    lo, hi = bootstrap_mean_ci([0.01] * 50 + [-0.005] * 50)
    assert lo < 0.0025 < hi
    trades = [{"exit_ts": i, "net": -1 if i in (1, 2, 5, 6) else 1, "entry_ts": i, "net_ret": 0.0,
               "gross_ret": 0.0} for i in range(10)]
    assert breaker_halts(trades) == 2
    g = evaluate_gate([], 0, 86400)
    assert g["verdict"].startswith("INSUFFICIENT")
