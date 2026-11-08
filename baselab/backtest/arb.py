"""Cross-DEX arbitrage tracker on real history (GO-NO-GO.md stage 1e).

For one token with pools on Uniswap and on Aerodrome, at every block where something changed:
  * state per pool = (WETH, token) reserves at END of the block (V2: Sync; V3/CL: virtual reserves of the
    active range from the Swap event - exact inside the range, an assumption that FAVOURS the strategy outside it)
  * for each (uniswap pool, aerodrome pool) and each direction: buy token on the cheap pool, sell on the dear one.

Closed form (two constant-product legs compose to a Moebius map):
    out(x) = A x / (B + C x)          A = a b Rw_e Rt_c, B = Rt_e Rw_c, C = a Rt_e + a b Rt_c
    a = 1 - fee_buy_pool, b = 1 - fee_sell_pool            (c = cheap/buy pool, e = dear/sell pool)
    profit(x) = out(x) - x,  optimum x* = (sqrt(A B) - B) / C,  profitable iff A > B (the zero-size spread > 0).

Execution model: the attempt is decided at block t (size x* capped), lands at block t+d, and the contract reverts
unless profit(x) > gas at the state of block t+d. A revert still costs gas (Base charges reverts).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..ingestion.decode import v2_sync, v3_state
from .real_replay import Dataset, v3_virtual_reserves


@dataclass(frozen=True, slots=True)
class Attempt:
    pair: str
    block: int
    direction: int      # 1 = buy on uniswap / sell on aerodrome, 2 = reverse
    size: float         # WETH in
    predicted: float    # profit predicted at signal time, before gas
    net: float          # realised at block t+d, after gas (negative = reverted, gas lost)
    success: bool


def pool_states(ds: Dataset, pm: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(blocks, weth_reserve, token_reserve) at the end of every block with an event of this pool."""
    blocks: list[int] = []
    rw: list[float] = []
    rt: list[float] = []
    dec, w0 = pm["token_decimals"], pm["weth_is_token0"]
    v3 = pm["is_v3"]
    for b, _, kind, data in ds.logs.get(pm["pool"], ()):
        if v3:
            if kind != "v3swap":
                continue
            _, _, sqrt_x96, liq, _ = v3_state(data)
            w, t = v3_virtual_reserves(sqrt_x96, liq, w0, dec)
        else:
            if kind != "sync":
                continue
            r0, r1 = v2_sync(data)
            w, t = ((r0, r1) if w0 else (r1, r0))
            w, t = w / 1e18, t / 10**dec
        if blocks and blocks[-1] == b:  # keep the LAST state of the block
            rw[-1], rt[-1] = w, t
        else:
            blocks.append(b)
            rw.append(w)
            rt.append(t)
    return np.asarray(blocks, dtype=np.int64), np.asarray(rw), np.asarray(rt)


def aligned(blocks_all: np.ndarray, blocks: np.ndarray, rw: np.ndarray, rt: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Forward-fill a pool's state onto the union of event blocks (nan before its first event)."""
    idx = np.searchsorted(blocks, blocks_all, side="right") - 1
    ok = idx >= 0
    w = np.where(ok, rw[np.clip(idx, 0, None)], np.nan) if len(rw) else np.full(len(blocks_all), np.nan)
    t = np.where(ok, rt[np.clip(idx, 0, None)], np.nan) if len(rt) else np.full(len(blocks_all), np.nan)
    return w, t


def legs(rw_c: np.ndarray, rt_c: np.ndarray, f_c: float, rw_e: np.ndarray, rt_e: np.ndarray, f_e: float
         ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    a, b = 1 - f_c, 1 - f_e
    return a * b * rw_e * rt_c, rt_e * rw_c, a * rt_e + a * b * rt_c


def optimal_size(A: np.ndarray, B: np.ndarray, C: np.ndarray, cap: float) -> np.ndarray:
    with np.errstate(all="ignore"):
        x = (np.sqrt(A * B) - B) / C
    x = np.where((A > B) & (C > 0) & np.isfinite(x), x, 0.0)
    return np.minimum(x, cap)


def out_of(A: np.ndarray, B: np.ndarray, C: np.ndarray, x: np.ndarray) -> np.ndarray:
    with np.errstate(all="ignore"):
        return A * x / (B + C * x)


def track_pair(name: str, blocks: np.ndarray, u: tuple[np.ndarray, np.ndarray, float], a: tuple[np.ndarray, np.ndarray, float],
               gas: float, cap: float, delay: int, gas_fail: float | None = None,
               bid_frac: float = 0.0, intra: str = "end") -> list[Attempt]:
    """u / a = (weth reserve, token reserve, fee) of the uniswap / aerodrome pool, aligned on `blocks`.

    Stress options (GO-NO-GO.md S3/S4):
      bid_frac : priority fee = bid_frac x the profit predicted at signal time; paid on success AND on a revert
                 that gets included (the contract's min-profit guard is gas + bid)
      intra    : "end"   -> state at the END of block t+d
                 "worst" -> the lower profit of (end of block t+d-1, end of block t+d): our tx position inside the
                            block is unknown
    """
    gas_fail = gas if gas_fail is None else gas_fail
    out: list[Attempt] = []
    cands = []
    for direction, (c, e) in ((1, (u, a)), (2, (a, u))):
        A, B, C = legs(c[0], c[1], c[2], e[0], e[1], e[2])
        valid = np.isfinite(A) & np.isfinite(B) & np.isfinite(C) & (c[0] > 0) & (c[1] > 0) & (e[0] > 0) & (e[1] > 0)
        A, B, C = np.where(valid, A, 0.0), np.where(valid, B, 1.0), np.where(valid, C, 1.0)
        x = optimal_size(A, B, C, cap)
        pred = out_of(A, B, C, x) - x
        mask = valid & (x > 0) & (pred * (1 - bid_frac) > gas)   # a rational bot attempts only if it nets > 0 after the bid
        # one attempt per gap EPISODE: our own trade would have closed it, the historical state does not show that
        starts = mask & ~np.concatenate(([False], mask[:-1]))
        for i in np.nonzero(starts)[0]:
            cands.append((int(blocks[i]), direction, int(i), float(x[i]), float(pred[i]), (A, B, C)))
    cands.sort(key=lambda r: (r[0], -r[4]))
    next_ok = -1
    for blk, direction, i, size, pred, (A, B, C) in cands:
        if blk < next_ok:
            continue
        next_ok = blk + delay + 1  # our own trade closes the gap (or a revert has been paid): cool down
        idx = [int(np.searchsorted(blocks, blk + delay, side="right") - 1)]
        if intra == "worst" and delay > 0:
            idx.append(int(np.searchsorted(blocks, blk + delay - 1, side="right") - 1))
        profits = [float(out_of(A[j:j + 1], B[j:j + 1], C[j:j + 1], np.asarray([size]))[0] - size) for j in idx]
        profit_j = min(profits)
        bid = bid_frac * pred
        if np.isfinite(profit_j) and profit_j > gas + bid:
            out.append(Attempt(name, blk, direction, size, pred, profit_j - gas - bid, True))
        else:
            out.append(Attempt(name, blk, direction, size, pred, -(gas_fail + bid), False))
    return out


def episode_durations(blocks: np.ndarray, u: tuple[np.ndarray, np.ndarray, float], a: tuple[np.ndarray, np.ndarray, float],
                      gas: float, cap: float) -> list[int]:
    """Length in blocks of every profitable-gap episode (from first profitable block to the first block it is gone)."""
    out: list[int] = []
    for c, e in ((u, a), (a, u)):
        A, B, C = legs(c[0], c[1], c[2], e[0], e[1], e[2])
        valid = np.isfinite(A) & np.isfinite(B) & np.isfinite(C) & (c[0] > 0) & (c[1] > 0) & (e[0] > 0) & (e[1] > 0)
        A, B, C = np.where(valid, A, 0.0), np.where(valid, B, 1.0), np.where(valid, C, 1.0)
        x = optimal_size(A, B, C, cap)
        mask = valid & (x > 0) & (out_of(A, B, C, x) - x > gas)
        i, n = 0, len(mask)
        while i < n:
            if mask[i]:
                j = i
                while j + 1 < n and mask[j + 1]:
                    j += 1
                end_block = int(blocks[j + 1]) if j + 1 < n else None
                if end_block is not None:
                    out.append(end_block - int(blocks[i]))
                i = j + 1
            else:
                i += 1
    return out
