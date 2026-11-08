"""Synthetic Base-like market for end-to-end replay. NOT evidence of edge: it exists to exercise
every code path (regimes, rugs, honeypots, taxes, concentration) deterministically.

The generator is deliberately not tuned to make the strategy win. Pools that rug or are honeypots
stay in the universe (anti-survivorship): only the security layer may exclude them.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from ..domain import BlockHead, Side, Swap
from ..risk.slippage import Route, V2Pool, split_route
from ..security.checks import HolderLedger, OwnerInfo, RoundTrip


@dataclass(slots=True)
class SynthPool:
    address: str
    token: str
    reserve_weth: float
    reserve_token: float
    buy_tax: float = 0.0
    sell_tax: float = 0.0
    honeypot: bool = False
    rug_at: int | None = None  # block number
    renounced: bool = True
    proxy: bool = False
    lp_locked: float = 1.0
    whale_share: float = 0.05
    regime: float = 0.5  # P(buy)
    regime_until: int = 0
    log_index: int = 0
    holders: HolderLedger = field(default_factory=HolderLedger)

    @property
    def price(self) -> float:
        return self.reserve_weth / self.reserve_token

    def amm(self, side: Side) -> V2Pool:
        return V2Pool(self.reserve_weth, self.reserve_token) if side is Side.BUY \
            else V2Pool(self.reserve_token, self.reserve_weth)

    def apply(self, side: Side, amount_in: float) -> float:
        out = self.amm(side).out(amount_in)
        if side is Side.BUY:
            self.reserve_weth += amount_in
            self.reserve_token -= out
        else:
            self.reserve_token += amount_in
            self.reserve_weth -= out
        return out


class SyntheticMarket:
    def __init__(self, n_pools: int = 8, seed: int = 42, start_ts: int = 1_767_225_600,
                 swaps_per_block: float = 0.8) -> None:
        self.rng = random.Random(seed)
        self.block = 0
        self.ts = start_ts
        self._lam = swaps_per_block
        self.pools: dict[str, SynthPool] = {}
        kinds = ["clean", "clean", "clean", "honeypot", "tax", "rug", "whale", "proxy"]
        for i in range(n_pools):
            kind = kinds[i % len(kinds)]
            addr = f"0xpool{i:02d}"
            p = SynthPool(addr, f"0xtoken{i:02d}", 20.0, 20e6)
            if kind == "honeypot":
                p.honeypot = True
            elif kind == "tax":
                p.buy_tax, p.sell_tax = 0.08, 0.12
            elif kind == "rug":
                p.rug_at = self.rng.randint(4_000, 9_000)
            elif kind == "whale":
                p.whale_share = 0.4
            elif kind == "proxy":
                p.proxy = True
            self._seed_holders(p)
            self.pools[addr] = p

    def _seed_holders(self, p: SynthPool) -> None:
        supply = 1_000_000_000
        p.holders.apply("0x0000000000000000000000000000000000000000", p.address, supply // 2, 0)
        whale = int(supply * p.whale_share)
        p.holders.apply("0x0000000000000000000000000000000000000000", "0xwhale", whale, 0)
        rest = supply - supply // 2 - whale
        for k in range(400):
            p.holders.apply("0x0000000000000000000000000000000000000000", f"0xh{k:04d}", rest // 400, 0)

    # ---------------------------------------------------------------- block generation
    def step(self) -> tuple[BlockHead, list[Swap]]:
        self.block += 1
        self.ts += 2
        swaps: list[Swap] = []
        head_hash = f"0xb{self.block:08x}"
        for p in self.pools.values():
            if self.block >= p.regime_until:
                p.regime = self.rng.choice([0.5, 0.5, 0.62, 0.38, 0.7])
                p.regime_until = self.block + self.rng.randint(300, 1500)
            if p.rug_at is not None and self.block == p.rug_at:
                self._emit(p, Side.SELL, p.reserve_token * 20, head_hash, swaps)  # dev dumps supply
                continue
            if p.reserve_weth < 1e-3:
                continue  # dead pool: stays in the universe (anti-survivorship), nobody trades it
            n = self._poisson(self._lam)
            for _ in range(n):
                side = Side.BUY if self.rng.random() < p.regime else Side.SELL
                size_weth = math.exp(self.rng.gauss(math.log(0.03), 1.0))
                amt = size_weth if side is Side.BUY else size_weth / p.price
                self._emit(p, side, amt, head_hash, swaps)
        head = BlockHead(self.block, head_hash, f"0xb{self.block - 1:08x}", self.ts,
                         base_fee=10_000_000, gas_used=20_000_000, gas_limit=120_000_000)
        return head, swaps

    def _emit(self, p: SynthPool, side: Side, amount_in: float, head_hash: str, swaps: list[Swap]) -> None:
        out = p.apply(side, amount_in)
        if out <= 0:
            return
        w, t = (amount_in, out) if side is Side.BUY else (out, amount_in)
        p.log_index += 1
        swaps.append(Swap(p.address, self.block, head_hash, self.ts, p.log_index, side, w / t, w))

    def _poisson(self, lam: float) -> int:
        k, l, prod = 0, math.exp(-lam), self.rng.random()
        while prod > l:
            k += 1
            prod *= self.rng.random()
        return k

    # ---------------------------------------------------------------- MarketView
    def route(self, pool: str, side: Side, amount_in: float) -> Route:
        p = self.pools[pool]
        tax = p.buy_tax if side is Side.BUY else p.sell_tax
        amm = p.amm(side)
        r = split_route({"uni_v2": amm.out}, {"uni_v2": amm.marginal}, amount_in, chunks=4)
        out = 0.0 if (p.honeypot and side is Side.SELL) else r.total_out * (1 - tax)
        return Route(r.allocation, out, r.impact, r.reference_mid)  # tax is a cost, not slippage

    def price(self, pool: str) -> float:
        return self.pools[pool].price

    def tick_spacing(self, pool: str) -> int | None:
        return None

    def apply_trade(self, pool: str, side: Side, amount_in: float) -> None:
        self.pools[pool].apply(side, amount_in)


class SyntheticSecurityReader:
    """Feeds the REAL SecurityValidator with synthetic chain answers."""

    def __init__(self, market: SyntheticMarket) -> None:
        self._m = market
        self._by_token = {p.token: p for p in market.pools.values()}
        self._by_pair = {p.address: p for p in market.pools.values()}

    async def simulate_round_trip(self, token: str, amount_in_wei: int, block: int) -> RoundTrip:
        p = self._by_token[token]
        exp_buy = 10**18
        got_buy = int(exp_buy * (1 - p.buy_tax))
        exp_sell = 10**18
        got_sell = 0 if p.honeypot else int(exp_sell * (1 - p.sell_tax))
        return RoundTrip(True, True, not p.honeypot, exp_buy, got_buy, exp_sell, got_sell)

    async def lp_locked(self, pair: str, holders: tuple[str, ...], block: int) -> tuple[int, int]:
        p = self._by_pair[pair]
        return int(p.lp_locked * 10**18), 10**18

    async def owner_info(self, token: str, block: int) -> OwnerInfo:
        p = self._by_token[token]
        return OwnerInfo(None if p.renounced else "0xowner", None, 1 if p.proxy else 0)

    async def holder_ledger(self, token: str, block: int) -> HolderLedger:
        return self._by_token[token].holders
