# Methodology: how the lab avoids fooling itself

The rule that matters most: **a strategy is judged by criteria written before its first run, on data it was not tuned on.**
If it fails, it is not "re-tuned until it passes": any parameter change is a new hypothesis with new data and a stricter bar.

## 1. Pre-registration

Before running a stage I write its hypothesis, data window, cost model, sample-size rule and pass/fail gates, then run it once.
Amendments are allowed only *before* real results exist and are logged with the reason (the working log is
[`docs/greek/GO-NO-GO.el.md`](greek/GO-NO-GO.el.md), in Greek). Two examples of amendments that were made for good reasons:

- The proposed 15-minute swing exit "z-score ≥ 0" fires once the dip is older than the 3-hour window **even if the price never recovered**.
  A synthetic unit test exposed it; the exit was redefined as "price back to its level 3 hours before the signal" before touching real data.
- A stage's data window was shortened from "4 days" to "all blocks after Base moved to 0.2 s blocks", because mixing regimes would have changed what latency means.

## 2. Execution realism

| Item | Treatment |
|------|-----------|
| Fill timing | Next block (or `t + d` blocks), never the signal candle's close |
| Price impact | Real pool reserves (V2) or virtual reserves of the active range (V3); trades with impact above the cap are skipped |
| Fees | The pool's own LP fee, **not** counted as slippage; plus a fixed adverse haircut per fill |
| Gas | Measured gas price × gas units plus the OP-stack L1 data fee; reverted transactions still pay |
| Time | Exact block→timestamp index; the regime change from 2 s to 0.2 s blocks is located by bisection |

## 3. Statistical hygiene

- **Time split**, never random: in-sample first 60%, out-of-sample last 40% (or discovery / validation / holdout 50/25/25 for variant searches).
- **Fresh windows**: each new idea runs on a window that no earlier idea has been tuned on.
- **Clustered bootstrap**: trades on the same day are correlated (tokens move together), so confidence intervals resample whole days.
- **Multiple testing**: the confidence level tightens as hypotheses accumulate (95% → 97.5% → 99.67%).
- **Robustness gates**: the result must survive a 5-minute execution delay, fees × 1.5, removal of the three best trades, and a 50% priority-fee bid where relevant.
- **Concentration check**: no single pool may supply most of the profit.
- **Insufficient data is not "go"**: if the sample is below the registered minimum, the verdict is "insufficient data", never a pass.

## 4. Known biases left in (and stated)

Survivorship (pools chosen by today's activity), approximate V3 liquidity outside the active range, no competitors in the arbitrage model, a short window of 0.2 s blocks.
Every positive result would have required a re-test that removes the bias favouring it; none was needed because nothing passed.

## 5. What I would do differently

Choose the universe at the *start* of the window (including tokens that later died), record transaction hashes in the collectors so winners' actual priority fees can be measured, and run a tick-level liquidity simulation for V3 pools.
