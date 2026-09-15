"""VM-10 相对比较（纯函数）：组级排名与成对相对价差，独立于绝对估值。

  B_i = N0_i / (I(a_i) · X0_i)，S_i = P_i / B_i，R_ij = S_i / S_j，δ_ij = R_ij − 1。
锚点（净值日期）选择（评审 R4）：
- 合格成员净值日期全部相同：I(a)·X0 为公共因子，B_i = N0_i；
- 日期不同：有跨日归一化因子（I(a_i)、X0_i）的成员用完整公式；没有因子时，取最大的同日期子集比较，
  其余成员以 NAV_FX_RULE_UNKNOWN 退出。单只基金先披露新净值不得阻断其他基金。

差分边界（评审 R1、二审 F5，VM-10/VM-11）：
  u_ij = 日终历史差分 P95 × √max(1, k_i, k_j) + 报价错位情景项 + 净值舍入项
  k 为该成员自身锚点（净值日）之后已完成的 A 股交易日数；只由参与该对比较的两只基金决定，
  被剔除成员的旧锚点不影响其他成员（F5）。任一成员锚点 EXTENDED / 未知时该对不授予强结论。
  错位情景项 = z · σ年 · √((|t_i − t_j| + 时间分辨率) / 年化秒数)，按 VM-11 的波动集中假设取保守值。
- 没有边界：MODEL_REFERENCE（DIFFERENTIAL_UNCALIBRATED）；
- 报价错位超过 robust_max_skew_s：MODEL_REFERENCE（TIME_SKEW），不授予 ROBUST_DIFFERENCE；
- |ln R| > u：ROBUST_DIFFERENCE（含义仅为“超出已声明的情景边界”）；否则 UNRESOLVED。
"""

from __future__ import annotations

import itertools
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum

from qdii.core.types import ReasonCode


class RelativeMode(StrEnum):
    CURRENT = "CURRENT"  # 连续交易中的当前比较
    CLOSING_REFERENCE = "CLOSING_REFERENCE"  # 午休/收盘后：以冻结快照为输入的参考（勘误 E1）


class RelativeStatus(StrEnum):  # QS-01 relative_status
    MODEL_REFERENCE = "MODEL_REFERENCE"
    ROBUST_DIFFERENCE = "ROBUST_DIFFERENCE"
    UNRESOLVED = "UNRESOLVED"
    INELIGIBLE = "INELIGIBLE"


@dataclass(frozen=True, slots=True)
class MemberInput:
    code: str
    price: Decimal | None  # 已按 price_basis 取好的有效价格（QS-04 之后）
    quote_time_utc_ns: int | None  # 该成员行情的供应商快照时间
    phase_ok: bool
    nav: Decimal | None
    nav_date: date | None
    nav_verified: bool
    index_at_anchor: float | None = None  # I(a_i)：跨日归一化因子
    fx_at_anchor: float | None = None  # X0_i
    nonseparable_event: bool = False
    extra_reasons: tuple[ReasonCode, ...] = ()
    last_price: Decimal | None = None  # 官方净值对照固定用最新价（SRD §4），与比较口径分开
    anchor_sessions: int | None = None  # 该成员净值日之后已完成的 A 股交易日数（None = 日历无法确定）


@dataclass(frozen=True, slots=True)
class RelativePolicy:
    version: str = "QPOL-1.1-MVP"
    max_age_s: float = 60.0  # QS-02 RELATIVE_REFERENCE
    max_span_s: float = 60.0
    robust_max_skew_s: float = 15.0  # 报价错位超过该值不授予 ROBUST_DIFFERENCE
    skew_sigma_annual: float = 0.20  # VM-11 情景假设
    skew_z: float = 2.0
    skew_basis_seconds: float = 252 * 6.5 * 3600  # 波动集中于 6.5 小时的保守折算
    time_resolution_s: float = 1.0  # 新浪供应商时间为秒级


@dataclass(frozen=True, slots=True)
class PairBound:
    daily_p95_bp: float  # 日终历史成对差分 P95（单日，未放大）
    rounding_bp: float
    source: str


def anchor_health(sessions: int | None) -> str:
    """QS-03：锚点后已完成交易时段数 0—3 NORMAL，4—10 AGED，>10 EXTENDED；无法确定为 INVALID。"""
    if sessions is None or sessions < 0:
        return "INVALID"
    if sessions <= 3:
        return "NORMAL"
    if sessions <= 10:
        return "AGED"
    return "EXTENDED"


HEALTH_ORDER = ("NORMAL", "AGED", "EXTENDED", "INVALID")


@dataclass(frozen=True, slots=True)
class MemberResult:
    code: str
    eligible: bool
    reasons: tuple[ReasonCode, ...]
    price: Decimal | None
    nav: Decimal | None
    nav_date: date | None
    nav_premium: float | None  # 最新价 / 已披露单位净值 − 1（历史对照，非估算溢价）
    s: float | None
    rank: int | None
    anchor_sessions: int | None = None
    anchor_health: str = "INVALID"


@dataclass(frozen=True, slots=True)
class PairResult:
    i: str
    j: str
    ratio: float
    delta: float
    ln_ratio: float
    skew_s: float | None
    u_diff: float | None  # ln 单位，= (daily + skew + rounding) / 1e4
    daily_bp: float | None
    skew_bp: float | None
    rounding_bp: float | None
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
    anchor_date: date | None  # 共同锚点时的净值日期；归一化比较时为 None
    status: RelativeStatus
    members: tuple[MemberResult, ...]
    pairs: tuple[PairResult, ...]
    reasons: tuple[ReasonCode, ...] = ()
    policy_version: str = ""
    opportunity_alert_allowed: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)


HARD_EXIT = frozenset({
    ReasonCode.QUOTE_MISSING, ReasonCode.NAV_MISSING, ReasonCode.PENDING_VERIFY, ReasonCode.NONSEPARABLE_EVENT,
    ReasonCode.SESSION_BOUNDARY, ReasonCode.TIME_UNVERIFIED, ReasonCode.TIME_SKEW, ReasonCode.FUTURE_TIMESTAMP,
    ReasonCode.NAV_FX_RULE_UNKNOWN, ReasonCode.CORPORATE_ACTION_PENDING, ReasonCode.FACTOR_GROUP_MISMATCH,
    ReasonCode.CALENDAR_UNCERTAIN,  # AT56：日历缺口抑制依赖结果
})


def _positive(x: Decimal | float | None) -> bool:
    return x is not None and math.isfinite(float(x)) and float(x) > 0


def skew_bp(skew_s: float, policy: RelativePolicy) -> float:
    """报价错位情景项（bp）：z·σ·√((Δt+分辨率)/年化秒数)。"""
    return policy.skew_z * policy.skew_sigma_annual * math.sqrt(
        (skew_s + policy.time_resolution_s) / policy.skew_basis_seconds) * 1e4


def _choose_anchor_set(ok: list[MemberInput]) -> tuple[list[MemberInput], bool, date | None, list[MemberInput]]:
    """返回 (参与成员, 是否共同锚点, 共同锚点日期, 被排除成员)。"""
    dates = Counter(m.nav_date for m in ok)
    if len(dates) <= 1:
        return ok, True, next(iter(dates), None), []
    with_factors = [m for m in ok if _positive(m.index_at_anchor) and _positive(m.fx_at_anchor)]
    best_date, best_n = max(dates.items(), key=lambda kv: (kv[1], kv[0]))  # 人数最多，同数取较新日期
    if len(with_factors) >= 2 and len(with_factors) >= best_n:
        chosen = {m.code for m in with_factors}
        dropped = [m for m in ok if m.code not in chosen]
        factor_dates = {m.nav_date for m in with_factors}
        if len(factor_dates) == 1:  # 有因子的成员恰好同日：因子相消，按共同锚点比较
            return with_factors, True, next(iter(factor_dates)), dropped
        return with_factors, False, None, dropped
    subset = [m for m in ok if m.nav_date == best_date]
    return subset, True, best_date, [m for m in ok if m.nav_date != best_date]


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
    ok = [m for m in members if not (set(reasons[m.code]) & HARD_EXIT)]

    # 2) 共同时点 τ 与组内跨度（QS-02）
    tau = max((m.quote_time_utc_ns for m in ok), default=None)
    if tau is not None:
        for m in list(ok):
            if (tau - m.quote_time_utc_ns) / 1e9 > policy.max_span_s:  # type: ignore[operator]
                reasons[m.code].append(ReasonCode.TIME_SKEW)
                ok.remove(m)

    # 3) 锚点（评审 R4）
    ok, common_anchor, anchor_date, dropped = _choose_anchor_set(ok)
    for m in dropped:
        reasons[m.code].append(ReasonCode.NAV_FX_RULE_UNKNOWN)
    b = {m.code: float(m.nav) if common_anchor  # type: ignore[arg-type]
         else float(m.nav) / (m.index_at_anchor * m.fx_at_anchor)  # type: ignore[arg-type, operator]
         for m in ok}

    s = {m.code: float(m.price) / b[m.code] for m in ok}  # type: ignore[arg-type]
    order = sorted(s, key=lambda c: (s[c], c))
    rank = {c: k + 1 for k, c in enumerate(order)}
    by_code = {m.code: m for m in members}

    member_results = tuple(
        MemberResult(
            code=m.code,
            eligible=m.code in s,
            reasons=tuple(dict.fromkeys(reasons[m.code])),
            price=m.price,
            nav=m.nav,
            nav_date=m.nav_date,
            nav_premium=(float(m.last_price) / float(m.nav) - 1)
            if _positive(m.last_price) and _positive(m.nav) else None,
            s=s.get(m.code),
            rank=rank.get(m.code),
            anchor_sessions=m.anchor_sessions,
            anchor_health=anchor_health(m.anchor_sessions),
        )
        for m in sorted(members, key=lambda m: (rank.get(m.code, 10**6), m.code))
    )

    # 4) 成对判断（评审 R1）
    pairs = []
    for i, j in itertools.combinations(order, 2):
        ratio = s[i] / s[j]
        ln_ratio = math.log(ratio)
        ti, tj = by_code[i].quote_time_utc_ns, by_code[j].quote_time_utc_ns
        skew = abs(ti - tj) / 1e9 if ti is not None and tj is not None else None
        bound = bounds.get(frozenset((i, j)))
        daily = rnd = sk = u = None
        healths = {anchor_health(by_code[i].anchor_sessions), anchor_health(by_code[j].anchor_sessions)}
        if bound is None:
            status, pr = RelativeStatus.MODEL_REFERENCE, (ReasonCode.DIFFERENTIAL_UNCALIBRATED,)
        elif healths & {"EXTENDED", "INVALID"}:  # QS-03：锚点过旧或无法确定的成员对不授予强结论
            reason = ReasonCode.ANCHOR_INVALID if "INVALID" in healths else ReasonCode.ANCHOR_EXTENDED
            status, pr = RelativeStatus.MODEL_REFERENCE, (reason,)
        else:
            k = max(1, by_code[i].anchor_sessions or 0, by_code[j].anchor_sessions or 0)
            daily, rnd = bound.daily_p95_bp * math.sqrt(k), bound.rounding_bp
            sk = skew_bp(skew or 0.0, policy)
            u = (daily + sk + rnd) / 1e4
            if skew is None or skew > policy.robust_max_skew_s:
                status, pr = RelativeStatus.MODEL_REFERENCE, (ReasonCode.TIME_SKEW,)
            elif abs(ln_ratio) > u:
                status, pr = RelativeStatus.ROBUST_DIFFERENCE, ()
            else:
                status, pr = RelativeStatus.UNRESOLVED, (ReasonCode.DIFFERENCE_UNRESOLVED,)
        pairs.append(PairResult(i, j, ratio, ratio - 1, ln_ratio, skew, u, daily, sk, rnd,
                                bound.source if bound else None, status, pr))

    group_status = RelativeStatus.MODEL_REFERENCE if len(ok) >= 2 else RelativeStatus.INELIGIBLE
    group_reasons = (ReasonCode.FULL_EXPOSURE_ASSUMPTION,) if len(ok) >= 2 else ()
    return GroupResult(
        mode=mode, price_basis=price_basis, cutoff_utc_ns=cutoff_utc_ns, tau_utc_ns=tau if ok else None,
        common_anchor=common_anchor, anchor_date=anchor_date if common_anchor else None, status=group_status,
        members=member_results, pairs=tuple(pairs), reasons=group_reasons, policy_version=policy.version,
        opportunity_alert_allowed=False,  # 首版关闭价格机会通知（FR16）
    )
