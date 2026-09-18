"""盘中估算净值（勘误 E8）：纯函数公式与降级、契约（昨结算、CFETS 即期）、状态层 as-of 与输入包回放、页面。"""

import json
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from qdii.apps.relative_snapshot import render, view
from qdii.apps.status import render_html
from qdii.contracts import cfets_fx_spot_v1, sina_hf_v2
from qdii.core.enav import EnavInput, EnavPolicy, evaluate_enav
from qdii.core.relative_bundle import bundle_from_dict, bundle_id, canonical_json, evaluate_bundle
from qdii.core.types import PriceType, ReasonCode
from tests.helpers import make_msg, recorded_sina
from tests.unit.test_relative_bundle_state import bj, factor_msgs, fed_state

SH = ZoneInfo("Asia/Shanghai")
C_0914 = int(datetime(2026, 9, 14, 20, 0, tzinfo=ZoneInfo("UTC")).timestamp() * 1e9)  # 09-14 16:00 ET
T = bj(14, 49, 55)

BASE = EnavInput(
    code="A", price=2.2, quote_time_utc_ns=T, cutoff_utc_ns=T + 5 * 10**9, current=True, phase_ok=True,
    calendar_ok=True, roll_window=False, contract_known=True, nav=2.0, nav_usable=True, index_date=date(2026, 9, 11),
    index_at_anchor=29368.44, fx_at_anchor=6.7743, us_close_date=date(2026, 9, 14), us_close_utc_ns=C_0914,
    index_at_close=29127.16, futures_mid=29058.375, futures_settle=29152.25, futures_time_utc_ns=T - 4 * 10**9,
    fx_spot=6.7136, fx_time_utc_ns=T - 8 * 10**9,
)


# ---------- 纯函数 ----------

def test_enav_formula_and_disclosed_proxy_reasons():
    r = evaluate_enav(BASE, EnavPolicy())
    expected = 2.0 * (29127.16 / 29368.44) * (29058.375 / 29152.25) * (6.7136 / 6.7743)
    assert r.status == "PROXY_ANCHOR" and r.enav == pytest.approx(expected, rel=1e-12)
    assert r.premium == pytest.approx(2.2 / expected - 1, rel=1e-12)
    assert r.basis == pytest.approx(29152.25 / 29127.16 - 1) and r.futures_age_s == 4 and r.fx_age_s == 8
    assert set(r.reasons) == {ReasonCode.SETTLEMENT_ANCHOR_PROXY, ReasonCode.FULL_EXPOSURE_ASSUMPTION}


def test_nav_already_at_last_us_close_has_no_index_move():
    r = evaluate_enav(replace(BASE, index_date=date(2026, 9, 14), index_at_anchor=29127.16), EnavPolicy())
    assert r.index_move == 1.0 and r.status == "PROXY_ANCHOR"


@pytest.mark.parametrize(("change", "reason"), [
    ({"nav": None}, ReasonCode.NAV_MISSING),
    ({"nav_usable": False}, ReasonCode.NAV_REJECTED),
    ({"index_at_anchor": None}, ReasonCode.NAV_FX_RULE_UNKNOWN),
    ({"index_at_close": None}, ReasonCode.INDEX_CLOSE_MISSING),
    ({"us_close_date": date(2026, 9, 10)}, ReasonCode.ANCHOR_INVALID),
    ({"price": None}, ReasonCode.QUOTE_MISSING),
    ({"futures_settle": None}, ReasonCode.FUTURE_ANCHOR_MISSING),
    ({"futures_time_utc_ns": T - 61 * 10**9}, ReasonCode.TIME_SKEW),
    ({"quote_time_utc_ns": C_0914 + 3600 * 10**9, "cutoff_utc_ns": C_0914 + 3601 * 10**9,
      "futures_time_utc_ns": C_0914 + 3596 * 10**9, "fx_time_utc_ns": C_0914 + 3590 * 10**9},
     ReasonCode.FUTURE_ANCHOR_MISSING),  # 收盘后 1 小时：昨结算尚未滚动到 c
    # 四审 D1：当前估算按知识截止时刻检查时效、阶段与日历
    ({"cutoff_utc_ns": T + 61 * 10**9}, ReasonCode.TIME_SKEW),
    ({"calendar_ok": False}, ReasonCode.CALENDAR_UNCERTAIN),
    ({"phase_ok": False}, ReasonCode.SESSION_BOUNDARY),
    ({"fx_spot": None}, ReasonCode.FX_MISSING),
    ({"fx_time_utc_ns": T - 121 * 10**9}, ReasonCode.FX_STALE),
    ({"futures_settle": 29127.16 * 0.99}, ReasonCode.SETTLEMENT_BASIS_SUSPECT),
    ({"futures_settle": 29127.16 * 1.04}, ReasonCode.SETTLEMENT_BASIS_SUSPECT),
    # 身份未知 + 换月窗口：无法确认同一合约
    ({"roll_window": True, "contract_known": False}, ReasonCode.ROLL_ANCHOR_MISSING),
    ({"futures_mid": 29152.25 * 1.05}, ReasonCode.SOURCE_ANCHOR_MISMATCH),  # 异常熔断
])
def test_missing_or_failed_input_makes_enav_unavailable_with_reason(change, reason):
    r = evaluate_enav(replace(BASE, **change), EnavPolicy())
    assert r.status == "UNAVAILABLE" and r.enav is None and r.premium is None and reason in r.reasons


# ---------- 契约 ----------

def test_sina_hf_v2_adds_previous_settlement_from_recorded_sample():
    msg = replace(recorded_sina("hf_NQ", "referer"), endpoint_id="sina.hf_NQ")
    recs = {r.price_type: r for r in sina_hf_v2.parse(msg).records}
    settle = recs[PriceType.SETTLE]
    assert settle.value is not None and settle.value > 0
    assert settle.provider_time == recs[PriceType.BID].provider_time
    assert {ReasonCode.CONTRACT_UNKNOWN, ReasonCode.SETTLEMENT_ANCHOR_PROXY} <= set(settle.reason_codes)


def cfets_body(show="2026-09-15 14:49:47", bid="6.7131", ask="6.7132", code="200"):
    return json.dumps({"head": {"rep_code": code}, "data": {"showDateCN": show}, "records": [
        {"bidPrc": "---", "askPrc": "---", "ccyPair": "EUR/CNY"},
        {"bidPrc": bid, "askPrc": ask, "midprice": "---", "time": "", "ccyPair": "USD/CNY"}]}).encode()


def test_cfets_spot_contract():
    recs = {r.price_type: r for r in cfets_fx_spot_v1.parse(make_msg(cfets_body(), source_id="cfets")).records}
    assert recs[PriceType.BID].value == Decimal("6.7131") and recs[PriceType.ASK].value == Decimal("6.7132")
    assert recs[PriceType.BID].provider_time.utc_ns == bj(14, 49, 47)
    empty = cfets_fx_spot_v1.parse(make_msg(cfets_body(bid="---", ask="---"), source_id="cfets")).records
    assert all(r.value is None and ReasonCode.QUOTE_MISSING in r.reason_codes for r in empty)
    assert cfets_fx_spot_v1.parse(make_msg(cfets_body(code="500"), source_id="cfets")).records == ()
    assert cfets_fx_spot_v1.parse(make_msg(b"<html>", source_id="cfets")).issues


# ---------- 状态层、输入包、页面 ----------

def hf(t_bj, bid, ask, settle, received, seq, quarterly=("NQ2612",)):
    """连续代码 + 可选的身份已知季月合约（同一响应，与生产一致）。"""
    local = datetime.fromtimestamp(t_bj / 1e9, tz=SH)

    def line(symbol, name, b, a, s):
        f = ["0", "", b, a, "", "", local.strftime("%H:%M:%S"), s, "", "", "", "", local.strftime("%Y-%m-%d"),
             name, ""]
        return f'var hq_str_hf_{symbol}="{",".join(f)}";'

    rows = [line("NQ", "纳斯达克指数期货", bid, ask, settle)]
    rows += [line(c, f"纳斯达克指数期货{c[2:]}", bid, ask, settle) for c in quarterly]
    return make_msg("\n".join(rows).encode("gb18030"), received_utc_ns=received, seq=seq,
                    run_id="LIVE-test", endpoint_id="sina.hf_NQ")


def fx(show_ns, bid, ask, received, seq):
    show = datetime.fromtimestamp(show_ns / 1e9, tz=SH).strftime("%Y-%m-%d %H:%M:%S")
    return make_msg(cfets_body(show, bid, ask), received_utc_ns=received, seq=seq, source_id="cfets",
                    run_id="LIVE-test", endpoint_id="cfets.fx_spot_quot")


def enav_state(quarterly=True):
    st = fed_state()  # 五只基金净值 09-11，14:49:55 卖一 2.300
    ndx = [("2026-09-11", "29,368.44"), ("2026-09-14", "29,127.16")]
    for msg in factor_msgs(ndx, [("2026-09-11", "6.7743"), ("2026-09-14", "6.7698")]):
        st.ingest(msg)
    st.ingest(hf(bj(14, 49, 50), "29058.25", "29058.50", "29152.25", bj(14, 49, 51), 90,
                 quarterly=("NQ2612",) if quarterly else ()))
    st.ingest(fx(bj(14, 49, 47), "6.7131", "6.7132", bj(14, 49, 49), 91))
    return st


def test_state_supplies_enav_inputs_and_bundle_replays():
    b = enav_state().bundle(bj(14, 50))
    assert (b.us_close_date, b.us_close_index) == ("2026-09-14", "29127.16")
    m = b.members[0]
    assert (m.futures_mid, m.futures_settle, m.fx_spot) == ("29058.375", "29152.25", "6.71315")
    snap = evaluate_bundle(b)
    assert {x.status for x in snap.enav} == {"PROXY_ANCHOR"}
    x = next(x for x in snap.enav if x.code == m.code)
    expected = float(m.nav) * (29127.16 / 29368.44) * (29058.375 / 29152.25) * (6.71315 / 6.7743)
    assert x.enav == pytest.approx(expected, rel=1e-12) and x.premium == pytest.approx(2.3 / expected - 1, rel=1e-12)
    again = bundle_from_dict(json.loads(canonical_json(b)))
    assert again == b and bundle_id(again) == bundle_id(b)


def test_futures_sample_is_taken_as_of_member_quote_time():
    st = enav_state()
    st.ingest(hf(bj(14, 49, 58), "29999.00", "29999.25", "29152.25", bj(14, 49, 59), 92))  # 晚于成员报价 14:49:55
    m = st.bundle(bj(14, 50)).members[0]
    assert m.futures_mid == "29058.375"  # 不用报价之后的期货样本（as-of）


def test_future_timestamped_futures_tick_rejected():
    st = enav_state()
    before = st.future_rejected
    st.ingest(hf(bj(15, 0), "1.00", "1.25", "29152.25", bj(14, 49, 52), 93))  # 连续 + 季月各一条
    assert st.future_rejected == before + 2
    assert all(ticks[-1].t == bj(14, 49, 50) for ticks in st.futures.values())


def test_missing_index_close_for_last_us_session_disables_enav_not_ranking():
    st = fed_state()
    for msg in factor_msgs([("2026-09-11", "29,368.44")], [("2026-09-11", "6.7743")]):
        st.ingest(msg)
    st.ingest(hf(bj(14, 49, 50), "29058.25", "29058.50", "29152.25", bj(14, 49, 51), 90))
    st.ingest(fx(bj(14, 49, 47), "6.7131", "6.7132", bj(14, 49, 49), 91))
    b = st.bundle(bj(14, 50))
    snap = evaluate_bundle(b)
    assert "enav:index_close_missing:2026-09-14" in b.notes
    assert all(ReasonCode.INDEX_CLOSE_MISSING in x.reasons for x in snap.enav)
    assert sum(1 for m in snap.result.members if m.eligible) == 5  # 相对比较不受影响


def test_cli_and_status_page_show_estimated_premium():
    st = enav_state()
    b = st.bundle(bj(14, 50))
    snap = evaluate_bundle(b)
    names = {f.code: f.name for f in st.funds}
    text = render(snap, b, names)
    assert "估算溢价" in text and "昨结算 29152.25" in text
    rel = view(snap, b, names)
    assert rel["absolute_premium_available"] is True  # 兼容别名：只表示存在代理估算
    assert rel["estimate_available"] is True and rel["absolute_decision_eligible"] is False  # 复审：误差未验证
    page = render_html({"run_id": "t", "started_utc_ns": 0, "last_heartbeat_utc_ns": None, "warnings": [],
                        "window": {"active": True}, "host": None, "endpoints": [], "etf": {}, "parse_issues": {},
                        "events_recent": [], "relative": rel})
    assert "估算溢价" in page and "估算净值" in page and "基差" in page


def test_closing_reference_viewed_after_us_close_keeps_the_close_before_the_snapshot():
    st = enav_state()
    b = st.bundle(bj(5, 0, day=16))  # 09-16 凌晨查看：09-15 美股已收盘，但展示的仍是 09-15 14:49:55 的快照
    assert b.mode == "CLOSING_REFERENCE" and b.us_close_date == "2026-09-14"
    snap = evaluate_bundle(b)
    assert {x.status for x in snap.enav} == {"REFERENCE"}  # 历史参考，不是当前可用的估算
    assert view(snap, b, {})["absolute_premium_available"] is False
    assert view(snap, b, {})["absolute_decision_status"] == "REFERENCE_ONLY"


def test_d1_stale_quotes_at_cutoff_make_current_estimate_unavailable():
    st = enav_state()
    b = st.bundle(bj(14, 55))  # 14:49:55 之后行情停更
    snap = evaluate_bundle(b)
    assert snap.result.status.value == "INELIGIBLE"
    assert {x.status for x in snap.enav} == {"UNAVAILABLE"}
    assert all(ReasonCode.TIME_SKEW in x.reasons for x in snap.enav)
    assert view(snap, b, {})["absolute_premium_available"] is False
    assert view(snap, b, {})["absolute_decision_status"] == "NO_ESTIMATE"
    uncovered = evaluate_bundle(replace(st.bundle(bj(14, 50)), calendar_covered=False))
    assert all(x.status == "UNAVAILABLE" and ReasonCode.CALENDAR_UNCERTAIN in x.reasons for x in uncovered.enav)


def test_d1_estimate_does_not_inherit_relative_group_eligibility():
    from qdii.apps.relative_snapshot import new_state
    from tests.unit.test_relative_bundle_state import REPO, etf_msg, nav_msg
    from tests.unit.test_relative_snapshot_replay import CODES

    st = new_state(REPO)
    for i, (vendor, nav) in enumerate(CODES.items()):
        st.ingest(nav_msg(vendor[2:], [{"FSRQ": "2026-09-11", "DWJZ": nav, "LJJZ": nav, "JZZZL": ""}], bj(9, 0, i), i))
    only = next(iter(CODES))
    st.ingest(etf_msg("14:49:55", {only: "2.300"}, bj(14, 49, 58), 10))  # 只有一只有行情：相对比较无法成组
    for msg in factor_msgs([("2026-09-11", "29,368.44"), ("2026-09-14", "29,127.16")], [("2026-09-11", "6.7743")]):
        st.ingest(msg)
    st.ingest(hf(bj(14, 49, 50), "29058.25", "29058.50", "29152.25", bj(14, 49, 51), 90))
    st.ingest(fx(bj(14, 49, 47), "6.7131", "6.7132", bj(14, 49, 49), 91))
    snap = evaluate_bundle(st.bundle(bj(14, 50)))
    assert snap.result.status.value == "INELIGIBLE"
    assert next(x for x in snap.enav if x.code == only[2:]).status == "PROXY_ANCHOR"


def test_input_gaps_name_endpoints_that_can_fill_missing_estimate_inputs():
    from qdii.pipeline.relative_state import input_gaps

    st = fed_state()
    assert input_gaps(st.bundle(bj(14, 50))) == {"nasdaq.ndx_history"}  # 无指数、无中间价时先缺指数
    for msg in factor_msgs([("2026-09-11", "29,368.44"), ("2026-09-14", "29,127.16")], [("2026-09-14", "6.7698")]):
        st.ingest(msg)
    assert input_gaps(st.bundle(bj(14, 50))) == {"chinamoney.ccpr"}  # 09-11 中间价缺失
    for msg in factor_msgs([], [("2026-09-11", "6.7743")], seq=80):
        st.ingest(msg)
    assert input_gaps(st.bundle(bj(14, 50))) == set()
    st.navs.pop("513100")
    assert input_gaps(st.bundle(bj(14, 50))) == {"eastmoney.lsjz.513100"}


def test_known_contract_survives_the_roll_window_unknown_one_does_not():
    """D4 专项 F1：净变动无法拆分合约价差与行情变动，因此身份未知时换月窗口内一律不出估算。"""
    known = evaluate_enav(replace(BASE, roll_window=True), EnavPolicy())
    assert known.status == "PROXY_ANCHOR" and ReasonCode.ROLL_WINDOW in known.reasons
    assert ReasonCode.CONTRACT_UNKNOWN not in known.reasons

    # 合约价差 +1% 与行情 −0.6% 相抵，净变动只有 +0.4%：旧阈值放行，现在按身份判断直接拒绝
    offset = replace(BASE, roll_window=True, contract_known=False, futures_mid=BASE.futures_settle * 1.01 * 0.994)
    assert evaluate_enav(offset, EnavPolicy()).status == "UNAVAILABLE"
    assert ReasonCode.ROLL_ANCHOR_MISSING in evaluate_enav(offset, EnavPolicy()).reasons

    # 身份未知但不在换月窗口：可用，且如实披露 CONTRACT_UNKNOWN
    outside = evaluate_enav(replace(BASE, roll_window=False, contract_known=False), EnavPolicy())
    assert outside.status == "PROXY_ANCHOR" and ReasonCode.CONTRACT_UNKNOWN in outside.reasons


def test_state_picks_an_identified_quarterly_contract_and_marks_roll_window():
    st = enav_state()  # c = 2026-09-14，9 月到期 09-18：截止日 +3 天已过 09-18，应选 12 月合约
    b = st.bundle(bj(14, 50))
    assert b.roll_window is True and b.futures_contract == "NQ2612"
    snap = evaluate_bundle(b)
    assert all(x.status == "PROXY_ANCHOR" for x in snap.enav)
    assert all(ReasonCode.ROLL_WINDOW in x.reasons and ReasonCode.CONTRACT_UNKNOWN not in x.reasons
               for x in snap.enav)


def test_state_falls_back_to_continuous_when_no_identified_contract_is_available():
    st = enav_state(quarterly=False)
    b = st.bundle(bj(14, 50))
    assert b.futures_contract == "CONTINUOUS"
    snap = evaluate_bundle(b)  # 换月窗口内 + 身份未知 → 不出估算
    assert all(x.status == "UNAVAILABLE" and ReasonCode.ROLL_ANCHOR_MISSING in x.reasons for x in snap.enav)


def test_sina_hf_quarterly_contract_parses_identity_and_settlement():
    from qdii.contracts import sina_hf_quarterly_v1
    from qdii.pipeline.sources import nq_quarterlies

    msg = hf(bj(14, 49, 50), "29061.25", "29063.75", "28955.00", bj(14, 49, 51), 95,
             quarterly=("NQ2609", "NQ2612"))
    recs = sina_hf_quarterly_v1.parse(msg).records
    assert {r.contract_id for r in recs} == {"NQ2609", "NQ2612"}  # 连续代码那一行不在其中
    sep = {r.price_type: r for r in recs if r.contract_id == "NQ2609"}
    assert sep[PriceType.BID].value == Decimal("29061.25") and sep[PriceType.SETTLE].value == Decimal("28955.00")
    assert ReasonCode.CONTRACT_UNKNOWN not in sep[PriceType.BID].reason_codes  # 身份来自合约代码
    assert ReasonCode.SETTLEMENT_ANCHOR_PROXY in sep[PriceType.SETTLE].reason_codes  # 结算日仍未证实
    assert sep[PriceType.BID].provider_time.utc_ns == bj(14, 49, 50)
    assert nq_quarterlies(date(2026, 9, 16)) == ["NQ2609", "NQ2612"]  # 到期前仍列当季
    assert nq_quarterlies(date(2026, 9, 19)) == ["NQ2612", "NQ2703"]  # 到期后自动前移
