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
import sys
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import exchange_calendars
import httpx
import pandas as pd

from qdii.core.futures_basis_research import (
    CashSession,
    analyze,
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


def analysis_from_messages(fut_msg: RawMessage, idx_msg: RawMessage, cal: CalendarProvider,
                           official: dict | None = None) -> dict:
    """两条原始响应 → 确定性分析。日历调度在此（I/O 层），统计全部在 core 纯函数里。"""
    fut_raw = parse_chart(fut_msg.body, source_ref=fut_msg.msg_id, received_s=fut_msg.received_utc_ns / 1e9)
    idx_raw = parse_chart(idx_msg.body, source_ref=idx_msg.msg_id, received_s=idx_msg.received_utc_ns / 1e9)
    as_of = min(fut_raw.received_s, idx_raw.received_s)
    sessions = None
    if idx_raw.timestamps:
        first = min(datetime.fromtimestamp(t, NY).date() for t in idx_raw.timestamps)
        sessions = expected_sessions(cal, first, datetime.fromtimestamp(as_of, NY).date())
    return analyze(fut_raw, idx_raw, sessions if idx_raw.timestamps else (), as_of_s=as_of, official=official)


def main(contract: str) -> None:
    msgs = asyncio.run(fetch(contract))
    fut_msg, idx_msg = msgs[f"yahoo.chart.{contract}"], msgs["yahoo.chart.NDX"]
    out = {"contract": contract, "generated_utc": datetime.now(UTC).isoformat(timespec="seconds"),
           "analysis": analysis_from_messages(fut_msg, idx_msg, CalendarProvider(REPO / "config" / "calendar_overrides.toml"))}
    path = REPO / "reports" / "mvp" / "evidence" / f"futures-basis-{fut_msg.run_id}.json"
    if path.exists():  # 不覆盖历史证据
        raise SystemExit(f"输出已存在，拒绝覆盖：{path}")
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    a = out["analysis"]
    print(f"跨会话（{a['cross_session']['rule']}）：{a['cross_session']['n']} 个，状态 {a['cross_session']['status']}")
    print(f"亚洲决策时点误差：{a['asia_decision_error_status']}（界限 {a['asia_decision_error_bound_bp']}）")
    print(path)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "NQZ26")
