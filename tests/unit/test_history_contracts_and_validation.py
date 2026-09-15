import json
from datetime import date, timedelta
from decimal import Decimal

import pytest

from qdii.contracts import chinamoney_ccpr_his_v1 as ccpr
from qdii.contracts import eastmoney_lsjz_v1 as lsjz
from qdii.contracts import index_history_v1 as idx
from qdii.core import history_validation as hv
from qdii.core.model_m0 import m0_factors, m0_nav, nav_ratio, premium
from qdii.core.types import NavObservation, ReasonCode
from tests.helpers import make_msg


def msg(body, url="https://example.invalid/", **kw):
    from dataclasses import replace

    from qdii.core.types import RequestTemplate

    m = make_msg(body if isinstance(body, bytes) else body.encode(), **kw)
    return replace(m, request=RequestTemplate("GET", url))


# ---------- VM-13 金标 ----------

def test_vm13_m0_golden():
    f = m0_factors(i_a=20000, i_c=20400, f_c=20500, f_t=20705, x0=7.0, x_t=7.14)
    nav = m0_nav(2.0, f)
    assert nav == pytest.approx(2.101608, abs=1e-8)
    assert premium(2.200, nav) == pytest.approx(0.0468174845, abs=1e-8)
    assert premium(2.203, nav) == pytest.approx(0.0482449629, abs=1e-8)
    assert premium(0.0, nav) is None  # 零价不得产生 −100%
    assert m0_factors(20000, 20400, 0, 20705, 7.0, 7.14) is None


def test_nav_ratio_requires_both_fx_or_none():
    assert nav_ratio(100, 102) == pytest.approx(1.02)
    assert nav_ratio(100, 102, 7.0, 7.07) == pytest.approx(1.02 * 1.01)
    assert nav_ratio(100, 102, 7.0, None) is None


# ---------- 契约 ----------

def test_lsjz_parses_rows_events_and_code():
    body = json.dumps({"ErrCode": 0, "TotalCount": 315, "Data": {"LSJZList": [
        {"FSRQ": "2026-09-11", "DWJZ": "1.9831", "LJJZ": "9.9155", "JZZZL": "0.87", "FHSP": ""},
        {"FSRQ": "2026-06-20", "DWJZ": "0.3966", "LJJZ": "9.1000", "JZZZL": "", "FHFCZ": "5", "FHSP": "每份拆分5份"},
        {"FSRQ": "bad", "DWJZ": "1"},
        {"FSRQ": "2026-06-19", "DWJZ": "0.0000", "LJJZ": "9.0"},
    ]}})
    r = lsjz.parse(msg(body, url="https://api.fund.eastmoney.com/f10/lsjz?fundCode=513100&pageIndex=1"))
    assert [n.nav_date for n in r.records] == [date(2026, 9, 11), date(2026, 6, 20), date(2026, 6, 19)]
    first, split, zero = r.records
    assert first.code == "513100" and first.unit_nav == Decimal("1.9831") and first.growth_pct == Decimal("0.87")
    assert split.event_fields == (("FHSP", "每份拆分5份"), ("FHFCZ", "5")) and split.growth_pct is None
    assert zero.unit_nav is None and zero.reason_codes == (ReasonCode.NONPOSITIVE_NAV,)
    assert r.issues[0].code is ReasonCode.QUOTE_INVALID
    assert lsjz.total_count(msg(body)) == 315


def test_fred_and_nasdaq_parse_same_close():
    fred = idx.parse_fred(msg("observation_date,NASDAQ100\n2026-09-11,29368.440\n2026-09-14,29127.160\n2026-09-15,.\n"))
    assert [(c.trade_date, c.close) for c in fred.records[:2]] == [
        (date(2026, 9, 11), Decimal("29368.440")), (date(2026, 9, 14), Decimal("29127.160"))]
    assert fred.records[2].close is None and fred.records[2].reason_codes == (ReasonCode.QUOTE_MISSING,)
    nasdaq = idx.parse_nasdaq(msg(json.dumps({"data": {"symbol": "NDX", "tradesTable": {"rows": [
        {"date": "09/14/2026", "close": "29,127.16"}]}}})))
    assert nasdaq.records[0].trade_date == date(2026, 9, 14)
    assert nasdaq.records[0].close == fred.records[1].close


def test_ccpr_parses_usd_cny_only():
    body = json.dumps({"data": {"searchlist": ["USD/CNY"], "pageTotal": 3},
                       "records": [{"date": "2026-09-15", "values": ["6.7670"]}, {"date": "2026-09-14", "values": []}]})
    r = ccpr.parse(msg(body))
    assert [(f.publish_date, f.rate) for f in r.records] == [(date(2026, 9, 15), Decimal("6.7670"))]
    assert r.issues and ccpr.page_total(msg(body)) == 3


# ---------- 历史验证：合成数据中真实规则为 L0 + T+1 ----------

def _synthetic(n_days=140, rule="T+1"):
    start = date(2026, 1, 5)
    us_days = [start + timedelta(days=i) for i in range(n_days * 2) if (start + timedelta(days=i)).weekday() < 5][:n_days]
    closes, fixings, navs = {}, {}, []
    level, x = 20000.0, 7.0
    for i, d in enumerate(us_days):
        level *= 1 + ((i * 37) % 11 - 5) / 1000
        closes[d] = level
    for i, d in enumerate(us_days + [us_days[-1] + timedelta(days=3)]):
        x *= 1 + ((i * 13) % 7 - 3) / 5000
        fixings[d] = x
    for i, d in enumerate(us_days[:-1]):
        fx_d = us_days[i + 1] if rule == "T+1" else d
        nav = 1.0 * closes[d] / closes[us_days[0]] * fixings[fx_d] / fixings[us_days[1] if rule == "T+1" else us_days[0]]
        navs.append(NavObservation("m", "syn", "513100", d, "UNIT", "CNY", Decimal(str(round(nav, 10))), None,
                                   None, (), 0, "syn"))
    return navs, closes, fixings


def test_selection_recovers_true_rule_and_reports_holdout():
    navs, closes, fixings = _synthetic(rule="T+1")
    intervals = hv.build_intervals(navs, closes, fixings)
    sel = hv.select_rule(hv.clean(intervals))
    assert sel.selected == "L0_FX_T+1"
    assert sel.shortfall == 0
    assert sel.holdout["L0_FX_T+1"].n == 60 and sel.holdout["L0_FX_T+1"].mae_bp == pytest.approx(0, abs=1e-3)
    assert sel.holdout["L0_FX_T"].mae_bp > 1


def test_event_and_growth_mismatch_intervals_excluded():
    base = NavObservation("m", "s", "513100", date(2026, 1, 5), "UNIT", "CNY", Decimal("2.0"), None, None, (), 0, "v")
    from dataclasses import replace

    navs = [
        base,
        replace(base, nav_date=date(2026, 1, 6), unit_nav=Decimal("0.4"), event_fields=(("FHFCZ", "5"),)),
        replace(base, nav_date=date(2026, 1, 7), unit_nav=Decimal("0.41"), growth_pct=Decimal("-3.00")),
    ]
    closes = {date(2026, 1, 5): 100.0, date(2026, 1, 6): 101.0, date(2026, 1, 7): 102.0}
    ivs = hv.build_intervals(navs, closes, {date(2026, 1, 2): 7.0, date(2026, 1, 8): 7.0})
    assert [iv.excluded for iv in ivs] == ["EVENT_FIELDS", "GROWTH_MISMATCH"]
    assert hv.clean(ivs) == []


def test_stats_nearest_rank_p95():
    s = hv.stats([i / 1e4 for i in range(1, 21)])  # 1..20 bp
    assert (s.n, s.mae_bp, s.p95_bp, s.max_bp) == (20, pytest.approx(10.5), pytest.approx(19), pytest.approx(20))


def test_nav_on_us_holiday_uses_latest_close_and_fx_only():
    # 2025-01-09 美股休市（国丧日），A 股开市且基金照常公布净值
    base = NavObservation("m", "s", "513100", date(2025, 1, 8), "UNIT", "CNY", Decimal("2.0"), None, None, (), 0, "v")
    from dataclasses import replace

    navs = [base, replace(base, nav_date=date(2025, 1, 9), unit_nav=Decimal("2.002"))]
    closes = {date(2025, 1, 7): 100.0, date(2025, 1, 8): 101.0, date(2025, 1, 10): 99.0}
    fixings = {date(2025, 1, 8): 7.000, date(2025, 1, 9): 7.007}
    iv = hv.build_intervals(navs, closes, fixings)[0]
    assert iv.residuals["L0_FX_T"] == pytest.approx(0.0, abs=1e-12)  # 指数因子 1，只剩汇率 +0.1%
    assert iv.residuals["L0_NOFX"] == pytest.approx(0.001, abs=1e-12)
