# Base-QuantLab — Architecture Blueprint (v1)

> Σύνοψη στα ελληνικά: πρώτα η αρχιτεκτονική, μετά ο κώδικας. 5 layers, πλήρως αποσυνδεδεμένα μέσω
> event bus + κεντρικού state machine. Default mode = **paper / backtest**. Live απαιτεί ρητό flag
> και δικό σου κλειδί σε env. Ό,τι από το spec είναι τεχνικά αδύνατο στο Base, γράφεται ρητά στο §9
> και υλοποιείται η πιο κοντινή σωστή εκδοχή — όχι ψεύτικη.

---

## 0. Design principles

| # | Principle | Consequence in code |
|---|-----------|---------------------|
| P1 | **Fail closed** | Any unknown / un-decodable / timed-out check ⇒ REJECT, never PASS. |
| P2 | **Single writer of system state** | Only `SystemState` (guarded by `asyncio.Lock`) changes phase. Every other module *requests* transitions. |
| P3 | **Decoupling via typed events** | Ingestion → Bus → Strategy → Bus → Router. No module imports another layer's internals. |
| P4 | **Point-in-time correctness** | A candle is emitted only after a block with a later-minute timestamp exists ⇒ no lookahead. |
| P5 | **Check at time-of-use** | Latency, freshness, slippage and phase are re-checked inside the router immediately before signing. |
| P6 | **Pure math, impure edges** | All indicators / sizing / slippage are pure functions (unit-tested). Network code lives in adapters. |
| P7 | **Fail-safes isolated** | Freeze handlers run in `gather(return_exceptions=True)` with per-handler timeouts: one failing handler cannot corrupt state or block the others. |

---

## 1. Component map

```
                       ┌────────────────────────────── SystemState (FSM, asyncio.Lock) ──────────────────────────────┐
                       │ BOOTING → ARMED ⇄ FROZEN       ARMED → SLEEPING(until next UTC 00:00) → ARMED                 │
                       │ any → HALTED (terminal, manual reset)     on_freeze/on_halt handlers (isolated, timed)         │
                       └───────▲───────────────▲──────────────────▲────────────────────▲──────────────────────────────┘
                               │ request       │ request          │ request            │ request
┌──────────────┐  heads/swaps  │   ┌───────────┴────┐  candles  ┌──┴─────────────┐ sig ┌┴───────────────────┐
│ L0 INGESTION │──────────────────▶│  EVENT BUS     │──────────▶│ L2 STRATEGY    │────▶│ L3 EXECUTION ROUTER│
│ WSS newHeads │  reorg events     │ (typed topics, │           │ MATRIX         │     │ exact approve→swap │
│ Swap logs    │                   │  bounded,      │           │ Hurst regime   │     │ →revoke, private   │
│ ReorgGuard   │                   │  backpressure  │           │ VWAP dev / ATR │     │ RPC / bundle,      │
│ CandleAgg 1m │                   │  ⇒ FREEZE)     │           │ flow imbalance │     │ eth_simulateV1     │
│ Watchdog     │                   └───────▲────────┘           └──────▲─────────┘     └─────────┬──────────┘
└──────┬───────┘                           │                           │ gate                    │ fills
       │ RTT samples                        │                  ┌────────┴─────────┐     ┌─────────▼──────────┐
┌──────▼───────┐                            │                  │ L1 SECURITY      │     │ L3 RISK            │
│ L5 LATENCY   │── breach ──────────────────┘                  │ honeypot sim     │     │ TrailingStop +180s │
│ RTT monitor  │                                               │ tax ≤ 5%         │     │ CircuitBreaker     │
│ ≤150 ms gate │                                               │ LP lock / owner  │     │ DailyLossGuard 3%  │
└──────────────┘                                               │ HHI / top-10     │     │ L4 Kelly+ATR sizing│
                                                               └──────────────────┘     │ dynamic slippage   │
                                                                                        └────────────────────┘
```

Package layout (`baselab/`):

| Module | Layer | Responsibility |
|--------|-------|----------------|
| `config.py` | — | Frozen dataclasses; all thresholds in one place. |
| `domain.py` | — | Immutable domain types (Swap, Candle, Signal, Fill, …). |
| `bus.py` | — | Typed async pub/sub, bounded queues, overflow ⇒ freeze. |
| `state.py` | — | System FSM + isolated freeze/halt handler fan-out. |
| `ingestion/candles.py` | L0 | Reorg-safe, lookahead-free 1-minute aggregator, zero-gap fill. |
| `ingestion/reorg.py` | L0 | Parent-hash chain tracking, fork-point detection. |
| `ingestion/engine.py` | L0 | WSS subscription loop + disconnect watchdog (500 ms). |
| `latency.py` | L5 | RTT heartbeat per channel, EWMA + p95, 150 ms gate. |
| `security/*.py` | L1 | Honeypot/tax simulation, LP lock, ownership, holder concentration. |
| `quant/indicators.py` | L2 | VWAP, ATR (Wilder), Hurst (R/S + Anis-Lloyd-Peters correction), flow imbalance. |
| `quant/strategy.py` | L2 | Strategy Matrix → `Signal`. |
| `risk/slippage.py` | L3/L5 | V2 & V3 tick-walk price impact, multi-venue greedy split, dynamic tolerance. |
| `risk/sizing.py` | L4 | Bayesian fractional Kelly × volatility scaling × liquidity cap. |
| `risk/guards.py` | L3/L4 | Circuit breaker (2 losses / >3% slip), daily 3% drawdown sleep. |
| `risk/exits.py` | L3 | ATR trailing stop + 180 s time exit. |
| `execution/gas.py` | L3 | EIP-1559 next-base-fee projection + fee-history priority percentile. |
| `execution/router.py` | L3 | Intent lifecycle, TOCTOU re-checks, ephemeral allowances, paper/live senders. |
| `execution/chain.py` | adapters | web3.py v7 adapters (lazy import; core runs without web3). |
| `backtest/*.py` | — | Point-in-time replay incl. dead tokens (anti-survivorship), synthetic market. |
| `app.py` / `__main__.py` | — | Composition root, `asyncio.TaskGroup`, graceful shutdown, CLI. |

---

## 2. Event topics

| Topic | Producer | Consumers | Payload |
|-------|----------|-----------|---------|
| `head` | Ingestion | ReorgGuard, Aggregator, Watchdog | `BlockHead` |
| `swap` | Ingestion | Aggregator, HolderLedger | `Swap` |
| `reorg` | ReorgGuard | Aggregator, Router (receipt re-validation) | `Reorg` |
| `candle` | Aggregator | Strategy, Exits, Sizing | `Candle` (closed, final for that minute unless revised) |
| `candle_revision` | Aggregator | Strategy (invalidates pending signal) | `Candle` |
| `signal` | Strategy | Router | `Signal` |
| `fill` | Router | Guards, Sizing (edge stats), Exits | `Fill` |

Backpressure: queues are bounded. A full queue means a consumer is behind real time ⇒ data is stale ⇒
`SystemState.freeze("bus_overflow:<topic>")`. Dropping market data silently is never allowed.

---

## 3. System state machine

| From \ To | ARMED | FROZEN | SLEEPING | HALTED |
|-----------|-------|--------|----------|--------|
| BOOTING   | ✔ boot checks pass | ✔ | — | ✔ |
| ARMED     | — | ✔ latency / WS gap / bus overflow / unsimulated slip | ✔ daily loss ≥ 3% | ✔ 2 losses / slip > 3% |
| FROZEN    | ✔ after `recovery_s` of healthy RTT | — | ✔ | ✔ |
| SLEEPING  | ✔ at next UTC midnight | — | — | ✔ |
| HALTED    | only `manual_reset()` | — | — | — |

Entering FROZEN / SLEEPING / HALTED fires, concurrently and isolated:
1. `router.cancel_all_intents()` — cancels un-sent intents; sent-but-unmined txs are replaced by a
   0-value self-transfer with the same nonce and +12.5% fees (the only real "cancel" on an EVM).
2. `router.revoke_all_allowances()` — `approve(spender, 0)` for every non-zero tracked allowance.
3. `cache.flush()` — strategy/quote caches dropped, so recovery starts from fresh state.
4. HALTED additionally: `router.emergency_flatten()` under `emergency_max_slippage`.

Only ARMED accepts new entries. Exits of open positions remain allowed in FROZEN but **only** with a
fresh quote obtained after RTT is healthy again (stale-price exits are as dangerous as stale entries).

---

## 4. Layer 1 — security validation matrix (fail-closed)

All checks run concurrently (`asyncio.gather`) against a **pinned block number** (one snapshot, so the
checks can't disagree about state) with a global timeout. Any exception ⇒ REJECT.

| Check | Method | Pass rule |
|-------|--------|-----------|
| Honeypot / tax | `eth_call` with **state override**: a simulator contract is injected at a throwaway address, funded with ETH, and executes buy → transfer → sell in one call, returning `(expected_buy, got_buy, expected_sell, got_sell)`. | both legs succeed, `tax_buy = 1 − got/expected ≤ 5%`, same for sell |
| LP lock (V2) | `balanceOf(dead/zero/known lockers) / totalSupply(LP)` | `≥ 95%` locked |
| LP lock (V3) | Positions are NFTs; locked only if the position NFT is held by a known locker contract | otherwise UNVERIFIABLE ⇒ reject |
| Ownership | `owner()` reverts or == 0x0 ⇒ renounced; else `getMinDelay()` on owner (OZ TimelockController) | renounced, or timelock delay ≥ 48 h |
| Proxy | EIP-1967 implementation slot non-zero ⇒ upgradeable | reject (owner can swap code after the check) |
| Concentration | Holder ledger rebuilt from **all** `Transfer` logs since creation (exact, not a top-holders API) | top-10 share of circulating ≤ 15% |

**Concentration math.** Circulating supply `C = totalSupply − Σ balance(excluded)` where excluded =
LP pairs, dead/zero, bridges, lockers, the token contract itself. Shares `sᵢ = bᵢ / C`.
`Top10 = Σ_{i∈top10} sᵢ`, `HHI = Σ sᵢ²` (0 = perfectly dispersed, 1 = one holder). HHI is reported
and gated separately (`max_hhi`), top-10 is the spec gate.

---

## 5. Layer 2 — strategy matrix math

All inputs are **closed** 1-minute candles. Let `cₜ` close, `hₜ` high, `lₜ` low, `vₜ` volume (quote units).

* **VWAP** over window N: `VWAP = Σ τₜ vₜ / Σ vₜ`, `τₜ = (hₜ+lₜ+cₜ)/3`.
* **ATR (Wilder)**: `TRₜ = max(hₜ−lₜ, |hₜ−cₜ₋₁|, |lₜ−cₜ₋₁|)`, `ATRₜ = ATRₜ₋₁ + (TRₜ − ATRₜ₋₁)/n`.
* **VWAP deviation in ATR units**: `D = (c − VWAP) / ATR`. Entry band: `D ∈ [d_min, d_max]`
  (momentum present but not over-extended).
* **Volatility expansion**: `ρ = ATR_fast / ATR_slow`. Require `ρ ≥ ρ_min` (expansion) **and**
  `ATR_fast / c ≥ atr_floor` (rejects dead-liquidity phases).
* **Flow imbalance (the honest OBI)**: an AMM has no order book. We use signed swap flow
  `OFI = (V_buy − V_sell)/(V_buy + V_sell) ∈ [−1, 1]` over the last k candles, optionally blended
  with V3 tick-liquidity skew `(L_above − L_below)/(L_above + L_below)` within ±w ticks.
* **Hurst exponent (rescaled range)**: for log returns r split into chunks of size n,
  `R/S(n) = mean over chunks of [max(Y) − min(Y)] / σ`, `Y = cumsum(r − r̄)`.
  Small-sample bias is removed with the Anis–Lloyd expectation, Peters-corrected:
  `E[R/S](n) = ((n−½)/n) · Γ((n−1)/2)/(√π Γ(n/2)) · Σ_{i=1}^{n−1} √((n−i)/i)`,
  `H = 0.5 + slope( log R/S(n) − log E[R/S](n) ,  log n )`.
  Gate: trade only if `H ≥ 0.5 + h_margin` (default margin 0.05, because the estimator's standard error
  on ~120 points is ≈ 0.05–0.08; a raw `H > 0.5` rule would trade on noise). `H < 0.5 + margin` ⇒ halt
  that pool (not the system).

Signal score (all must pass, score used only for ranking): `S = OFI · min(ρ, 3) · (H − 0.5)`.

---

## 6. Layer 3/5 — slippage & latency

**V2 exact output**: `out = in·γ·R_out / (R_in + in·γ)`, γ = 0.997.
**V3 tick walk** (float, per active range with liquidity L, fee φ, `x = in·(1−φ)`):
* zeroForOne: capacity to next lower tick `Δx_max = L(1/√P_t − 1/√P)`; if `x < Δx_max`:
  `1/√P' = 1/√P + x/L`, `out = L(√P − √P')`; else consume, cross tick, `L ← L − liquidityNet`.
* oneForZero: `Δy_max = L(√P_t − √P)`; `√P' = √P + y/L`, `out = L(1/√P − 1/√P')`; crossing up `L ← L + liquidityNet`.

**Price impact** `I = 1 − (out/in)/mid`. **Multi-venue**: trade split into K chunks, each chunk to the venue
with best marginal output (greedy, converges to the equal-marginal-price split as K→∞). Abort if
`I_total > max_impact` (spec: 0.2%).

**Dynamic tolerance** (not a static %):
`tol = I_expected + z · σ_block · √(blocks_to_inclusion) + tick_spacing_term`, where
`σ_block = (ATR_fast / c) / √(60 / block_time)` and `tick_spacing_term = (1.0001^{tickSpacing} − 1)/2` for V3
(half a tick-spacing of price granularity), 0 for V2. `minOut = quote · (1 − tol)`; `tol` capped at `max_tolerance`.

**Realized slippage** `s = (quote − actual)/quote`. `s > 3%` ⇒ HALT. `s > tol` (un-simulated) ⇒ FREEZE.

**Latency**: per channel (`wss`, `private_rpc`) an RTT probe every 250 ms. Healthy iff
`last ≤ 150 ms` **and** `p95(window) ≤ 150 ms`. A signal records `rtt_ok_at`; router rejects if any channel
is unhealthy at signing time, or `now − signal.created > max_signal_age`.

**WS watchdog**: disconnect timestamp recorded; if still disconnected (or no pong) after 500 ms ⇒ FREEZE.

---

## 7. Layer 4 — position sizing

1. **Edge estimate (Bayesian)**: wins/losses with Beta prior `(α₀, β₀)` ⇒ `p̂ = (α₀+W)/(α₀+β₀+W+L)`;
   payoff `b̂ = avg_win/avg_loss` shrunk toward prior `b₀` with weight `n₀`.
2. **Kelly**: `f* = p̂ − (1−p̂)/b̂`; `f = κ·max(f*, 0)` with κ = 0.25 (fractional Kelly).
   Default prior = **no edge** (`p₀ = 0.5, b₀ = 1 ⇒ f* = 0`): live refuses to size up until the measured
   record (≥ `min_trades`) shows positive edge. This *is* the validation gate, encoded in math.
3. **Volatility scaling**: `σ = ATR_fast / c`; `scale = min(1, σ_target / σ)`; if `ρ ≥ ρ_extreme` ⇒ `scale ·= ¼`.
4. **Risk cap**: `notional ≤ risk_per_trade · equity / stop_distance_pct`.
5. **Liquidity cap**: binary search largest size with `I ≤ max_impact`.
6. `size = min(f·scale·equity, risk cap, liquidity cap, max_position_fraction·equity)`.

**Daily guard**: `equity_open(UTC day)` snapshot; if `equity ≤ equity_open·(1 − 3%)` ⇒ SLEEPING until next 00:00 UTC.

---

## 8. Anti-bias rules for data / backtests

* **Lookahead**: candle emitted only after a later-minute block; signals only on closed candles;
  backtest fills at the **next** block's pool state (never the signal candle's close).
* **Survivorship**: the replay universe is every pool *created* up to time t, including pools that later
  rugged or went to zero; security checks use the snapshot as of the check block, not today's state.
* **Reorg**: candle contents are a pure function of swaps in canonical blocks; a reorg drops orphaned
  swaps, rebuilds affected minutes and emits `candle_revision`; P&L is booked final only once the
  receipt's block hash is still canonical at `confirm_depth`.
* **Zero-gap**: minutes with no swaps are emitted as `synthetic=True` flat candles (vol 0) so ATR/Hurst
  windows are time-consistent.

---

## 9. Spec items that are not physically possible as written — and what is implemented instead

| Spec item | Reality | Implemented |
|-----------|---------|-------------|
| Mempool ingestion on Base | Base has no public mempool (single sequencer, priority-fee ordering) | Disabled for Base; available for L1 via `newPendingTransactions` |
| Order Book Imbalance | AMMs have no order book | Signed swap-flow imbalance + V3 tick-liquidity skew |
| Flashbots / MEV-Share on Base | Flashbots Protect / MEV-Share are Ethereum L1 | L1: `eth_sendBundle` (all-or-nothing). Base: private/sequencer RPC, back-to-back nonces |
| "Failed trades cost zero gas" | Only true for L1 bundles. On Base a reverted tx pays gas | Every tx set is pre-simulated with `eth_simulateV1` at the pinned block; no simulation ⇒ no send |
| approve + swap + revoke "in the same block bundle" | Guaranteed atomic only via L1 bundle | L1 bundle; Base: 3 consecutive nonces sent together (revoke cannot be skipped, but same-block not guaranteed) |
| "Zero-loss vector" | No pre-trade check can see future owner actions | Fail-closed checks + proxy rejection + small sizes; risk reduced, not eliminated |
| Anchor execution to finalized/safe | Base `safe` lags minutes, `finalized` ~ hour ⇒ a 1-min strategy cannot execute on them | Security checks + P&L on `safe`/confirmed depth; signal on `latest` guarded by ReorgGuard |
| Max slippage 0.2% & top-10 ≤ 15% | Will reject the vast majority of new Base tokens | Enforced exactly as configured (it is a filter, expect few trades) |

---

## 10. Run modes

| Mode | Network | Keys | Purpose |
|------|---------|------|---------|
| `backtest` | none | none | Synthetic or recorded events, full pipeline, metrics. |
| `paper` | read-only WSS/HTTP | none | Live data, simulated fills. |
| `live` | WSS + private RPC | `PRIVATE_KEY` env | Requires `--live --i-understand-risk` and a positive measured Kelly edge. |
