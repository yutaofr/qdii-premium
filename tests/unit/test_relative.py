from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from qdii.core.relative import (
    MemberInput,
    PairBound,
    RelativeMode,
    RelativeStatus,
    evaluate_relative,
)
from qdii.core.types import ReasonCode

T = 1_789_455_595_000_000_000  # 2026-09-15 06:59:55 UTC
D0 = date(2026, 9, 11)


def m(code, price, nav, **kw):
    base = MemberInput(code, Decimal(price), T - 3 * 10**9, True, Decimal(nav), D0, True, last_price=Decimal(price),
                       anchor_sessions=1)
    return replace(base, **kw)


def run(members, **kw):
    return evaluate_relative(members, mode=kw.pop("mode", RelativeMode.CURRENT), price_basis="ASK", cutoff_utc_ns=T,
                             **kw)


def test_ranking_and_delta_common_anchor():
    g = run([m("A", "2.200", "2.000"), m("B", "2.240", "2.000"), m("C", "1.890", "1.800")])
    assert g.common_anchor and g.status is RelativeStatus.MODEL_REFERENCE
    assert [x.code for x in g.members] == ["C", "A", "B"]  # S: 1.05, 1.10, 1.12
    pair = next(p for p in g.pairs if {p.i, p.j} == {"A", "B"})
    assert pair.delta == pytest.approx(1.10 / 1.12 - 1)  # VM-10 示例 −1.785714%
    assert pair.status is RelativeStatus.MODEL_REFERENCE
    assert ReasonCode.DIFFERENTIAL_UNCALIBRATED in pair.reasons
    assert g.members[0].nav_premium == pytest.approx(0.05)
    assert g.opportunity_alert_allowed is False


def test_at61_common_factor_invariance_with_different_anchors():
    # 不同净值日期：需要 I(a)·X0；把共同因子整体乘以任意正数，R 不变
    a = m("A", "2.2", "2.0", nav_date=date(2026, 9, 10), index_at_anchor=29000.0, fx_at_anchor=6.77)
    b = m("B", "2.3", "2.1", nav_date=date(2026, 9, 11), index_at_anchor=29300.0, fx_at_anchor=6.78)
    g1 = run([a, b])
    k = 1.37
    g2 = run([replace(a, index_at_anchor=a.index_at_anchor * k), replace(b, index_at_anchor=b.index_at_anchor * k)])
    assert not g1.common_anchor
    assert g1.pairs[0].ratio == pytest.approx(g2.pairs[0].ratio, rel=1e-12)


def test_at62_missing_anchor_factors_exits_member_not_group():
    a = m("A", "2.2", "2.0", nav_date=date(2026, 9, 10), index_at_anchor=29000.0, fx_at_anchor=6.77)
    b = m("B", "2.3", "2.1", nav_date=date(2026, 9, 11))
    c = m("C", "2.4", "2.1", nav_date=date(2026, 9, 11), index_at_anchor=29300.0, fx_at_anchor=6.78)
    g = run([a, b, c])
    bres = next(x for x in g.members if x.code == "B")
    assert not bres.eligible and ReasonCode.NAV_FX_RULE_UNKNOWN in bres.reasons
    assert len(g.pairs) == 1


def test_at63_events_invalid_quotes_and_single_member():
    g = run([m("A", "2.2", "2.0", nonseparable_event=True), replace(m("B", "2.3", "2.0"), price=None),
             m("C", "2.4", "2.0")])
    assert g.status is RelativeStatus.INELIGIBLE and g.pairs == ()
    reasons = {x.code: x.reasons for x in g.members}
    assert ReasonCode.NONSEPARABLE_EVENT in reasons["A"] and ReasonCode.QUOTE_MISSING in reasons["B"]


def test_staleness_span_and_phase():
    stale = m("A", "2.2", "2.0", quote_time_utc_ns=T - 61 * 10**9)
    g = run([stale, m("B", "2.3", "2.0"), m("C", "2.4", "2.0", phase_ok=False)])
    reasons = {x.code: x.reasons for x in g.members}
    assert ReasonCode.TIME_SKEW in reasons["A"] and ReasonCode.SESSION_BOUNDARY in reasons["C"]
    # 收盘参考模式不按当前年龄拒绝，但仍要求组内跨度
    g2 = run([replace(stale, quote_time_utc_ns=T - 3600 * 10**9), replace(stale, code="B",
             quote_time_utc_ns=T - 3590 * 10**9)], mode=RelativeMode.CLOSING_REFERENCE)
    assert g2.status is RelativeStatus.MODEL_REFERENCE and len(g2.pairs) == 1


def test_pair_bounds_resolve_or_not():
    members = [m("A", "2.200", "2.000"), m("B", "2.2011", "2.000")]  # |ln R| ≈ 5bp，报价同时刻
    near = run(members, bounds={frozenset("AB"): PairBound(8.0, 0.5, "PH0-07 test")})
    far = run(members, bounds={frozenset("AB"): PairBound(1.0, 0.5, "PH0-07 test")})
    assert near.pairs[0].status is RelativeStatus.UNRESOLVED
    p = far.pairs[0]
    assert p.status is RelativeStatus.ROBUST_DIFFERENCE and p.bound_source == "PH0-07 test"
    assert p.u_diff == pytest.approx((p.daily_bp + p.skew_bp + p.rounding_bp) / 1e4)  # 边界分项可披露


# ---------- 评审 R1：报价错位进入差分边界 ----------

def test_r1_skew_widens_bound_and_large_skew_is_not_robust():
    bound = {frozenset("AB"): PairBound(1.0, 0.5, "t")}
    a = m("A", "2.200", "2.000")
    same = run([a, m("B", "2.2011", "2.000")], bounds=bound).pairs[0]
    lag10 = run([a, m("B", "2.2011", "2.000", quote_time_utc_ns=T - 13 * 10**9)], bounds=bound).pairs[0]
    lag59 = run([a, m("B", "2.2011", "2.000", quote_time_utc_ns=T - 59 * 10**9)], bounds=bound).pairs[0]
    assert same.skew_s == 0 and lag10.skew_s == pytest.approx(10) and lag59.skew_s == pytest.approx(56)
    assert same.u_diff < lag10.u_diff < lag59.u_diff  # 错位越大，边界越宽
    assert same.status is RelativeStatus.ROBUST_DIFFERENCE  # 5bp > 1+2.0+0.5
    assert lag10.status is RelativeStatus.UNRESOLVED  # 10 秒错位情景项约 5bp，差异不再可分辨
    assert lag59.status is RelativeStatus.MODEL_REFERENCE and ReasonCode.TIME_SKEW in lag59.reasons
    # 远超边界的差异，在错位 > robust_max_skew_s 时也只给模型参考
    big = run([a, m("B", "2.400", "2.000", quote_time_utc_ns=T - 40 * 10**9)], bounds=bound).pairs[0]
    assert big.status is RelativeStatus.MODEL_REFERENCE


def test_skew_term_matches_vm11_scenario():
    from qdii.core.relative import RelativePolicy, skew_bp

    pol = RelativePolicy()
    assert skew_bp(14, pol) == pytest.approx(2 * 3.19, rel=0.01)  # (14+1)s ≈ VM-11 的 15 秒 3.19bp（1σ）×2
    assert skew_bp(59, pol) == pytest.approx(2 * 6.38, rel=0.01)


# ---------- 评审 R4：单只基金先披露净值 ----------

def test_r4_one_fund_newer_nav_keeps_other_four_comparable():
    members = [m(c, "2.2", "2.0") for c in "ABCD"] + [m("E", "2.3", "2.1", nav_date=date(2026, 9, 14))]
    g = run(members)
    assert g.common_anchor and g.anchor_date == D0 and g.status is RelativeStatus.MODEL_REFERENCE
    assert [x.code for x in g.members if x.eligible] == ["A", "B", "C", "D"]
    e = next(x for x in g.members if x.code == "E")
    assert not e.eligible and ReasonCode.NAV_FX_RULE_UNKNOWN in e.reasons


def test_r4_with_normalization_factors_all_funds_compare_across_dates():
    old = [m(c, "2.2", "2.0", index_at_anchor=29368.44, fx_at_anchor=6.7743) for c in "ABCD"]
    new = m("E", "2.3", "2.1", nav_date=date(2026, 9, 14), index_at_anchor=29127.16, fx_at_anchor=6.7698)
    g = run([*old, new])
    assert not g.common_anchor and g.anchor_date is None
    assert sum(x.eligible for x in g.members) == 5


def test_r4_long_lagging_single_fund_excluded_not_group():
    members = [m(c, "2.2", "2.0", nav_date=date(2026, 9, 14)) for c in "ABCD"] + [m("E", "2.3", "2.1")]
    g = run(members)
    assert g.anchor_date == date(2026, 9, 14) and sum(x.eligible for x in g.members) == 4


def test_nav_premium_uses_last_price_not_ask():
    g = run([m("A", "2.203", "2.000", last_price=Decimal("2.200")), m("B", "2.3", "2.0")])
    a = next(x for x in g.members if x.code == "A")
    assert a.price == Decimal("2.203") and a.nav_premium == pytest.approx(0.10)


def test_unverified_nav_excluded():
    g = run([m("A", "2.2", "2.0", nav_verified=False), m("B", "2.3", "2.0")])
    assert ReasonCode.PENDING_VERIFY in next(x for x in g.members if x.code == "A").reasons


def test_f5_pair_bound_scales_with_its_own_members_anchor_age():
    a = m("A", "2.20", "2.0", anchor_sessions=1)
    b = m("B", "2.30", "2.0", anchor_sessions=4)
    c = m("C", "2.40", "2.0", anchor_sessions=11)  # EXTENDED：仅涉及它的成员对失去强结论
    bounds = {frozenset(k): PairBound(2.0, 0.5, "t") for k in ("AB", "AC", "BC")}
    g = run([a, b, c], bounds=bounds)
    pairs = {frozenset((p.i, p.j)): p for p in g.pairs}
    assert pairs[frozenset("AB")].daily_bp == pytest.approx(2.0 * 2)  # √max(1, 4)
    assert pairs[frozenset("AB")].status is RelativeStatus.ROBUST_DIFFERENCE
    for key in ("AC", "BC"):
        assert pairs[frozenset(key)].status is RelativeStatus.MODEL_REFERENCE
        assert ReasonCode.ANCHOR_EXTENDED in pairs[frozenset(key)].reasons
    g2 = run([a, replace(b, anchor_sessions=None)], bounds=bounds)
    assert ReasonCode.ANCHOR_INVALID in g2.pairs[0].reasons
