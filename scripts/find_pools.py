"""Read-only: find the most active Uniswap V2 and V3 WETH pools on Base right now and write pools.json.

Counts Swap events over the last N blocks, keeps only pools whose factory() is the real Uniswap factory
(forks emit identical events) and that have WETH on one side.
    python scripts/find_pools.py --blocks 3000 --top 8 --kinds v2,v3 --out pools.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from pathlib import Path

from eth_utils import keccak, to_checksum_address
from web3 import AsyncWeb3
from web3.providers.rpc import AsyncHTTPProvider

FACTORY = {"v2": "0x8909dc15e40173ff4699343b6eb8132c65e18ec6", "v3": "0x33128a8fc17869897dce68ed026d694621f6fdfd"}
WETH = "0x4200000000000000000000000000000000000006"
TOPIC = {"v2": "0x" + keccak(text="Swap(address,uint256,uint256,uint256,uint256,address)").hex(),
         "v3": "0x" + keccak(text="Swap(address,address,int256,int256,uint160,uint128,int24)").hex()}
POOL_ABI = json.loads('[{"name":"factory","type":"function","stateMutability":"view","inputs":[],"outputs":[{"type":"address"}]},'
                      '{"name":"token0","type":"function","stateMutability":"view","inputs":[],"outputs":[{"type":"address"}]},'
                      '{"name":"token1","type":"function","stateMutability":"view","inputs":[],"outputs":[{"type":"address"}]},'
                      '{"name":"fee","type":"function","stateMutability":"view","inputs":[],"outputs":[{"type":"uint24"}]}]')
ERC_ABI = json.loads('[{"name":"decimals","type":"function","stateMutability":"view","inputs":[],"outputs":[{"type":"uint8"}]},'
                     '{"name":"symbol","type":"function","stateMutability":"view","inputs":[],"outputs":[{"type":"string"}]},'
                     '{"name":"balanceOf","type":"function","stateMutability":"view","inputs":[{"type":"address"}],"outputs":[{"type":"uint256"}]}]')


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", default="https://base.drpc.org", help="needs unfiltered eth_getLogs")
    ap.add_argument("--blocks", type=int, default=3000)
    ap.add_argument("--chunk", type=int, default=100, help="log range per request (free RPC limits)")
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--kinds", default="v2,v3")
    ap.add_argument("--min-weth", type=float, default=5.0, help="min WETH held by the pool")
    ap.add_argument("--out", type=Path, default=Path("pools.json"))
    a = ap.parse_args()
    kinds = [k.strip() for k in a.kinds.split(",")]
    w3 = AsyncWeb3(AsyncHTTPProvider(a.rpc))
    head = await w3.eth.block_number
    counts: Counter[tuple[str, str]] = Counter()
    for lo in range(head - a.blocks, head + 1, a.chunk):
        logs = await w3.eth.get_logs({"topics": [[TOPIC[k] for k in kinds]], "fromBlock": lo,
                                      "toBlock": min(lo + a.chunk - 1, head)})
        for lg in logs:
            t0 = "0x" + bytes(lg["topics"][0]).hex()
            kind = "v2" if t0 == TOPIC["v2"] else "v3"
            counts[(lg["address"].lower(), kind)] += 1
    print(f"{sum(counts.values())} swaps across {len(counts)} contracts in {a.blocks} blocks "
          f"(~{a.blocks * 0.2 / 60:.0f} min)")
    weth = w3.eth.contract(address=to_checksum_address(WETH), abi=ERC_ABI)
    pools = []
    for (addr, kind), n in counts.most_common(400):
        p = w3.eth.contract(address=to_checksum_address(addr), abi=POOL_ABI)
        try:
            if (await p.functions.factory().call()).lower() != FACTORY[kind]:
                continue
            t0, t1 = (await p.functions.token0().call()).lower(), (await p.functions.token1().call()).lower()
            fee = await p.functions.fee().call() if kind == "v3" else 3000
        except Exception:
            continue
        if WETH not in (t0, t1):
            continue
        weth_held = await weth.functions.balanceOf(to_checksum_address(addr)).call() / 1e18
        if weth_held < a.min_weth:
            continue
        token = t1 if t0 == WETH else t0
        erc = w3.eth.contract(address=to_checksum_address(token), abi=ERC_ABI)
        dec = await erc.functions.decimals().call()
        try:
            sym = await erc.functions.symbol().call()
        except Exception:
            sym = "?"
        pools.append({"pool": addr, "token": token, "weth_is_token0": t0 == WETH, "token_decimals": dec,
                      "is_v3": kind == "v3", "fee_ppm": fee, "symbol": sym})
        print(f"  {kind} {sym:<10} fee={fee / 1e4:.2f}%  swaps={n:<5} weth_in_pool={weth_held:,.1f}  {addr}")
        if len(pools) >= a.top:
            break
    a.out.write_text(json.dumps(pools, indent=2))
    print(f"wrote {len(pools)} pools to {a.out}")


if __name__ == "__main__":
    asyncio.run(main())
