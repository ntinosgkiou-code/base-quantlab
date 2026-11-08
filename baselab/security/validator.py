"""Security Validator: orchestrates Layer 1 checks against ONE pinned block, concurrently, fail-closed.

Any exception, timeout or missing data => rejection with an explicit reason. Passing reports are
cached for `ttl_blocks` only: ownership/tax can change, so approval expires quickly.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol

from ..config import SecurityLimits
from ..domain import SecurityReport
from .checks import (
    HolderLedger,
    OwnerInfo,
    RoundTrip,
    check_concentration,
    check_lp_lock,
    check_ownership,
    check_round_trip,
)

log = logging.getLogger(__name__)


class SecurityReader(Protocol):
    async def simulate_round_trip(self, token: str, amount_in_wei: int, block: int) -> RoundTrip: ...
    async def lp_locked(self, pair: str, holders: tuple[str, ...], block: int) -> tuple[int, int]: ...
    async def owner_info(self, token: str, block: int) -> OwnerInfo: ...
    async def holder_ledger(self, token: str, block: int) -> HolderLedger: ...


@dataclass(frozen=True, slots=True)
class Target:
    token: str
    pair: str
    is_v3: bool = False


class SecurityValidator:
    def __init__(self, reader: SecurityReader, limits: SecurityLimits,
                 probe_amount_wei: int = 10**15, ttl_blocks: int = 30, reject_ttl_blocks: int = 150) -> None:
        self._r = reader
        self._l = limits
        self._probe = probe_amount_wei
        self._ttl = ttl_blocks
        self._reject_ttl = reject_ttl_blocks  # don't re-scan a rejected token every minute
        self._cache: dict[str, SecurityReport] = {}

    def cached(self, token: str, block: int) -> SecurityReport | None:
        rep = self._cache.get(token.lower())
        if rep is None:
            return None
        ttl = self._ttl if rep.passed else self._reject_ttl
        return rep if block - rep.block_number <= ttl else None

    def flush(self, keep_rejections: bool = True) -> None:
        """Safety flush: drop every APPROVAL (must be re-proven). Rejections are kept by default:
        a latency freeze says nothing new about a token, and fail-closed must stay closed."""
        if keep_rejections:
            self._cache = {k: v for k, v in self._cache.items() if not v.passed}
        else:
            self._cache.clear()

    async def check(self, t: Target, block: int) -> SecurityReport:
        hit = self.cached(t.token, block)
        if hit is not None:
            return hit
        try:
            rep = await asyncio.wait_for(self._run(t, block), timeout=self._l.timeout_s)
        except Exception as exc:  # fail closed
            log.warning("security check failed closed for %s: %r", t.token, exc)
            rep = SecurityReport(t.token, block, False,
                                 (f"check_error:{type(exc).__name__}:{str(exc)[:90]}",))
        self._cache[t.token.lower()] = rep
        return rep

    async def _run(self, t: Target, block: int) -> SecurityReport:
        lim = self._l
        lock_holders = lim.excluded_holders + lim.known_lockers
        rt, lp, owner, ledger = await asyncio.gather(
            self._r.simulate_round_trip(t.token, self._probe, block),
            self._lp(t, lock_holders, block),
            self._r.owner_info(t.token, block),
            self._r.holder_ledger(t.token, block),
        )
        reasons: list[str] = []
        _, rt_reasons, buy_tax, sell_tax = check_round_trip(rt, lim.max_buy_tax, lim.max_sell_tax)
        reasons += rt_reasons

        locked_frac: float | None = None
        if lp is None:
            reasons.append("lp:v3_position_unverifiable")
        else:
            ok_lp, why, locked_frac = check_lp_lock(lp[0], lp[1], lim.min_locked_lp_fraction)
            if not ok_lp:
                reasons.append(why)

        _, own_reasons = check_ownership(owner, lim.min_timelock_delay_s, lim.reject_proxies)
        reasons += own_reasons

        excluded = lim.excluded_holders + lim.known_lockers + (t.pair, t.token)
        top10, hhi, _ = ledger.concentration(excluded)
        _, conc_reasons = check_concentration(top10, hhi, lim.max_top10_share, lim.max_hhi)
        reasons += conc_reasons

        return SecurityReport(t.token, block, not reasons, tuple(reasons), buy_tax, sell_tax,
                              locked_frac, top10, hhi)

    async def _lp(self, t: Target, holders: tuple[str, ...], block: int) -> tuple[int, int] | None:
        if t.is_v3:
            return None  # V3 liquidity = NFT positions; only a known-locker NFT check could pass it
        return await self._r.lp_locked(t.pair, holders, block)
