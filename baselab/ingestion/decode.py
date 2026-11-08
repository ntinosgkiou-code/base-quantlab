"""Fast, dependency-free decoding of Uniswap V2/V3 Swap and V2 Sync log data (32-byte ABI words).

eth_abi's generic decoder was 73% of replay CPU time; these events are fixed-layout, so plain
int.from_bytes on each word is exact and ~10x faster. Shared by the live reader and the replay.
"""
from __future__ import annotations

from ..domain import BlockHead, Side, Swap


def word(data: bytes, i: int, signed: bool = False) -> int:
    return int.from_bytes(data[32 * i:32 * (i + 1)], "big", signed=signed)


def v2_sync(data: bytes) -> tuple[int, int]:
    return word(data, 0), word(data, 1)


def v3_state(data: bytes) -> tuple[int, int, int, int, int]:
    """(amount0, amount1, sqrtPriceX96, liquidity, tick) of a V3 Swap event."""
    return (word(data, 0, True), word(data, 1, True), word(data, 2), word(data, 3), word(data, 4, True))


def decode_swap(pool: str, weth_is_token0: bool, token_decimals: int, is_v3: bool, data: bytes,
                log_index: int, head: BlockHead) -> Swap | None:
    """Swap with price = WETH per token and quote volume in WETH. BUY = someone bought the token."""
    wd, td = 10**18, 10**token_decimals
    if is_v3:
        a0, a1 = word(data, 0, True), word(data, 1, True)
        weth_amt, tok_amt = (a0, a1) if weth_is_token0 else (a1, a0)
        side = Side.BUY if weth_amt > 0 else Side.SELL  # pool received WETH = token bought
        w, t = abs(weth_amt) / wd, abs(tok_amt) / td
    else:
        a0i, a1i, a0o, a1o = word(data, 0), word(data, 1), word(data, 2), word(data, 3)
        wi, ti, wo, to = (a0i, a1i, a0o, a1o) if weth_is_token0 else (a1i, a0i, a1o, a0o)
        side = Side.BUY if wi > 0 else Side.SELL
        w, t = (wi / wd, to / td) if side is Side.BUY else (wo / wd, ti / td)
    if w <= 0 or t <= 0:
        return None
    return Swap(pool, head.number, head.hash, head.timestamp, log_index, side, w / t, w)
