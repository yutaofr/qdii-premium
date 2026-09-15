"""输入包确定性、质量对象（QS-02/03）、增量状态（as-of、净值修订隔离）、快照存储与回放校验。"""

import json
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from qdii.apps.relative_snapshot import new_state, view
from qdii.apps.replay_relative import verify
from qdii.apps.status import render_html
from qdii.core.relative import RelativePolicy
from qdii.core.relative_bundle import (
    MemberSpec,
    RelativeBundle,
    anchor_health,
    bundle_from_dict,
    bundle_id,
    canonical_json,
    diff_plain,
    evaluate_bundle,
    freshness,
    snapshot_to_dict,
)
from qdii.core.types import RequestTemplate
from qdii.io.snapshot_store import SnapshotStore
from tests.helpers import make_msg
from tests.unit.test_relative_snapshot_replay import CODES, sina_body

REPO = Path(__file__).resolve().parents[2]
SH = ZoneInfo("Asia/Shanghai")


def bj(h, m, s=0, day=15):
    return int(datetime(2026, 9, day, h, m, s, tzinfo=SH).timestamp() * 1e9)


def spec(code, price, nav, t, **kw):
    return replace(MemberSpec(code, price, "1000", t, True, nav, "2026-09-11", True, last_price=price), **kw)


POLICY = tuple(sorted(asdict(RelativePolicy()).items()))


def bundle(**kw):
    base = RelativeBundle(
        schema=2, cutoff_utc_ns=bj(14, 50), mode="CURRENT", price_basis="ASK", policy=POLICY, calendar_covered=True,
        sessions_since_anchor=2, model_status="HISTORICAL_VALIDATED",
        members=(spec("A", "2.200", "2.0000", bj(14, 49, 55)), spec("B", "2.240", "2.0000", bj(14, 49, 52))),
        bounds=(("A", "B", 2.0, 0.5, "test"),), versions=(("calendar", "x"),),
    )
    return replace(base, **kw)


# ---------- 输入包与质量 ----------

def test_bundle_id_is_stable_and_roundtrips():
    b = bundle()
    again = bundle_from_dict(json.loads(canonical_json(b)))
    assert again == b and bundle_id(again) == bundle_id(b)
    assert bundle_id(replace(b, cutoff_utc_ns=b.cutoff_utc_ns + 1)) != bundle_id(b)


def test_evaluate_bundle_deterministic_and_serializable():
    a, b = snapshot_to_dict(evaluate_bundle(bundle())), snapshot_to_dict(evaluate_bundle(bundle()))
    assert diff_plain(a, b) == []
    assert a["result"]["pairs"][0]["status"] == "ROBUST_DIFFERENCE"
    assert a["quality"]["freshness"] == "CURRENT" and a["quality"]["max_skew_s"] == pytest.approx(3.0)


@pytest.mark.parametrize(("age", "expected"), [(15, "CURRENT"), (60, "RECENT"), (61, "AGING"), (120, "AGING"),
                                               (121, "STALE"), (None, "UNKNOWN")])
def test_at65_freshness_boundaries(age, expected):
    assert freshness(age) == expected


@pytest.mark.parametrize(("sessions", "expected"), [(3, "NORMAL"), (4, "AGED"), (10, "AGED"), (11, "EXTENDED")])
def test_at67_anchor_health(sessions, expected):
    assert anchor_health(sessions) == expected


def test_extended_anchor_never_robust_and_aged_is_flagged():
    ext = snapshot_to_dict(evaluate_bundle(bundle(sessions_since_anchor=11)))
    assert ext["result"]["pairs"][0]["status"] == "MODEL_REFERENCE"
    assert "ANCHOR_EXTENDED" in ext["result"]["reasons"]
    aged = snapshot_to_dict(evaluate_bundle(bundle(sessions_since_anchor=5)))
    assert "ANCHOR_AGED" in aged["result"]["reasons"] and aged["result"]["pairs"][0]["status"] == "ROBUST_DIFFERENCE"


def test_closing_reference_freshness_not_applicable():
    q = snapshot_to_dict(evaluate_bundle(bundle(mode="CLOSING_REFERENCE", price_basis="LAST")))["quality"]
    assert q["freshness"] == "NOT_APPLICABLE"


def test_unverified_fx_rule_excludes_member():
    b = bundle(members=(spec("A", "2.2", "2.0", bj(14, 49, 55), nav_reasons=("NAV_FX_RULE_UNKNOWN",)),
                        spec("B", "2.3", "2.0", bj(14, 49, 55))))
    r = snapshot_to_dict(evaluate_bundle(b))["result"]
    assert r["status"] == "INELIGIBLE"


def test_diff_plain_float_tolerance():
    assert diff_plain({"x": 1.0}, {"x": 1.0 + 5e-11}) == []
    assert diff_plain({"x": 1.0}, {"x": 1.0 + 5e-9}) != []


# ---------- 增量状态 ----------

def etf_msg(t_str, prices, received, seq):
    return make_msg(sina_body(t_str, prices), received_utc_ns=received, seq=seq, run_id="LIVE-test",
                    endpoint_id="sina.etf_batch")


def nav_msg(code, rows, received, seq):
    body = json.dumps({"ErrCode": 0, "TotalCount": len(rows), "Data": {"LSJZList": rows}}).encode()
    m = make_msg(body, received_utc_ns=received, seq=seq, source_id="eastmoney", run_id="LIVE-test",
                 endpoint_id=f"eastmoney.lsjz.{code}")
    return replace(m, request=RequestTemplate("GET", f"https://x/lsjz?fundCode={code}"))


def fed_state():
    st = new_state(REPO)
    for i, (vendor, nav) in enumerate(CODES.items()):
        st.ingest(nav_msg(vendor[2:], [{"FSRQ": "2026-09-11", "DWJZ": nav, "LJJZ": nav, "JZZZL": ""}], bj(9, 0, i), i))
    st.ingest(etf_msg("14:49:55", {c: "2.300" for c in CODES}, bj(14, 49, 58), 10))
    return st


def test_state_bundle_current_mode_and_counts_sessions():
    b = fed_state().bundle(bj(14, 50))
    assert b.mode == "CURRENT" and b.price_basis == "ASK" and b.sessions_since_anchor == 2
    assert all(m.nav_verified for m in b.members) and len(b.bounds) == 10
    assert evaluate_bundle(b).result.status.value == "MODEL_REFERENCE"


def test_at46_nav_revision_isolates_fund_and_duplicates_do_not():
    st = fed_state()
    st.ingest(nav_msg("513100", [{"FSRQ": "2026-09-11", "DWJZ": "1.9831", "LJJZ": "1", "JZZZL": ""}], bj(10, 0), 20))
    assert all(m.nav_verified for m in st.bundle(bj(14, 50)).members)  # 重复报文不算修订（AT50）
    st.ingest(nav_msg("513100", [{"FSRQ": "2026-09-11", "DWJZ": "1.9000", "LJJZ": "1", "JZZZL": ""}], bj(10, 1), 21))
    m = next(x for x in st.bundle(bj(14, 50)).members if x.code == "513100")
    assert not m.nav_verified and "PENDING_VERIFY" in m.nav_reasons
    res = evaluate_bundle(st.bundle(bj(14, 50))).result
    assert not next(x for x in res.members if x.code == "513100").eligible
    assert sum(1 for x in res.members if x.eligible) == 4  # 其余四只继续（FR10）


def test_out_of_order_snapshot_does_not_rewind():
    st = fed_state()
    st.ingest(etf_msg("14:40:00", {c: "9.999" for c in CODES}, bj(14, 40, 2), 30))  # 接收时间也在过去
    assert str(st.latest["SSE:513100"].ask) == "2.300"


def test_r3_late_arriving_older_quote_does_not_override():
    st = fed_state()
    # 接收时间更晚（14:49:59），但行情快照时间更早（14:49:30）
    st.ingest(etf_msg("14:49:30", {c: "9.999" for c in CODES}, bj(14, 49, 59), 31))
    b = st.bundle(bj(14, 50))
    assert {m.price for m in b.members} == {"2.300"} and st.stale_rejected == 5


def test_r3_single_member_regression_and_incomplete_batch():
    st = fed_state()
    newer = {c: "2.310" for c in CODES}
    newer["sh513100"] = "9.999"
    body_codes = dict(newer)
    st.ingest(etf_msg("14:49:57", {k: v for k, v in body_codes.items() if k != "sh513100"}, bj(14, 49, 59), 32))
    st.ingest(etf_msg("14:49:40", {"sh513100": "9.999"}, bj(14, 50, 0), 33))  # 单只倒退
    b = st.bundle(bj(14, 50, 1))
    prices = {m.code: m.price for m in b.members}
    assert prices["513100"] == "2.300"  # 不完整批次不抹去旧值，倒退行情被拒绝
    assert {prices[c[2:]] for c in CODES if c != "sh513100"} == {"2.310"}


# ---------- 存储、回放、首屏 ----------

def test_r5_empty_replay_is_no_data_not_success(tmp_path):
    for stream in (False, True):
        report = verify(tmp_path, REPO, ["2026-09-15"], stream=stream)
        assert report["status"] == "NO_DATA" and report["snapshots"] == 0


def test_store_and_bundle_replay_detects_tampering(tmp_path):
    store = SnapshotStore(tmp_path)
    b = fed_state().bundle(bj(14, 50))
    snap = snapshot_to_dict(evaluate_bundle(b))
    store.append(canonical_json(b), snap, 0)
    ok = verify(tmp_path, REPO, ["2026-09-15"])
    assert ok["status"] == "PASSED" and ok["verified"] == 1
    path = store.path_for(b.cutoff_utc_ns)
    rec = json.loads(path.read_text())
    rec["result"]["pairs"][0]["delta"] += 1e-6
    path.write_text(json.dumps(rec) + "\n")
    report = verify(tmp_path, REPO, ["2026-09-15"])
    assert report["status"] == "FAILED" and report["mismatches"] == 1 and report["examples"][0]["kind"] == "RESULT"


def test_r2_same_value_nav_with_later_event_field_isolated():
    st = fed_state()
    st.ingest(nav_msg("513100", [{"FSRQ": "2026-09-11", "DWJZ": "1.9831", "LJJZ": "1.9831", "JZZZL": "",
                                  "FHSP": "每份分红0.1元"}], bj(10, 0), 40))
    m = next(x for x in st.bundle(bj(14, 50)).members if x.code == "513100")
    assert not m.nav_verified and "CORPORATE_ACTION_PENDING" in m.nav_reasons
    res = evaluate_bundle(st.bundle(bj(14, 50))).result
    assert sum(1 for x in res.members if x.eligible) == 4


def test_r4_state_supplies_normalization_factors_across_dates():
    from qdii.core.types import RequestTemplate as RT

    st = fed_state()
    ndx = make_msg(json.dumps({"data": {"symbol": "NDX", "tradesTable": {"rows": [
        {"date": "09/11/2026", "close": "29,368.44"}, {"date": "09/14/2026", "close": "29,127.16"}]}}}).encode(),
        received_utc_ns=bj(9, 1), seq=60, source_id="nasdaq", run_id="LIVE-test", endpoint_id="nasdaq.ndx_history")
    ccpr = make_msg(json.dumps({"data": {"searchlist": ["USD/CNY"]}, "records": [
        {"date": "2026-09-14", "values": ["6.7698"]}, {"date": "2026-09-11", "values": ["6.7743"]}]}).encode(),
        received_utc_ns=bj(9, 2), seq=61, source_id="chinamoney", run_id="LIVE-test", endpoint_id="chinamoney.ccpr")
    for msg in (ndx, replace(ccpr, request=RT("GET", "https://x"))):
        st.ingest(msg)
    st.ingest(nav_msg("513100", [{"FSRQ": "2026-09-14", "DWJZ": "1.9700", "LJJZ": "1.97", "JZZZL": ""}], bj(10, 0), 62))
    b = st.bundle(bj(14, 50))
    m = next(x for x in b.members if x.code == "513100")
    assert (m.nav_date, m.index_at_anchor, m.fx_at_anchor) == ("2026-09-14", "29127.16", "6.7698")
    res = evaluate_bundle(b).result
    assert not res.common_anchor and sum(1 for x in res.members if x.eligible) == 5


def test_r6_calendar_out_of_range_degrades_without_exception():
    st = fed_state()
    b = st.bundle(int(datetime(2027, 1, 4, 10, 0, tzinfo=SH).timestamp() * 1e9))
    assert not b.calendar_covered and b.sessions_since_anchor is None and b.bounds == ()
    snap = snapshot_to_dict(evaluate_bundle(b))
    assert snap["result"]["status"] == "INELIGIBLE" and "CALENDAR_UNCERTAIN" in snap["result"]["reasons"]


def test_decision_snapshot_saved_and_stream_verified(tmp_path):
    raw_state = fed_state()
    b = raw_state.bundle(bj(14, 50))
    SnapshotStore(tmp_path, "decisions").append(canonical_json(b), snapshot_to_dict(evaluate_bundle(b)), 0)
    assert verify(tmp_path, REPO, ["2026-09-15"], kind="decisions")["status"] == "PASSED"


def test_status_page_renders_first_screen():
    st = fed_state()
    b = st.bundle(bj(14, 50))
    rel = view(evaluate_bundle(b), b, {f.code: f.name for f in st.funds})
    page = render_html({
        "run_id": "t", "started_utc_ns": 0, "last_heartbeat_utc_ns": None, "warnings": [],
        "window": {"active": True}, "host": None, "endpoints": [], "etf": {}, "parse_issues": {},
        "events_recent": [], "relative": rel,
    })
    assert "相对比较" in page and "513100" in page and "卖一量" in page
    assert "超出情景边界" in page and "日终历史" in page and "报价错位" in page  # 结论与边界分项同时出现（R1）
    assert "无法判断某只的绝对溢价是否满足买入条件" in page  # 范围声明
    assert "/relative/bundle.json" in page and "未写入快照库" in page


def test_at69_other_factor_group_not_ranked():
    st = fed_state()
    st.funds[0] = replace(st.funds[0], factor_group="SPX_USD_UNHEDGED")
    res = evaluate_bundle(st.bundle(bj(14, 50))).result
    other = next(x for x in res.members if x.code == st.funds[0].code)
    assert not other.eligible and "FACTOR_GROUP_MISMATCH" in [r.value for r in other.reasons]
    assert sum(1 for x in res.members if x.eligible) == 4
