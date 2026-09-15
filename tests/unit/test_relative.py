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
    base = MemberInput(code, Decimal(price), T - 3 * 10**9, True, Decimal(nav), D0, True)
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
    members = [m("A", "2.200", "2.000"), m("B", "2.2011", "2.000")]  # |ln R| ≈ 5bp
    near = run(members, bounds={frozenset("AB"): PairBound(0.0010, "PH0-07 test")})
    far = run(members, bounds={frozenset("AB"): PairBound(0.0002, "PH0-07 test")})
    assert near.pairs[0].status is RelativeStatus.UNRESOLVED
    assert far.pairs[0].status is RelativeStatus.ROBUST_DIFFERENCE and far.pairs[0].bound_source == "PH0-07 test"


def test_unverified_nav_excluded():
    g = run([m("A", "2.2", "2.0", nav_verified=False), m("B", "2.3", "2.0")])
    assert ReasonCode.PENDING_VERIFY in next(x for x in g.members if x.code == "A").reasons
