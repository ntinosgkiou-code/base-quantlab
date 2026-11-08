# Build log

**How this was built.** Base-QuantLab was developed with **Claude Code** as an AI pair-programmer, in a single long working session on 2026-11-08.
The code was written with AI assistance; the gates, the data and every result are reproducible from the scripts and tests in this repository, and the
decisions and trade-offs are written down in [DECISIONS.md](DECISIONS.md). The git history is short because the working folder was exported into this
repository at the end; it does not reflect the order of work, this log does.

## 1. Specification and architecture first
- Started from a request for a 1-minute scalping bot on Base with pre-trade security checks, risk controls and private execution.
- Wrote the architecture (five decoupled modules, a state machine, an event bus) **before** any code, then implemented it.
- A synthetic market was used only to exercise the plumbing: P&L near zero, one to four trades a day. It is not evidence of anything.
- First verdict from the author: Base has no public mempool and no order book, and Flashbots-style bundle relays are an Ethereum mechanism, so those parts of the spec were adapted (swap-flow imbalance instead of order-book imbalance).

## 2. Meeting reality
- **Real new tokens:** the validator rejected 7 of 7 recent pairs (unlocked LP, top-10 holders at 90–100%).
- **Fork test with fake ETH:** deploys a clean token, one with a 10% sell tax and one honeypot on a fork of Base; 13 of 13 checks after fixes.
- **First paper run on live data** exposed what a synthetic market hides: Base's blocks are 0.2 s, free RPCs rate-limit and drop sockets, load-balanced backends say "block not found", missed blocks after a reconnect leave holes in candles, and one infinite round-trip value crashed the reporter. Fourteen fixes followed (see the list in the README).

## 3. Pre-registered evaluation on real logs
- Wrote the gates **before** running (`docs/greek/GO-NO-GO.el.md`, Greek working log).
- 1-minute multi-factor scalper: −2.14% per trade out of sample; five looser variants all negative; cost-filtered version made no trades.
- Downloaded two further 14-day windows so that each new idea ran on data nobody had tuned on.

## 4. Outside ideas, tested the same way
Ideas that arrived from other sources (other AI answers, articles) were pre-registered and run once:
- RSI reversion and volume breakout: both negative out of sample.
- Cross-DEX arbitrage (Uniswap ↔ Aerodrome): found that Aerodrome emits different event hashes than Uniswap V2 (which would have hidden all its data); 186 opportunities, 28% still alive two blocks later, negative after a 50% priority fee. Not GO.
- 15-minute swing / mean reversion: a synthetic test showed the proposed exit rule fired without any price recovery; it was redefined **before** touching real data. No arm passed.
- Funding-rate carry measured on 12 months of public data from three exchanges: 1.5% mean net APR.
- A proposed "safe fallback" that approves a token when the RPC times out was rejected (see DECISIONS.md, entry 1).

## 5. Packaging
- Exported a clean copy, renamed the package, removed personal paths, added license, CI, generated graphics and documentation.
- After an outside review, added the live sender's missing nonce management, the latency-gate issue and the delay assumption to the stated limitations.

## What is not done
Nonce manager and concurrent live intents; tick-level simulation for V3 pools; a survivorship-free universe; recording transaction hashes to measure what winners pay in priority fees; metrics/monitoring beyond JSON event logs; dependency and security scanning in CI.
