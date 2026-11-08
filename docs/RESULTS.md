# Results

All numbers come from real Base mainnet logs (Uniswap V2/V3, Aerodrome) collected from free public RPCs in November 2026.
Each stage's pass/fail gate was written **before** its first run (see [METHODOLOGY.md](METHODOLOGY.md)). The original working
notes, in Greek, are in [`docs/greek/RESULTS.el.md`](greek/RESULTS.el.md).

Unless stated otherwise: fixed notional 0.05 WETH per trade, fills against the real pool reserves one block after the signal,
LP fee of each pool, +0.1% adverse slippage per fill, measured gas plus OP-stack L1 data fee, in-sample 60% / out-of-sample 40% by time.

## 1. One-minute multi-factor scalper (14.3 days, 32 pools; the cost filter and later ideas use separate, fresh windows)

Signals: Hurst (R/S, bias-corrected) + VWAP deviation in ATR units + volatility expansion + swap-flow imbalance.

| | Trades | Mean net per trade | 95% CI | Win rate | Profit factor |
|---|---|---|---|---|---|
| In-sample | 19 | −1.96% | [−2.27%, −1.57%] | 5% | 0.02 |
| **Out-of-sample** | 35 | **−2.14%** | [−2.54%, −1.66%] | 3% | 0.04 |

Gate verdict: G1 (≥ 200 OOS trades) fail, G2 (positive mean with CI > 0) fail, G3 (profit factor ≥ 1.2) fail, G5 (≤ 1 breaker halt/day) fail (1.7).

**Six pre-registered variants** (loosening the filters one at a time, discovery 50% / validation 25% / holdout 25%):

| Variant | Discovery | Validation |
|---------|-----------|------------|
| V1 spec | n = 19, −1.96% | n = 5, −2.21% |
| V2 (Hurst > 0.5, literal) | n = 60, −1.84% | n = 21, −2.22% |
| V3 | n = 63, −1.81% | n = 22, −2.22% |
| V4 | n = 77, −1.65% | n = 23, −2.17% |
| V5 | n = 114, −1.82% | n = 34, −2.18% |
| V6 (loosest) | n = 195, −1.52% | n = 76, −1.55% |

No variant had a positive mean, so none qualified for the holdout. The loosest variant trades 10× more and loses the same amount per trade.

**Shadow ledger** (3-minute forward return of a hypothetical buy at every evaluation, before costs): signals that passed all
filters +0.315% (n = 69); signals rejected *only* by the Hurst gate −0.241% (n = 4,471). The regime filter works, but a +0.3% gross edge cannot pay 0.8%–2.2% of round-trip fees.

**Cost filter v2** (`round-trip cost < 0.5 × ATR/price`), run once on a fresh 14-day window: **0 trades**.

## 2. RSI and volume breakout (third unseen 14-day window, 97.5% CI)

| Strategy | Trades | In-sample | Out-of-sample | OOS win rate | OOS profit factor |
|----------|--------|-----------|---------------|--------------|-------------------|
| RSI(14) < 20 → exit at SMA20, −0.5% stop, 15 min | 114 | n = 63, −1.49% [−1.96%, −1.01%] | n = 51, −0.66% [−1.33%, +0.12%] | 22% | 0.48 |
| Volume ≥ 3× + 20-minute high → +1% target, −0.5% stop, 5 min | 200 | n = 106, −0.28% | n = 94, −0.32% [−0.36%, −0.29%] | 4% | 0.01 |

## 3. Cross-DEX arbitrage, Uniswap ↔ Aerodrome (2.92 days of 0.2 s blocks, 10 tokens, 32 pools)

Model: atomic two-leg trade with closed-form optimal size (cap 2 WETH), decided at block *t*, executed at block *t + 2* (home connection),
reverts still pay gas, one attempt per gap episode, **no competing bots** (a deliberately favourable assumption).

| Measure | Value |
|---------|-------|
| Independent opportunities | 186 |
| Still profitable 2 blocks later | 53 (**28%**) |
| Mean net per attempt | +1.97·10⁻⁵ WETH (95% CI [+0.94, +3.23]·10⁻⁵) |
| Total uncontested net | +0.00366 WETH over 2.92 days (≈ 0.00125 WETH/day) |
| Share from one pair (USDC 0.05% ↔ Slipstream 0.015%) | 60% |
| Median winning attempt | 2.2·10⁻⁵ WETH |
| Gap lifetimes | median 2 blocks, p90 6 blocks, 31% last ≥ 3 blocks (flagged suspect) |

Stress tests: positive with worst-case intra-block ordering and with Slipstream fees × 1.5; **negative (mean −2.2·10⁻⁵ WETH) when 50% of the predicted profit is paid as priority fee**.
Gate (≥ 200 episodes, survival ≥ 40%, stress positive): **not passed** (186 episodes, 28% survival, negative under the priority-fee stress).

## 4. Fifteen-minute swing / mean reversion (42.3 days, 32 pools, 99.67% CI clustered by day)

Three readings of one proposed spec (z-score of the 3-hour return vs 7 days; Hurst gate; stop −1.5%; 4 h time exit).

| Arm | Trades (OOS) | OOS mean | OOS 99.67% CI | Note |
|-----|--------------|----------|---------------|------|
| M: z ≤ −2.5 and H < 0.5 | 43 (16) | +2.13% | [−1.35%, +3.70%] | positive only through 3 trades; ex-top-3 mean −2.5%; price recovered in 3 of 43 trades |
| L: z ≤ −2.5 and H > 0.55 | 13 (8) | −1.51% | [−3.76%, +0.36%] | |
| T: z ≥ +2.5 and H > 0.55 | 29 (17) | −1.36% | [−4.21%, +4.32%] | |

The cost rule ("expected move ≥ 3× round-trip cost") removed ~92% of z-score signals. No arm reached 100 OOS trades or the other gates.

## 5. Delta-neutral funding carry (12 months, public funding history)

Long spot + short perpetual; costs 1.25%/year.

| Venue:coin | Net APR (12 m) | Net APR (last 90 d) | 30-day windows positive | Worst 30 days |
|-----------|----------------|---------------------|-------------------------|---------------|
| Binance BTC / ETH / SOL | 2.2% / 1.2% / −1.3% | 4.9% / 3.6% / 2.9% | 71% / 66% / 52% | −0.40% / −0.43% / −1.20% |
| Bybit BTC / ETH / SOL | 1.6% / 1.2% / −0.9% | 4.1% / 3.1% / 1.8% | 74% / 75% / 48% | −0.26% / −0.19% / −0.74% |
| Hyperliquid BTC / ETH / SOL | 4.5% / 5.2% / −0.2% | 7.9% / 7.6% / 6.3% | 83% / 92% / 58% | −0.19% / −0.15% / −1.57% |

Mean net APR **1.5%**, 69% of 30-day windows positive, worst window −1.57%. Capital required for €1,000/month at the measured rates: roughly €150k (best recent case) to €800k (12-month mean).
Counterparty and liquidation risk are *not* captured by this data.

## 6. Local mainnet-fork test (fake ETH)

13 / 13 checks: validator passes a clean token, measures a 10% sell tax, catches a honeypot; buy and sell settle through the real Uniswap V2 router;
the router allowance is 0 after every trade; an impossible `minOut` is rejected before broadcast with no gas spent; `revoke_all()` zeroes a dangling approval; gas accounting matches the chain.
