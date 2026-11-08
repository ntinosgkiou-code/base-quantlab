# Design decisions

Each entry records what was decided, why, what was rejected, and what it costs. Most were made while building and testing; the evidence is in
[RESULTS.md](RESULTS.md), the tests, and [BUILD-LOG.md](BUILD-LOG.md).

## 1. The security validator fails closed

**Decision.** Any error, timeout, unreadable result or unreconciled holder history rejects the token.
**Rejected.** A "safe fallback" that approves when the RPC times out or hits a log limit, proposed in an outside draft. It would approve a scam token *exactly when verification fails*, and spam-heavy scam tokens are what trigger those limits.
**Cost.** Most tokens are rejected: new ones have unlocked LP and 90–100% top-10 concentration, old ones cannot be reconciled within the scan window (about 17 hours of Base blocks). That is the safe answer, but it means the validator trades very little. Seen directly: 7 of 7 recent pairs rejected.

## 2. Honeypot check = state-override `eth_call` at a pinned block

**Decision.** A small Solidity probe is injected at a throwaway address with an ETH balance and simulates buy → transfer → sell in one call; no transaction is sent, all checks use the same block.
**Rejected.** Third-party scanner APIs (extra trust, rate limits, not reproducible) and a fresh local fork per token (heavy). A local Anvil fork is used in the **tests** instead.
**Cost.** Needs an RPC that supports state overrides and a compiled probe; the probe goes through the Uniswap V2 router, so a token with no V2 pair cannot be probed and is rejected.

## 3. Fills happen on the next block's real reserves; the LP fee is not slippage

**Decision.** A signal on block *t* executes against the pool state after block *t + d*. The pool's LP fee is a known cost; "slippage" is price impact plus a fixed adverse haircut.
**Why.** Filling at the signal price is the commonest silent bias. A bug where the LP fee was counted as slippage made the 0.2% impact cap reject *every* trade; fixing it was a change in definition, not tuning.

## 4. Pre-registered gates, one run per idea, a fresh window each time

**Decision.** Hypothesis, cost model, minimum sample and pass/fail criteria are written before the first run. A failed idea becomes a new hypothesis on new data, never a re-tune. Amendments are allowed only before real results exist and are logged (two are described in [METHODOLOGY.md](METHODOLOGY.md)).
**Why.** Fourteen configurations were tried; with that many, a "pass" by luck is likely, so the confidence level tightened as hypotheses accumulated (95% → 97.5% → 99.67%).
**Cost.** Slower, and several results are "insufficient data" instead of a clean yes/no. That is deliberate.

## 5. Day-clustered bootstrap where trades overlap in time

**Decision.** In the 15-minute study, trades across tokens are correlated (tokens move together), so intervals resample whole days. Earlier stages used a plain bootstrap over trades.
**Why.** A test shows the clustered interval is more than twice as wide as the naive one on correlated data.

## 6. Position sizing starts from "no edge"

**Decision.** The fractional-Kelly prior is a 50% win rate with a payoff of 1, so Kelly is 0 until ≥ 30 measured trades show a positive edge; live mode sizes to 0 until then. Paper mode uses a small fixed exploration size instead.
**Why.** Sizing from a hoped-for edge is how backtests turn into losses. The rule encodes the evidence gate in the code.

## 7. Holder concentration from a log replay reconciled with `totalSupply`

**Decision.** Replay `Transfer` logs backwards until the ledger sums to `totalSupply` and no balance is negative; if that does not happen inside the scan window, reject.
**Rejected.** Archive-state lookups (not available on free RPCs) and trusting a top-holders API.

## 8. Exact block→time index instead of a linear formula

**Decision.** Block timestamps are sampled and bisected until each segment has a constant block time.
**Why.** Base changed from 2 s to 0.2 s blocks about three days before the final measurements; a linear formula would be off by days on part of the data, and the latency model depends on block time.

## 9. Arbitrage sizing in closed form

**Decision.** Two constant-product legs compose into `out(x) = A·x / (B + C·x)`, so `x* = (√(AB) − B) / C`. Verified against a brute-force search in the tests.
**Why.** Exact, fast over hundreds of thousands of events, and checkable. **Approximation kept:** V3 / Slipstream pools use the active-range virtual reserves, which flatters the strategy outside that range; a pass would have required a tick-level re-test.

## 10. Hurst gate at 0.55, not 0.5

**Decision.** The default regime gate is H ≥ 0.5 + 0.05 because the estimator's standard error at ~120 points is about 0.05–0.08.
**Check.** A variant with the literal 0.5 was pre-registered and tested; it failed as well. Result: the filter works (+0.56% better forward return than what it rejected) but cannot pay the fees.

## 11. Evidence before a contract

**Decision.** No arbitrage contract was written before the measurement stage passed.
**Why.** A fork test shows that code runs; it cannot show whether you win inclusion against faster participants. Only the measured gap lifetime (28% survive 2 blocks) and the priority-fee stress could say that, and they said no.

## 12. Scope: no strategy that profits at other users' expense

The lab does not evaluate sandwiching, wash trading, pump-and-dump or spoofing, and its documentation makes no profit promise.

## 13. Live mode is off by default

**Decision.** `live` requires `--i-understand-risk`, `--max-notional` and an HTTP RPC; there is a `STOP`-file kill switch, a daily-loss sleep and a 2-loss / 3%-slippage breaker. It has never been run with real funds, and its known gaps (no nonce manager, no concurrency testing) are listed in the README.
