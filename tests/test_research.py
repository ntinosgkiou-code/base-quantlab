import asyncio

import pytest

from baselab.config import SecurityLimits
from baselab.domain import Candle
from baselab.research.shadow import ShadowLedger
from baselab.security.checks import HolderLedger, OwnerInfo, RoundTrip
from baselab.security.validator import SecurityValidator, Target


def candle(minute, close, pool="p"):
    return Candle(pool, minute, close, close, close, close, 1, 0.5, 0.5)


def test_shadow_ledger_measures_gate_value():
    sh = ShadowLedger(horizon_s=180)
    sh.record("p", 0, 100.0, (), {})                          # passed
    sh.record("p", 60, 100.0, ("regime_halt:H=0.41",), {})    # rejected only by Hurst
    sh.record("p", 60, 100.0, ("warmup:5/121",), {})          # ignored
    assert sh.on_candle(candle(120, 90.0)) == 0               # not yet 3 minutes
    assert sh.on_candle(candle(180, 103.0)) == 1              # resolves the passed one
    assert sh.on_candle(candle(240, 98.0)) == 1
    s = sh.summary()
    assert s["passed"]["mean_fwd"] == pytest.approx(0.03)
    assert s["rejected:regime_halt"]["mean_fwd"] == pytest.approx(-0.02)
    assert "Hurst gate value = +5.000%" in sh.report()


def test_holder_ledger_reconciliation_helpers():
    L = HolderLedger()
    z = "0x0000000000000000000000000000000000000000"
    L.apply("0xa", "0xb", 5, 2)          # seen out-of-order first (backward scan)
    assert L.min_balance() < 0           # history incomplete
    L.apply(z, "0xa", 10, 1)
    assert L.total() == 10 and L.min_balance() >= 0


class _CountingReader:
    def __init__(self):
        self.calls = 0

    async def simulate_round_trip(self, token, amount, block):
        self.calls += 1
        return RoundTrip(True, True, False, 1, 1, 1, 0)   # honeypot

    async def lp_locked(self, pair, holders, block):
        return 1, 1

    async def owner_info(self, token, block):
        return OwnerInfo(None, None, 0)

    async def holder_ledger(self, token, block):
        L = HolderLedger()
        for i in range(100):
            L.apply("0x0000000000000000000000000000000000000000", f"0x{i}", 1, 1)
        return L


def test_rejections_are_cached_not_rescanned_every_minute():
    async def go():
        r = _CountingReader()
        v = SecurityValidator(r, SecurityLimits(), ttl_blocks=30, reject_ttl_blocks=150)
        for blk in (100, 130, 160, 249):
            rep = await v.check(Target("t", "pair"), blk)
            assert not rep.passed
        assert r.calls == 1
        await v.check(Target("t", "pair"), 251)            # past reject TTL -> re-checked
        assert r.calls == 2
        v.flush()                                           # safety flush keeps rejections
        await v.check(Target("t", "pair"), 260)
        assert r.calls == 2
    asyncio.run(go())
