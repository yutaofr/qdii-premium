"""输入包确定性、质量对象（QS-02/03）、增量状态（as-of、净值修订隔离）、快照存储与回放校验。"""

import hashlib
import json
from dataclasses import asdict, replace
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from qdii.apps.relative_snapshot import new_state, view
from qdii.apps.replay_relative import verify
from qdii.apps.status import render_html
from qdii.core.relative import RelativePolicy, anchor_health
from qdii.core.relative_bundle import (
    SCHEMA_VERSION,
    MemberSpec,
    RelativeBundle,
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
    return replace(MemberSpec(code, price, "1000", t, True, nav, "2026-09-11", True, last_price=price,
                              anchor_sessions=2), **kw)


POLICY = tuple(sorted(asdict(RelativePolicy()).items()))


def bundle(**kw):
    base = RelativeBundle(
        schema=SCHEMA_VERSION, cutoff_utc_ns=bj(14, 50), mode="CURRENT", price_basis="ASK", policy=POLICY, calendar_covered=True,
        model_status="HISTORICAL_VALIDATED",
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


def sessions(n):
    return (spec("A", "2.200", "2.0000", bj(14, 49, 55), anchor_sessions=n),
            spec("B", "2.240", "2.0000", bj(14, 49, 52), anchor_sessions=n))


def test_extended_anchor_never_robust_and_aged_is_flagged():
    ext = snapshot_to_dict(evaluate_bundle(bundle(members=sessions(11))))
    assert ext["result"]["pairs"][0]["status"] == "MODEL_REFERENCE"
    assert "ANCHOR_EXTENDED" in ext["result"]["reasons"]
    aged = snapshot_to_dict(evaluate_bundle(bundle(members=sessions(5))))
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
    assert b.mode == "CURRENT" and b.price_basis == "ASK" and {m.anchor_sessions for m in b.members} == {2}
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
    assert not b.calendar_covered and all(m.anchor_sessions is None for m in b.members) and b.bounds == ()
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
    assert "结算日与合约月份未经供应商证实" in page and "即使全部都很贵也会有第一名" in page  # 范围与锚点声明
    assert f"/relative/bundle/{rel['bundle_id']}.json" in page and "未写入快照库" in page


def test_at69_other_factor_group_not_ranked():
    st = fed_state()
    st.funds[0] = replace(st.funds[0], factor_group="SPX_USD_UNHEDGED")
    res = evaluate_bundle(st.bundle(bj(14, 50))).result
    other = next(x for x in res.members if x.code == st.funds[0].code)
    assert not other.eligible and "FACTOR_GROUP_MISMATCH" in [r.value for r in other.reasons]
    assert sum(1 for x in res.members if x.eligible) == 4


# ---------- 二审 F1—F5 ----------

def factor_msgs(ndx_rows, ccpr_rows, seq=70):
    from qdii.core.types import RequestTemplate as RT

    ndx = make_msg(json.dumps({"data": {"symbol": "NDX", "tradesTable": {"rows": [
        {"date": f"{d[5:7]}/{d[8:10]}/{d[:4]}", "close": c} for d, c in ndx_rows]}}}).encode(),
        received_utc_ns=bj(9, 1), seq=seq, source_id="nasdaq", run_id="LIVE-test", endpoint_id="nasdaq.ndx_history")
    ccpr = make_msg(json.dumps({"data": {"searchlist": ["USD/CNY"]}, "records": [
        {"date": d, "values": [v]} for d, v in ccpr_rows]}).encode(),
        received_utc_ns=bj(9, 2), seq=seq + 1, source_id="chinamoney", run_id="LIVE-test", endpoint_id="chinamoney.ccpr")
    return ndx, replace(ccpr, request=RT("GET", "https://x"))


def state_with_navs(nav_dates):
    """nav_dates: 代码 → 净值日；未列出的基金用 2026-09-11。"""
    st = new_state(REPO)
    for i, (vendor, nav) in enumerate(CODES.items()):
        d = nav_dates.get(vendor[2:], "2026-09-11")
        st.ingest(nav_msg(vendor[2:], [{"FSRQ": d, "DWJZ": nav, "LJJZ": nav, "JZZZL": ""}], bj(9, 0, i), i))
    st.ingest(etf_msg("14:49:55", {c: "2.300" for c in CODES}, bj(14, 49, 58), 10))
    return st


def test_f1_missing_close_on_us_trading_day_is_not_replaced_by_older_close():
    st = fed_state()
    for msg in factor_msgs([("2026-09-11", "29,368.44")], [("2026-09-11", "6.7743"), ("2026-09-14", "6.7698")]):
        st.ingest(msg)
    st.ingest(nav_msg("513100", [{"FSRQ": "2026-09-14", "DWJZ": "1.9700", "LJJZ": "1.97", "JZZZL": ""}], bj(10, 0), 62))
    b = st.bundle(bj(14, 50))
    m = next(x for x in b.members if x.code == "513100")
    assert st.cal.session("NASDAQ", date(2026, 9, 14)) is not None  # 09-14 是美股交易日
    assert (m.index_at_anchor, m.index_date, m.fx_at_anchor) == (None, None, None)  # 不得绑定 09-11 的旧收盘
    assert "513100:index_close_missing:2026-09-14" in b.notes
    res = evaluate_bundle(b).result
    assert res.common_anchor and res.anchor_date.isoformat() == "2026-09-11"  # 回退到有效共同日期子集
    assert not next(x for x in res.members if x.code == "513100").eligible
    assert sum(1 for x in res.members if x.eligible) == 4


def test_f1_real_us_holiday_uses_previous_session_close():
    st = state_with_navs({"513100": "2026-09-07"})  # 美国劳工节休市、A 股交易
    rows = [("2026-09-04", "28,900.00"), ("2026-09-11", "29,368.44")]
    for msg in factor_msgs(rows, [("2026-09-07", "6.7800"), ("2026-09-11", "6.7743")]):
        st.ingest(msg)
    assert st.cal.session("NASDAQ", date(2026, 9, 7)) is None
    b = st.bundle(bj(14, 50))
    by = {m.code: m for m in b.members}
    assert (by["513100"].index_date, by["513100"].index_at_anchor, by["513100"].fx_date) == (
        "2026-09-04", "28900.00", "2026-09-07")
    assert by["159696"].index_date == "2026-09-11"
    res = evaluate_bundle(b).result
    assert not res.common_anchor and sum(1 for x in res.members if x.eligible) == 5


def test_f3_future_timestamp_does_not_block_recovery():
    st = fed_state()
    st.ingest(etf_msg("14:59:55", {c: "9.999" for c in CODES}, bj(14, 49, 59), 80))  # 供应商时间领先接收 10 分钟
    st.ingest(etf_msg("14:50:00", {c: "2.310" for c in CODES}, bj(14, 50, 1), 81))
    b = st.bundle(bj(14, 50, 2))
    assert {m.price for m in b.members} == {"2.310"} and st.future_rejected == 5
    res = evaluate_bundle(b).result
    assert sum(1 for x in res.members if x.eligible) == 5


@pytest.mark.parametrize("old_date", ["2026-08-14", "1999-01-04"])  # 过旧 / 超出日历覆盖范围
def test_f5_excluded_member_anchor_does_not_degrade_others(old_date):
    st = state_with_navs({"513100": old_date})
    b = st.bundle(bj(14, 50))
    assert b.calendar_covered
    by = {m.code: m for m in b.members}
    assert {by[c].anchor_sessions for c in by if c != "513100"} == {2}
    snap = evaluate_bundle(b)
    res, q = snap.result, snap.quality
    assert not next(x for x in res.members if x.code == "513100").eligible
    assert sum(1 for x in res.members if x.eligible) == 4
    assert q.anchor_health == "NORMAL" and q.max_anchor_sessions == 2
    assert "ANCHOR_EXTENDED" not in [r.value for r in res.reasons]
    assert "CALENDAR_UNCERTAIN" not in [r.value for r in res.reasons]
    assert res.pairs and all(p.u_diff is not None for p in res.pairs)  # 其余成员对仍按各自锚点取得边界


def test_f4_downloaded_bundle_is_the_one_shown_on_the_page(tmp_path):
    import asyncio
    import re
    import socket

    import httpx

    from qdii.apps.collect import Collector, CollectorConfig
    from qdii.apps.status import serve_status

    class TickClock:  # 每次取时间前进 1 秒：若下载重新构包，bundle_id 必然不同
        def __init__(self):
            self.t = bj(14, 50)

        def now_utc_ns(self):
            self.t += 10**9
            return self.t

        def monotonic_ns(self):
            return self.t

    cfg = CollectorConfig(
        data_root=tmp_path, run_label="test", heartbeat_interval_s=30, gap_threshold_s=90,
        host_probe_interval_s=600, min_disk_free_gb=0, max_clock_offset_ms=2000, status_enabled=False,
        status_interface="lo0", status_port=0, host_window=None, host_probe_enabled=False,
    )
    collector = Collector(cfg, [], 5, clock=TickClock(), repo_root=REPO)
    collector.relative = fed_state()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    async def run():
        stop = asyncio.Event()
        server = asyncio.create_task(serve_status(collector._status_snapshot, interface="lo0", port=port, stop=stop,
                                                  bundles=collector.page_bundles))
        await asyncio.sleep(0.2)
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
                page = (await client.get("/")).text
                shown = re.search(r"/relative/bundle/([0-9a-f]{64})\.json", page).group(1)
                await client.get("/")  # 之后又刷新一次页面，产生新的输入包
                first = await client.get(f"/relative/bundle/{shown}.json")
                missing = await client.get(f"/relative/bundle/{'0' * 64}.json")
                legacy = await client.get("/relative/bundle.json")
                return shown, first, missing, legacy
        finally:
            stop.set()
            await server

    shown, first, missing, legacy = asyncio.run(run())
    assert first.status_code == 200
    body = first.json()
    assert body["bundle_id"] == shown == bundle_id(bundle_from_dict(body["bundle"]))  # 导出包即页面所示包
    assert missing.status_code == 404 and legacy.status_code == 404  # 不重新构包冒充


def test_old_schema_snapshot_reported_as_version_change_not_crash(tmp_path):
    store = SnapshotStore(tmp_path)
    b = fed_state().bundle(bj(14, 50))
    store.append(canonical_json(b), snapshot_to_dict(evaluate_bundle(b)), 0)
    path = store.path_for(b.cutoff_utc_ns)
    rec = json.loads(path.read_text())
    rec["bundle"]["schema"] = SCHEMA_VERSION - 1  # 模拟旧代码写下的记录：bundle_id 与当时内容一致
    rec["bundle_id"] = hashlib.sha256(json.dumps(rec["bundle"], sort_keys=True, separators=(",", ":"),
                                                 ensure_ascii=False).encode()).hexdigest()
    path.write_text(path.read_text() + json.dumps(rec) + "\n")
    report = verify(tmp_path, REPO, ["2026-09-15"])
    assert report["status"] == "VERSION_CHANGED" and report["verified"] == 1 and report["version_changes"] == 1
    assert report["legacy_integrity_ok"] == 1  # 旧记录本身未被篡改：内容哈希仍等于当时的 bundle_id
    path.write_text(json.dumps(rec) + "\n")  # 只有旧结构快照：流模式同样不崩溃
    report = verify(tmp_path, REPO, ["2026-09-15"], stream=True)
    assert report["status"] == "VERSION_CHANGED" and report["examples"][0]["kind"] == "SCHEMA"

    tampered = json.loads(json.dumps(rec))  # 篡改旧记录：完整性核验必须发现
    tampered["bundle"]["cutoff_utc_ns"] += 1
    path.write_text(json.dumps(tampered) + "\n")
    report = verify(tmp_path, REPO, ["2026-09-15"])
    assert report["legacy_integrity_failures"] == 1 and report["legacy_integrity_ok"] == 0
