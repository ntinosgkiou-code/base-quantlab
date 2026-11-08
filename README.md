<p align="center">
  <img src="docs/assets/banner.svg" alt="base-strategy-lab: pre-registered testing and execution infrastructure for on-chain trading strategies" width="100%">
</p>

<p align="center">
  <a href="https://github.com/ntinosgkiou-code/base-strategy-lab/actions/workflows/ci.yml"><img src="https://github.com/ntinosgkiou-code/base-strategy-lab/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/chain-Base-0052FF" alt="Base">
  <img src="https://img.shields.io/badge/tests-55%20passing-2ea44f" alt="55 tests">
  <img src="https://img.shields.io/badge/license-MIT-lightgrey" alt="MIT">
</p>

# base-strategy-lab

**Does your trading strategy survive fees, latency and reality?**
`base-strategy-lab` is an async Python toolkit and research lab for building, stress-testing and *honestly evaluating*
on-chain trading strategies on **Base** (Uniswap V2/V3, Aerodrome). It contains a full execution stack (ingestion, security
validator, strategy, risk, router) **and** the evaluation harness I used to test 14 pre-registered strategy configurations on
real on-chain data.

> **The headline result is negative, and that is the point.** None of the 14 configurations passed its gate once real fees,
> delay, gas and out-of-sample data were applied. The repo shows how to find that out in hours, with real data and no capital at risk,
> instead of after launch.

---

## Why this exists

Most "trading bot" claims are backtests that quietly ignore at least one of: fees, latency, look-ahead, survivorship, or the
fact that the author tried 50 variants and showed the best one. This lab makes those shortcuts hard:

| The usual failure | What this repo does about it |
|-------------------|------------------------------|
| Backtest ignores execution costs | Fills happen on the **next block's real pool reserves**, with the pool's LP fee, extra adverse slippage, gas **and the OP-stack L1 data fee** |
| Tuned until it passes | **Pass/fail gates are written before any result is seen** ([docs/METHODOLOGY.md](docs/METHODOLOGY.md)); changes are logged with the reason and the date |
| Look-ahead bias | Candles are emitted only after a later-minute block exists; signals use closed candles; tests assert future data cannot change past values |
| Survivorship / one lucky window | Every new idea runs **once** on a window nobody has looked at; bootstrap confidence intervals **clustered by day**; Bonferroni-style correction per stage |
| "Is this token a honeypot?" | Fail-closed validator: a state-override `eth_call` simulates buy → transfer → sell, plus LP-lock, owner/timelock, proxy and exact holder-concentration checks |
| "What happens when the network misbehaves?" | A state machine freezes on latency spikes, WebSocket gaps, bus overflow, reorgs or unexpected slippage, and revokes allowances |

## What I found

<p align="center"><img src="docs/assets/results.svg" alt="Out-of-sample net return per trade for every tested configuration" width="100%"></p>

| Strategy family | Data (real Base logs) | Outcome |
|-----------------|-----------------------|---------|
| 1-minute multi-factor scalper (Hurst + VWAP + ATR + swap-flow imbalance) and 5 looser variants | 14.3 days, 32 pools | −2.14% per trade out-of-sample (n = 35, 95% CI [−2.54%, −1.66%]); every variant negative |
| Same scalper with a "cost must be < 0.5 × ATR" filter | fresh 14 days | 0 trades: 1-minute moves never cover round-trip fees |
| RSI(14) < 20 mean reversion | fresh 14 days | −0.66% out-of-sample (n = 51), −1.49% in-sample |
| Volume breakout | same window | −0.32% out-of-sample (n = 94), win rate 4% |
| Cross-DEX arbitrage, Uniswap ↔ Aerodrome (Base, 0.2 s blocks) | 2.92 days, 10 tokens, 32 pools | Only **28%** of profitable gaps survive 2 blocks; uncontested ceiling ≈ 0.0013 WETH/day; **negative** once half the profit goes to priority fees |
| 15-minute swing / mean reversion (3 readings of one spec) | 42.3 days, 32 pools | 43 / 13 / 29 trades, none significant; price recovered in only 3 of 43 trades |
| Delta-neutral funding carry (spot + perp) | 12 months, Binance/Bybit/Hyperliquid × BTC/ETH/SOL | **1.5%** mean net APR (best 90-day window 7.9%) |

Full tables, confidence intervals and caveats: [docs/RESULTS.md](docs/RESULTS.md).

**What the numbers say, in plain words**

- At 1 minute, the typical price move is smaller than the cost of getting in and out (0.8%–2.2% in LP fees alone). A larger ATR is *volatility*, not *edge*.
- The Hurst regime filter really does help (+0.56% better forward return than the signals it rejected), but not enough to pay the fees.
- Arbitrage gaps between Uniswap and Aerodrome close within a fraction of a second. From a home connection (1–2 blocks of delay) most of the profit is gone before a transaction can land.
- Funding carry is real but small: for BTC and ETH roughly 1–5% a year after costs over the last 12 months (SOL was negative), plus exchange risk the data cannot show.

## How it works

<p align="center"><img src="docs/assets/architecture.svg" alt="Architecture" width="100%"></p>

| Layer | What it does | Code |
|-------|--------------|------|
| **Ingestion** | WSS new heads, one batched log query per second, parent-hash **reorg guard**, lookahead-free 1-minute candles with zero-gap fill, automatic **gap backfill** after reconnects | [`baselab/ingestion/`](baselab/ingestion) |
| **Security validator** | Simulates buy → transfer → sell with a state-override `eth_call` (a Solidity probe injected at a throwaway address), measures buy/sell tax, checks LP lock, owner or timelock, EIP-1967 proxies and **exact** top-10 / HHI holder concentration (log replay reconciled against `totalSupply`). Any doubt = reject | [`baselab/security/`](baselab/security) |
| **Strategy matrix** | Bias-corrected R/S **Hurst exponent**, VWAP deviation in ATR units, volatility expansion, swap-flow imbalance (the AMM substitute for order-book imbalance). Pure functions | [`baselab/quant/`](baselab/quant) |
| **Risk** | Bayesian **fractional Kelly** whose prior is "no edge" (it will not size above zero until a measured record exists), volatility scaling, daily-loss sleep, 2-loss / 3%-slippage circuit breaker, ATR trailing stop, kill switch | [`baselab/risk/`](baselab/risk) |
| **Execution router** | Re-checks every gate at the moment of use, `approve(exact)` → swap → `approve(0)`, `eth_simulateV1` before any send, EIP-1559 next-base-fee projection, dynamic slippage tolerance, V2 / V3 tick-walk price impact | [`baselab/execution/`](baselab/execution) |
| **State machine + latency** | `ARMED / FROZEN / SLEEPING / HALTED` with isolated, time-boxed safety handlers; 150 ms RTT gate; 500 ms WebSocket-gap watchdog | [`baselab/state.py`](baselab/state.py), [`baselab/latency.py`](baselab/latency.py) |
| **Research lab** | Replay on real logs with exact block→time mapping, cross-DEX arbitrage tracker (closed form), 15-minute swing backtester, shadow ledger (what each filter is worth), day-clustered bootstrap, funding-carry measurement | [`baselab/backtest/`](baselab/backtest), [`baselab/research/`](baselab/research), [`scripts/`](scripts) |

### Engineering details worth a look

- **Closed-form arbitrage sizing.** Two constant-product legs compose into `out(x) = A·x / (B + C·x)`, so the optimal size is `x* = (√(AB) − B) / C`; verified against brute force in [`tests/test_arb.py`](tests/test_arb.py).
- **Exact timestamps across a regime change.** Base moved from 2 s to 0.2 s blocks inside the dataset; [`scripts/block_times.py`](scripts/block_times.py) bisects the switch point so a linear block→time formula cannot mis-date the data by days.
- **Fail-closed holder ledger without an archive node.** Replays `Transfer` logs backwards until the ledger sums to `totalSupply`; if it cannot reconcile, the token is rejected.
- **Local mainnet-fork test with fake ETH.** [`scripts/fork_test.py`](scripts/fork_test.py) deploys tokens with a 10% sell tax and a honeypot onto a fork of Base, then checks that the validator catches them, that real Uniswap swaps settle, that allowances end at 0, and that impossible `minOut` is rejected **before** broadcast (13/13 checks).
- **Pre-registration as code.** Gates are evaluated by [`baselab/research/stats.py`](baselab/research/stats.py) and the scripts in `scripts/`; the gate definitions and every amendment live in [`docs/greek/GO-NO-GO.el.md`](docs/greek/GO-NO-GO.el.md) (Greek working notes) and are summarised in [docs/METHODOLOGY.md](docs/METHODOLOGY.md).

### Bugs the process caught

Writing a hostile evaluation harness exposed real defects in my own work. A sample:

1. LP fees were being counted as slippage, so the 0.2% impact cap rejected every trade.
2. Exit gas was missing from P&L, so losing trades looked like winners.
3. OP-stack receipts return `l1Fee` as a hex string; live gas accounting would have crashed.
4. The holder ledger needed archive state; replaced by a log replay reconciled with `totalSupply`.
5. Paper fills happened at the signal price; changed to the next block's reserves.
6. Block time was assumed to be 2 s; measured 0.2 s (and found it had *changed* inside the data).
7. Aerodrome pools emit different `Swap`/`Sync` event hashes than Uniswap V2. Treated as identical, the arbitrage test would have seen no Aerodrome data and falsely concluded "no opportunities".
8. A variable-shadowing bug in the replay (caught by tests), and an exit rule in a proposed swing spec that fired without any price recovery (caught by a synthetic test before touching real data).

## Quickstart

```bash
git clone https://github.com/ntinosgkiou-code/base-strategy-lab && cd base-strategy-lab
python -m venv .venv && source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

pytest -q                                                # 55 tests, offline
python -m baselab backtest --blocks 43200 --seed 1       # full pipeline on a synthetic market, offline
```

**Reproduce the real-data results** (free public RPCs are rate-limited; expect minutes to an hour per window):

```bash
python scripts/find_pools.py --out pools.json                          # most active Uniswap V2/V3 WETH pools
python scripts/collect_history.py --pools pools.json --days 3 --out data/hist
python scripts/block_times.py --data data/hist                         # exact block -> timestamp index
python scripts/real_backtest.py --data data/hist                       # 1-minute spec strategy + gate verdict
python scripts/real_backtest.py --data data/hist --strategy rsi        # or: breakout
python scripts/find_arb_pairs.py --tokens-from pools.json --out arb-pools.json   # then collect_history per token, then:
python scripts/arb_tracker.py --root data/arb                          # cross-DEX arbitrage gate
python scripts/funding_carry.py                                        # 12-month funding-carry measurement
```

**Fork test with fake money** (needs [Foundry](https://getfoundry.sh)):

```bash
anvil --fork-url https://mainnet.base.org --chain-id 8453 --block-time 1 --compute-units-per-second 15
python scripts/fork_test.py
```

**Paper mode on live data** (no keys, simulated fills): `python -m baselab paper --pools pools.json --wss wss://base-rpc.publicnode.com --http https://base-rpc.publicnode.com`

## Repository map

```
baselab/            the library (ingestion, security, quant, risk, execution, backtest, research)
scripts/            data collection, backtests, gates, fork test, funding-carry measurement
tests/              55 offline tests (math, state machine, router, replay, arbitrage, swing)
docs/               methodology, results, architecture, generated SVG graphics, Greek working notes
```

## Limitations (read before trusting any number)

- **Survivorship bias favours the strategies.** Pools were chosen because they are active *today*; tokens that died are missing. A positive result would have required a survivorship-free re-test before it counted.
- **V3 / Slipstream pools use the active-range virtual reserves** from each swap event: exact inside the range, an approximation outside it.
- **Arbitrage assumes no competitors** and uses end-of-block states; sequencer ordering and priority-fee auctions cannot be reconstructed from logs. The data covers only ~3 days of 0.2 s blocks.
- **Funding carry** excludes counterparty risk (for example the 2026-10-29 Binance hack), liquidation risk on the short leg, and taxes.
- **Live mode has never traded real funds.** It was exercised on a local fork and in short paper sessions. It is research code and has not been audited.
- Free public RPCs impose limits (log ranges, rate limits); the collectors retry and resume, but they are not a production data pipeline.

## Disclaimer

This is research software. It is **not financial advice**, offers no profit guarantee, and the live mode is disabled unless you
pass `--i-understand-risk` **and** `--max-notional`. Do not run it with funds you cannot afford to lose. The `scripts/fork_test.py`
key is the public default key of local test chains and holds fake ETH only.

## Work with me

I run independent **strategy reality checks** (a pre-registered GO/NO-GO on real on-chain data, with a short written report) and
build Base/EVM bots and on-chain automations. If you have a backtest you want a second pair of eyes on, open an issue titled
`Reality check` or contact me through my GitHub profile: [@ntinosgkiou-code](https://github.com/ntinosgkiou-code).

## License

MIT. See [LICENSE](LICENSE).
