"""Single source of truth for every threshold. All dataclasses are frozen."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum


class Mode(str, Enum):
    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE = "live"


@dataclass(frozen=True, slots=True)
class ChainConfig:
    name: str = "base"
    chain_id: int = 8453
    # Measured on Base mainnet 2026-11-08: 0.2 s/block (5000 blocks = 1000 s). Paper/live re-measure it
    # from the chain at startup (app.measure_block_time); never trust a hardcoded value.
    block_time_s: float = 0.2
    # EIP-1559 params differ per chain (Ethereum: 8 / 2, Base/OP Stack: 250 / 6).
    base_fee_change_denominator: int = 250
    elasticity_multiplier: int = 6
    has_public_mempool: bool = False
    supports_bundles: bool = False  # True only for Ethereum L1 with a Flashbots relay
    wss_url: str = ""
    http_url: str = ""
    private_rpc_url: str = ""
    weth: str = "0x4200000000000000000000000000000000000006"
    v2_router: str = "0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24"  # Uniswap V2 router on Base
    balancer_vault: str = "0xBA12222222228d8Ba445958a75a0704d566BF2C8"


@dataclass(frozen=True, slots=True)
class SecurityLimits:
    max_buy_tax: float = 0.05
    max_sell_tax: float = 0.05
    min_locked_lp_fraction: float = 0.95
    min_timelock_delay_s: int = 48 * 3600
    max_top10_share: float = 0.15
    max_hhi: float = 0.05
    reject_proxies: bool = True
    timeout_s: float = 4.0  # live; scripts override for slow public RPCs
    max_holder_scan_blocks: int = 300_000  # ~16.7 h at 0.2 s/block; older tokens: concentration unverifiable
    known_lockers: tuple[str, ...] = ()
    excluded_holders: tuple[str, ...] = (
        "0x0000000000000000000000000000000000000000",
        "0x000000000000000000000000000000000000dEaD",
    )


@dataclass(frozen=True, slots=True)
class StrategyParams:
    vwap_window: int = 30
    atr_fast: int = 5
    atr_slow: int = 30
    hurst_min_points: int = 120
    hurst_margin: float = 0.05
    min_vol_expansion: float = 1.2
    extreme_vol_expansion: float = 3.0
    atr_floor: float = 0.002  # ATR_fast / price; below = dead liquidity
    ofi_window: int = 5
    min_ofi: float = 0.25
    dev_min: float = 0.3  # (close - vwap) / ATR
    dev_max: float = 2.5
    target_atr_mult: float = 1.5
    trail_atr_mult: float = 1.0
    max_cost_to_atr: float = float("inf")  # v2 cost filter, off in v1 (GO-NO-GO.md stage 1c uses 0.5)


@dataclass(frozen=True, slots=True)
class RiskParams:
    max_hold_s: float = 180.0
    max_consecutive_losses: int = 2
    halt_slippage: float = 0.03
    max_daily_loss: float = 0.03
    max_impact: float = 0.002
    max_tolerance: float = 0.01
    tolerance_z: float = 2.0
    emergency_max_slippage: float = 0.05
    kelly_fraction: float = 0.25
    prior_win_rate: float = 0.5
    prior_payoff: float = 1.0
    prior_strength: float = 20.0
    min_trades_for_edge: int = 30
    risk_per_trade: float = 0.005
    max_position_fraction: float = 0.10
    target_vol: float = 0.01
    paper_exploration_fraction: float = 0.01  # paper/backtest only: size used while edge is unproven
    max_notional_quote: float = float("inf")  # hard cap per trade (live: mandatory via --max-notional)
    max_open_positions: int = 3


@dataclass(frozen=True, slots=True)
class LatencyParams:
    max_rtt_ms: float = 150.0
    probe_interval_s: float = 1.0  # free RPCs rate-limit; 1 probe/s per channel is enough
    window: int = 40
    ws_gap_ms: float = 500.0
    max_signal_age_s: float = 3.0
    recovery_s: float = 5.0


@dataclass(frozen=True, slots=True)
class Config:
    mode: Mode = Mode.BACKTEST
    chain: ChainConfig = field(default_factory=ChainConfig)
    security: SecurityLimits = field(default_factory=SecurityLimits)
    strategy: StrategyParams = field(default_factory=StrategyParams)
    risk: RiskParams = field(default_factory=RiskParams)
    latency: LatencyParams = field(default_factory=LatencyParams)
    confirm_depth: int = 3
    bus_queue_size: int = 10_000

    @staticmethod
    def from_env(mode: Mode) -> "Config":
        chain = ChainConfig(
            wss_url=os.environ.get("BASE_WSS_URL", ""),
            http_url=os.environ.get("BASE_HTTP_URL", ""),
            private_rpc_url=os.environ.get("BASE_PRIVATE_RPC_URL", ""),
        )
        return Config(mode=mode, chain=chain)
