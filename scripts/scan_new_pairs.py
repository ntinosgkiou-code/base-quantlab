"""Read-only: run the REAL Layer-1 validator against the newest Uniswap V2 WETH pairs on Base mainnet.

No key, no transactions. Uses eth_call with state override only.
    python scripts/scan_new_pairs.py [--rpc URL] [--lookback 20000] [--limit 15]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eth_utils import keccak, to_checksum_address  # noqa: E402
from web3 import AsyncWeb3  # noqa: E402
from web3.providers.rpc import AsyncHTTPProvider  # noqa: E402

from baselab.config import Config, Mode  # noqa: E402
from baselab.execution.chain import Web3SecurityReader  # noqa: E402
from baselab.security.validator import SecurityValidator, Target  # noqa: E402

V2_FACTORY = "0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6"
PAIR_CREATED = "0x" + keccak(text="PairCreated(address,address,address,uint256)").hex()
ROUTER_ABI = json.loads('[{"name":"factory","type":"function","stateMutability":"view","inputs":[],"outputs":[{"type":"address"}]},'
                        '{"name":"WETH","type":"function","stateMutability":"view","inputs":[],"outputs":[{"type":"address"}]}]')


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", default="https://mainnet.base.org")
    ap.add_argument("--lookback", type=int, default=20_000)
    ap.add_argument("--limit", type=int, default=15)
    a = ap.parse_args()
    cfg = Config(mode=Mode.PAPER)
    w3 = AsyncWeb3(AsyncHTTPProvider(a.rpc))
    router = w3.eth.contract(address=to_checksum_address(cfg.chain.v2_router), abi=ROUTER_ABI)
    factory, weth = await router.functions.factory().call(), await router.functions.WETH().call()
    print(f"router.factory={factory} (expected {V2_FACTORY})  router.WETH={weth}")
    assert factory.lower() == V2_FACTORY.lower() and weth.lower() == cfg.chain.weth.lower(), "router config wrong"

    head = await w3.eth.block_number
    logs = []
    for start in range(head - a.lookback, head, 2_000):
        logs += await w3.eth.get_logs({"address": V2_FACTORY, "topics": [PAIR_CREATED],
                                       "fromBlock": start, "toBlock": min(start + 1_999, head)})
    pairs = []
    for lg in logs:
        t0 = "0x" + bytes(lg["topics"][1])[-20:].hex()
        t1 = "0x" + bytes(lg["topics"][2])[-20:].hex()
        pair = "0x" + bytes(lg["data"])[12:32].hex()
        if weth.lower() in (t0, t1):
            pairs.append((t1 if t0 == weth.lower() else t0, pair, int(lg["blockNumber"])))
    print(f"{len(logs)} pairs created in last {a.lookback} blocks, {len(pairs)} vs WETH; scanning newest {a.limit}\n")

    validator = SecurityValidator(Web3SecurityReader(cfg, w3, Path(__file__).resolve().parents[1]
                                                     / "baselab" / "security" / "simulator.bin"),
                                  replace(cfg.security, timeout_s=120.0), probe_amount_wei=10**15)
    passed = 0
    for token, pair, born in reversed(pairs[-a.limit:]):
        rep = await validator.check(Target(token, pair), head)
        passed += rep.passed
        tax = (f"buy_tax={rep.buy_tax:.1%} sell_tax={rep.sell_tax:.1%}"
               if rep.buy_tax is not None else "")
        print(f"{'PASS' if rep.passed else 'REJECT'}  {token}  age={head - born} blk  {tax}\n"
              f"        {', '.join(rep.reasons) or '-'}")
    print(f"\n{passed}/{min(a.limit, len(pairs))} passed")


if __name__ == "__main__":
    asyncio.run(main())
