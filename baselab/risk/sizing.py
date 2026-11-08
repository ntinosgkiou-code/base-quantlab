"""Layer 4: Bayesian fractional-Kelly x volatility scaling x risk cap x liquidity cap.

Default prior is "no edge" (p=0.5, b=1 => f*=0): live trading cannot size above zero until the
measured record proves an edge. See ARCHITECTURE.md §7.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Mode, RiskParams


class EdgeEstimator:
    def __init__(self, prior_p: float, prior_b: float, strength: float) -> None:
        self._p0, self._b0, self._k = prior_p, prior_b, strength
        self.wins: list[float] = []
        self.losses: list[float] = []

    @property
    def n(self) -> int:
        return len(self.wins) + len(self.losses)

    def record(self, ret: float) -> None:
        """ret = net return on notional of a closed trade (after fees, tax, slippage)."""
        (self.wins if ret > 0 else self.losses).append(ret)

    def p_hat(self) -> float:
        return (self._p0 * self._k + len(self.wins)) / (self._k + self.n)

    def b_hat(self) -> float:
        if self.wins and self.losses:
            b_obs = (sum(self.wins) / len(self.wins)) / abs(sum(self.losses) / len(self.losses))
            w = self.n / (self.n + self._k)
            return w * b_obs + (1 - w) * self._b0
        return self._b0

    def kelly(self) -> float:
        p, b = self.p_hat(), self.b_hat()
        return p - (1 - p) / b if b > 0 else -1.0

    def expectancy(self) -> float:
        rets = self.wins + self.losses
        return sum(rets) / len(rets) if rets else 0.0


@dataclass(frozen=True, slots=True)
class SizeDecision:
    quote_amount: float
    reasons: tuple[str, ...]
    components: dict[str, float] = field(default_factory=dict)


class PositionSizer:
    def __init__(self, params: RiskParams, mode: Mode, edge: EdgeEstimator | None = None) -> None:
        self._p = params
        self._mode = mode
        self.edge = edge or EdgeEstimator(params.prior_win_rate, params.prior_payoff, params.prior_strength)

    def size(self, equity: float, price: float, atr_fast: float, vol_expansion: float,
             stop_distance_pct: float, liquidity_cap: float, extreme_vol: float) -> SizeDecision:
        p = self._p
        reasons: list[str] = []
        f_star = self.edge.kelly()
        proven = self.edge.n >= p.min_trades_for_edge
        if not proven and self._mode is not Mode.LIVE:
            f = p.paper_exploration_fraction
            reasons.append("exploration_size(edge_unproven)")
        elif not proven:
            return SizeDecision(0.0, ("live_requires_proven_edge",), {"n": self.edge.n})
        elif f_star <= 0:
            return SizeDecision(0.0, (f"no_edge:kelly={f_star:.3f}",), {"kelly": f_star})
        else:
            f = p.kelly_fraction * f_star

        sigma = atr_fast / price if price > 0 else float("inf")
        vol_scale = min(1.0, p.target_vol / sigma) if sigma > 0 else 0.0
        if vol_expansion >= extreme_vol:
            vol_scale *= 0.25
            reasons.append("extreme_vol_scale_down")

        kelly_notional = f * vol_scale * equity
        risk_cap = p.risk_per_trade * equity / stop_distance_pct if stop_distance_pct > 0 else 0.0
        hard_cap = p.max_position_fraction * equity
        amount = max(0.0, min(kelly_notional, risk_cap, liquidity_cap, hard_cap))
        comps = {"kelly": f_star, "f": f, "vol_scale": vol_scale, "kelly_notional": kelly_notional,
                 "risk_cap": risk_cap, "liquidity_cap": liquidity_cap, "hard_cap": hard_cap}
        binding = min(("kelly_notional", kelly_notional), ("risk_cap", risk_cap),
                      ("liquidity_cap", liquidity_cap), ("hard_cap", hard_cap), key=lambda kv: kv[1])[0]
        reasons.append(f"binding:{binding}")
        return SizeDecision(amount, tuple(reasons), comps)
