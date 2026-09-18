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
from collections.abc import Callable, Mapping, Sequence
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


# ---------- 统计：只回答可观测问题 ----------

METRIC_VERSION = "futures-basis-research-2"
ASIA_STATUS = "UNIDENTIFIED"
ANALYSIS_UNKNOWNS = ("asia_decision_error_status", "asia_decision_error_bound_bp", "decision_grade")
QUANTILE_METHOD = "NEAREST_RANK: 升序排列后取第 ceil(p·n) 个（下标 ceil(p·n)−1）；样本分位，不是总体分位的精确估计或上界"
DEFAULT_SEED = 20260918
MIN_BOOTSTRAP_SESSIONS = 30  # 保守的软件展示门槛，不代表统计充分
LIMITATIONS = (
    "亚洲决策时点误差未识别：美股时段端点不约束区间内部；亚洲时段没有指数观测，也没有独立的经济公允价值参照",
    "扩大端点样本或统计量变稳定都不能关闭上一条；本输出不提供任何亚洲时点误差界限",
    "Yahoo 与生产所用新浪源是不同供应商；五分钟线收盘既不是正式指数收盘，也不是结算锚点",
    "分位为 nearest-rank 样本分位，不是总体分位的精确估计或上界",
    "不同期限统计量相近不解释为均值回复；重叠窗口已改为同一开盘起点的不重叠窗口",
    "样本期未覆盖压力行情，结论不外推到压力时段",
)


def estimated_premium_given_error(p_true: float, e: float) -> float:
    """估算净值相对误差 e = V̂/V − 1 时，估算溢价 = (1 + p_true)/(1 + e) − 1。仅用于符号与量级核对。"""
    return (1 + p_true) / (1 + e) - 1


def nearest_rank(values: Sequence[float], p: float) -> dict:
    xs = sorted(values)
    n = len(xs)
    if n == 0:
        return {"method": "NEAREST_RANK", "p": p, "n": 0, "rank": None, "index": None, "value": None}
    rank = max(1, math.ceil(p * n - 1e-12))
    return {"method": "NEAREST_RANK", "p": p, "n": n, "rank": rank, "index": rank - 1, "value": xs[rank - 1]}


def summarize(values: Sequence[float]) -> dict:
    """未舍入的汇总：带符号均值与方差，绝对值的 nearest-rank 中位数/P95 与最大值。"""
    xs = list(values)
    n = len(xs)
    mean = sum(xs) / n if n else None
    var = sum((x - mean) ** 2 for x in xs) / (n - 1) if n > 1 else None  # type: ignore[operator]
    abs_xs = [abs(x) for x in xs]
    return {"n": n, "signed_mean": mean, "signed_variance": var, "abs_median": nearest_rank(abs_xs, 0.5),
            "abs_p95": nearest_rank(abs_xs, 0.95), "abs_max": max(abs_xs) if abs_xs else None}


@dataclass(frozen=True, slots=True)
class Window:
    session_date: date
    hours: int
    offset_min: int  # 起点相对会话开盘的分钟数
    start: BasisObs
    end: BasisObs

    @property
    def basis_drift(self) -> float:
        return self.end.basis - self.start.basis


def _complete(fb: dict[int, PriceRecord], ib: dict[int, PriceRecord], start: int, end: int) -> str | None:
    if start not in fb or start not in ib or end not in fb or end not in ib:
        return "ENDPOINT_BAR_MISSING"
    if any(t not in fb or t not in ib for t in range(start + BAR_S, end, BAR_S)):
        return "INTERIOR_BAR_MISSING"
    return None


def _obs_at(fb: dict[int, PriceRecord], ib: dict[int, PriceRecord], t: int) -> BasisObs:
    f, i = fb[t], ib[t]
    return BasisObs(t, f.close, i.close, i.kind, _ref(f), _ref(i))  # type: ignore[arg-type]


def horizon_windows(fut: Sequence[PriceRecord], idx: Sequence[PriceRecord], sessions: Sequence[CashSession] | None,
                    *, hours: int, as_of_s: float) -> tuple[tuple[Window, ...], tuple[Exclusion, ...]]:
    """每个会话从首根线收盘（开盘 + 5 分钟）起按 h 小时切成不重叠窗口（相邻窗口共享边界点）。
    窗口端点与内部任一五分钟线缺失即排除，不前向填充；网格不因缺口平移。"""
    fb, ib = _bars_by_end(fut), _bars_by_end(idx)
    out, excl = [], []
    for s in sorted(sessions or (), key=lambda s: s.open_s):
        t0, step = s.open_s + BAR_S, hours * 3600
        start = t0
        while start + step <= min(s.close_s, as_of_s):
            end = start + step
            offset = (start - s.open_s) // 60
            reason = _complete(fb, ib, start, end)
            if reason:
                excl.append(Exclusion("WINDOW", f"{s.local_date} open+{offset}m/{hours}h", reason))
            else:
                out.append(Window(s.local_date, hours, offset, _obs_at(fb, ib, start), _obs_at(fb, ib, end)))
            start = end
    return tuple(out), tuple(excl)


def same_start_comparison(fut: Sequence[PriceRecord], idx: Sequence[PriceRecord],
                          sessions: Sequence[CashSession] | None, *, hours: Sequence[int], as_of_s: float) -> dict:
    """同一批会话、同一开盘起点：只保留从首根线到最长期限全程完整的会话，各期限各取一个样本。"""
    fb, ib = _bars_by_end(fut), _bars_by_end(idx)
    longest = max(hours) * 3600
    used, by_h = [], {h: [] for h in hours}
    for s in sorted(sessions or (), key=lambda s: s.open_s):
        t0 = s.open_s + BAR_S
        if t0 + longest > min(s.close_s, as_of_s) or _complete(fb, ib, t0, t0 + longest):
            continue
        used.append(s.local_date.isoformat())
        b0 = _obs_at(fb, ib, t0).basis
        for h in hours:
            by_h[h].append((_obs_at(fb, ib, t0 + h * 3600).basis - b0) * 1e4)
    return {"start": "SESSION_OPEN_PLUS_5M", "sessions": used,
            "by_horizon": {f"{h}h": summarize(v) for h, v in by_h.items()}, "values_bp": {f"{h}h": v for h, v in by_h.items()}}


# ---------- 按交易日整块重采样（确定性，core 不用 random 模块） ----------

_MASK = (1 << 64) - 1


def _splitmix64(state: int) -> tuple[int, int]:
    state = (state + 0x9E3779B97F4A7C15) & _MASK
    z = state
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _MASK
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _MASK
    return state, z ^ (z >> 31)


def resample_session_indices(n: int, *, reps: int, seed: int) -> list[list[int]]:
    """每次有放回地抽 n 个会话下标（拒绝采样，无取模偏差）。同一 seed 结果逐位可复现。"""
    state, limit, out = seed & _MASK, (1 << 64) - ((1 << 64) % n) if n else 0, []
    for _ in range(reps):
        draw = []
        while len(draw) < n:
            state, z = _splitmix64(state)
            if z < limit:
                draw.append(z % n)
        out.append(draw)
    return out


def block_bootstrap_ci(by_key: Mapping[str, Sequence[Sequence[float]]],
                       stat: Callable[[list[float]], float | None], *, reps: int = 2000,
                       seed: int = DEFAULT_SEED, min_sessions: int = MIN_BOOTSTRAP_SESSIONS, level: float = 0.95) -> dict:
    """各键的会话列表须按同一会话顺序对齐，所有键共用同一批抽样（期限比较共享重采样日期）。
    会话数不足 min_sessions 时不给区间（INSUFFICIENT_SESSIONS）。"""
    counts = {len(v) for v in by_key.values()}
    if len(counts) > 1:
        raise ValueError("session lists must be aligned across keys")
    n = counts.pop() if counts else 0
    if n < min_sessions:
        return {k: {"status": "INSUFFICIENT_SESSIONS", "sessions": n, "ci": None} for k in by_key}
    draws = resample_session_indices(n, reps=reps, seed=seed)
    out = {}
    for key, sessions in by_key.items():
        stats = []
        for draw in draws:
            xs = [x for i in draw for x in sessions[i]]
            if xs:
                stats.append(stat(xs))
        tail = (1 - level) / 2
        out[key] = {"status": "OK", "sessions": n, "method": "DAY_BLOCK_PERCENTILE", "reps": reps,
                    "reps_used": len(stats), "seed": seed, "level": level,
                    "ci": [nearest_rank(stats, tail)["value"], nearest_rank(stats, 1 - tail)["value"]]}
    return out


def within_session_acf(levels_by_session: Sequence[Sequence[float | None]], lags: Sequence[int]) -> list[dict]:
    """会话内自相关：输入为按五分钟网格对齐的基差水平（缺线为 None），先减去各会话自身均值；
    只在同一会话内配对，不跨日拼接。常数或配对不足时为 null。"""
    demeaned = []
    for xs in levels_by_session:
        vals = [x for x in xs if x is not None]
        m = sum(vals) / len(vals) if vals else 0.0
        demeaned.append([None if x is None else x - m for x in xs])
    denom = sum(x * x for xs in demeaned for x in xs if x is not None)
    out = []
    for lag in lags:
        num, pairs, used = 0.0, 0, 0
        for xs in demeaned:
            k = 0
            for a, b in zip(xs, xs[lag:], strict=False):
                if a is not None and b is not None:
                    num, k = num + a * b, k + 1
            pairs, used = pairs + k, used + (k > 0)
        acf = num / denom if pairs >= 2 and denom > 1e-18 else None
        out.append({"lag_bars": lag, "lag_minutes": lag * BAR_S // 60, "pairs": pairs, "sessions": used,
                    "input": "BASIS_LEVEL_MINUS_SESSION_MEAN", "acf": acf})
    return out


# ---------- 汇总输出 ----------

def _sample_row(x: CrossSession) -> dict:
    return {"from": x.from_date.isoformat(), "to": x.to_date.isoformat(), "gap": x.gap.value,
            "non_session_weekdays": [d.isoformat() for d in x.non_session_weekdays],
            "early_close_from": x.early_close_from, "start_time_utc_s": x.start.time_s,
            "end_time_utc_s": x.end.time_s, "elapsed_hours": x.elapsed_hours,
            "start_futures": x.start.futures, "start_index": x.start.index, "start_index_kind": x.start.index_kind.value,
            "end_futures": x.end.futures, "end_index": x.end.index,
            "start_basis_bp": x.start.basis * 1e4, "end_basis_bp": x.end.basis * 1e4,
            "basis_drift_bp": x.basis_drift * 1e4, "relative_return_error_bp": x.relative_return_error * 1e4,
            "return_difference_bp": x.return_difference * 1e4,
            "refs": [x.start.futures_ref, x.start.index_ref, x.end.futures_ref, x.end.index_ref]}


def _cross_block(res: CrossSessionResult) -> dict:
    drift = [x.basis_drift * 1e4 for x in res.samples]
    return {
        "rule": res.rule, "status": res.status, "n": len(res.samples),
        "trading_days": len({d for x in res.samples for d in (x.from_date, x.to_date)}),
        "samples": [_sample_row(x) for x in res.samples],
        "values_bp": {"basis_drift": drift,
                      "relative_return_error": [x.relative_return_error * 1e4 for x in res.samples],
                      "return_difference": [x.return_difference * 1e4 for x in res.samples]},
        "summary": summarize(drift),
        "summary_relative_return_error": summarize([x.relative_return_error * 1e4 for x in res.samples]),
        "summary_return_difference": summarize([x.return_difference * 1e4 for x in res.samples]),
        "by_gap": {g.value: {**summarize([x.basis_drift * 1e4 for x in res.samples if x.gap is g]),
                             "elapsed_hours": sorted({x.elapsed_hours for x in res.samples if x.gap is g})}
                   for g in GapKind},
        "exclusions": [{"scope": e.scope, "key": e.key, "reason": e.reason} for e in res.exclusions],
    }


def _record_counts(records: Sequence[PriceRecord]) -> dict:
    out: dict[str, dict[str, int]] = {}
    for r in records:
        by_q = out.setdefault(r.kind.value, {})
        by_q[r.quality.value] = by_q.get(r.quality.value, 0) + 1
    return out


def analyze(fut_raw: RawSeries, idx_raw: RawSeries, sessions: Sequence[CashSession] | None, *, as_of_s: float,
            official: Mapping[date, OfficialClose] | None = None, horizons: Sequence[int] = (1, 2, 3, 6),
            bootstrap_reps: int = 2000, seed: int = DEFAULT_SEED, min_sessions: int = MIN_BOOTSTRAP_SESSIONS,
            acf_lags: Sequence[int] = (1, 6, 12)) -> dict:
    """确定性分析（同输入同输出）。亚洲时点误差状态固定为 UNIDENTIFIED，界限为 null，不随样本升级。"""
    fut = classify(fut_raw, sessions, cash_index=False)
    idx = classify(idx_raw, sessions, cash_index=True)
    idx, verification = verify_official_closes(idx, sessions, official or {})
    main = pair_sessions(fut, idx, sessions, as_of_s=as_of_s)
    diag = pair_sessions(fut, idx, sessions, as_of_s=as_of_s, rule=OFFICIAL_RULE)
    ordered = sorted(sessions or (), key=lambda s: s.open_s)

    by_h, per_session = {}, {}
    for h in horizons:
        ws, excl = horizon_windows(fut, idx, sessions, hours=h, as_of_s=as_of_s)
        offsets: dict[str, int] = {}
        for w in ws:
            offsets[str(w.offset_min)] = offsets.get(str(w.offset_min), 0) + 1
        by_h[f"{h}h"] = {"summary": summarize([w.basis_drift * 1e4 for w in ws]), "windows": len(ws),
                         "sessions": len({w.session_date for w in ws}), "start_offsets_min": offsets,
                         "excluded_windows": len(excl),
                         "exclusions": [{"key": e.key, "reason": e.reason} for e in excl]}
        grouped: dict[date, list[float]] = {}
        for w in ws:
            grouped.setdefault(w.session_date, []).append(w.basis_drift * 1e4)
        per_session[f"{h}h"] = grouped
    # 只用至少有一个完整窗口的会话；各期限按同一会话顺序对齐，共用同一批抽样
    valid = [d for d in (s.local_date for s in ordered) if any(d in g for g in per_session.values())]
    aligned = {k: [g.get(d, []) for d in valid] for k, g in per_session.items()}
    boot_kw = {"reps": bootstrap_reps, "seed": seed, "min_sessions": min_sessions}
    bootstrap = {
        "unit": "TRADING_SESSION", "shared_draws_across_horizons": True, "valid_sessions": len(valid),
        "min_sessions": min_sessions,
        "abs_p95": block_bootstrap_ci(aligned, lambda xs: nearest_rank([abs(x) for x in xs], 0.95)["value"],
                                      **boot_kw),
        "signed_mean": block_bootstrap_ci(aligned, lambda xs: sum(xs) / len(xs), **boot_kw),
        "note": ("会话数低于门槛时不给区间；门槛只是软件展示条件，不代表统计充分。"
                 "区间即使给出，也只描述样本内重采样波动，不是亚洲时点误差界限"),
    }

    fb, ib = _bars_by_end(fut), _bars_by_end(idx)
    levels = []
    for s in ordered:
        grid = range(s.open_s + BAR_S, min(s.close_s, int(as_of_s)) + 1, BAR_S)
        levels.append([(_obs_at(fb, ib, t).basis * 1e4 if t in fb and t in ib else None) for t in grid])

    return {
        "metric_version": METRIC_VERSION,
        "pairing_rule": PAIRING_RULE,
        "time_semantics": {
            "bar_label": "INTERVAL_START（依据：NDX 每个会话首条标签为开盘时刻、最后一条普通线标签为收盘前 5 分钟）",
            "observation_time": "INTERVAL_END",
            "close_endpoint": "最后一根完整五分钟线 [收盘−5 分钟, 收盘] 的 close；NDX 普通线 close 不等于正式收盘值",
            "open_endpoint": "首根五分钟线 [开盘, 开盘+5 分钟] 的 close",
            "index_close_instant_record": "SESSION_CLOSE_RECORD，不进主序列；与独立官方收盘在公布精度内一致才标 OFFICIAL_CLOSE，仅作诊断",
        },
        "quantile_method": QUANTILE_METHOD,
        "record_counts": {fut_raw.instrument: _record_counts(fut), idx_raw.instrument: _record_counts(idx)},
        "sessions": {"calendar_status": "UNCERTAIN" if sessions is None else "COVERED", "expected": len(ordered),
                     "with_close_endpoint": sum(1 for e in main.endpoints if e.close is not None),
                     "with_open_endpoint": sum(1 for e in main.endpoints if e.open is not None),
                     "session_close_basis": [
                         {"date": e.session.local_date.isoformat(), "early_close": e.session.early_close,
                          "bar_close_basis_bp": None if e.close is None else e.close.basis * 1e4,
                          "bar_close_futures": None if e.close is None else e.close.futures,
                          "bar_close_index": None if e.close is None else e.close.index,
                          "official_close_basis_bp": None if e.official_close is None else e.official_close.basis * 1e4,
                          "official_close_index": None if e.official_close is None else e.official_close.index}
                         for e in main.endpoints]},
        "cross_session": _cross_block(main),
        "official_close_diagnostic": {**_cross_block(diag), "verification": list(verification)},
        "intraday": {
            "window_rule": "每个会话从首根线收盘起按期限切成不重叠窗口，相邻窗口共享边界点；任一内部线缺失即排除，不前向填充",
            "by_horizon": by_h,
            "same_start_comparison": same_start_comparison(fut, idx, sessions, hours=horizons, as_of_s=as_of_s),
            "bootstrap": bootstrap,
        },
        "acf": {"note": "会话内、去日均值的基差水平；相关性不自动解释为均值回复",
                "lags": within_session_acf(levels, acf_lags)},
        "asia_decision_error_status": ASIA_STATUS,
        "asia_decision_error_bound_bp": None,
        "decision_grade": False,
        "limitations": list(LIMITATIONS),
    }
