"""VM-10 相对比较（纯函数）：组级排名与成对相对价差，独立于绝对估值。

  B_i = N0_i / (I(a_i) · X0_i)，S_i = P_i / B_i，R_ij = S_i / S_j，δ_ij = R_ij − 1。
所有合格成员净值日期相同时，I(a)·X0 为公共因子，B_i 直接取 N0_i（不需要指数与汇率数据）。
不同日期时必须提供每只基金的 I(a_i) 与 X0_i，否则该成员退出比较（不猜）。

差分误差：只接受调用方提供、带来源的成对情景边界 u_diff（ln 单位）；
没有边界时 relative_status=MODEL_REFERENCE，不授予 ROBUST_DIFFERENCE。
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum

from qdii.core.types import ReasonCode


class RelativeMode(StrEnum):
    CURRENT = "CURRENT"  # 连续交易中的当前比较
    CLOSING_REFERENCE = "CLOSING_REFERENCE"  # 午休/收盘后：以最后快照为输入的参考（勘误 E1）


class RelativeStatus(StrEnum):  # QS-01 relative_status
    MODEL_REFERENCE = "MODEL_REFERENCE"
    ROBUST_DIFFERENCE = "ROBUST_DIFFERENCE"
    UNRESOLVED = "UNRESOLVED"
    INELIGIBLE = "INELIGIBLE"


@dataclass(frozen=True, slots=True)
class MemberInput:
    code: str
    price: Decimal | None  # 已按 price_basis 取好的有效价格（QS-04 之后）
    quote_time_utc_ns: int | None  # 供应商快照时间
    phase_ok: bool  # 调用方按日历与模式判定：快照所在阶段是否允许用于本模式
    nav: Decimal | None
    nav_date: date | None
    nav_verified: bool
    index_at_anchor: float | None = None
    fx_at_anchor: float | None = None
    nonseparable_event: bool = False
    extra_reasons: tuple[ReasonCode, ...] = ()


@dataclass(frozen=True, slots=True)
class RelativePolicy:
    version: str = "QPOL-1.0+E1"
    max_age_s: float = 60.0  # QS-02 RELATIVE_REFERENCE
    max_span_s: float = 60.0


@dataclass(frozen=True, slots=True)
class PairBound:
    u_diff: float  # ln 单位
    source: str


@dataclass(frozen=True, slots=True)
class MemberResult:
    code: str
    eligible: bool
    reasons: tuple[ReasonCode, ...]
    price: Decimal | None
    nav: Decimal | None
    nav_date: date | None
    nav_premium: float | None  # P / N0 − 1（官方净值对照，非估算溢价）
    s: float | None
    rank: int | None


@dataclass(frozen=True, slots=True)
class PairResult:
    i: str
    j: str
    ratio: float
    delta: float
    ln_ratio: float
    u_diff: float | None
    bound_source: str | None
    status: RelativeStatus
    reasons: tuple[ReasonCode, ...]


@dataclass(frozen=True, slots=True)
class GroupResult:
    mode: RelativeMode
    price_basis: str
    cutoff_utc_ns: int
    tau_utc_ns: int | None
    common_anchor: bool
    status: RelativeStatus
    members: tuple[MemberResult, ...]
    pairs: tuple[PairResult, ...]
    reasons: tuple[ReasonCode, ...] = ()
    policy_version: str = ""
    opportunity_alert_allowed: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)


def _positive(x: Decimal | float | None) -> bool:
    return x is not None and math.isfinite(float(x)) and float(x) > 0


def evaluate_relative(
    members: Sequence[MemberInput],
    *,
    mode: RelativeMode,
    price_basis: str,
    cutoff_utc_ns: int,
    policy: RelativePolicy | None = None,
    bounds: Mapping[frozenset[str], PairBound] | None = None,
) -> GroupResult:
    bounds = bounds or {}
    policy = policy or RelativePolicy()
    reasons: dict[str, list[ReasonCode]] = {m.code: list(m.extra_reasons) for m in members}

    # 1) 成员自身准入
    for m in members:
        r = reasons[m.code]
        if not _positive(m.price):
            r.append(ReasonCode.QUOTE_MISSING)
        if not _positive(m.nav) or m.nav_date is None:
            r.append(ReasonCode.NAV_MISSING)
        elif not m.nav_verified:
            r.append(ReasonCode.PENDING_VERIFY)
        if m.nonseparable_event:
            r.append(ReasonCode.NONSEPARABLE_EVENT)
        if not m.phase_ok:
            r.append(ReasonCode.SESSION_BOUNDARY)
        if m.quote_time_utc_ns is None:
            r.append(ReasonCode.TIME_UNVERIFIED)
        elif mode is RelativeMode.CURRENT and (cutoff_utc_ns - m.quote_time_utc_ns) / 1e9 > policy.max_age_s:
            r.append(ReasonCode.TIME_SKEW)
        elif m.quote_time_utc_ns - cutoff_utc_ns > 2e9:
            r.append(ReasonCode.FUTURE_TIMESTAMP)

    hard = {ReasonCode.QUOTE_MISSING, ReasonCode.NAV_MISSING, ReasonCode.PENDING_VERIFY, ReasonCode.NONSEPARABLE_EVENT,
            ReasonCode.SESSION_BOUNDARY, ReasonCode.TIME_UNVERIFIED, ReasonCode.TIME_SKEW, ReasonCode.FUTURE_TIMESTAMP,
            ReasonCode.NAV_FX_RULE_UNKNOWN, ReasonCode.CORPORATE_ACTION_PENDING, ReasonCode.FACTOR_GROUP_MISMATCH}  # QS-01：X₀ 未知时默认相对比较不可用
    ok = [m for m in members if not (set(reasons[m.code]) & hard)]

    # 2) 共同时点 τ 与跨度（QS-02）：以最新快照为 τ，跨度超限者退出
    tau = max((m.quote_time_utc_ns for m in ok), default=None)
    if tau is not None:
        for m in list(ok):
            if (tau - m.quote_time_utc_ns) / 1e9 > policy.max_span_s:  # type: ignore[operator]
                reasons[m.code].append(ReasonCode.TIME_SKEW)
                ok.remove(m)

    # 3) 锚点：共同净值日期则 B=N0；否则需要各自 I(a)·X0
    common_anchor = len({m.nav_date for m in ok}) <= 1
    b: dict[str, float] = {}
    for m in list(ok):
        if common_anchor:
            b[m.code] = float(m.nav)  # type: ignore[arg-type]
        elif _positive(m.index_at_anchor) and _positive(m.fx_at_anchor):
            b[m.code] = float(m.nav) / (m.index_at_anchor * m.fx_at_anchor)  # type: ignore[operator, arg-type]
        else:
            reasons[m.code].append(ReasonCode.NAV_FX_RULE_UNKNOWN)
            ok.remove(m)

    s = {m.code: float(m.price) / b[m.code] for m in ok}  # type: ignore[arg-type]
    order = sorted(s, key=lambda c: (s[c], c))
    rank = {c: k + 1 for k, c in enumerate(order)}

    member_results = tuple(
        MemberResult(
            code=m.code,
            eligible=m.code in s,
            reasons=tuple(dict.fromkeys(reasons[m.code])),
            price=m.price,
            nav=m.nav,
            nav_date=m.nav_date,
            nav_premium=(float(m.price) / float(m.nav) - 1) if _positive(m.price) and _positive(m.nav) else None,
            s=s.get(m.code),
            rank=rank.get(m.code),
        )
        for m in sorted(members, key=lambda m: (rank.get(m.code, 10**6), m.code))
    )

    pairs = []
    for i, j in itertools.combinations(order, 2):
        ratio = s[i] / s[j]
        ln_ratio = math.log(ratio)
        bound = bounds.get(frozenset((i, j)))
        if bound is None:
            status, pr = RelativeStatus.MODEL_REFERENCE, (ReasonCode.DIFFERENTIAL_UNCALIBRATED,)
        elif abs(ln_ratio) > bound.u_diff:
            status, pr = RelativeStatus.ROBUST_DIFFERENCE, ()
        else:
            status, pr = RelativeStatus.UNRESOLVED, (ReasonCode.DIFFERENCE_UNRESOLVED,)
        pairs.append(PairResult(i, j, ratio, ratio - 1, ln_ratio, bound.u_diff if bound else None,
                                bound.source if bound else None, status, pr))

    group_status = RelativeStatus.MODEL_REFERENCE if len(ok) >= 2 else RelativeStatus.INELIGIBLE
    group_reasons = (ReasonCode.FULL_EXPOSURE_ASSUMPTION,) if len(ok) >= 2 else ()
    return GroupResult(
        mode=mode, price_basis=price_basis, cutoff_utc_ns=cutoff_utc_ns, tau_utc_ns=tau if ok else None,
        common_anchor=common_anchor, status=group_status, members=member_results, pairs=tuple(pairs),
        reasons=group_reasons, policy_version=policy.version,
        opportunity_alert_allowed=False,  # 首版关闭价格机会通知（FR16）
    )
