"""Read-only: for each candidate token, find its WETH pools on Uniswap (V2/V3) and Aerodrome (V2 volatile / Slipstream CL).

    python scripts/find_arb_pairs.py --tokens-from <pools.json> --out arb-pools.json

Keeps tokens that have at least one qualifying pool on BOTH dexes (>= min-weth WETH in the pool), and the top
`per_dex` pools per dex. Every factory is verified on-chain (pool.factory() must equal the factory queried).
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from eth_utils import to_checksum_address as cs
from web3 import AsyncWeb3
from web3.providers.rpc import AsyncHTTPProvider

WETH = "0x4200000000000000000000000000000000000006"
UNI_V2 = "0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6"
UNI_V3 = "0x33128a8fC17869897dcE68Ed026d694621f6FDfD"
AERO_V2 = "0x420DD381b31aEf6683db6B902084cB0FFECe40Da"
AERO_CL = "0x5e7BB104d84c7CB9B682AaC2F3d509f5F406809A"
EXTRA = ["0x940181a94A35A4569E4529A3CDfB74e38FD98631",   # AERO
         "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf"]   # cbBTC

ABI = json.loads("""[
 {"name":"getPair","type":"function","stateMutability":"view","inputs":[{"type":"address"},{"type":"address"}],"outputs":[{"type":"address"}]},
 {"name":"getPool","type":"function","stateMutability":"view","inputs":[{"type":"address"},{"type":"address"},{"type":"uint24"}],"outputs":[{"type":"address"}]},
 {"name":"getFee","type":"function","stateMutability":"view","inputs":[{"type":"address"},{"type":"bool"}],"outputs":[{"type":"uint256"}]},
 {"name":"factory","type":"function","stateMutability":"view","inputs":[],"outputs":[{"type":"address"}]},
 {"name":"token0","type":"function","stateMutability":"view","inputs":[],"outputs":[{"type":"address"}]},
 {"name":"fee","type":"function","stateMutability":"view","inputs":[],"outputs":[{"type":"uint24"}]},
 {"name":"balanceOf","type":"function","stateMutability":"view","inputs":[{"type":"address"}],"outputs":[{"type":"uint256"}]},
 {"name":"decimals","type":"function","stateMutability":"view","inputs":[],"outputs":[{"type":"uint8"}]},
 {"name":"symbol","type":"function","stateMutability":"view","inputs":[],"outputs":[{"type":"string"}]}
]""")
ABI_BOOL = json.loads('[{"name":"getPool","type":"function","stateMutability":"view","inputs":[{"type":"address"},{"type":"address"},{"type":"bool"}],"outputs":[{"type":"address"}]},'
                      '{"name":"getFee","type":"function","stateMutability":"view","inputs":[{"type":"address"},{"type":"bool"}],"outputs":[{"type":"uint256"}]}]')
ABI_INT24 = json.loads('[{"name":"getPool","type":"function","stateMutability":"view","inputs":[{"type":"address"},{"type":"address"},{"type":"int24"}],"outputs":[{"type":"address"}]}]')


async def retry(coro_fn, tries=6):
    for i in range(tries):
        try:
            return await coro_fn()
        except Exception as exc:
            if i == tries - 1 or not any(k in str(exc).lower() for k in ("429", "rate", "limit", "timeout")):
                raise
            await asyncio.sleep(1.5 * (i + 1))


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", default="https://base.drpc.org")
    ap.add_argument("--tokens-from", type=Path, required=True)
    ap.add_argument("--min-weth", type=float, default=5.0)
    ap.add_argument("--per-dex", type=int, default=2)
    ap.add_argument("--out", type=Path, default=Path("arb-pools.json"))
    a = ap.parse_args()
    w3 = AsyncWeb3(AsyncHTTPProvider(a.rpc))
    tokens = {p["token"].lower() for p in json.loads(a.tokens_from.read_text())} | {t.lower() for t in EXTRA}
    tokens.discard(WETH.lower())
    weth = w3.eth.contract(address=cs(WETH), abi=ABI)
    uv2 = w3.eth.contract(address=cs(UNI_V2), abi=ABI)
    uv3 = w3.eth.contract(address=cs(UNI_V3), abi=ABI)
    av2 = w3.eth.contract(address=cs(AERO_V2), abi=ABI_BOOL)
    acl = w3.eth.contract(address=cs(AERO_CL), abi=ABI_INT24)
    out, report = [], []
    for tok in sorted(tokens):
        cand = []  # (dex, kind, pool, fee_ppm)
        t = cs(tok)
        p = await retry(lambda: uv2.functions.getPair(cs(WETH), t).call())
        if int(p, 16):
            cand.append(("uniswap", "v2", p, 3000, UNI_V2))
        for fee in (100, 500, 3000, 10000):
            p = await retry(lambda: uv3.functions.getPool(cs(WETH), t, fee).call())
            if int(p, 16):
                cand.append(("uniswap", "v3", p, fee, UNI_V3))
        p = await retry(lambda: av2.functions.getPool(cs(WETH), t, False).call())
        if int(p, 16):
            fee_bps = await retry(lambda: av2.functions.getFee(p, False).call())
            cand.append(("aerodrome", "v2", p, int(fee_bps) * 100, AERO_V2))
        for ts in (1, 10, 50, 100, 200, 2000):
            p = await retry(lambda: acl.functions.getPool(cs(WETH), t, ts).call())
            if int(p, 16):
                pool = w3.eth.contract(address=p, abi=ABI)
                fee = await retry(lambda: pool.functions.fee().call())
                cand.append(("aerodrome", "cl", p, int(fee), AERO_CL))
        rows = []
        for dex, kind, pool, fee_ppm, factory in cand:
            c = w3.eth.contract(address=pool, abi=ABI)
            if (await retry(lambda: c.functions.factory().call())).lower() != factory.lower():
                continue  # not the real factory's pool
            held = await retry(lambda: weth.functions.balanceOf(pool).call()) / 1e18
            if held >= a.min_weth:
                rows.append((held, dex, kind, pool, fee_ppm))
        by_dex = {"uniswap": [r for r in rows if r[1] == "uniswap"], "aerodrome": [r for r in rows if r[1] == "aerodrome"]}
        if not by_dex["uniswap"] or not by_dex["aerodrome"]:
            continue
        erc = w3.eth.contract(address=t, abi=ABI)
        sym = await retry(lambda: erc.functions.symbol().call())
        dec = await retry(lambda: erc.functions.decimals().call())
        for dex, lst in by_dex.items():
            for held, d, kind, pool, fee_ppm in sorted(lst, reverse=True)[:a.per_dex]:
                pc = w3.eth.contract(address=pool, abi=ABI)
                t0 = (await retry(lambda: pc.functions.token0().call())).lower()
                out.append({"pool": pool.lower(), "token": tok, "weth_is_token0": t0 == WETH.lower(),
                            "token_decimals": dec, "is_v3": kind in ("v3", "cl"), "fee_ppm": fee_ppm, "symbol": sym,
                            "dex": d, "kind": kind, "weth_in_pool": round(held, 2)})
        report.append(sym)
        print(f"{sym:<10} " + " | ".join(f"{r[1][:4]}-{r[2]} fee={r[4] / 1e4:.2f}% weth={r[0]:,.0f}" for r in sorted(rows, reverse=True)), flush=True)
    a.out.write_text(json.dumps(out, indent=2))
    print(f"\n{len(report)} tokens with pools on both dexes -> {len(out)} pools -> {a.out}")


if __name__ == "__main__":
    asyncio.run(main())
