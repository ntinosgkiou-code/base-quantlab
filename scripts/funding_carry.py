"""Stage 1 zeta of GO-NO-GO.md: delta-neutral funding carry measured on PUBLIC funding history (no keys).

    python scripts/funding_carry.py

Long spot + short perp earns the funding rate while it is positive and PAYS it while negative. Venues: Binance (8h),
Bybit (8h), Hyperliquid (1h). Costs fixed in the registration: 0.30% round trip every 90 days + 0.05%/yr rebalancing.
This measures the SIZE of the effect. It cannot measure exchange / counterparty risk (e.g. the 2026-10-29 Binance hack).
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import aiohttp
import numpy as np

DAY = 86400.0
NOW = time.time()
START = NOW - 365 * DAY
COST_PER_YEAR = 0.0030 * (365 / 90) + 0.0005   # 1.25%/yr
COINS = ("BTC", "ETH", "SOL")


async def get_json(s: aiohttp.ClientSession, method: str, url: str, **kw):
    for i in range(5):
        try:
            async with s.request(method, url, timeout=aiohttp.ClientTimeout(total=30), **kw) as r:
                if r.status == 200:
                    return await r.json()
                await asyncio.sleep(1 + i)
        except Exception:
            await asyncio.sleep(1 + i)
    raise RuntimeError(f"failed: {url}")


async def binance(s, coin):
    out, t = [], int(START * 1000)
    while True:
        d = await get_json(s, "GET", "https://fapi.binance.com/fapi/v1/fundingRate",
                           params={"symbol": f"{coin}USDT", "startTime": t, "limit": 1000})
        if not d:
            break
        out += [(x["fundingTime"] / 1000, float(x["fundingRate"])) for x in d]
        t = d[-1]["fundingTime"] + 1
        if len(d) < 1000:
            break
    return out


async def hyperliquid(s, coin):
    out, t = [], int(START * 1000)
    while True:
        d = await get_json(s, "POST", "https://api.hyperliquid.xyz/info", json={"type": "fundingHistory", "coin": coin, "startTime": t})
        if not d:
            break
        out += [(x["time"] / 1000, float(x["fundingRate"])) for x in d]
        t = d[-1]["time"] + 1
        if len(d) < 500:
            break
    return out


async def bybit(s, coin):
    out, end = [], int(NOW * 1000)
    while True:
        d = await get_json(s, "GET", "https://api.bybit.com/v5/market/funding/history",
                           params={"category": "linear", "symbol": f"{coin}USDT", "endTime": end, "limit": 200})
        rows = d["result"]["list"]
        if not rows:
            break
        out += [(int(x["fundingRateTimestamp"]) / 1000, float(x["fundingRate"])) for x in rows]
        oldest = min(int(x["fundingRateTimestamp"]) for x in rows)
        if oldest / 1000 <= START or len(rows) < 200:
            break
        end = oldest - 1
    out = sorted(x for x in out if x[0] >= START)
    return out


def analyse(rows: list[tuple[float, float]]) -> dict:
    ts = np.asarray([r[0] for r in rows])
    fr = np.asarray([r[1] for r in rows])
    order = np.argsort(ts)
    ts, fr = ts[order], fr[order]
    span_days = (ts[-1] - ts[0]) / DAY
    gross_apr = fr.sum() * 365 / span_days
    net_apr = gross_apr - COST_PER_YEAR
    cum = np.cumsum(fr)
    # rolling 30-day sums (by time)
    sums, nets = [], []
    j = 0
    for i in range(len(ts)):
        while ts[i] - ts[j] > 30 * DAY:
            j += 1
        if ts[i] - ts[0] >= 30 * DAY:
            s30 = cum[i] - (cum[j - 1] if j > 0 else 0.0)
            sums.append(s30)
            nets.append(s30 - COST_PER_YEAR * 30 / 365)
    nets = np.asarray(nets)
    last90 = ts >= ts[-1] - 90 * DAY
    apr90 = fr[last90].sum() * 365 / 90 - COST_PER_YEAR
    last7 = ts >= ts[-1] - 7 * DAY
    apr7 = fr[last7].sum() * 365 / 7
    dd = float(np.max(np.maximum.accumulate(cum) - cum))
    return {"days": span_days, "n": len(fr), "gross_apr": gross_apr, "net_apr": net_apr, "pos_share": float((fr > 0).mean()),
            "win30": float((nets > 0).mean()) if len(nets) else float("nan"), "worst30": float(nets.min()) if len(nets) else float("nan"),
            "net_apr_90d": apr90, "apr_7d": apr7, "max_dd_of_funding": dd}


async def main() -> None:
    results: dict[str, dict] = {}
    async with aiohttp.ClientSession() as s:
        for venue, fn in (("Binance", binance), ("Bybit", bybit), ("Hyperliquid", hyperliquid)):
            for coin in COINS:
                try:
                    rows = await fn(s, coin)
                    results[f"{venue}:{coin}"] = analyse(rows)
                except Exception as exc:
                    print(f"{venue}:{coin} FAILED {exc}")
    print(f"{'venue:coin':<16}{'days':>6}{'gross APR':>11}{'NET APR':>9}{'last90d net':>12}{'last7d gross':>13}{'30d>0':>7}{'worst30d':>9}{'maxDD fund':>11}{'rate>0':>8}")
    for k, v in results.items():
        print(f"{k:<16}{v['days']:>6.0f}{v['gross_apr']:>11.1%}{v['net_apr']:>9.1%}{v['net_apr_90d']:>12.1%}{v['apr_7d']:>13.1%}"
              f"{v['win30']:>7.0%}{v['worst30']:>9.2%}{v['max_dd_of_funding']:>11.2%}{v['pos_share']:>8.0%}")
    ok = [v for v in results.values()]
    mean_net = float(np.mean([v["net_apr"] for v in ok]))
    win30 = float(np.mean([v["win30"] for v in ok]))
    worst = float(np.min([v["worst30"] for v in ok]))
    r90 = float(np.mean([v["net_apr_90d"] for v in ok]))
    gates = {
        "F1_net_apr>=8%": mean_net >= 0.08,
        "F2_30d_windows_positive>=80%": win30 >= 0.80,
        "F3_worst_30d>=-1%": worst >= -0.01,
        "F4_last90d>=50%_of_12m": r90 >= 0.5 * mean_net,
    }
    print(f"\nmean NET APR across venues/coins: {mean_net:.1%} | positive 30d windows: {win30:.0%} | worst 30d: {worst:.2%} | last 90d net APR: {r90:.1%}")
    for k, v in gates.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    print("capital needed for EUR 1000/month at the measured net APR:", f"EUR {12 * 1000 / mean_net:,.0f}" if mean_net > 0 else "n/a")
    out = Path("data/funding_carry.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"results": results, "gates": gates, "mean_net_apr": mean_net}, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
