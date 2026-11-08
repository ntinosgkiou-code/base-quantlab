"""Price-impact math (Uniswap V2 / V3 tick walk), multi-venue greedy split, dynamic slippage tolerance.

Units: amounts are floats in human units of the respective token. `mid` is the raw marginal price
(output per input, no fee); `marginal` = mid * (1 - fee) is the zero-size output rate.
Impact I = 1 - (out / in) / marginal: pure price impact. The LP fee is a known cost, not slippage,
so it is excluded (otherwise a 0.3% pool could never pass a 0.2% slippage gate).
"""
from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass

OutFn = Callable[[float], float]


# --------------------------------------------------------------------------- Uniswap V2
@dataclass(frozen=True, slots=True)
class V2Pool:
    reserve_in: float
    reserve_out: float
    fee: float = 0.003

    @property
    def mid(self) -> float:
        return self.reserve_out / self.reserve_in

    @property
    def marginal(self) -> float:
        return self.mid * (1 - self.fee)

    def out(self, amount_in: float) -> float:
        x = amount_in * (1 - self.fee)
        return x * self.reserve_out / (self.reserve_in + x)


# --------------------------------------------------------------------------- Uniswap V3
def sqrt_price_at_tick(tick: int) -> float:
    return 1.0001 ** (tick / 2)


@dataclass(frozen=True, slots=True)
class V3Pool:
    """Pool state in float units. Price P = token1 per token0; sqrt_price = sqrt(P)."""
    sqrt_price: float
    liquidity: float
    tick: int
    fee: float
    tick_spacing: int
    ticks: tuple[tuple[int, float], ...]  # (tick index, liquidityNet), any order
    zero_for_one: bool  # direction of OUR trade (token0 in -> token1 out)

    @property
    def mid(self) -> float:
        p = self.sqrt_price ** 2
        return p if self.zero_for_one else 1 / p

    @property
    def marginal(self) -> float:
        return self.mid * (1 - self.fee)

    def out(self, amount_in: float) -> float:
        return v3_out(self, amount_in)[0]


def v3_out(pool: V3Pool, amount_in: float) -> tuple[float, float]:
    """Walk initialized ticks. Returns (amount_out, unfilled_in). unfilled > 0 = liquidity exhausted."""
    rem = amount_in * (1 - pool.fee)
    sp, liq, out = pool.sqrt_price, pool.liquidity, 0.0
    if pool.zero_for_one:
        path = sorted((t for t in pool.ticks if t[0] <= pool.tick), key=lambda t: -t[0])
    else:
        path = sorted((t for t in pool.ticks if t[0] > pool.tick), key=lambda t: t[0])
    for idx, liq_net in [*path, (None, 0.0)]:
        if rem <= 0:
            break
        if liq <= 0:
            if idx is None:
                break
            sp = sqrt_price_at_tick(idx)
            liq += -liq_net if pool.zero_for_one else liq_net
            continue
        target = None if idx is None else sqrt_price_at_tick(idx)
        if pool.zero_for_one:
            cap = math.inf if target is None else liq * (1 / target - 1 / sp)
            if rem < cap:
                new_sp = 1 / (1 / sp + rem / liq)
                out += liq * (sp - new_sp)
                rem, sp = 0.0, new_sp
                break
            out += liq * (sp - target)
            rem -= cap
            sp = target
            liq -= liq_net
        else:
            cap = math.inf if target is None else liq * (target - sp)
            if rem < cap:
                new_sp = sp + rem / liq
                out += liq * (1 / sp - 1 / new_sp)
                rem, sp = 0.0, new_sp
                break
            out += liq * (1 / sp - 1 / target)
            rem -= cap
            sp = target
            liq += liq_net
    unfilled = rem / (1 - pool.fee) if rem > 0 else 0.0
    return out, unfilled


# --------------------------------------------------------------------------- impact & routing
def impact(out: float, amount_in: float, mid: float) -> float:
    if amount_in <= 0 or mid <= 0:
        return 0.0
    return 1 - (out / amount_in) / mid


@dataclass(frozen=True, slots=True)
class Route:
    allocation: dict[str, float]
    total_out: float
    impact: float
    reference_mid: float


def split_route(venues: Mapping[str, OutFn], mids: Mapping[str, float], amount_in: float,
                chunks: int = 40) -> Route:
    """Greedy marginal-output allocation across venues (approaches the equal-marginal-price optimum).

    `mids` are the venues' zero-size output rates (`marginal`). Reference = the BEST venue's rate,
    so the reported impact is conservative.
    """
    if not venues:
        raise ValueError("no venues")
    alloc = {v: 0.0 for v in venues}
    outs = {v: 0.0 for v in venues}
    step = amount_in / chunks
    for _ in range(chunks):
        best, best_gain = None, -1.0
        for v, fn in venues.items():
            gain = fn(alloc[v] + step) - outs[v]
            if gain > best_gain:
                best, best_gain = v, gain
        assert best is not None
        alloc[best] += step
        outs[best] += best_gain
    total = sum(outs.values())
    ref = max(mids.values())
    return Route({k: v for k, v in alloc.items() if v > 0}, total, impact(total, amount_in, ref), ref)


def max_size_under_impact(route_fn: Callable[[float], Route], max_impact: float, hi: float,
                          iters: int = 40) -> float:
    """Largest input with impact <= max_impact (impact is monotone increasing in size)."""
    if route_fn(hi).impact <= max_impact:
        return hi
    lo = 0.0
    for _ in range(iters):
        mid = (lo + hi) / 2
        if route_fn(mid).impact <= max_impact:
            lo = mid
        else:
            hi = mid
    return lo


def dynamic_tolerance(expected_impact: float, atr_over_price: float, block_time_s: float,
                      blocks_to_inclusion: int, z: float, max_tolerance: float,
                      tick_spacing: int | None = None) -> float:
    """tol = I_expected + z * sigma_block * sqrt(blocks) + half a tick-spacing of price granularity.

    sigma_block rescales 1-minute ATR to one block: sigma_1m / sqrt(60 / block_time).
    """
    sigma_block = atr_over_price / math.sqrt(60.0 / block_time_s)
    tick_term = 0.0 if tick_spacing is None else (1.0001 ** tick_spacing - 1) / 2
    tol = max(0.0, expected_impact) + z * sigma_block * math.sqrt(max(1, blocks_to_inclusion)) + tick_term
    return min(tol, max_tolerance)
