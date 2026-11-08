"""End-to-end FAKE-MONEY test on a local anvil fork of Base mainnet (real Uniswap V2 contracts).

    anvil --fork-url https://mainnet.base.org --chain-id 8453 --block-time 1
    python scripts/fork_test.py

1. deploys 3 fixture tokens (clean / 10% sell tax / honeypot), lists them on the real V2 router,
   burns LP to 0xdead, airdrops to 100 holders, renounces ownership
2. runs the REAL SecurityValidator (eth_call state-override simulator) -> expects PASS / REJECT / REJECT
3. runs the REAL LiveSender: buy then sell on the clean token -> checks exact approve -> swap -> revoke,
   allowance == 0 afterwards, receipt confirmation depth, gas accounting, round-trip P&L
4. checks revoke_all() on a dangling allowance

Uses anvil's public dev key #0 (localhost only, never a real key).
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eth_account import Account  # noqa: E402
from web3 import AsyncWeb3  # noqa: E402
from web3.providers.rpc import AsyncHTTPProvider  # noqa: E402

from baselab.config import Config, Mode  # noqa: E402
from baselab.domain import Side  # noqa: E402
from baselab.execution.chain import ERC20_ABI, LiveSender, PoolMeta, Web3SecurityReader  # noqa: E402
from baselab.execution.gas import FeeOracle  # noqa: E402
from baselab.execution.router import Intent  # noqa: E402
from baselab.security.validator import SecurityValidator, Target  # noqa: E402

ANVIL = "http://127.0.0.1:8545"
# PUBLIC, well-known default dev key #0 of anvil/hardhat (holds fake ETH on a LOCAL fork only). Never put real funds on it.
DEV_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
DEAD = "0x000000000000000000000000000000000000dEaD"
ROUTER_ABI = json.loads("""[
 {"name":"factory","type":"function","stateMutability":"view","inputs":[],"outputs":[{"type":"address"}]},
 {"name":"addLiquidityETH","type":"function","stateMutability":"payable","inputs":[{"name":"token","type":"address"},{"name":"amountTokenDesired","type":"uint256"},{"name":"amountTokenMin","type":"uint256"},{"name":"amountETHMin","type":"uint256"},{"name":"to","type":"address"},{"name":"deadline","type":"uint256"}],"outputs":[{"type":"uint256"},{"type":"uint256"},{"type":"uint256"}]}
]""")
FACTORY_ABI = json.loads("""[
 {"name":"createPair","type":"function","stateMutability":"nonpayable","inputs":[{"type":"address"},{"type":"address"}],"outputs":[{"type":"address"}]},
 {"name":"getPair","type":"function","stateMutability":"view","inputs":[{"type":"address"},{"type":"address"}],"outputs":[{"type":"address"}]}
]""")
WETH_ABI = json.loads('[{"name":"deposit","type":"function","stateMutability":"payable","inputs":[],"outputs":[]}]')

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


def build_fixture() -> dict:
    out = ROOT / "scripts" / "fork" / "out"
    subprocess.run(["forge", "build", "--contracts", str(ROOT / "scripts" / "fork" / "TestToken.sol"),
                    "--out", str(out), "--cache-path", str(out / "cache"), "--use", "0.8.24"],
                   check=True, capture_output=True)
    return json.loads((out / "TestToken.sol" / "TestToken.json").read_text())


async def send(w3: AsyncWeb3, acct, tx: dict) -> dict:
    tx = {"from": acct.address, "chainId": 8453, "nonce": await w3.eth.get_transaction_count(acct.address),
          "gas": tx.pop("gas", 3_000_000), "maxFeePerGas": 10**10, "maxPriorityFeePerGas": 10**6, **tx}
    h = await w3.eth.send_raw_transaction(acct.sign_transaction(tx).raw_transaction)
    r = await w3.eth.wait_for_transaction_receipt(h, timeout=60)
    assert r["status"] == 1, f"tx failed: {tx.get('to')}"
    return r


async def deploy_token(w3, acct, art, cfg, name, buy_bps, sell_bps, hp) -> tuple[str, str]:
    from eth_abi import encode
    data = art["bytecode"]["object"] + encode(["string", "uint256", "uint256", "bool"],
                                              [name, buy_bps, sell_bps, hp]).hex()
    r = await send(w3, acct, {"data": data, "value": 0})
    token = r["contractAddress"]
    t = w3.eth.contract(address=token, abi=art["abi"])
    router = w3.eth.contract(address=w3.to_checksum_address(cfg.chain.v2_router), abi=ROUTER_ABI)
    factory = w3.eth.contract(address=await router.functions.factory().call(), abi=FACTORY_ABI)
    weth = w3.to_checksum_address(cfg.chain.weth)
    await send(w3, acct, {"to": factory.address, "data": factory.encode_abi("createPair", [token, weth])})
    pair = await factory.functions.getPair(token, weth).call()
    await send(w3, acct, {"to": token, "data": t.encode_abi("setPair", [pair])})
    holders = [Account.create().address for _ in range(100)]
    await send(w3, acct, {"to": token, "data": t.encode_abi("airdrop", [holders, 4 * 10**24]), "gas": 6_000_000})
    liq_tokens = 6 * 10**26
    await send(w3, acct, {"to": token, "data": t.encode_abi("approve", [router.address, liq_tokens])})
    await send(w3, acct, {"to": router.address, "value": 5 * 10**18,
                          "data": router.encode_abi("addLiquidityETH",
                                                    [token, liq_tokens, 0, 0, acct.address, int(time.time()) + 600])})
    lp = w3.eth.contract(address=pair, abi=ERC20_ABI)
    bal = await lp.functions.balanceOf(acct.address).call()
    await send(w3, acct, {"to": pair, "data": t.encode_abi("transfer", [DEAD, bal])})  # lock LP forever
    await send(w3, acct, {"to": token, "data": t.encode_abi("renounceOwnership", [])})
    return token, pair


async def main() -> None:
    cfg = Config(mode=Mode.LIVE, confirm_depth=2)
    cfg = replace(cfg, security=replace(cfg.security, timeout_s=120.0, max_holder_scan_blocks=2_000))
    w3 = AsyncWeb3(AsyncHTTPProvider(ANVIL))
    acct = Account.from_key(DEV_KEY)
    check("anvil fork reachable, chain id 8453", await w3.eth.chain_id == 8453)
    art = build_fixture()

    tokens = {}
    for name, b, s, hp in (("CLEAN", 0, 0, False), ("TAX10", 0, 1_000, False), ("HONEY", 0, 0, True)):
        tokens[name] = await deploy_token(w3, acct, art, cfg, name, b, s, hp)
    print("fixtures deployed:", {k: v[0] for k, v in tokens.items()})

    # ---------------------------------------------------------------- Layer 1 on real router
    validator = SecurityValidator(Web3SecurityReader(cfg, w3, ROOT / "baselab" / "security" / "simulator.bin"),
                                  cfg.security)
    head = await w3.eth.block_number
    info = await w3.provider.make_request("anvil_nodeInfo", [])
    fork_block = int(info["result"]["forkConfig"]["forkBlockNumber"])
    # fixtures were minted on the fork: scan only post-fork blocks (pre-fork logs go to the rate-limited upstream)
    validator = SecurityValidator(Web3SecurityReader(replace(cfg, security=replace(
        cfg.security, max_holder_scan_blocks=head - fork_block - 1)), w3,
        ROOT / "baselab" / "security" / "simulator.bin"), replace(cfg.security, timeout_s=300.0))
    t_v = time.perf_counter()
    rep = {k: await validator.check(Target(t, p), head) for k, (t, p) in tokens.items()}
    print(f"   validator: {time.perf_counter() - t_v:.1f}s for 3 tokens")
    for k, r in rep.items():
        print(f"   {k}: passed={r.passed} buy_tax={r.buy_tax} sell_tax={r.sell_tax} "
              f"lp={r.locked_lp_fraction} top10={r.top10_share} reasons={r.reasons}")
    check("validator PASSES clean token", rep["CLEAN"].passed, str(rep["CLEAN"].reasons))
    check("validator measures 10% sell tax", rep["TAX10"].sell_tax is not None
          and abs(rep["TAX10"].sell_tax - 0.10) < 0.01 and not rep["TAX10"].passed, str(rep["TAX10"].reasons))
    check("validator catches honeypot", "honeypot:sell_reverts" in rep["HONEY"].reasons, str(rep["HONEY"].reasons))

    # ---------------------------------------------------------------- live sender with fake ETH
    weth = w3.eth.contract(address=w3.to_checksum_address(cfg.chain.weth), abi=WETH_ABI + ERC20_ABI)
    await send(w3, acct, {"to": weth.address, "value": 10**18, "data": weth.encode_abi("deposit", [])})
    token, pair = tokens["CLEAN"]
    meta = PoolMeta(pair, token, weth_is_token0=int(cfg.chain.weth, 16) < int(token, 16), token_decimals=18)
    sender = LiveSender(cfg, w3, DEV_KEY, {pair: meta}, FeeOracle(cfg.chain.base_fee_change_denominator,
                                                                   cfg.chain.elasticity_multiplier))
    router_addr = w3.to_checksum_address(cfg.chain.v2_router)
    tok = w3.eth.contract(address=token, abi=ERC20_ABI)
    w0 = await weth.functions.balanceOf(acct.address).call()
    eth0 = await w3.eth.get_balance(acct.address)

    buy_in = 0.05
    exp_tokens = buy_in * 0.997 * 6e8 / (5 + buy_in * 0.997)  # V2 formula on 5 WETH / 6e8 tokens
    buy = Intent("fork-buy", pair, token, Side.BUY, buy_in, exp_tokens, exp_tokens * 0.99, 0.01)
    t0 = time.perf_counter()
    rb = await sender.execute(buy)
    check("buy executed (simulateV1 -> approve -> swap -> revoke, confirmed)", rb.actual_out > 0,
          f"got {rb.actual_out:,.0f} tokens vs quote {exp_tokens:,.0f}, gas {rb.gas_cost_quote:.8f} ETH, "
          f"{time.perf_counter() - t0:.1f}s")
    check("buy fill within tolerance of quote", abs(rb.actual_out / exp_tokens - 1) < 0.01)
    check("WETH allowance to router is 0 after buy",
          await weth.functions.allowance(acct.address, router_addr).call() == 0)

    sell = Intent("fork-sell", pair, token, Side.SELL, rb.actual_out, 0.0, 0.0, 0.05)
    rs = await sender.execute(sell)
    check("sell executed", rs.actual_out > 0, f"got {rs.actual_out:.6f} WETH back")
    check("token allowance to router is 0 after sell",
          await tok.functions.allowance(acct.address, router_addr).call() == 0)
    w1 = await weth.functions.balanceOf(acct.address).call()
    eth1 = await w3.eth.get_balance(acct.address)
    pnl_weth = (w1 - w0) / 1e18
    gas_eth = (eth0 - eth1) / 1e18
    check("round trip loses only ~2x0.3% fee + price impact", -0.0035 < pnl_weth < -0.0002,
          f"WETH P&L {pnl_weth:.6f} ({pnl_weth / buy_in:.3%}), gas {gas_eth:.8f} ETH, "
          f"sender-reported gas {rb.gas_cost_quote + rs.gas_cost_quote:.8f}")
    # anvil charges the OP-stack L1 data fee but omits l1Fee from receipts; real Base includes it
    check("sender gas accounting matches chain (within anvil's hidden L1 fee, < 1e-8 ETH)",
          abs((rb.gas_cost_quote + rs.gas_cost_quote) - gas_eth) < 1e-8,
          f"diff {abs((rb.gas_cost_quote + rs.gas_cost_quote) - gas_eth):.2e} ETH")

    # ---------------------------------------------------------------- reverted simulation blocks send
    bad = Intent("fork-bad", pair, token, Side.BUY, buy_in, exp_tokens, exp_tokens * 10, 0.0)  # minOut impossible
    n0 = await w3.eth.get_transaction_count(acct.address)
    try:
        await sender.execute(bad)
        check("impossible minOut is rejected before broadcast", False)
    except RuntimeError as exc:
        n1 = await w3.eth.get_transaction_count(acct.address)
        check("impossible minOut is rejected BEFORE broadcast (no gas spent)", n1 == n0 and not bad.sent, str(exc)[:60])

    # ---------------------------------------------------------------- revoke_all on a dangling approval
    await send(w3, acct, {"to": weth.address, "data": weth.encode_abi("approve", [router_addr, 123])})
    n = await sender.revoke_all()
    await asyncio.sleep(2.5)
    check("revoke_all() zeroes a dangling allowance", n >= 1
          and await weth.functions.allowance(acct.address, router_addr).call() == 0)

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
