"""Immutable domain types shared by every layer. No logic beyond trivial derived properties."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Side(str, Enum):
    BUY = "buy"   # quote (WETH) -> token
    SELL = "sell"  # token -> quote


class Venue(str, Enum):
    UNI_V2 = "uni_v2"
    UNI_V3 = "uni_v3"
    BALANCER = "balancer"


@dataclass(frozen=True, slots=True)
class BlockHead:
    number: int
    hash: str
    parent_hash: str
    timestamp: int
    base_fee: int = 0
    gas_used: int = 0
    gas_limit: int = 0


@dataclass(frozen=True, slots=True)
class Swap:
    pool: str
    block_number: int
    block_hash: str
    timestamp: int
    log_index: int
    side: Side
    price: float         # quote per token, execution price of this swap
    quote_volume: float  # in quote units (WETH)


@dataclass(frozen=True, slots=True)
class Candle:
    pool: str
    minute: int  # unix seconds of minute start
    open: float
    high: float
    low: float
    close: float
    volume: float
    buy_volume: float
    sell_volume: float
    synthetic: bool = False  # True = no swaps that minute (zero-gap fill)
    revision: int = 0


@dataclass(frozen=True, slots=True)
class Reorg:
    fork_block: int  # first orphaned block number
    depth: int


@dataclass(frozen=True, slots=True)
class SecurityReport:
    token: str
    block_number: int
    passed: bool
    reasons: tuple[str, ...]
    buy_tax: float | None = None
    sell_tax: float | None = None
    locked_lp_fraction: float | None = None
    top10_share: float | None = None
    hhi: float | None = None


@dataclass(frozen=True, slots=True)
class Signal:
    pool: str
    token: str
    side: Side
    created_at: float  # monotonic seconds
    candle_minute: int
    price: float
    atr: float
    hurst: float
    ofi: float
    vol_expansion: float
    score: float
    target: float
    stop: float


@dataclass(frozen=True, slots=True)
class Fill:
    intent_id: str
    pool: str
    token: str
    side: Side
    quote_amount: float
    token_amount: float
    expected_out: float
    actual_out: float
    block_number: int
    tx_hash: str
    timestamp: float
    gas_cost: float = 0.0  # in quote units, all txs of the intent (approve + swap + revoke)

    @property
    def realized_slippage(self) -> float:
        if self.expected_out <= 0:
            return 0.0
        return (self.expected_out - self.actual_out) / self.expected_out


@dataclass(slots=True)
class Position:
    pool: str
    token: str
    entry_price: float
    token_amount: float
    quote_spent: float
    opened_at: float
    target: float
    stop: float
    peak: float = 0.0
    meta: dict[str, float] = field(default_factory=dict)
