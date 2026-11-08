"""Replay REAL historical Base swaps through the same strategy / candle / exit code as live.

Per pool, a block-by-block event loop (heap of block numbers to visit):
    1. apply that block's real swaps (pool state + candle aggregator)
    2. execute actions scheduled for this block against the state AFTER its swaps (our tx assumed
       to land behind everyone else's in the block: conservative)
    3. at a minute boundary: close candles -> shadow ledger -> strategy -> schedule entry at block+1
    4. with an open position: trailing stop / time exit -> schedule exit at block+1

Pool state: V2 = reserves from Sync events (exact). V3 = virtual reserves of the active range from the
Swap event (sqrtPriceX96, liquidity); exact while a trade stays inside the current tick range, which the
0.2% impact cap makes the normal case. Our own trades do not feed back into the historical state.
Costs: LP fee (in the AMM formula), `haircut` extra adverse slippage per fill, gas + L1 fee per round trip.
"""
from __future__ import annotations

import gzip
import heapq
import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..config import Config
from ..domain import BlockHead, Position, Side
from ..execution.chain import PoolMeta
from ..ingestion.decode import decode_swap, v2_sync, v3_state
from ..ingestion.candles import CandleAggregator, minute_of
from ..quant.alt_strategies import EVALUATORS
from ..quant.indicators import atr
from ..quant.strategy import StrategyMatrix
from ..research.shadow import ShadowLedger
from ..risk.exits import ExitPolicy
from ..risk.slippage import V2Pool


@dataclass(slots=True)
class Dataset:
    anchor_block: int
    anchor_ts: int
    block_time: float
    from_block: int
    done_block: int
    pools: list[dict[str, Any]]
    logs: dict[str, list[tuple[int, int, str, bytes]]]  # pool -> (block, logIndex, kind, data), sorted
    # exact block->timestamp knots (scripts/block_times.py). Base changed 2 s -> 0.2 s blocks inside our
    # window, so the linear fallback is only valid for short recent snapshots.
    knot_blocks: np.ndarray | None = None
    knot_ts: np.ndarray | None = None

    def ts_of(self, block: int) -> int:
        if self.knot_blocks is not None:
            return int(np.interp(block, self.knot_blocks, self.knot_ts))
        return int(self.anchor_ts - (self.anchor_block - block) * self.block_time)

    def block_at(self, ts: float) -> int:
        """First block whose timestamp is >= ts."""
        if self.knot_blocks is not None:
            return int(math.ceil(np.interp(ts, self.knot_ts, self.knot_blocks)))
        return self.anchor_block - int((self.anchor_ts - ts) / self.block_time)


def load_dataset(path: Path, require_block_times: bool = False) -> Dataset:
    meta = json.loads((path / "meta.json").read_text())
    kb = kt = None
    bt_path = path / "blocktimes.json"
    if bt_path.exists():
        knots = json.loads(bt_path.read_text())
        kb = np.asarray([k[0] for k in knots], dtype=float)
        kt = np.asarray([k[1] for k in knots], dtype=float)
    elif require_block_times:
        raise FileNotFoundError(f"{bt_path} missing: run scripts/block_times.py (linear block time is wrong)")
    logs: dict[str, list[tuple[int, int, str, bytes]]] = defaultdict(list)
    seen: set[tuple[str, int, int]] = set()
    done = meta["done_block"]
    try:
        with gzip.open(path / "logs.jsonl.gz", "rt", encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    break  # truncated last line (file still being written)
                key = (r["a"], r["b"], r["i"])
                if r["b"] <= done and key not in seen:
                    seen.add(key)
                    logs[r["a"]].append((r["b"], r["i"], r["k"], bytes.fromhex(r["d"][2:])))  # compact
    except (EOFError, OSError, gzip.BadGzipFile):
        pass  # truncated gzip tail: keep every complete record before it
    for v in logs.values():
        v.sort()
    if logs:
        done = min(done, max(v[-1][0] for v in logs.values()))
    return Dataset(meta["anchor_block"], meta["anchor_ts"], meta["block_time"], meta["from_block"],
                   done, meta["pools"], dict(logs), kb, kt)


def v3_virtual_reserves(sqrt_x96: int, liquidity: int, weth_is_token0: bool, dec: int) -> tuple[float, float]:
    sp = sqrt_x96 / 2**96  # sqrt(raw1 / raw0)
    if sp <= 0 or liquidity <= 0:
        return 0.0, 0.0
    raw0, raw1 = liquidity / sp, liquidity * sp
    w_raw, t_raw = (raw0, raw1) if weth_is_token0 else (raw1, raw0)
    return w_raw / 1e18, t_raw / 10**dec


@dataclass(slots=True)
class PoolSimResult:
    pool: str
    symbol: str
    trades: list[dict[str, Any]] = field(default_factory=list)
    evaluations: int = 0
    signals: int = 0
    skipped_impact: int = 0
    swaps: int = 0
    reject_reasons: dict[str, int] = field(default_factory=lambda: defaultdict(int))


def simulate_pool(ds: Dataset, pm: dict[str, Any], cfg: Config, notional: float, gas_round_trip: float,
                  haircut: float, shadow: ShadowLedger, strat: str = "matrix") -> PoolSimResult:
    """strat: 'matrix' (the spec strategy) or a key of quant.alt_strategies.EVALUATORS ('rsi', 'breakout')."""
    meta = PoolMeta(pm["pool"], pm["token"], pm["weth_is_token0"], pm["token_decimals"], pm["is_v3"],
                    pm.get("fee_ppm", 3000))
    fee = meta.fee_ppm / 1e6
    res = PoolSimResult(meta.pool, pm.get("symbol", "?"))
    raw = ds.logs.get(meta.pool, [])
    if not raw:
        return res
    by_block: dict[int, list[tuple[int, int, str, bytes]]] = defaultdict(list)
    for r in raw:
        by_block[r[0]].append(r)

    now = [0.0]
    strategy = StrategyMatrix(cfg.strategy, clock=lambda: now[0])
    exits = ExitPolicy(cfg.strategy.trail_atr_mult, cfg.risk.max_hold_s)
    agg = CandleAggregator(history=500, retain_minutes=15)
    state = [0.0, 0.0]  # weth, token reserves (human units)

    heap: list[int] = list(by_block)
    first_ts, last_ts = ds.ts_of(raw[0][0]), ds.ts_of(raw[-1][0])
    m = minute_of(first_ts) + 60
    while m <= last_ts:
        heap.append(ds.block_at(m))
        m += 60
    heapq.heapify(heap)
    seen: set[int] = set()
    scheduled: dict[int, list[tuple[str, Any]]] = defaultdict(list)
    time_checks: set[int] = set()
    pos: Position | None = None
    pending_exit = False
    last_minute_closed = minute_of(first_ts)

    def route_out(side: Side, amount: float) -> tuple[float, float]:
        w, t = state
        if w <= 0 or t <= 0:
            return 0.0, 1.0
        amm = V2Pool(w, t, fee) if side is Side.BUY else V2Pool(t, w, fee)
        out = amm.out(amount)
        imp = 1 - (out / amount) / amm.marginal if amount > 0 else 0.0
        return out * (1 - haircut), imp

    while heap:
        b = heapq.heappop(heap)
        if b in seen:
            continue
        seen.add(b)
        ts = ds.ts_of(b)
        now[0] = float(ts)
        head = BlockHead(b, "", "", ts)
        # 1) real swaps of this block
        for _, log_index, kind, data in by_block.get(b, ()):
            if kind == "sync":
                r0, r1 = v2_sync(data)
                w, t = (r0, r1) if meta.weth_is_token0 else (r1, r0)
                state[0], state[1] = w / 1e18, t / 10**meta.token_decimals
                continue
            s = decode_swap(meta.pool, meta.weth_is_token0, meta.token_decimals, meta.is_v3, data, log_index, head)
            if kind == "v3swap":
                _, _, sqrt_x96, liq, _ = v3_state(data)
                state[0], state[1] = v3_virtual_reserves(sqrt_x96, liq, meta.weth_is_token0, meta.token_decimals)
            if s is not None:
                agg.add_swap(s)
                res.swaps += 1
        # 2) execute what was decided on an earlier block
        for kind, payload in scheduled.pop(b, ()):
            if kind == "entry" and pos is None:
                sig = payload
                tokens, imp = route_out(Side.BUY, notional)
                if imp > cfg.risk.max_impact or tokens <= 0:
                    res.skipped_impact += 1
                    continue
                pos = Position(meta.pool, meta.token, notional / tokens, tokens, notional, float(ts),
                               target=sig.target, stop=sig.stop, peak=notional / tokens,
                               meta={"atr": sig.atr, "max_hold_s": getattr(sig, "max_hold_s", cfg.risk.max_hold_s)})
                hold_block = ds.block_at(ts + getattr(sig, "max_hold_s", cfg.risk.max_hold_s))
                time_checks.add(hold_block)
                heapq.heappush(heap, hold_block)
            elif kind == "exit" and pos is not None:
                weth_out, _ = route_out(Side.SELL, pos.token_amount)
                net = weth_out - notional - gas_round_trip
                res.trades.append({"pool": meta.pool, "symbol": res.symbol, "entry_ts": pos.opened_at,
                                   "exit_ts": float(ts), "hold_s": ts - pos.opened_at, "reason": payload,
                                   "gross_ret": weth_out / notional - 1, "net_ret": net / notional, "net": net})
                pos, pending_exit = None, False
        # 3) minute close
        if minute_of(ts) > last_minute_closed:
            last_minute_closed = minute_of(ts)
            closed = agg.on_head(head)
            for c in closed:
                shadow.on_candle(c)
            if closed and pos is None and not scheduled.get(b + 1):
                c = closed[-1]  # evaluate once, on the newest closed candle
                cost = 2 * fee + 2 * haircut
                if strat == "matrix":
                    ev = strategy.evaluate(meta.pool, meta.token, agg.candles(meta.pool), round_trip_cost=cost)
                    signal, reasons, metrics = ev.signal, ev.reasons, ev.metrics
                else:
                    signal, reasons = EVALUATORS[strat](agg.candles(meta.pool), cost)
                    metrics = {}
                res.evaluations += 1
                shadow.record(meta.pool, c.minute, c.close, reasons, metrics)
                for reason in reasons:
                    res.reject_reasons[reason.split(":")[0]] += 1
                if signal is not None:
                    res.signals += 1
                    scheduled[b + 1].append(("entry", signal))
                    heapq.heappush(heap, b + 1)
        # 4) exits on the current price
        if pos is not None and not pending_exit and state[1] > 0:
            price_now = state[0] / state[1]
            if strat == "matrix":
                hist = agg.candles(meta.pool)
                a = atr(hist[-15:], cfg.strategy.atr_fast) if len(hist) >= 15 else pos.meta["atr"]
                d = exits.update(pos, price_now, a if math.isfinite(a) else pos.meta["atr"], float(ts))
                reason = d.reason if d is not None else None
            else:  # fixed take profit / hard stop / time exit (pre-registered, no trailing)
                held = ts - pos.opened_at
                reason = ("take_profit" if price_now >= pos.target else
                          "hard_stop" if price_now <= pos.stop else
                          "time_exit" if held >= pos.meta["max_hold_s"] else None)
            if reason is not None:
                scheduled[b + 1].append(("exit", reason))
                heapq.heappush(heap, b + 1)
                pending_exit = True
    return res
