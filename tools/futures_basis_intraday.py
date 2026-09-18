"""探索性研究：Yahoo 美股现金时段与跨会话端点的基差变化（勘误 E8 遗留项，2026-09-18 复审后降级）。

本工具**不测量**亚洲决策时点的期货代理误差。E-NAV 用 F(t)/S(c) 代替"从昨晚美股收盘到此刻持仓价值的变化"；
亚洲时段官方指数停在昨收，没有独立的经济公允价值参照，误差保持未识别（UNIDENTIFIED）。
美股时段的端点只说明端点本身，不约束区间内部，不是亚洲时点误差的包络或界限；研究所用的五分钟线收盘
也不是正式指数收盘或结算锚点（reports/phase0/futures/findings.md 第 9 节）。

数据：Yahoo chart 接口（NQZ26.CME 与 ^NDX，5 分钟），与生产使用的新浪源是不同供应商。
本文件只做 I/O：读写原始日志、按交易日历排定会话、读取官方收盘、写输出；配对与统计全部在
qdii.core.futures_basis_research 的纯函数里。

用法：
  离线重算（不发请求，按 run_id 与端点各取唯一一条响应并校验 body SHA-256）：
    uv run python tools/futures_basis_intraday.py NQZ26 --raw-file <原始日志.jsonl[.gz]> --run-id <RUN> \\
        --output <输出.json> [--official-close-root ~/qdii-data] [--legacy <旧版证据.json>]
  网络抓取（先写研究原始日志，再从日志重解析；输出文件名带 run_id）：
    uv run python tools/futures_basis_intraday.py NQZ26
输出文件已存在时拒绝覆盖。退出码：0 有跨会话样本；1 输入错误；2 输出冲突；3 NO_DATA。
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import itertools
import json
import sys
from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import exchange_calendars
import httpx
import pandas as pd

from qdii.contracts import index_history_v1
from qdii.core.futures_basis_research import (
    BAR_S,
    CashSession,
    ChartError,
    OfficialClose,
    PriceKind,
    RawSeries,
    analyze,
    classify,
    parse_chart,
    verify_official_closes,
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
SCHEMA_VERSION = "futures-basis-evidence-2"
PAUSE_S = 1.0


def endpoints(contract: str) -> tuple[str, str]:
    return f"research.yahoo.chart.{contract}", "research.yahoo.chart.NDX"


def _home(p: Path | str) -> str:
    s = str(p)
    return "~" + s[len(str(Path.home())):] if s.startswith(str(Path.home())) else s


# ---------- 日历调度 ----------

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


# ---------- 原始日志 ----------

class SelectionError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


def read_raw(paths: Sequence[Path]) -> Iterator[RawMessage]:
    for path in paths:
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as fh:  # type: ignore[operator]
            for n, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    yield rawlog.decode(line)
                except (ValueError, KeyError) as exc:
                    raise SelectionError("RAW_LINE_INVALID", f"{path}:{n}: {exc}") from exc


def select_messages(paths: Sequence[Path], run_id: str, wanted: Sequence[str]) -> dict[str, RawMessage]:
    """每个端点在该 run_id 下必须恰好一条、哈希一致、HTTP 200；重复匹配不取最后一条，直接拒绝。"""
    found: dict[str, list[RawMessage]] = {ep: [] for ep in wanted}
    for m in read_raw(paths):
        if m.run_id == run_id and m.endpoint_id in found:
            found[m.endpoint_id].append(m)
    for ep, ms in found.items():
        if not ms:
            raise SelectionError("MISSING_RESPONSE", f"{ep} (run_id {run_id})")
        if len(ms) > 1:
            raise SelectionError("DUPLICATE_RESPONSE", f"{ep}: {[m.msg_id for m in ms]}")
        m = ms[0]
        if hashlib.sha256(m.body).hexdigest() != m.body_sha256:
            raise SelectionError("HASH_MISMATCH", f"{ep} {m.msg_id}")
        if m.status != 200 or m.error:
            raise SelectionError("HTTP_ERROR", f"{ep} {m.msg_id}: status={m.status} error={m.error}")
    return {ep: ms[0] for ep, ms in found.items()}


async def fetch(contract: str, research_root: Path) -> tuple[str, list[Path]]:
    """网络模式：抓取两条响应，先写入研究原始日志（异常时也关闭写入器），返回 run_id 与写入的日志文件。"""
    run_id = f"RESEARCH-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    writer = rawlog.RawLogWriter(research_root)
    seq = itertools.count()
    paths: list[Path] = []
    symbols = {f"research.yahoo.chart.{contract}": f"{contract}.CME", "research.yahoo.chart.NDX": "%5ENDX"}
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, trust_env=False) as client:
            fetcher = Fetcher(client, SystemClock(), run_id)
            for endpoint_id, symbol in symbols.items():
                url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=5m&range=1mo"
                req = EndpointRequest(endpoint_id, "yahoo", "GET", url, (("User-Agent", UA),))
                path = writer.append(await fetcher.fetch(req, next(seq)))
                if path not in paths:
                    paths.append(path)
                await asyncio.sleep(PAUSE_S)
    finally:
        writer.close()
    return run_id, paths


def load_official_closes(root: Path, first: date, last: date) -> tuple[dict[date, OfficialClose], list[dict]]:
    """从生产原始日志离线读取 Nasdaq 官方 NDX 收盘（index_history_v1）。同一日期出现不同数值时排除该日。"""
    values: dict[date, dict[str, str]] = {}
    for m in rawlog.iter_messages(root, sources=["nasdaq"]):
        if m.status != 200:
            continue
        for rec in index_history_v1.parse_nasdaq(m).records:
            if rec.close is not None and first <= rec.trade_date <= last:
                values.setdefault(rec.trade_date, {}).setdefault(str(rec.close), m.msg_id)
    official, conflicts = {}, []
    for d, vs in sorted(values.items()):
        if len(vs) == 1:
            value, ref = next(iter(vs.items()))
            official[d] = OfficialClose(d, value, ref)
        else:
            conflicts.append({"date": d.isoformat(), "values": sorted(vs)})
    return official, conflicts


# ---------- 旧版（f237d46）对比 ----------

def _ny(t: int) -> str:
    return datetime.fromtimestamp(t, NY).strftime("%H:%M")


def _span(end: int) -> str:
    return f"{_ny(end - BAR_S)}—{_ny(end)}"


def legacy_endpoints(fut_raw: RawSeries, idx_raw: RawSeries):
    """原样复现 f237d46 的配对规则，只用于对比：按标签取交集、按美东日期分组、取每日首尾标签。"""
    fut = {t: c for t, c in zip(fut_raw.timestamps, fut_raw.closes, strict=True) if c}
    idx = {t: c for t, c in zip(idx_raw.timestamps, idx_raw.closes, strict=True) if c}
    by_day: dict[str, list[int]] = {}
    for t in sorted(set(fut) & set(idx)):
        by_day.setdefault(datetime.fromtimestamp(t, NY).date().isoformat(), []).append(t)
    return {d: (ts[0], ts[-1]) for d, ts in by_day.items()}, fut, idx


def compare_with_legacy(legacy: dict, fut_raw: RawSeries, idx_raw: RawSeries, analysis: dict,
                        sessions: Sequence[CashSession], idx_kinds: dict[int, str]) -> dict:
    days, fut, idx = legacy_endpoints(fut_raw, idx_raw)

    def basis(t: int) -> float:
        return (fut[t] / idx[t] - 1) * 1e4

    repro = {(a, b): (days[a][1], days[b][0]) for a, b in itertools.pairwise(sorted(days))}
    old = {(o["from"], o["to"]): o for o in legacy["overnight_drift"]}
    new = {(s["from"], s["to"]): s for s in analysis["cross_session"]["samples"]}
    pairs = []
    for key in sorted(set(old) | set(new)):
        o, n = old.get(key), new.get(key)
        row = {"from": key[0], "to": key[1], "legacy_drift_bp": o and o["drift_bp"],
               "new_drift_bp": n and n["basis_drift_bp"], "legacy_reproduced": None, "same_records": None}
        if o is None or n is None:
            row["change"] = "ONLY_IN_LEGACY" if n is None else "ONLY_IN_NEW"
        else:
            r = repro.get(key)
            row["legacy_reproduced"] = r is not None and round(basis(r[1]) - basis(r[0]), 1) == o["drift_bp"]
            same = r is not None and (r[0] + BAR_S, r[1] + BAR_S) == (n["start_time_utc_s"], n["end_time_utc_s"])
            row.update(same_records=same, legacy_close_label_ny=None if r is None else _ny(r[0]),
                       legacy_open_label_ny=None if r is None else _ny(r[1]),
                       new_close_interval_ny=_span(n["start_time_utc_s"]), new_open_interval_ny=_span(n["end_time_utc_s"]),
                       change=("ROUNDING_ONLY" if same and round(n["basis_drift_bp"], 1) == o["drift_bp"]
                               else "VALUE_CHANGED" if same else "ENDPOINT_CHANGED"))
        pairs.append(row)

    last_day = max(days) if days else None
    closes = {s.local_date.isoformat(): s for s in sessions}
    new_close = {s["date"]: s for s in analysis["sessions"]["session_close_basis"]}
    last = {"legacy_bp": legacy.get("last_close_basis_bp"), "date": last_day}
    if last_day is not None:
        t = days[last_day][1]
        sess = closes.get(last_day)
        same = sess is not None and t + BAR_S == sess.close_s and idx_kinds.get(t) == PriceKind.BAR_5M.value
        entry = new_close.get(last_day, {})
        last.update(
            legacy_reproduced=round(basis(t), 1) == legacy.get("last_close_basis_bp"),
            legacy_records={"futures_label_ny": _ny(t), "futures_interval_ny": _span(t + BAR_S), "futures": fut[t],
                            "index_label_ny": _ny(t), "index": idx[t], "index_kind": idx_kinds.get(t, "UNKNOWN")},
            new_bar_close_bp=entry.get("bar_close_basis_bp"), new_bar_close_futures=entry.get("bar_close_futures"),
            new_bar_close_index=entry.get("bar_close_index"), new_official_close_bp=entry.get("official_close_basis_bp"),
            change="SAME_RECORDS" if same else "ENDPOINT_CHANGED")

    s = analysis["cross_session"]["summary"]
    n = s["n"]
    return {
        "pairs": pairs,
        "cross_session_summary": {
            "legacy": legacy.get("overnight_abs_drift_bp"),
            "new": {"n": n, "abs_median": s["abs_median"]["value"], "abs_p95": s["abs_p95"]["value"],
                    "abs_p95_rank": s["abs_p95"]["rank"], "abs_max": s["abs_max"]},
            "notes": [
                "旧版对 0.1bp 舍入后的值计算；新版全部用未舍入值，只在展示时舍入",
                "中位数：旧版 statistics.median，新版 nearest-rank；n 为奇数时取同一位置",
                (f"P95 位置：旧版下标 int(0.95n)={int(0.95 * n)}，新版 ceil(0.95n)−1={s['abs_p95']['index']}"
                 "（p·n 为整数时两者不同）"),
            ]},
        "last_close_basis": last,
        "intraday": {
            "legacy": legacy.get("intraday_abs_drift_bp_by_horizon"),
            "new": {h: {"windows": v["windows"], "sessions": v["sessions"],
                        "abs_median": v["summary"]["abs_median"]["value"], "abs_p95": v["summary"]["abs_p95"]["value"],
                        "abs_max": v["summary"]["abs_max"]} for h, v in analysis["intraday"]["by_horizon"].items()},
            "change": "METHOD_CHANGED_NOT_COMPARABLE",
            "note": "旧版为重叠窗口（同一段行情多次计数，各期限覆盖时段不同）；新版为同一开盘起点的不重叠窗口，缺线窗口排除",
        },
    }


# ---------- 组装 ----------

def build_document(contract: str, mode: str, run_id: str, paths: Sequence[Path], msgs: dict[str, RawMessage],
                   cal: CalendarProvider, official_root: Path | None, legacy_path: Path | None) -> dict:
    ep_fut, ep_idx = endpoints(contract)
    fm, im = msgs[ep_fut], msgs[ep_idx]
    fut_raw = parse_chart(fm.body, source_ref=fm.msg_id, received_s=fm.received_utc_ns / 1e9)
    idx_raw = parse_chart(im.body, source_ref=im.msg_id, received_s=im.received_utc_ns / 1e9)
    as_of = min(fut_raw.received_s, idx_raw.received_s)
    sessions: tuple[CashSession, ...] | None = ()
    first = last = None
    if idx_raw.timestamps:
        first = min(datetime.fromtimestamp(t, NY).date() for t in idx_raw.timestamps)
        last = datetime.fromtimestamp(as_of, NY).date()
        sessions = expected_sessions(cal, first, last)
    official, conflicts = ({}, [])
    if official_root is not None and first is not None:
        official, conflicts = load_official_closes(official_root, first, last)  # type: ignore[arg-type]
    analysis = analyze(fut_raw, idx_raw, sessions, as_of_s=as_of, official=official)
    idx_recs, _ = verify_official_closes(classify(idx_raw, sessions, cash_index=True), sessions, official)
    comparison = None
    if legacy_path is not None:
        legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
        comparison = {"legacy_file": _home(legacy_path),
                      **compare_with_legacy(legacy, fut_raw, idx_raw, analysis, sessions or (),
                                            {r.raw_ts: r.kind.value for r in idx_recs})}
    names = holiday_names(cal, first, last) if first is not None else {}
    return {
        "schema_version": SCHEMA_VERSION,
        "meta": {"generated_utc": datetime.now(UTC).isoformat(timespec="seconds"), "mode": mode,
                 "tool": "tools/futures_basis_intraday.py"},
        "inputs": {
            "contract": contract, "run_id": run_id, "raw_files": [_home(p) for p in paths],
            "responses": [{"endpoint_id": m.endpoint_id, "msg_id": m.msg_id, "run_id": m.run_id, "url": m.request.url,
                           "status": m.status, "body_sha256": m.body_sha256, "body_sha256_verified": True,
                           "received_utc_ns": m.received_utc_ns,
                           "received_at_utc": datetime.fromtimestamp(m.received_utc_ns / 1e9, UTC).isoformat()}
                          for m in (fm, im)],
            "calendar": {"market": MARKET, "provider_version": cal.version, "tzdb": cal.tzdb_version,
                         "errors": list(cal.errors), "status": "UNCERTAIN" if sessions is None else "COVERED",
                         "non_session_weekdays": {d.isoformat(): n for d, n in sorted(names.items())}},
            "official_closes": None if official_root is None else {
                "root": _home(official_root), "source": "Nasdaq 官方 NDX 历史（生产原始日志，index_history_v1）",
                "records": [{"date": d.isoformat(), "value": o.value, "ref": o.source_ref}
                            for d, o in sorted(official.items())],
                "conflicts": conflicts},
        },
        "analysis": analysis,
        "legacy_comparison": comparison,
    }


def run(argv: Sequence[str]) -> int:
    ap = argparse.ArgumentParser(description="Yahoo 美股现金时段与跨会话端点的基差变化（探索性，不测亚洲时点误差）")
    ap.add_argument("contract", nargs="?", default="NQZ26")
    ap.add_argument("--raw-file", type=Path, help="离线重算：研究原始日志文件（.jsonl 或 .jsonl.gz），不发请求")
    ap.add_argument("--run-id", help="离线重算时必需：只取该 run_id 的响应")
    ap.add_argument("--output", type=Path, help="输出文件；已存在时拒绝覆盖")
    ap.add_argument("--output-dir", type=Path, default=REPO / "reports" / "mvp" / "evidence")
    ap.add_argument("--research-root", type=Path, default=DATA / "research", help="网络模式写入的研究原始日志根目录")
    ap.add_argument("--official-close-root", type=Path, help="可选：离线读取 Nasdaq 官方收盘的原始日志根目录")
    ap.add_argument("--legacy", type=Path, help="可选：旧版证据 JSON，生成逐项对比")
    args = ap.parse_args(argv)
    if args.raw_file is not None and not args.run_id:
        ap.error("--raw-file 需要同时给出 --run-id")
    if args.output is not None and args.output.exists():
        print(f"输出已存在，拒绝覆盖：{args.output}", file=sys.stderr)
        return 2
    cal = CalendarProvider(REPO / "config" / "calendar_overrides.toml")
    try:
        if args.raw_file is not None:
            mode, run_id, paths = "OFFLINE_RAW_FILE", args.run_id, [args.raw_file]
        else:
            mode = "NETWORK"
            run_id, paths = asyncio.run(fetch(args.contract, args.research_root))
        out = args.output or args.output_dir / f"futures-basis-{run_id}.json"
        msgs = select_messages(paths, run_id, endpoints(args.contract))
        doc = build_document(args.contract, mode, run_id, paths, msgs, cal, args.official_close_root, args.legacy)
    except (SelectionError, ChartError) as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        return 1
    try:
        with out.open("x", encoding="utf-8") as fh:  # "x"：已存在即失败，不覆盖
            fh.write(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
    except FileExistsError:
        print(f"输出已存在，拒绝覆盖：{out}", file=sys.stderr)
        return 2
    a = doc["analysis"]
    cs = a["cross_session"]
    p95 = cs["summary"]["abs_p95"]
    print(f"{args.contract} × NDX（{mode}，run_id {run_id}）：跨会话 {cs['rule']} {cs['n']} 个，状态 {cs['status']}")
    if cs["n"]:
        print(f"  |基差变化| 中位 {cs['summary']['abs_median']['value']:.1f}bp、P95 {p95['value']:.1f}bp"
              f"（nearest-rank 第 {p95['rank']}/{p95['n']} 个）、最大 {cs['summary']['abs_max']:.1f}bp（展示舍入）")
    print(f"  亚洲决策时点误差：{a['asia_decision_error_status']}；界限 {a['asia_decision_error_bound_bp']}；"
          f"决策级 {a['decision_grade']}")
    print(out)
    return 0 if cs["status"] == "OK" else 3


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
