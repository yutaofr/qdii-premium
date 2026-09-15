"""相对比较的不可变输入包与质量对象（FR06 / FR11 / FR12，ADR-009/010/011，纯函数）。

InputBundle 自包含：成员价格、净值、时间、准入判定所需的全部输入，以及差分边界与规则版本。
bundle_id = sha256(规范 JSON)。evaluate_bundle(bundle) 只依赖包内容，不读时钟、不做 I/O，
因此在线计算与离线回放得到相同结果（浮点按 ADR-011 容差比较）。
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, replace
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any

from qdii.core.enav import EnavInput, EnavPolicy, EnavResult, evaluate_enav
from qdii.core.relative import (
    HEALTH_ORDER,
    GroupResult,
    MemberInput,
    PairBound,
    RelativeMode,
    RelativePolicy,
    RelativeStatus,
    evaluate_relative,
)
from qdii.core.types import ReasonCode

SCHEMA_VERSION = 4  # v4（勘误 E8）：盘中估算净值输入（期货中点/昨结算、即期汇率、最近美股收盘）
FLOAT_TOLERANCE = 1e-10


@dataclass(frozen=True, slots=True)
class MemberSpec:
    code: str
    price: str | None  # 十进制字符串，已按 price_basis 取值（QS-04 之后）
    volume: str | None  # 该侧可见数量，仅展示
    quote_time_utc_ns: int | None
    phase_ok: bool
    nav: str | None
    nav_date: str | None  # ISO 日期
    nav_verified: bool
    nav_reasons: tuple[str, ...] = ()
    snapshot_msg_id: str | None = None
    nav_msg_id: str | None = None
    last_price: str | None = None  # 官方净值对照口径（SRD §4）
    index_at_anchor: str | None = None  # I(a)：不晚于净值日的最近 NDX 收盘
    fx_at_anchor: str | None = None  # X0：净值日当日中间价（L0_FX_T）
    anchor_factor_msg_ids: tuple[str, ...] = ()
    index_date: str | None = None  # I(a) 实际对应的美股交易日（由日历确定，F1）
    fx_date: str | None = None  # X0 实际对应的中间价日期
    anchor_sessions: int | None = None  # 该成员净值日之后已完成的 A 股交易日数
    anchor_calendar_covered: bool = True  # 该成员锚点日期在日历覆盖范围内
    futures_mid: str | None = None  # F(t)：按成员报价时刻 as-of 的 hf_NQ 买卖价中点
    futures_settle: str | None = None  # F(c)：同一行情行的昨结算（代理锚点）
    futures_time_utc_ns: int | None = None
    futures_msg_id: str | None = None
    fx_spot: str | None = None  # X(t)：CFETS USD/CNY 即期买卖价中点
    fx_time_utc_ns: int | None = None
    fx_msg_id: str | None = None


@dataclass(frozen=True, slots=True)
class RelativeBundle:
    schema: int
    cutoff_utc_ns: int
    mode: str
    price_basis: str
    policy: tuple[tuple[str, float | str], ...]  # RelativePolicy 全部字段
    calendar_covered: bool  # 截止日在日历覆盖范围内（成员锚点覆盖见 MemberSpec）
    model_status: str  # HISTORICAL_VALIDATED / UNCALIBRATED
    members: tuple[MemberSpec, ...]
    bounds: tuple[tuple[str, str, float, float, str], ...]  # (i, j, daily_p95_bp, rounding_bp, source)，i<j
    versions: tuple[tuple[str, str], ...]
    notes: tuple[str, ...] = ()
    us_close_date: str | None = None  # c：截止前最近已收盘的美股交易日
    us_close_utc_ns: int | None = None
    us_close_index: str | None = None  # I(c)
    us_close_msg_id: str | None = None
    enav_policy: tuple[tuple[str, float | str], ...] = ()


def canonical_json(bundle: RelativeBundle) -> str:
    return json.dumps(asdict(bundle), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def bundle_id(bundle: RelativeBundle) -> str:
    return hashlib.sha256(canonical_json(bundle).encode("utf-8")).hexdigest()


def bundle_from_dict(d: dict[str, Any]) -> RelativeBundle:
    if d.get("schema") != SCHEMA_VERSION:
        raise ValueError(f"unsupported bundle schema {d.get('schema')}")
    return RelativeBundle(
        schema=d["schema"], cutoff_utc_ns=d["cutoff_utc_ns"], mode=d["mode"], price_basis=d["price_basis"],
        policy=tuple((k, v) for k, v in d["policy"]), calendar_covered=d["calendar_covered"],
        us_close_date=d["us_close_date"], us_close_utc_ns=d["us_close_utc_ns"], us_close_index=d["us_close_index"],
        us_close_msg_id=d["us_close_msg_id"], enav_policy=tuple((k, v) for k, v in d["enav_policy"]),
        model_status=d["model_status"],
        members=tuple(MemberSpec(**{**m, "nav_reasons": tuple(m["nav_reasons"]),
                                    "anchor_factor_msg_ids": tuple(m["anchor_factor_msg_ids"])})
                      for m in d["members"]),
        bounds=tuple((b[0], b[1], b[2], b[3], b[4]) for b in d["bounds"]),
        versions=tuple((v[0], v[1]) for v in d["versions"]),
        notes=tuple(d["notes"]),
    )


# ---------- 质量（QS-01~03） ----------

def freshness(age_s: float | None) -> str:
    """QS-02 唯一决策表：含上界。"""
    if age_s is None or not math.isfinite(age_s):
        return "UNKNOWN"
    if age_s <= 15:
        return "CURRENT"
    if age_s <= 60:
        return "RECENT"
    if age_s <= 120:
        return "AGING"
    return "STALE"


_FRESH_ORDER = ("CURRENT", "RECENT", "AGING", "STALE", "UNKNOWN")


@dataclass(frozen=True, slots=True)
class MemberQuality:
    code: str
    age_s: float | None
    freshness: str


@dataclass(frozen=True, slots=True)
class RelativeQuality:
    availability: str
    freshness: str
    provenance_confidence: str
    delay_status: str
    model_status: str
    anchor_health: str
    alignment: str
    max_skew_s: float | None
    max_anchor_sessions: int | None  # 参与比较成员中锚点最旧者的交易日数
    reason_codes: tuple[str, ...]
    members: tuple[MemberQuality, ...]


@dataclass(frozen=True, slots=True)
class RelativeSnapshot:
    bundle_id: str
    result: GroupResult
    quality: RelativeQuality
    enav: tuple[EnavResult, ...] = ()


def evaluate_bundle(bundle: RelativeBundle) -> RelativeSnapshot:
    if bundle.schema != SCHEMA_VERSION:
        raise ValueError(f"unsupported bundle schema {bundle.schema}")
    mode = RelativeMode(bundle.mode)
    members = [
        MemberInput(
            code=m.code,
            price=Decimal(m.price) if m.price is not None else None,
            quote_time_utc_ns=m.quote_time_utc_ns,
            phase_ok=m.phase_ok,
            nav=Decimal(m.nav) if m.nav is not None else None,
            nav_date=date.fromisoformat(m.nav_date) if m.nav_date else None,
            nav_verified=m.nav_verified,
            index_at_anchor=float(m.index_at_anchor) if m.index_at_anchor is not None else None,
            fx_at_anchor=float(m.fx_at_anchor) if m.fx_at_anchor is not None else None,
            extra_reasons=tuple(ReasonCode(r) for r in m.nav_reasons)
            + (() if bundle.calendar_covered and m.anchor_calendar_covered else (ReasonCode.CALENDAR_UNCERTAIN,)),
            last_price=Decimal(m.last_price) if m.last_price is not None else None,
            anchor_sessions=m.anchor_sessions,
        )
        for m in bundle.members
    ]
    bounds = {frozenset((i, j)): PairBound(p95, rnd, src) for i, j, p95, rnd, src in bundle.bounds}
    policy = RelativePolicy(**dict(bundle.policy))
    result = evaluate_relative(members, mode=mode, price_basis=bundle.price_basis,
                               cutoff_utc_ns=bundle.cutoff_utc_ns, policy=policy, bounds=bounds)

    # F5：组级锚点健康只看参与比较的成员
    eligible_members = [m for m in result.members if m.eligible]
    health = (max((m.anchor_health for m in eligible_members), key=HEALTH_ORDER.index)
              if eligible_members else "NOT_APPLICABLE")
    extra: list[ReasonCode] = []
    if not bundle.calendar_covered:
        extra.append(ReasonCode.CALENDAR_UNCERTAIN)
    if health == "AGED":
        extra.append(ReasonCode.ANCHOR_AGED)
    elif health == "EXTENDED":
        extra.append(ReasonCode.ANCHOR_EXTENDED)
    elif health == "INVALID":
        extra.append(ReasonCode.ANCHOR_INVALID)
    if result.pairs and all(p.u_diff is None for p in result.pairs):
        extra.append(ReasonCode.DIFFERENTIAL_UNCALIBRATED)
    if extra:
        result = replace(result, reasons=tuple(dict.fromkeys((*result.reasons, *extra))))

    eligible = {m.code for m in result.members if m.eligible}
    mq = tuple(
        MemberQuality(
            m.code,
            None if m.quote_time_utc_ns is None else max(0.0, (bundle.cutoff_utc_ns - m.quote_time_utc_ns) / 1e9),
            "NOT_APPLICABLE" if mode is RelativeMode.CLOSING_REFERENCE
            else freshness(None if m.quote_time_utc_ns is None
                           else (bundle.cutoff_utc_ns - m.quote_time_utc_ns) / 1e9),
        )
        for m in bundle.members
    )
    elig_times = [m.quote_time_utc_ns for m in bundle.members if m.code in eligible and m.quote_time_utc_ns]
    if mode is RelativeMode.CLOSING_REFERENCE:
        group_fresh = "NOT_APPLICABLE"
    elif eligible:
        group_fresh = max((q.freshness for q in mq if q.code in eligible), key=_FRESH_ORDER.index)
    else:
        group_fresh = "UNKNOWN"
    quality = RelativeQuality(
        availability="AVAILABLE" if result.status is not RelativeStatus.INELIGIBLE else "UNAVAILABLE",
        freshness=group_fresh,
        provenance_confidence="MODEL_ASSUMPTION",  # M0 满仓假设（QS-01）
        delay_status="UNKNOWN",  # 新浪供应商时间尚未盘中验证
        model_status=bundle.model_status,
        anchor_health=health,
        alignment=("ALIGNED" if len(eligible) >= 2 else "NOT_APPLICABLE"),
        max_skew_s=((max(elig_times) - min(elig_times)) / 1e9) if len(elig_times) >= 2 else None,
        max_anchor_sessions=max((m.anchor_sessions for m in eligible_members if m.anchor_sessions is not None),
                                default=None),
        reason_codes=tuple(r.value for r in result.reasons),
        members=mq,
    )
    return RelativeSnapshot(bundle_id(bundle), result, quality, _enav(bundle))


def _num(x: str | None) -> float | None:
    return float(x) if x is not None else None


def _enav(bundle: RelativeBundle) -> tuple[EnavResult, ...]:
    policy = EnavPolicy(**dict(bundle.enav_policy))
    c = date.fromisoformat(bundle.us_close_date) if bundle.us_close_date else None
    return tuple(
        evaluate_enav(EnavInput(
            code=m.code, price=_num(m.price), quote_time_utc_ns=m.quote_time_utc_ns,
            cutoff_utc_ns=bundle.cutoff_utc_ns, current=bundle.mode == RelativeMode.CURRENT.value,
            phase_ok=m.phase_ok, calendar_ok=bundle.calendar_covered and m.anchor_calendar_covered, nav=_num(m.nav),
            nav_usable=m.nav_verified and not m.nav_reasons,
            index_date=date.fromisoformat(m.index_date) if m.index_date else None,
            index_at_anchor=_num(m.index_at_anchor), fx_at_anchor=_num(m.fx_at_anchor),
            us_close_date=c, us_close_utc_ns=bundle.us_close_utc_ns, index_at_close=_num(bundle.us_close_index),
            futures_mid=_num(m.futures_mid), futures_settle=_num(m.futures_settle),
            futures_time_utc_ns=m.futures_time_utc_ns, fx_spot=_num(m.fx_spot), fx_time_utc_ns=m.fx_time_utc_ns,
        ), policy)
        for m in bundle.members
    )


# ---------- 序列化与确定性比较 ----------

def to_plain(obj: Any) -> Any:
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, Decimal | date):
        return str(obj)
    if isinstance(obj, dict):
        return {k: to_plain(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [to_plain(v) for v in obj]
    return obj


def snapshot_to_dict(snap: RelativeSnapshot) -> dict[str, Any]:
    return {"bundle_id": snap.bundle_id, "result": to_plain(asdict(snap.result)),
            "quality": to_plain(asdict(snap.quality)), "enav": to_plain([asdict(x) for x in snap.enav])}


def diff_plain(a: Any, b: Any, path: str = "", tol: float = FLOAT_TOLERANCE) -> list[str]:
    """枚举/字符串/整数精确相等，浮点绝对差 ≤ tol（ADR-011）。返回差异路径列表。"""
    if isinstance(a, float) or isinstance(b, float):
        if isinstance(a, int | float) and isinstance(b, int | float) and abs(a - b) <= tol:
            return []
        return [f"{path}: {a!r} != {b!r}"]
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in sorted(set(a) | set(b)):
            out += diff_plain(a.get(k), b.get(k), f"{path}.{k}", tol)
        return out
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return [f"{path}: len {len(a)} != {len(b)}"]
        out = []
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            out += diff_plain(x, y, f"{path}[{i}]", tol)
        return out
    return [] if a == b else [f"{path}: {a!r} != {b!r}"]
