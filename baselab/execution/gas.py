"""EIP-1559 fee oracle: project next block's base fee exactly, price priority from fee history.

Next base fee (EIP-1559, parametrised for OP-stack chains):
    target = gas_limit / elasticity
    b' = b + b * (gas_used - target) / target / denominator      (integer math, min +1 when above target)
The tx pays base + min(tip, maxFee - base). maxFee headroom is NOT overpayment (refunded); the tip is,
so the tip is the percentile of recent inclusion tips, scaled by urgency and clamped.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ..domain import BlockHead


def next_base_fee(head: BlockHead, denominator: int, elasticity: int) -> int:
    if head.gas_limit <= 0:
        return head.base_fee
    target = head.gas_limit // elasticity
    if target == 0 or head.gas_used == target:
        return head.base_fee
    if head.gas_used > target:
        delta = max(head.base_fee * (head.gas_used - target) // target // denominator, 1)
        return head.base_fee + delta
    delta = head.base_fee * (target - head.gas_used) // target // denominator
    return max(head.base_fee - delta, 0)


def percentile_int(values: Sequence[int], q: float) -> int:
    if not values:
        return 0
    s = sorted(values)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


@dataclass(frozen=True, slots=True)
class FeeQuote:
    max_fee_per_gas: int
    max_priority_fee_per_gas: int
    projected_base_fee: int


@dataclass(frozen=True, slots=True)
class GasPolicy:
    tip_percentile: float = 0.6
    min_tip_wei: int = 1_000
    max_tip_wei: int = 2 * 10**9
    base_fee_headroom_blocks: int = 2  # survive this many max-increase blocks before inclusion
    urgency: float = 1.0               # 1.0 = next block; >1 for exits under stress


class FeeOracle:
    def __init__(self, denominator: int, elasticity: int, policy: GasPolicy = GasPolicy()) -> None:
        self._den = denominator
        self._el = elasticity
        self._policy = policy

    def quote(self, head: BlockHead, recent_tips: Sequence[int], urgency: float | None = None) -> FeeQuote:
        pol = self._policy
        projected = next_base_fee(head, self._den, self._el)
        u = pol.urgency if urgency is None else urgency
        tip = int(percentile_int(recent_tips, pol.tip_percentile) * u)
        tip = min(max(tip, pol.min_tip_wei), pol.max_tip_wei)
        # worst-case growth per block when a block is completely full: (elasticity - 1) / denominator
        growth = 1 + (self._el - 1) / self._den
        ceiling = int(projected * growth ** pol.base_fee_headroom_blocks) + 1
        return FeeQuote(max_fee_per_gas=ceiling + tip, max_priority_fee_per_gas=tip, projected_base_fee=projected)
