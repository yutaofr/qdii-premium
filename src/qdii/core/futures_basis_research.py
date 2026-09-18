"""期货基差探索性研究（勘误 E8 遗留项，2026-09-18 复审整改）。纯函数，不做 I/O、不读时钟。

只回答美股现金时段看得到的问题：同合约期货五分钟线与 NDX 五分钟线在会话端点、会话内的基差变化。
**不测量亚洲决策时点的误差**：亚洲时段没有指数观测，也没有独立的经济公允价值参照；端点统计不约束区间内部。

时间语义（依据：原始响应中 NDX 每个会话首条记录标签为 09:30、最后一条普通记录标签为 15:55，符合"标签 = 区间起点"）：
- 普通五分钟线：供应商时间戳为区间起点，区间 [ts, ts+300]，观测时刻取区间终点；
- 指数恰在现金收盘时刻的额外记录：SESSION_CLOSE_RECORD，类型未核实，不进主序列；
  只有与独立官方收盘在其公布精度内一致时才标 OFFICIAL_CLOSE，且只用于单列诊断；
- 不在五分钟网格上、不在现金会话内的指数记录：UNKNOWN；接收时区间尚未结束：PARTIAL_BAR。
不按价格远近推断记录类型。
"""

from __future__ import annotations

import bisect
import itertools
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum

BAR_S = 300
PAIRING_RULE = "BAR_CLOSE_TO_FIRST_BAR_CLOSE"  # 前会话最后完整五分钟线收盘 → 下一会话首根五分钟线收盘
OFFICIAL_RULE = "OFFICIAL_CLOSE_TO_FIRST_BAR_CLOSE"  # 诊断：已核实的正式收盘（配结束于收盘的期货线）→ 首根线


class PriceKind(StrEnum):
    BAR_5M = "BAR_5M"
    SESSION_CLOSE_RECORD = "SESSION_CLOSE_RECORD"
    OFFICIAL_CLOSE = "OFFICIAL_CLOSE"
    PARTIAL_BAR = "PARTIAL_BAR"
    UNKNOWN = "UNKNOWN"


class Quality(StrEnum):
    OK = "OK"
    MISSING = "MISSING"
    NON_NUMERIC = "NON_NUMERIC"
    NON_FINITE = "NON_FINITE"
    NON_POSITIVE = "NON_POSITIVE"
    DUPLICATE_IDENTICAL = "DUPLICATE_IDENTICAL"  # 与同时间戳的第一条完全相同，只保留第一条
    DUPLICATE_CONFLICT = "DUPLICATE_CONFLICT"  # 同时间戳内容不同，全部排除


class GapKind(StrEnum):
    NORMAL = "NORMAL"
    WEEKEND = "WEEKEND"
    HOLIDAY_EXTENDED = "HOLIDAY_EXTENDED"  # 两个相邻预期会话之间有非交易的工作日；优先于周末


class ChartError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


# ---------- 原始序列与记录分类 ----------

@dataclass(frozen=True, slots=True)
class RawSeries:
    instrument: str
    timestamps: tuple[int, ...]
    closes: tuple[object, ...]  # 原样保留：数值、None、异常值都交给 classify 判定
    source_ref: str  # 原始消息 msg_id
    received_s: float  # 响应接收时刻（UTC 秒）


def parse_chart(body: bytes, *, source_ref: str, received_s: float) -> RawSeries:
    """Yahoo chart 响应 → 原始列。结构不符一律抛 ChartError（带 code），不猜测。"""
    try:
        doc = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ChartError("JSON_INVALID", str(exc)) from exc
    try:
        r = doc["chart"]["result"][0]
        ts, closes, symbol = r["timestamp"], r["indicators"]["quote"][0]["close"], r["meta"]["symbol"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ChartError("NO_RESULT", repr(exc)) from exc
    if not isinstance(ts, list) or not isinstance(closes, list):
        raise ChartError("NO_RESULT", "timestamp/close not lists")
    if len(ts) != len(closes):
        raise ChartError("LENGTH_MISMATCH", f"{len(ts)} timestamps vs {len(closes)} closes")
    if any(isinstance(t, bool) or not isinstance(t, int) for t in ts):
        raise ChartError("BAD_TIMESTAMP", "non-integer timestamp")
    return RawSeries(str(symbol), tuple(ts), tuple(closes), source_ref, received_s)


@dataclass(frozen=True, slots=True)
class CashSession:
    local_date: date
    open_s: int
    close_s: int
    early_close: bool = False


@dataclass(frozen=True, slots=True)
class PriceRecord:
    instrument: str
    raw_ts: int
    interval_start: int | None
    interval_end: int | None
    kind: PriceKind
    close: float | None
    quality: Quality
    source_ref: str
    session_date: date | None = None  # 记录所在的现金会话（区间完全落在会话内时）


def _quality(v: object) -> tuple[Quality, float | None]:
    if v is None:
        return Quality.MISSING, None
    if isinstance(v, bool) or not isinstance(v, int | float):
        return Quality.NON_NUMERIC, None
    x = float(v)
    if not math.isfinite(x):
        return Quality.NON_FINITE, None
    if x <= 0:
        return Quality.NON_POSITIVE, None
    return Quality.OK, x


class _Sessions:
    def __init__(self, sessions: Sequence[CashSession] | None) -> None:
        self.items = sorted(sessions or (), key=lambda s: s.open_s)
        self.opens = [s.open_s for s in self.items]

    def containing(self, t: int) -> CashSession | None:
        i = bisect.bisect_right(self.opens, t) - 1
        return self.items[i] if i >= 0 and t <= self.items[i].close_s else None


def classify(series: RawSeries, sessions: Sequence[CashSession] | None, *, cash_index: bool) -> tuple[PriceRecord, ...]:
    """按时间结构判定每条记录的类型与质量。cash_index=True 表示只在现金会话内有普通线的指数。"""
    cal = _Sessions(sessions)
    out: list[PriceRecord] = []
    for ts, raw in zip(series.timestamps, series.closes, strict=True):
        quality, close = _quality(raw)
        kind, start, end, sess = PriceKind.UNKNOWN, None, None, None
        s = cal.containing(ts)
        if ts % BAR_S == 0 and ts <= series.received_s:
            if cash_index and s is not None and ts == s.close_s:
                kind, start, end, sess = PriceKind.SESSION_CLOSE_RECORD, ts, ts, s.local_date
            elif cash_index and (s is None or ts + BAR_S > s.close_s):
                pass  # 会话外的指数记录，语义无法确认
            else:
                start, end = ts, ts + BAR_S
                kind = PriceKind.PARTIAL_BAR if end > series.received_s else PriceKind.BAR_5M
                sess = s.local_date if s is not None and end <= s.close_s else None
        out.append(PriceRecord(series.instrument, ts, start, end, kind, close, quality, series.source_ref, sess))
    return _mark_duplicates(out)


def _mark_duplicates(records: list[PriceRecord]) -> tuple[PriceRecord, ...]:
    groups: dict[int, list[int]] = {}
    for i, r in enumerate(records):
        groups.setdefault(r.raw_ts, []).append(i)
    for idxs in groups.values():
        if len(idxs) < 2:
            continue
        first = records[idxs[0]]
        same = all(records[i].quality is first.quality and records[i].close == first.close for i in idxs[1:])
        for n, i in enumerate(idxs):
            if not same:
                records[i] = replace(records[i], quality=Quality.DUPLICATE_CONFLICT)
            elif n:
                records[i] = replace(records[i], quality=Quality.DUPLICATE_IDENTICAL)
    return tuple(records)


@dataclass(frozen=True, slots=True)
class OfficialClose:
    session_date: date
    value: str  # 官方公布的十进制字符串，如 "29446.98"
    source_ref: str


def verify_official_closes(records: Sequence[PriceRecord], sessions: Sequence[CashSession] | None,
                           official: Mapping[date, OfficialClose]) -> tuple[tuple[PriceRecord, ...], tuple[dict, ...]]:
    """收盘时刻的额外记录与独立官方收盘在公布精度内（±半个最小单位）一致时，才标 OFFICIAL_CLOSE。"""
    by_close = {s.close_s: s for s in sessions or ()}
    out, log = [], []
    for r in records:
        s = by_close.get(r.raw_ts)
        if r.kind is PriceKind.SESSION_CLOSE_RECORD and r.quality is Quality.OK and s is not None:
            o = official.get(s.local_date)
            entry = {"session_date": s.local_date.isoformat(), "record_value": r.close, "record_ref": r.source_ref}
            if o is None:
                entry["status"] = "NO_OFFICIAL_SOURCE"
            else:
                ref = Decimal(o.value)
                tol = Decimal(1).scaleb(ref.as_tuple().exponent) / 2
                ok = abs(Decimal(repr(r.close)) - ref) <= tol
                entry.update(official_value=o.value, official_ref=o.source_ref, tolerance=str(tol),
                             status="VERIFIED" if ok else "MISMATCH")
                if ok:
                    r = replace(r, kind=PriceKind.OFFICIAL_CLOSE)
            log.append(entry)
        out.append(r)
    return tuple(out), tuple(log)


# ---------- 端点与跨会话样本 ----------

@dataclass(frozen=True, slots=True)
class BasisObs:
    time_s: int  # 观测时刻：普通线取区间终点，正式收盘取收盘时刻
    futures: float
    index: float
    index_kind: PriceKind
    futures_ref: str
    index_ref: str

    @property
    def basis(self) -> float:
        return self.futures / self.index - 1


@dataclass(frozen=True, slots=True)
class SessionEndpoints:
    session: CashSession
    open: BasisObs | None  # 首根五分钟线收盘（区间终点 = 开盘 + 5 分钟）
    close: BasisObs | None  # 最后一根完整五分钟线收盘（区间终点 = 现金收盘）
    official_close: BasisObs | None  # 诊断：结束于收盘的期货线 × 已核实的正式收盘
    reasons: tuple[str, ...]  # 主序列（普通线）缺口
    official_reasons: tuple[str, ...]  # 诊断口径缺口


def _ref(r: PriceRecord) -> str:
    return f"{r.source_ref}@{r.raw_ts}"


def _bars_by_end(records: Sequence[PriceRecord]) -> dict[int, PriceRecord]:
    return {r.interval_end: r for r in records
            if r.kind is PriceKind.BAR_5M and r.quality is Quality.OK and r.interval_end is not None}


def _obs(f: PriceRecord | None, i: PriceRecord | None, t: int, which: str, reasons: list[str]) -> BasisObs | None:
    if f is None:
        reasons.append(f"FUTURES_{which}_BAR_MISSING")
    if i is None:
        reasons.append(f"INDEX_{which}_BAR_MISSING")
    if f is None or i is None:
        return None
    return BasisObs(t, f.close, i.close, i.kind, _ref(f), _ref(i))  # type: ignore[arg-type]


def _endpoints(fb: dict[int, PriceRecord], ib: dict[int, PriceRecord], idx_close: dict[int, PriceRecord],
               s: CashSession, as_of_s: float) -> SessionEndpoints:
    reasons: list[str] = []
    official_reasons: list[str] = []
    first_end = s.open_s + BAR_S
    open_obs = close_obs = official = None
    if s.open_s >= as_of_s:
        reasons.append("SESSION_NOT_STARTED")
    elif first_end > as_of_s:
        reasons.append("FIRST_BAR_INCOMPLETE_AT_FETCH")
    else:
        open_obs = _obs(fb.get(first_end), ib.get(first_end), first_end, "FIRST", reasons)
    if s.open_s < as_of_s < s.close_s:
        reasons.append("SESSION_INCOMPLETE_AT_FETCH")
    elif s.close_s <= as_of_s:
        close_obs = _obs(fb.get(s.close_s), ib.get(s.close_s), s.close_s, "LAST", reasons)
        rec, f = idx_close.get(s.close_s), fb.get(s.close_s)
        if rec is None:
            official_reasons.append("INDEX_OFFICIAL_CLOSE_MISSING")
        elif rec.kind is not PriceKind.OFFICIAL_CLOSE:
            official_reasons.append("INDEX_CLOSE_RECORD_UNVERIFIED")
        elif f is None:
            official_reasons.append("FUTURES_LAST_BAR_MISSING")
        else:
            official = BasisObs(s.close_s, f.close, rec.close, rec.kind, _ref(f), _ref(rec))  # type: ignore[arg-type]
    return SessionEndpoints(s, open_obs, close_obs, official, tuple(reasons), tuple(official_reasons))


@dataclass(frozen=True, slots=True)
class CrossSession:
    rule: str
    from_date: date
    to_date: date
    start: BasisObs
    end: BasisObs
    gap: GapKind
    non_session_weekdays: tuple[date, ...]
    early_close_from: bool

    @property
    def elapsed_hours(self) -> float:
        return (self.end.time_s - self.start.time_s) / 3600

    @property
    def basis_drift(self) -> float:
        return self.end.basis - self.start.basis

    @property
    def relative_return_error(self) -> float:  # R_F / R_I − 1 = (b1 − b0) / (1 + b0)
        return (self.end.futures / self.start.futures) / (self.end.index / self.start.index) - 1

    @property
    def return_difference(self) -> float:  # R_F − R_I = R_I (b1 − b0) / (1 + b0)
        return self.end.futures / self.start.futures - self.end.index / self.start.index


@dataclass(frozen=True, slots=True)
class Exclusion:
    scope: str  # CALENDAR / SESSION / CROSS_SESSION / WINDOW
    key: str
    reason: str


@dataclass(frozen=True, slots=True)
class CrossSessionResult:
    rule: str
    status: str  # OK / NO_DATA / CALENDAR_UNCERTAIN
    endpoints: tuple[SessionEndpoints, ...]
    samples: tuple[CrossSession, ...]
    exclusions: tuple[Exclusion, ...]


def gap_kind(prev: date, nxt: date) -> tuple[GapKind, tuple[date, ...]]:
    """相邻预期会话之间的日历日：有非交易工作日即为假日延长（优先于周末）。预期会话本身来自交易日历。"""
    between = [prev + timedelta(days=k) for k in range(1, (nxt - prev).days)]
    weekdays = tuple(d for d in between if d.weekday() < 5)
    if weekdays:
        return GapKind.HOLIDAY_EXTENDED, weekdays
    return (GapKind.WEEKEND if between else GapKind.NORMAL), ()


def session_endpoints(fut: Sequence[PriceRecord], idx: Sequence[PriceRecord], sessions: Sequence[CashSession],
                      as_of_s: float) -> tuple[SessionEndpoints, ...]:
    fb, ib = _bars_by_end(fut), _bars_by_end(idx)
    idx_close = {r.raw_ts: r for r in idx if r.quality is Quality.OK
                 and r.kind in (PriceKind.SESSION_CLOSE_RECORD, PriceKind.OFFICIAL_CLOSE)}
    return tuple(_endpoints(fb, ib, idx_close, s, as_of_s) for s in sorted(sessions, key=lambda s: s.open_s))


def pair_sessions(fut: Sequence[PriceRecord], idx: Sequence[PriceRecord], sessions: Sequence[CashSession] | None,
                  *, as_of_s: float, rule: str = PAIRING_RULE) -> CrossSessionResult:
    """只在相邻的预期交易会话之间、两端端点齐全时构建跨会话样本；不跨越缺数的会话。"""
    if rule not in (PAIRING_RULE, OFFICIAL_RULE):
        raise ValueError(f"unknown pairing rule {rule}")
    if sessions is None:
        return CrossSessionResult(rule, "CALENDAR_UNCERTAIN", (), (), (Exclusion("CALENDAR", "*", "CALENDAR_UNCERTAIN"),))
    endpoints = session_endpoints(fut, idx, sessions, as_of_s)
    official = rule == OFFICIAL_RULE
    exclusions = [Exclusion("SESSION", e.session.local_date.isoformat(), r)
                  for e in endpoints for r in e.reasons + (e.official_reasons if official else ())]
    samples = []
    for a, b in itertools.pairwise(endpoints):
        key = f"{a.session.local_date}→{b.session.local_date}"
        start = a.official_close if official else a.close
        missing = []
        if start is None:
            missing.append("PREV_SESSION_INCOMPLETE_AT_FETCH" if "SESSION_INCOMPLETE_AT_FETCH" in a.reasons
                           else "PREV_SESSION_OFFICIAL_CLOSE_MISSING" if official else "PREV_SESSION_LAST_BAR_MISSING")
        if b.open is None:
            missing.append("NEXT_SESSION_NOT_STARTED" if "SESSION_NOT_STARTED" in b.reasons
                           else "NEXT_SESSION_FIRST_BAR_MISSING")
        if missing:
            exclusions += [Exclusion("CROSS_SESSION", key, m) for m in missing]
            continue
        gap, weekdays = gap_kind(a.session.local_date, b.session.local_date)
        samples.append(CrossSession(rule, a.session.local_date, b.session.local_date, start, b.open,  # type: ignore[arg-type]
                                    gap, weekdays, a.session.early_close))
    return CrossSessionResult(rule, "OK" if samples else "NO_DATA", endpoints, tuple(samples), tuple(exclusions))
