"""web3.py v7 adapters (the only module that touches the network). Lazy-imported by app.py so the
core, tests and backtests run without web3 installed.

STATUS: written against the web3.py 7.x API; NOT yet exercised against a live Base node. Run
`python -m baselab paper` against a real WSS endpoint before trusting any of it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eth_abi import decode as abi_decode
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import keccak, to_checksum_address
from web3 import AsyncWeb3, WebSocketProvider
from web3.providers.rpc import AsyncHTTPProvider

from ..config import Config
from ..domain import BlockHead, Side, Swap
from ..ingestion.decode import decode_swap
from ..security.checks import HolderLedger, OwnerInfo, RoundTrip
from .gas import FeeOracle
from .router import ExecResult, Intent

log = logging.getLogger(__name__)

V2_SWAP_TOPIC = "0x" + keccak(text="Swap(address,uint256,uint256,uint256,uint256,address)").hex()
V3_SWAP_TOPIC = "0x" + keccak(text="Swap(address,address,int256,int256,uint160,uint128,int24)").hex()
TRANSFER_TOPIC = "0x" + keccak(text="Transfer(address,address,uint256)").hex()
EIP1967_IMPL_SLOT = int("360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc", 16)
SIM_ADDRESS = "0x00000000000000000000000000000000005ca1ab"

ERC20_ABI = json.loads("""[
 {"name":"approve","type":"function","stateMutability":"nonpayable","inputs":[{"name":"s","type":"address"},{"name":"a","type":"uint256"}],"outputs":[{"type":"bool"}]},
 {"name":"allowance","type":"function","stateMutability":"view","inputs":[{"name":"o","type":"address"},{"name":"s","type":"address"}],"outputs":[{"type":"uint256"}]},
 {"name":"balanceOf","type":"function","stateMutability":"view","inputs":[{"name":"a","type":"address"}],"outputs":[{"type":"uint256"}]},
 {"name":"totalSupply","type":"function","stateMutability":"view","inputs":[],"outputs":[{"type":"uint256"}]},
 {"name":"owner","type":"function","stateMutability":"view","inputs":[],"outputs":[{"type":"address"}]},
 {"name":"getMinDelay","type":"function","stateMutability":"view","inputs":[],"outputs":[{"type":"uint256"}]}
]""")
V2_ROUTER_ABI = json.loads("""[
 {"name":"swapExactTokensForTokensSupportingFeeOnTransferTokens","type":"function","stateMutability":"nonpayable",
  "inputs":[{"name":"amountIn","type":"uint256"},{"name":"amountOutMin","type":"uint256"},{"name":"path","type":"address[]"},{"name":"to","type":"address"},{"name":"deadline","type":"uint256"}],"outputs":[]}
]""")
SIM_ABI = json.loads("""[
 {"name":"probe","type":"function","stateMutability":"nonpayable",
  "inputs":[{"name":"router","type":"address"},{"name":"weth","type":"address"},{"name":"token","type":"address"},{"name":"amountIn","type":"uint256"}],
  "outputs":[{"name":"r","type":"tuple","components":[{"name":"buyOk","type":"bool"},{"name":"transferOk","type":"bool"},{"name":"sellOk","type":"bool"},{"name":"expectedBuy","type":"uint256"},{"name":"receivedBuy","type":"uint256"},{"name":"expectedSell","type":"uint256"},{"name":"receivedSell","type":"uint256"}]}]}
]""")


@dataclass(frozen=True, slots=True)
class PoolMeta:
    pool: str
    token: str
    weth_is_token0: bool
    token_decimals: int
    is_v3: bool = False
    fee_ppm: int = 3000  # LP fee in millionths (V2 = 3000 = 0.3%; V3 = pool.fee())


def _qty(v: Any) -> int:
    """OP-stack receipt extras (l1Fee) arrive unformatted as hex strings; tolerate int/None too."""
    if v is None:
        return 0
    return int(v, 16) if isinstance(v, str) else int(v)


def _head(b: Any) -> BlockHead:
    return BlockHead(int(b["number"]), "0x" + bytes(b["hash"]).hex(), "0x" + bytes(b["parentHash"]).hex(),
                     int(b["timestamp"]), int(b.get("baseFeePerGas", 0) or 0), int(b["gasUsed"]), int(b["gasLimit"]))


class Web3ChainReader:
    def __init__(self, cfg: Config, pools: dict[str, PoolMeta]) -> None:
        self._cfg = cfg
        self._pools = {k.lower(): v for k, v in pools.items()}
        self.w3: AsyncWeb3 | None = None

    async def connect(self) -> AsyncWeb3:
        self.w3 = await AsyncWeb3(WebSocketProvider(self._cfg.chain.wss_url))
        return self.w3

    async def heads(self) -> AsyncIterator[BlockHead]:
        w3 = self.w3 or await self.connect()
        await w3.eth.subscribe("newHeads")
        async for msg in w3.socket.process_subscriptions():
            yield _head(msg["result"])

    async def block_by_hash(self, block_hash: str) -> BlockHead:
        assert self.w3 is not None
        return _head(await self.w3.eth.get_block(block_hash))

    async def ping(self) -> None:
        assert self.w3 is not None
        await self.w3.eth.chain_id  # cheap round trip over the same socket

    async def swaps_in_block(self, head: BlockHead, pools: set[str]) -> list[Swap]:
        assert self.w3 is not None
        if not pools:
            return []
        logs = await self._logs_for_block(head, [to_checksum_address(p) for p in pools])
        out: list[Swap] = []
        for lg in logs:
            meta = self._pools.get(lg["address"].lower())
            if meta is None:
                continue
            s = self._decode_swap(meta, lg, head)
            if s is not None:
                out.append(s)
        return out

    async def _get_logs_retry(self, params: dict[str, Any], tries: int = 6) -> list[Any]:
        """Retry transient free-RPC errors (rate limit, block not yet known by this backend)."""
        assert self.w3 is not None
        delay = 0.1
        for i in range(tries):
            try:
                return await self.w3.eth.get_logs(params)  # type: ignore[arg-type]
            except Exception as exc:
                msg = str(exc).lower()
                transient = any(k in msg for k in ("rate limit", "not found", "beyond", "429", "too many"))
                if not transient or i == tries - 1:
                    raise
                await asyncio.sleep(delay)
                delay *= 2
        raise RuntimeError("unreachable")

    async def swaps_in_blocks(self, heads: list[BlockHead], pools: set[str]) -> list[Swap]:
        """One by-number log query for a batch of consecutive heads; every log must carry the hash we
        hold as canonical for its height, otherwise the backends disagree (fork) and we refuse to mix."""
        if not pools or not heads:
            return []
        by_num = {h.number: h for h in heads}
        logs = await self._get_logs_retry({"fromBlock": heads[0].number, "toBlock": heads[-1].number,
                                           "address": [to_checksum_address(p) for p in pools],
                                           "topics": [[V2_SWAP_TOPIC, V3_SWAP_TOPIC]]})
        out: list[Swap] = []
        for lg in logs:
            h = by_num.get(int(lg["blockNumber"]))
            if h is None:
                continue
            if "0x" + bytes(lg["blockHash"]).hex() != h.hash:
                raise RuntimeError(f"block {h.number}: log hash != canonical head hash (fork)")
            meta = self._pools.get(lg["address"].lower())
            s = self._decode_swap(meta, lg, h) if meta is not None else None
            if s is not None:
                out.append(s)
        return out

    async def _logs_for_block(self, head: BlockHead, addrs: list[str], tries: int = 6) -> list[Any]:
        """Load-balanced RPCs often announce a head from one backend and answer get_logs from another
        that has not seen the block yet ("block not found"). Retry briefly; then query by number and keep
        only logs whose blockHash matches the head (a different hash = fork mismatch = raise, never mix)."""
        assert self.w3 is not None
        topics = [[V2_SWAP_TOPIC, V3_SWAP_TOPIC]]
        delay = 0.05
        for _ in range(tries):
            try:
                return await self.w3.eth.get_logs({"blockHash": head.hash, "address": addrs, "topics": topics})
            except Exception as exc:
                if "not found" not in str(exc).lower():
                    raise
                await asyncio.sleep(delay)
                delay *= 2
        logs = await self.w3.eth.get_logs({"fromBlock": head.number, "toBlock": head.number,
                                           "address": addrs, "topics": topics})
        if any("0x" + bytes(lg["blockHash"]).hex() != head.hash for lg in logs):
            raise RuntimeError(f"block {head.number}: RPC backends disagree on hash (fork)")
        return logs

    async def swaps_in_range(self, from_block: int, anchor: BlockHead, chunk: int = 2_000) -> list[Swap]:
        """Historical swaps for backfill. OP-stack blocks are exactly block_time apart, so a block's
        timestamp is derived from the anchor instead of fetching every header (block_time must be the
        MEASURED value: Base went from 2 s to 0.2 s blocks)."""
        assert self.w3 is not None
        addrs = [to_checksum_address(p) for p in self._pools]
        bt = self._cfg.chain.block_time_s
        out: list[Swap] = []
        for lo in range(from_block, anchor.number + 1, chunk):
            hi = min(lo + chunk - 1, anchor.number)
            logs = await self._get_logs_retry({"address": addrs, "topics": [[V2_SWAP_TOPIC, V3_SWAP_TOPIC]],
                                               "fromBlock": lo, "toBlock": hi})
            for lg in logs:
                meta = self._pools.get(lg["address"].lower())
                if meta is None:
                    continue
                n = int(lg["blockNumber"])
                ts = int(anchor.timestamp - (anchor.number - n) * bt)
                pseudo = BlockHead(n, "0x" + bytes(lg["blockHash"]).hex(), "", ts)
                s = self._decode_swap(meta, lg, pseudo)
                if s is not None:
                    out.append(s)
        return out

    async def latest_head(self) -> BlockHead:
        assert self.w3 is not None
        return _head(await self.w3.eth.get_block("latest"))

    async def measure_block_time(self, span: int = 5_000) -> float:
        assert self.w3 is not None
        hi = await self.w3.eth.get_block("latest")
        lo = await self.w3.eth.get_block(int(hi["number"]) - span)
        return (int(hi["timestamp"]) - int(lo["timestamp"])) / span

    async def reconnect(self) -> None:
        """A dead socket cannot be re-subscribed: drop it and open a new one."""
        if self.w3 is not None:
            try:
                await self.w3.provider.disconnect()
            except Exception:
                pass
        self.w3 = None
        await self.connect()

    @staticmethod
    def _decode_swap(meta: PoolMeta, lg: Any, head: BlockHead) -> Swap | None:
        return decode_swap(meta.pool, meta.weth_is_token0, meta.token_decimals, meta.is_v3,
                           bytes(lg["data"]), int(lg["logIndex"]), head)


class Web3SecurityReader:
    def __init__(self, cfg: Config, w3: AsyncWeb3, simulator_bin: Path) -> None:
        self._cfg = cfg
        self._w3 = w3
        self._sim_code = simulator_bin.read_text().strip() if simulator_bin.exists() else None

    def _erc20(self, addr: str) -> Any:
        return self._w3.eth.contract(address=to_checksum_address(addr), abi=ERC20_ABI)

    async def simulate_round_trip(self, token: str, amount_in_wei: int, block: int) -> RoundTrip:
        if not self._sim_code:
            raise RuntimeError("simulator.bin missing: compile HoneypotSimulator.sol (fail closed)")
        sim = self._w3.eth.contract(address=to_checksum_address(SIM_ADDRESS), abi=SIM_ABI)
        c = self._cfg.chain
        data = sim.encode_abi("probe", [to_checksum_address(c.v2_router), to_checksum_address(c.weth),
                                        to_checksum_address(token), amount_in_wei])
        raw = await self._w3.eth.call(
            {"to": to_checksum_address(SIM_ADDRESS), "data": data, "gas": 8_000_000},
            block_identifier=block,
            state_override={to_checksum_address(SIM_ADDRESS): {"code": self._sim_code,
                                                               "balance": hex(amount_in_wei * 2)}})
        (r,) = abi_decode(["(bool,bool,bool,uint256,uint256,uint256,uint256)"], bytes(raw))
        return RoundTrip(*r)

    async def lp_locked(self, pair: str, holders: tuple[str, ...], block: int) -> tuple[int, int]:
        lp = self._erc20(pair)
        bals = await asyncio.gather(*(lp.functions.balanceOf(to_checksum_address(h)).call(block_identifier=block)
                                      for h in holders))
        return sum(bals), await lp.functions.totalSupply().call(block_identifier=block)

    async def owner_info(self, token: str, block: int) -> OwnerInfo:
        t = self._erc20(token)
        try:
            owner: str | None = await t.functions.owner().call(block_identifier=block)
        except Exception:
            owner = None
        delay: int | None = None
        if owner and int(owner, 16) != 0:
            try:
                delay = await self._erc20(owner).functions.getMinDelay().call(block_identifier=block)
            except Exception:
                delay = None
        slot = await self._w3.eth.get_storage_at(to_checksum_address(token), EIP1967_IMPL_SLOT, block)
        return OwnerInfo(owner, delay, int.from_bytes(bytes(slot), "big"))

    async def holder_ledger(self, token: str, block: int, chunk: int = 5_000) -> HolderLedger:
        """Replay Transfer logs BACKWARDS from `block` until the ledger reconciles with totalSupply.

        Needs only eth_getLogs (no archive state). Balance arithmetic is order-independent, and
        sum(balances) == mints - burns seen; equality with totalSupply proves the mint history is
        covered. Not reconciled within max_holder_scan_blocks => unverifiable => fail closed.
        """
        ledger = HolderLedger()
        addr = to_checksum_address(token)
        supply = await self._erc20(token).functions.totalSupply().call(block_identifier=block)
        floor = max(0, block - self._cfg.security.max_holder_scan_blocks)
        hi = block
        while hi >= floor:
            lo = max(floor, hi - chunk + 1)
            logs = await self._w3.eth.get_logs({"address": addr, "topics": [TRANSFER_TOPIC],
                                                "fromBlock": lo, "toBlock": hi})
            for lg in logs:
                if len(lg["topics"]) < 3:
                    continue
                src = "0x" + bytes(lg["topics"][1])[-20:].hex()
                dst = "0x" + bytes(lg["topics"][2])[-20:].hex()
                ledger.apply(src, dst, int.from_bytes(bytes(lg["data"])[:32], "big"), int(lg["blockNumber"]))
            if supply > 0 and ledger.total() == supply and ledger.min_balance() >= 0:
                return ledger
            hi = lo - 1
        raise RuntimeError(f"holder_history_unreconciled_within_{self._cfg.security.max_holder_scan_blocks}_blocks")


class LiveSender:
    """approve(exact) -> swap(minOut) -> approve(0), simulated with eth_simulateV1 before broadcast.

    L1 with bundles: one all-or-nothing Flashbots bundle. Base: three consecutive nonces to the private
    RPC back-to-back (see ARCHITECTURE.md §9 for why same-block atomicity is not guaranteed there).
    """

    def __init__(self, cfg: Config, w3: AsyncWeb3, private_key: str, pools: dict[str, PoolMeta],
                 fee_oracle: FeeOracle, flashbots_auth_key: str | None = None) -> None:
        self._cfg = cfg
        self._w3 = w3
        self._acct = Account.from_key(private_key)
        self._pools = {k.lower(): v for k, v in pools.items()}
        self._fees = fee_oracle
        self._fb_key = flashbots_auth_key
        self._priv = AsyncWeb3(AsyncHTTPProvider(cfg.chain.private_rpc_url)) if cfg.chain.private_rpc_url else w3
        self._pending: dict[int, dict[str, Any]] = {}
        self._touched: set[str] = set()

    @property
    def address(self) -> str:
        return self._acct.address

    async def _fee_fields(self, urgency: float = 1.0) -> dict[str, int]:
        latest = await self._w3.eth.get_block("latest")
        hist = await self._w3.eth.fee_history(10, "latest", [60])
        tips = [int(r[0]) for r in hist["reward"] if r]
        q = self._fees.quote(_head(latest), tips, urgency)
        return {"maxFeePerGas": q.max_fee_per_gas, "maxPriorityFeePerGas": q.max_priority_fee_per_gas}

    def _wei(self, token: str, amount: float, decimals: int) -> int:
        return int(amount * 10**decimals)

    async def execute(self, intent: Intent) -> ExecResult:
        c = self._cfg.chain
        meta = self._pools[intent.pool.lower()]
        weth, token = to_checksum_address(c.weth), to_checksum_address(meta.token)
        router = to_checksum_address(c.v2_router)
        if intent.side is Side.BUY:
            t_in, t_out, d_in, d_out = weth, token, 18, meta.token_decimals
        else:
            t_in, t_out, d_in, d_out = token, weth, meta.token_decimals, 18
        amt_in = self._wei(t_in, intent.amount_in, d_in)
        min_out = self._wei(t_out, intent.min_out, d_out)
        erc_in = self._w3.eth.contract(address=t_in, abi=ERC20_ABI)
        erc_out = self._w3.eth.contract(address=t_out, abi=ERC20_ABI)
        rtr = self._w3.eth.contract(address=router, abi=V2_ROUTER_ABI)
        me = self._acct.address
        deadline = int(time.time()) + 30
        calls = [
            {"to": t_in, "data": erc_in.encode_abi("approve", [router, amt_in])},
            {"to": router, "data": rtr.encode_abi("swapExactTokensForTokensSupportingFeeOnTransferTokens",
                                                  [amt_in, min_out, [t_in, t_out], me, deadline])},
            {"to": t_in, "data": erc_in.encode_abi("approve", [router, 0])},
        ]
        self._touched.add(t_in)
        await self._simulate(calls)
        nonce = await self._w3.eth.get_transaction_count(me, "pending")
        fees = await self._fee_fields()
        signed = []
        for i, call in enumerate(calls):
            tx = {"from": me, "chainId": c.chain_id, "nonce": nonce + i, "type": 2, "value": 0,
                  "gas": 350_000 if i == 1 else 80_000, **call, **fees}
            self._pending[nonce + i] = tx
            signed.append(self._acct.sign_transaction(tx))
        before = await erc_out.functions.balanceOf(me).call()
        intent.sent = True
        if c.supports_bundles and self._fb_key:
            await self._send_bundle([s.raw_transaction for s in signed])
        else:
            for s in signed:
                await self._priv.eth.send_raw_transaction(s.raw_transaction)
        receipts = [await self._w3.eth.wait_for_transaction_receipt(s.hash, timeout=30) for s in signed]
        for i in range(3):
            self._pending.pop(nonce + i, None)
        if receipts[1]["status"] != 1:
            raise RuntimeError("swap reverted on-chain")
        await self._confirm(receipts[1])
        after = await erc_out.functions.balanceOf(me).call(block_identifier=receipts[1]["blockNumber"])
        gas_wei = sum(int(r["gasUsed"]) * int(r["effectiveGasPrice"]) + _qty(r.get("l1Fee")) for r in receipts)
        return ExecResult((after - before) / 10**d_out, gas_wei / 1e18, int(receipts[1]["blockNumber"]),
                          "0x" + bytes(receipts[1]["transactionHash"]).hex())

    async def _simulate(self, calls: list[dict[str, Any]]) -> None:
        me = self._acct.address
        payload = [{"blockStateCalls": [{"calls": [{"from": me, **c} for c in calls]}], "validation": False}, "latest"]
        resp = await self._w3.provider.make_request("eth_simulateV1", payload)  # type: ignore[arg-type]
        if "error" in resp:
            raise RuntimeError(f"simulation unavailable/failed: {resp['error']}")  # no simulation => no send
        for i, call in enumerate(resp["result"][0]["calls"]):
            if int(call["status"], 16) != 1:
                raise RuntimeError(f"simulated call {i} reverted")

    async def _confirm(self, receipt: Any) -> None:
        """Book only once the receipt's block is still canonical at confirm_depth (anti ghost-block)."""
        target = int(receipt["blockNumber"]) + self._cfg.confirm_depth
        while await self._w3.eth.block_number < target:
            await asyncio.sleep(self._cfg.chain.block_time_s / 2)
        again = await self._w3.eth.get_transaction_receipt(receipt["transactionHash"])
        if again is None or bytes(again["blockHash"]) != bytes(receipt["blockHash"]):
            raise RuntimeError("receipt reorged out")

    async def _send_bundle(self, raw_txs: list[bytes]) -> None:
        import aiohttp
        assert self._fb_key is not None
        target = await self._w3.eth.block_number + 1
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_sendBundle",
                           "params": [{"txs": ["0x" + r.hex() for r in raw_txs], "blockNumber": hex(target)}]})
        auth = Account.from_key(self._fb_key)
        sig = Account.sign_message(encode_defunct(text="0x" + keccak(text=body).hex()), auth.key).signature
        headers = {"Content-Type": "application/json", "X-Flashbots-Signature": f"{auth.address}:0x{sig.hex()}"}
        async with aiohttp.ClientSession() as s, s.post(self._cfg.chain.private_rpc_url, data=body,
                                                        headers=headers) as r:
            resp = await r.json()
            if "error" in resp:
                raise RuntimeError(f"bundle rejected: {resp['error']}")

    async def cancel_pending(self) -> None:
        if not self._pending:
            return
        fees = await self._fee_fields(urgency=1.5)
        for nonce, tx in list(self._pending.items()):
            bump = {k: max(int(tx[k] * 1.125) + 1, fees[k]) for k in ("maxFeePerGas", "maxPriorityFeePerGas")}
            repl = {"from": self.address, "to": self.address, "value": 0, "nonce": nonce, "gas": 21_000,
                    "chainId": self._cfg.chain.chain_id, "type": 2, **bump}
            try:
                await self._priv.eth.send_raw_transaction(self._acct.sign_transaction(repl).raw_transaction)
            except Exception:
                log.exception("replacement for nonce %d failed", nonce)
        self._pending.clear()

    async def revoke_all(self) -> int:
        router = to_checksum_address(self._cfg.chain.v2_router)
        n = 0
        for tok in self._touched:
            erc = self._w3.eth.contract(address=tok, abi=ERC20_ABI)
            if await erc.functions.allowance(self.address, router).call() == 0:
                continue
            nonce = await self._w3.eth.get_transaction_count(self.address, "pending")
            tx = {"from": self.address, "to": tok, "data": erc.encode_abi("approve", [router, 0]), "nonce": nonce,
                  "gas": 80_000, "chainId": self._cfg.chain.chain_id, "type": 2, "value": 0,
                  **(await self._fee_fields(urgency=1.5))}
            await self._priv.eth.send_raw_transaction(self._acct.sign_transaction(tx).raw_transaction)
            n += 1
        return n
