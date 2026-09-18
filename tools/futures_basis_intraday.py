"""探索性研究：Yahoo 美股现金时段与跨会话端点的基差变化（勘误 E8 遗留项，2026-09-18 复审后降级）。

本工具**不测量**亚洲决策时点的期货代理误差。E-NAV 用 F(t)/S(c) 代替"从昨晚美股收盘到此刻持仓价值的变化"；
亚洲时段官方指数停在昨收，没有独立的经济公允价值参照，误差保持未识别（UNIDENTIFIED）。
美股时段的端点只说明端点本身，不约束区间内部，不是亚洲时点误差的包络或界限；研究所用的五分钟线收盘
也不是正式指数收盘或结算锚点（reports/phase0/futures/findings.md 第 9 节）。

数据：Yahoo chart 接口（NQZ26.CME 与 ^NDX，5 分钟）。只读、无凭据、每日 2 次请求；抓取写入研究原始日志后再重解析。
与生产使用的新浪源是不同供应商。

用法：uv run python tools/futures_basis_intraday.py [合约代码]
输出：reports/mvp/evidence/futures-basis-<date>.json
"""

from __future__ import annotations

import asyncio
import itertools
import json
import statistics
import sys
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import exchange_calendars
import httpx
import pandas as pd

from qdii.core.futures_basis_research import (
    CashSession,
    PriceKind,
    Quality,
    classify,
    pair_sessions,
    parse_chart,
)
from qdii.core.types import RawMessage
from qdii.io import rawlog
from qdii.io.calendars import CalendarProvider
from qdii.io.clock import SystemClock
from qdii.io.http import EndpointRequest, Fetcher

REPO = Path(__file__).resolve().parents[1]
DATA = Path.home() / "qdii-data"
NY = ZoneInfo("America/New_York")
UA = "qdii-premium-research/0.1 (personal research)"
HORIZONS_H = (1, 2, 3, 6)
MARKET = "NASDAQ"  # 覆盖文件映射到 XNYS，与生产取美股交易日同一入口


def expected_sessions(cal: CalendarProvider, first: date, last: date) -> tuple[CashSession, ...] | None:
    """[first, last] 内按交易日历预期的现金会话（含提前收盘）。日历不确定或未覆盖时返回 None，不按工作日推断。"""
    if not (cal.covers(MARKET, first - timedelta(days=1)) and cal.covers(MARKET, last)):
        return None
    out = []
    for d in cal.sessions_between(MARKET, first - timedelta(days=1), last):
        s = cal.session(MARKET, d)
        if s is None:
            return None
        close_local = datetime.fromtimestamp(s.close_utc_ns / 1e9, NY).time()
        out.append(CashSession(d, s.open_utc_ns // 10**9, s.close_utc_ns // 10**9, early_close=close_local != time(16)))
    return tuple(out)


def holiday_names(cal: CalendarProvider, first: date, last: date) -> dict[date, str]:
    """非交易工作日的名称，仅作注释（调度只看 expected_sessions）。名称取自 exchange_calendars 的 XNYS 假日表。"""
    sessions = expected_sessions(cal, first, last)
    if sessions is None:
        return {}
    xnys = exchange_calendars.get_calendar("XNYS")
    named = {ts.date(): str(name) for ts, name in
             xnys.regular_holidays.holidays(pd.Timestamp(first), pd.Timestamp(last), return_name=True).items()}
    adhoc = {ts.date() for ts in xnys.adhoc_holidays}
    days = {s.local_date for s in sessions}
    out, d = {}, first
    while d <= last:
        if d.weekday() < 5 and d not in days:
            out[d] = named.get(d, "ADHOC_CLOSURE" if d in adhoc else "UNNAMED_CLOSURE")
        d += timedelta(days=1)
    return out


async def fetch(contract: str) -> dict[str, RawMessage]:
    """抓取并先写入研究原始日志；异常时也关闭写入器。"""
    run_id = f"RESEARCH-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    writer = rawlog.RawLogWriter(DATA / "research")
    seq = itertools.count()
    ids = {f"yahoo.chart.{contract}": f"{contract}.CME", "yahoo.chart.NDX": "%5ENDX"}
    msgs = {}
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, trust_env=False) as client:
            fetcher = Fetcher(client, SystemClock(), run_id)
            for endpoint_id, symbol in ids.items():
                url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=5m&range=1mo"
                req = EndpointRequest(f"research.{endpoint_id}", "yahoo", "GET", url, (("User-Agent", UA),))
                msg = await fetcher.fetch(req, next(seq))
                writer.append(msg)
                msgs[endpoint_id] = msg
                await asyncio.sleep(1.0)
    finally:
        writer.close()
    return msgs


def main(contract: str) -> None:
    msgs = asyncio.run(fetch(contract))
    fut_msg, idx_msg = msgs[f"yahoo.chart.{contract}"], msgs["yahoo.chart.NDX"]
    fut_raw = parse_chart(fut_msg.body, source_ref=fut_msg.msg_id, received_s=fut_msg.received_utc_ns / 1e9)
    idx_raw = parse_chart(idx_msg.body, source_ref=idx_msg.msg_id, received_s=idx_msg.received_utc_ns / 1e9)
    as_of = min(fut_raw.received_s, idx_raw.received_s)
    if not idx_raw.timestamps:
        print("指数序列为空，无法统计")
        return
    first = min(datetime.fromtimestamp(t, NY).date() for t in idx_raw.timestamps)
    sessions = expected_sessions(CalendarProvider(REPO / "config" / "calendar_overrides.toml"), first,
                                 datetime.fromtimestamp(as_of, NY).date())
    fut_recs = classify(fut_raw, sessions, cash_index=False)
    idx_recs = classify(idx_raw, sessions, cash_index=True)
    cross = pair_sessions(fut_recs, idx_recs, sessions, as_of_s=as_of)
    overnight = [{"from": x.from_date.isoformat(), "to": x.to_date.isoformat(), "gap": x.gap.value,
                  "close_basis_bp": x.start.basis * 1e4, "open_basis_bp": x.end.basis * 1e4,
                  "drift_bp": x.basis_drift * 1e4, "hours": x.elapsed_hours} for x in cross.samples]
    # 盘中重叠窗口仍按 v1 口径，只用完整普通线（下一步改为不重叠窗口）
    fut = {r.interval_start: r.close for r in fut_recs if r.kind is PriceKind.BAR_5M and r.quality is Quality.OK}
    idx = {r.interval_start: r.close for r in idx_recs if r.kind is PriceKind.BAR_5M and r.quality is Quality.OK}
    paired = sorted(set(fut) & set(idx))
    basis = {t: (fut[t] / idx[t] - 1) * 1e4 for t in paired}
    by_day: dict[str, list[int]] = {}
    for t in paired:
        by_day.setdefault(datetime.fromtimestamp(t, NY).date().isoformat(), []).append(t)
    days = sorted(by_day)

    # 盘中同长度区间（重叠窗口，v1 口径；v2 改为不重叠窗口）
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
    closes = [e.close for e in cross.endpoints if e.close is not None]
    out = {
        "nature": "EXPLORATORY_US_CASH_SESSION_BASIS_CHANGE (Yahoo 5 分钟配对；与生产源不同供应商)",
        "generated_utc": datetime.now(UTC).isoformat(timespec="seconds"), "contract": contract,
        "paired_points": len(paired), "sessions": len(days), "from": days[0] if days else None,
        "to": days[-1] if days else None,
        "caveat": "美股时段端点样本，不约束区间内部，不是亚洲决策时点误差的包络或界限；亚洲时点误差未识别",
        "overnight_drift": overnight,
        "overnight_abs_drift_bp": {"n": len(drifts),
                                   "median": round(statistics.median(drifts), 1) if drifts else None,
                                   "p95": round(drifts[int(0.95 * len(drifts))], 1) if drifts else None,
                                   "max": round(max(drifts), 1) if drifts else None},
        "intraday_abs_drift_bp_by_horizon": horizon_stats,
        "pairing_rule": cross.rule, "cross_session_status": cross.status,
        "last_close_basis_bp": closes[-1].basis * 1e4 if closes else None,
    }
    path = REPO / "reports" / "mvp" / "evidence" / f"futures-basis-{fut_msg.run_id}.json"
    if path.exists():  # 不覆盖历史证据
        raise SystemExit(f"输出已存在，拒绝覆盖：{path}")
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{contract} × NDX 配对 {len(paired)} 点，{len(days)} 个交易日（{out['from']}→{out['to']}）")
    print(f"跨会话端点变化（{cross.rule}，{len(overnight)} 个，状态 {cross.status}）：",
          json.dumps(out["overnight_abs_drift_bp"], ensure_ascii=False))
    for o in overnight[-5:]:
        print(f"  {o['from']} 收盘 {o['close_basis_bp']:.1f}bp → {o['to']} 首根线 {o['open_basis_bp']:.1f}bp"
              f"（{o['drift_bp']:+.1f}bp，{o['hours']:.1f} 小时，{o['gap']}）")
    print("盘中同长度区间漂移：", json.dumps(horizon_stats, ensure_ascii=False))
    print(path)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "NQZ26")
