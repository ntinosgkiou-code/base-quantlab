"""Layer 1 pure checks: tax from a simulated round trip, LP lock, ownership, holder concentration.

All functions return (passed, reason). Inputs come from chain adapters pinned to one block.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

ZERO = "0x0000000000000000000000000000000000000000"


@dataclass(frozen=True, slots=True)
class RoundTrip:
    """Result of the eth_call state-override simulator (see simulator.sol)."""
    buy_ok: bool
    transfer_ok: bool   # plain wallet-to-wallet transfer leg
    sell_ok: bool
    expected_buy: int   # router quote for WETH -> token
    received_buy: int   # balance delta actually received
    expected_sell: int  # router quote for token -> WETH using received_buy
    received_sell: int


@dataclass(frozen=True, slots=True)
class OwnerInfo:
    owner: str | None          # None = owner() not implemented
    timelock_delay_s: int | None  # getMinDelay() of the owner if it is a TimelockController
    implementation_slot: int   # EIP-1967 slot value; non-zero = upgradeable proxy


def tax(expected: int, received: int) -> float:
    if expected <= 0:
        return 1.0
    return max(0.0, 1.0 - received / expected)


def check_round_trip(rt: RoundTrip, max_buy: float, max_sell: float) -> tuple[bool, list[str], float, float]:
    reasons: list[str] = []
    if not rt.buy_ok:
        reasons.append("honeypot:buy_reverts")
    if rt.buy_ok and not rt.transfer_ok:
        reasons.append("honeypot:transfer_blocked")
    if not rt.sell_ok:
        reasons.append("honeypot:sell_reverts")
    bt, st = tax(rt.expected_buy, rt.received_buy), tax(rt.expected_sell, rt.received_sell)
    if rt.buy_ok and bt > max_buy:
        reasons.append(f"buy_tax:{bt:.2%}")
    if rt.sell_ok and st > max_sell:
        reasons.append(f"sell_tax:{st:.2%}")
    return not reasons, reasons, bt, st


def check_lp_lock(locked: int, total_supply: int, min_fraction: float) -> tuple[bool, str, float]:
    if total_supply <= 0:
        return False, "lp:no_supply", 0.0
    frac = locked / total_supply
    return frac >= min_fraction, ("" if frac >= min_fraction else f"lp_unlocked:{frac:.2%}"), frac


def check_ownership(info: OwnerInfo, min_delay_s: int, reject_proxies: bool) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if reject_proxies and info.implementation_slot != 0:
        reasons.append("upgradeable_proxy")
    renounced = info.owner is None or info.owner.lower() == ZERO
    if not renounced:
        if info.timelock_delay_s is None:
            reasons.append("owner_not_renounced")
        elif info.timelock_delay_s < min_delay_s:
            reasons.append(f"timelock_too_short:{info.timelock_delay_s}s")
    return not reasons, reasons


class HolderLedger:
    """Exact balances replayed from every Transfer since token creation (no third-party top-holder API,
    no survivorship: every address that ever held the token is in the ledger)."""

    def __init__(self) -> None:
        self._bal: dict[str, int] = defaultdict(int)
        self.last_block = -1

    def apply(self, sender: str, recipient: str, amount: int, block: int) -> None:
        if sender.lower() != ZERO:
            self._bal[sender.lower()] -= amount
        if recipient.lower() != ZERO:
            self._bal[recipient.lower()] += amount
        self.last_block = max(self.last_block, block)

    def total(self) -> int:
        return sum(self._bal.values())

    def min_balance(self) -> int:
        """Negative = we saw a transfer out without the transfer in (history incomplete)."""
        return min(self._bal.values(), default=0)

    def apply_many(self, transfers: Iterable[tuple[str, str, int, int]]) -> None:
        for s, r, a, b in transfers:
            self.apply(s, r, a, b)

    def concentration(self, excluded: Iterable[str]) -> tuple[float, float, int]:
        """Returns (top10_share, hhi, circulating) over circulating supply.

        C = sum of balances of non-excluded holders; s_i = b_i / C; HHI = sum s_i^2.
        """
        ex = {e.lower() for e in excluded}
        holders = [b for a, b in self._bal.items() if a not in ex and b > 0]
        circ = sum(holders)
        if circ <= 0:
            return 1.0, 1.0, 0
        shares = sorted((b / circ for b in holders), reverse=True)
        return sum(shares[:10]), sum(s * s for s in shares), circ


def check_concentration(top10: float, hhi: float, max_top10: float, max_hhi: float) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if top10 > max_top10:
        reasons.append(f"top10:{top10:.2%}")
    if hhi > max_hhi:
        reasons.append(f"hhi:{hhi:.4f}")
    return not reasons, reasons
