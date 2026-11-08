"""Build an exact block -> timestamp index for a dataset (blocktimes.json).

Base changed its block time (2 s -> 0.2 s) inside our data window, so a single linear formula is wrong by
days. We sample every `step` blocks; any segment whose average block time is not a clean constant is
bisected until the switch point is pinned to adjacent blocks. Between knots the time is linear (exact
inside a constant-block-time regime).
    python scripts/block_times.py --data data/hist4d
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from web3 import AsyncWeb3
from web3.providers.rpc import AsyncHTTPProvider


async def ts_of(w3: AsyncWeb3, cache: dict[int, int], b: int) -> int:
    if b not in cache:
        for attempt in range(8):
            try:
                cache[b] = int((await w3.eth.get_block(b))["timestamp"])
                break
            except Exception as exc:
                if attempt == 7:
                    raise
                await asyncio.sleep(0.5 * 2 ** attempt if "429" in str(exc) or "limit" in str(exc).lower() else 0.5)
    return cache[b]


def pure(a: int, ta: int, b: int, tb: int, regimes: tuple[float, ...]) -> bool:
    """True if the segment is consistent with ONE constant block time (seconds are integers)."""
    n = b - a
    return any(abs((tb - ta) - n * r) <= 1.0 for r in regimes)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--rpc", default="https://mainnet.base.org")
    ap.add_argument("--step", type=int, default=5_000)
    a = ap.parse_args()
    meta = json.loads((a.data / "meta.json").read_text())
    w3 = AsyncWeb3(AsyncHTTPProvider(a.rpc))
    lo, hi = meta["from_block"], meta["anchor_block"]
    cache: dict[int, int] = {}
    grid = list(range(lo, hi, a.step)) + [hi]
    for i, b in enumerate(grid):
        await ts_of(w3, cache, b)
        if i % 50 == 0:
            print(f"grid {i}/{len(grid)}", flush=True)
    regimes = (0.2, 1.0, 2.0)
    stack = [(grid[i], grid[i + 1]) for i in range(len(grid) - 1)]
    refined = 0
    while stack:
        x, y = stack.pop()
        if y - x <= 1 or pure(x, cache[x], y, cache[y], regimes):
            continue
        m = (x + y) // 2
        await ts_of(w3, cache, m)
        refined += 1
        stack += [(x, m), (m, y)]
    knots = sorted(cache.items())
    (a.data / "blocktimes.json").write_text(json.dumps(knots))
    span = knots[-1][1] - knots[0][1]
    switches = [(b0, b1, (t1 - t0) / (b1 - b0)) for (b0, t0), (b1, t1) in zip(knots, knots[1:])
                if b1 - b0 <= 2]
    print(f"{len(knots)} knots ({refined} refinement calls); real span {span / 86400:.2f} days")
    print(f"regime boundaries (adjacent-block knots): {switches[:6]}")


if __name__ == "__main__":
    asyncio.run(main())
