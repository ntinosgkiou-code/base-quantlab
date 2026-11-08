"""Shadow ledger: does each filter actually help? Measured, not assumed.

Every strategy evaluation (passed or rejected, with its reasons) is recorded with the close price.
`horizon_s` later, the forward return of a hypothetical BUY at that close is resolved. Comparing groups
answers e.g. "when the Hurst gate kept us out, would we have lost money?":

    gate value(regime) = mean_fwd(passed) - mean_fwd(rejected only by regime_halt)

A filter whose rejected group has HIGHER forward returns than the passed group is destroying edge.
Forward returns are gross (no fees); the trade log holds net results.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from ..domain import Candle


@dataclass(slots=True)
class _Pending:
    pool: str
    minute: int
    price: float
    group: str
    reasons: tuple[str, ...]
    metrics: dict[str, float]


@dataclass(slots=True)
class ShadowLedger:
    horizon_s: int = 180
    path: Path | None = None
    _pending: list[_Pending] = field(default_factory=list)
    resolved: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))

    @staticmethod
    def group_of(reasons: tuple[str, ...]) -> str:
        if not reasons:
            return "passed"
        kinds = {r.split(":")[0] for r in reasons}
        if any(k.startswith("warmup") for k in kinds):
            return "warmup"
        return "rejected:" + "+".join(sorted(kinds)) if len(kinds) <= 2 else "rejected:multiple"

    def record(self, pool: str, minute: int, price: float, reasons: tuple[str, ...],
               metrics: dict[str, float]) -> None:
        g = self.group_of(reasons)
        if g != "warmup" and price > 0:
            self._pending.append(_Pending(pool, minute, price, g, reasons, dict(metrics)))

    def on_candle(self, c: Candle) -> int:
        done, keep = [], []
        for p in self._pending:
            (done if p.pool == c.pool and c.minute >= p.minute + self.horizon_s else keep).append(p)
        self._pending = keep
        for p in done:
            fwd = c.close / p.price - 1
            self.resolved[p.group].append(fwd)
            if self.path is not None:
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"pool": p.pool, "minute": p.minute, "group": p.group, "fwd": fwd,
                                        "reasons": p.reasons,
                                        "metrics": {k: v for k, v in p.metrics.items() if math.isfinite(v)}}) + "\n")
        return len(done)

    def summary(self) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for g, xs in sorted(self.resolved.items()):
            n = len(xs)
            out[g] = {"n": n, "mean_fwd": sum(xs) / n, "hit_rate": sum(x > 0 for x in xs) / n,
                      "mean_abs_fwd": sum(abs(x) for x in xs) / n}
        return out

    def report(self) -> str:
        s = self.summary()
        lines = [f"{g:<40} n={v['n']:<6.0f} mean_fwd={v['mean_fwd']:+.3%}  hit={v['hit_rate']:.0%}  "
                 f"|fwd|={v['mean_abs_fwd']:.3%}" for g, v in s.items()]
        if "passed" in s and "rejected:regime_halt" in s:
            gv = s["passed"]["mean_fwd"] - s["rejected:regime_halt"]["mean_fwd"]
            lines.append(f"Hurst gate value = {gv:+.3%} per signal "
                         f"({'helps' if gv > 0 else 'HURTS or no effect'}; needs n>=100 per group to mean anything)")
        return "\n".join(lines) or "no resolved evaluations yet"
