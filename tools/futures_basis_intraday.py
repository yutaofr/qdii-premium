"""期货段误差的实测：基差漂移 b(τ) = F(τ)/I(τ) − 1 的时间行为（勘误 E8 遗留项）。

为什么需要它：E-NAV 用 F(t)/F(c) 代替"从昨晚美股收盘到此刻，持仓价值变了多少"。被代替的量在亚洲时段
**本身不可观测**（指数不交易），两者之差就是基差漂移 b(t) − b(c)。b(c) 每天精确已知（结算 vs 指数收盘），
缺的是区间内的 b(t)。

可测的两件事（都用同刻配对的 5 分钟数据）：

1. **隔夜漂移** b(次日开盘) − b(前日收盘)：两个端点都有指数值，而我们的估算时刻 t 落在这段区间**之内**，
   因此它是包住 t 的区间漂移，可直接统计分布。
2. **盘中同长度区间的漂移**：美股时段内 b(τ+h) − b(τ)，h 取 1—6 小时，用于看漂移随时间的增长方式
   （是否接近扩散），从而判断"把隔夜漂移按时间比例缩放到 t"是否有依据。

数据：Yahoo chart 接口（NQZ26.CME 与 ^NDX，5 分钟）。只读、无凭据、每日 2 次请求；抓取写入研究原始日志后再重解析。
口径说明：与生产使用的新浪源是**不同供应商**，5 分钟粒度；亚洲时段基差不可观测，故隔夜漂移是**包络**，不是 t 时刻的误差。

用法：uv run python tools/futures_basis_intraday.py [合约代码]
输出：reports/mvp/evidence/futures-basis-<date>.json
"""

from __future__ import annotations

import asyncio
import itertools
import json
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from qdii.io import rawlog
from qdii.io.clock import SystemClock
from qdii.io.http import EndpointRequest, Fetcher

REPO = Path(__file__).resolve().parents[1]
DATA = Path.home() / "qdii-data"
NY = ZoneInfo("America/New_York")
UA = "qdii-premium-research/0.1 (personal research)"
HORIZONS_H = (1, 2, 3, 6)


async def fetch(contract: str) -> dict[str, list]:
    """抓取并写入研究原始日志，再从原始日志重解析（与其他研究工具同一规则）。"""
    run_id = f"RESEARCH-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    writer = rawlog.RawLogWriter(DATA / "research")
    seq = itertools.count()
    ids = {f"yahoo.chart.{contract}": f"{contract}.CME", "yahoo.chart.NDX": "%5ENDX"}
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, trust_env=False) as client:
        fetcher = Fetcher(client, SystemClock(), run_id)
        msgs = {}
        for endpoint_id, symbol in ids.items():
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=5m&range=1mo"
            req = EndpointRequest(f"research.{endpoint_id}", "yahoo", "GET", url, (("User-Agent", UA),))
            msg = await fetcher.fetch(req, next(seq))
            writer.append(msg)
            msgs[endpoint_id] = msg
            await asyncio.sleep(1.0)
        writer.close()
    return {k: series(v.body) for k, v in msgs.items()}


def series(body: bytes) -> list[tuple[int, float]]:
    doc = json.loads(body)["chart"]["result"][0]
    closes = doc["indicators"]["quote"][0]["close"]
    return [(t, c) for t, c in zip(doc["timestamp"], closes, strict=True) if c]


def main(contract: str) -> None:
    data = asyncio.run(fetch(contract))
    fut = dict(data[f"yahoo.chart.{contract}"])
    idx = dict(data["yahoo.chart.NDX"])
    paired = sorted(set(fut) & set(idx))  # 只有美股时段两者同时有值
    basis = {t: (fut[t] / idx[t] - 1) * 1e4 for t in paired}
    if len(paired) < 50:
        print("配对样本过少，无法统计")
        return

    # 每个美股交易日的收盘与开盘基差 → 隔夜漂移（包住我们的估算时刻）
    by_day: dict[str, list[int]] = {}
    for t in paired:
        by_day.setdefault(datetime.fromtimestamp(t, NY).date().isoformat(), []).append(t)
    days = sorted(by_day)
    overnight = []
    for prev, nxt in itertools.pairwise(days):
        close_t, open_t = by_day[prev][-1], by_day[nxt][0]
        drift = basis[open_t] - basis[close_t]
        hours = (open_t - close_t) / 3600
        overnight.append({"from": prev, "to": nxt, "close_basis_bp": round(basis[close_t], 1),
                          "open_basis_bp": round(basis[open_t], 1), "drift_bp": round(drift, 1),
                          "hours": round(hours, 1)})

    # 盘中同长度区间漂移：衡量漂移随时间的增长
    horizon_stats = {}
    for h in HORIZONS_H:
        diffs = []
        for day, stamps in by_day.items():
            for t in stamps:
                t2 = t + h * 3600
                if t2 in basis and datetime.fromtimestamp(t2, NY).date().isoformat() == day:
                    diffs.append(abs(basis[t2] - basis[t]))
        if diffs:
            diffs.sort()
            horizon_stats[f"{h}h"] = {"n": len(diffs), "median": round(statistics.median(diffs), 1),
                                      "p95": round(diffs[int(0.95 * len(diffs))], 1), "max": round(max(diffs), 1)}

    drifts = sorted(abs(o["drift_bp"]) for o in overnight)
    out = {
        "nature": "FUTURES_LEG_BASIS_DRIFT (Yahoo 5 分钟配对；与生产源不同供应商)",
        "generated_utc": datetime.now(UTC).isoformat(timespec="seconds"), "contract": contract,
        "paired_points": len(paired), "sessions": len(days), "from": days[0], "to": days[-1],
        "caveat": "亚洲时段基差不可观测；隔夜漂移是包住估算时刻 t 的区间漂移，不是 t 时刻误差本身",
        "overnight_drift": overnight,
        "overnight_abs_drift_bp": {"n": len(drifts),
                                   "median": round(statistics.median(drifts), 1) if drifts else None,
                                   "p95": round(drifts[int(0.95 * len(drifts))], 1) if drifts else None,
                                   "max": round(max(drifts), 1) if drifts else None},
        "intraday_abs_drift_bp_by_horizon": horizon_stats,
        "last_close_basis_bp": round(basis[by_day[days[-1]][-1]], 1),
    }
    path = REPO / "reports" / "mvp" / "evidence" / f"futures-basis-{datetime.now(NY).date().isoformat()}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{contract} × NDX 配对 {len(paired)} 点，{len(days)} 个交易日（{days[0]}→{days[-1]}）")
    print(f"隔夜漂移（{len(overnight)} 次，约 {overnight[-1]['hours']:.0f} 小时）：",
          json.dumps(out["overnight_abs_drift_bp"], ensure_ascii=False))
    for o in overnight[-5:]:
        print(f"  {o['from']} 收盘 {o['close_basis_bp']}bp → {o['to']} 开盘 {o['open_basis_bp']}bp"
              f"（{o['drift_bp']:+.1f}bp）")
    print("盘中同长度区间漂移：", json.dumps(horizon_stats, ensure_ascii=False))
    print(path)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "NQZ26")
