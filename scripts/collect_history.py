"""Read-only: download raw Swap (V2+V3) and Sync (V2) logs for a pool list over the last N days.

    python scripts/collect_history.py --pools pools-research.json --days 3 --out data/hist3d

Writes <out>/meta.json and <out>/logs.jsonl.gz (one raw log per line; decoding happens in the replay,
so a decoding bug never forces a re-download). Adaptive chunking + retries for free-RPC limits.
Resumable: re-running continues after the last fully written block.
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import time
from pathlib import Path

from eth_utils import keccak, to_checksum_address
from web3 import AsyncWeb3
from web3.providers.rpc import AsyncHTTPProvider

V2_SWAP = "0x" + keccak(text="Swap(address,uint256,uint256,uint256,uint256,address)").hex()
V3_SWAP = "0x" + keccak(text="Swap(address,address,int256,int256,uint160,uint128,int24)").hex()
V2_SYNC = "0x" + keccak(text="Sync(uint112,uint112)").hex()
# Aerodrome V2 pools emit different signatures (verified on real logs 2026-11-08): data layout is identical
AERO_SWAP = "0x" + keccak(text="Swap(address,address,uint256,uint256,uint256,uint256)").hex()
AERO_SYNC = "0x" + keccak(text="Sync(uint256,uint256)").hex()
KIND = {V2_SWAP: "v2swap", V3_SWAP: "v3swap", V2_SYNC: "sync", AERO_SWAP: "v2swap", AERO_SYNC: "sync"}


def sanitize(path: Path, done_block: int) -> None:
    """After an interrupted run: keep only complete records up to done_block, deduplicated, rewritten
    as one clean gzip stream (an interrupted gzip member would break every later read)."""
    if not path.exists():
        return
    seen, keep = set(), []
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    break
                key = (r["a"], r["b"], r["i"])
                if r["b"] <= done_block and key not in seen:
                    seen.add(key)
                    keep.append(line if line.endswith("\n") else line + "\n")
    except (EOFError, OSError, gzip.BadGzipFile):
        pass  # truncated tail: everything before it is kept
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        f.writelines(keep)
    tmp.replace(path)
    print(f"resume: kept {len(keep)} clean records up to block {done_block}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", default="https://base-rpc.publicnode.com")
    ap.add_argument("--pools", type=Path, required=True)
    ap.add_argument("--days", type=float, default=3.0)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--chunk", type=int, default=5_000)
    ap.add_argument("--addr-batch", type=int, default=10, help="addresses per eth_getLogs (publicnode rejects 32)")
    ap.add_argument("--end-block", type=int, default=None, help="last block of the window (default: latest)")
    ap.add_argument("--blocks", type=int, default=None, help="window length in blocks (overrides --days)")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    pools = json.loads(a.pools.read_text())
    w3 = AsyncWeb3(AsyncHTTPProvider(a.rpc, request_kwargs={"timeout": 60}))
    meta_path = a.out / "meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text())
        sanitize(a.out / "logs.jsonl.gz", meta["done_block"])
    else:
        hi = await w3.eth.get_block(a.end_block if a.end_block is not None else "latest")
        lo = await w3.eth.get_block(int(hi["number"]) - 50_000)
        bt = (int(hi["timestamp"]) - int(lo["timestamp"])) / 50_000
        n_blocks = a.blocks if a.blocks is not None else int(a.days * 86400 / bt)
        meta = {"anchor_block": int(hi["number"]), "anchor_ts": int(hi["timestamp"]), "block_time": bt,
                "from_block": int(hi["number"]) - n_blocks, "pools": pools, "done_block": int(hi["number"]) - n_blocks - 1}
        meta_path.write_text(json.dumps(meta, indent=2))
    addrs = [to_checksum_address(p["pool"]) for p in pools]
    topics = [[V2_SWAP, V3_SWAP, V2_SYNC, AERO_SWAP, AERO_SYNC]]
    cur, end, chunk = meta["done_block"] + 1, meta["anchor_block"], a.chunk
    total = end - meta["from_block"]
    t0, n_logs = time.time(), 0
    with gzip.open(a.out / "logs.jsonl.gz", "at", encoding="utf-8") as f:
        while cur <= end:
            hi_b = min(cur + chunk - 1, end)
            try:
                logs = []
                for g in range(0, len(addrs), a.addr_batch):  # free RPCs reject long address lists
                    logs += await w3.eth.get_logs({"fromBlock": cur, "toBlock": hi_b,
                                                   "address": addrs[g:g + a.addr_batch], "topics": topics})
            except Exception as exc:
                msg = str(exc).lower()
                if any(k in msg for k in ("rate", "429", "too many", "timeout", "timed out")):
                    await asyncio.sleep(2)
                    continue
                if chunk > 50:
                    chunk //= 2  # result too large / range too large
                    continue
                raise
            for lg in logs:
                f.write(json.dumps({"a": lg["address"].lower(), "b": int(lg["blockNumber"]), "i": int(lg["logIndex"]),
                                    "k": KIND["0x" + bytes(lg["topics"][0]).hex()],
                                    "d": "0x" + bytes(lg["data"]).hex()}) + "\n")
            n_logs += len(logs)
            f.flush()
            meta["done_block"] = hi_b
            meta_path.write_text(json.dumps(meta, indent=2))
            cur = hi_b + 1
            if len(logs) < 2_000 and chunk < a.chunk:
                chunk = min(a.chunk, chunk * 2)
            done = cur - meta["from_block"]
            if (cur - meta["from_block"]) // chunk % 20 == 0 or cur > end:
                print(f"{done / total:6.1%}  block {cur}  logs {n_logs}  chunk {chunk}  "
                      f"{time.time() - t0:.0f}s", flush=True)
    print(f"done: {n_logs} logs -> {a.out}")


if __name__ == "__main__":
    asyncio.run(main())
