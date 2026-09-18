"""期货基差探索性研究（2026-09-18 复审整改）：时间语义、端点配对、质量状态与统计。

夹具 yahoo_basis_endpoints_20260917.json 从原始日志原样抽取（父响应 hash 与 msg_id 见文件）；
名称带 synthetic 的序列为合成数据，只用来覆盖边界。
"""

from __future__ import annotations

import importlib.util
import json
import math
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from qdii.core.futures_basis_research import (
    ANALYSIS_UNKNOWNS,
    OFFICIAL_RULE,
    PAIRING_RULE,
    BasisObs,
    CashSession,
    ChartError,
    CrossSession,
    GapKind,
    OfficialClose,
    PriceKind,
    Quality,
    RawSeries,
    analyze,
    block_bootstrap_ci,
    classify,
    estimated_premium_given_error,
    horizon_windows,
    nearest_rank,
    pair_sessions,
    parse_chart,
    resample_session_indices,
    same_start_comparison,
    summarize,
    verify_official_closes,
    within_session_acf,
)

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests" / "fixtures" / "recorded" / "yahoo_basis_endpoints_20260917.json"
NY = ZoneInfo("America/New_York")
FUT, IDX = "NQZ26.CME", "^NDX"
FAR_FUTURE = datetime(2030, 1, 1, tzinfo=NY).timestamp()


def ny(d: str, hm: str) -> int:
    h, m = map(int, hm.split(":"))
    return int(datetime.combine(date.fromisoformat(d), time(h, m), tzinfo=NY).timestamp())


def session(d: str, open_hm: str = "09:30", close_hm: str = "16:00") -> CashSession:
    return CashSession(date.fromisoformat(d), ny(d, open_hm), ny(d, close_hm), early_close=close_hm != "16:00")


# ---------- 夹具（实采） ----------

def recorded(symbol: str) -> RawSeries:
    s = json.loads(FIXTURE.read_text(encoding="utf-8"))["series"][symbol]
    return RawSeries(symbol, tuple(s["timestamp"]), tuple(s["close"]), s["parent_msg_id"],
                     s["received_utc_ns"] / 1e9)


REAL_SESSIONS = (session("2026-09-16"), session("2026-09-17"))
AS_OF = recorded(FUT).received_s


def real_records(official: dict | None = None):
    fut = classify(recorded(FUT), REAL_SESSIONS, cash_index=False)
    idx = classify(recorded(IDX), REAL_SESSIONS, cash_index=True)
    if official is not None:
        idx, _ = verify_official_closes(idx, REAL_SESSIONS, official)
    return fut, idx


OFFICIAL_0917 = {date(2026, 9, 17): OfficialClose(date(2026, 9, 17), "29446.98", "nasdaq:1789709415886284000:2927")}


def test_fixture_is_recorded_not_synthetic_and_carries_provenance():
    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert doc["synthetic"] is False and doc["origin"]["run_id"] == "RESEARCH-20260918T062540Z"
    for s in doc["series"].values():
        assert len(s["parent_body_sha256"]) == 64 and s["parent_msg_id"].startswith("yahoo:")


def test_close_endpoint_uses_the_bar_that_ends_at_the_cash_close():
    """f237d46 在 09-17 取了 NQ 16:00—16:05 线（29737.25）配 NDX 16:00 记录；收盘端点只能是 [15:55, 16:00]。"""
    fut, idx = real_records()
    ends = {e.session.local_date: e for e in pair_sessions(fut, idx, REAL_SESSIONS, as_of_s=AS_OF).endpoints}
    close = ends[date(2026, 9, 17)].close
    assert close.futures == 29744.50 and close.index == 29442.1171875
    assert close.time_s == ny("2026-09-17", "16:00") and close.index_kind is PriceKind.BAR_5M


def test_ndx_record_at_the_close_instant_is_not_a_regular_bar():
    _, idx = real_records()
    at_close = [r for r in idx if r.raw_ts == ny("2026-09-17", "16:00")]
    assert len(at_close) == 1 and at_close[0].kind is PriceKind.SESSION_CLOSE_RECORD
    assert at_close[0].close == 29446.98046875
    regular = [r for r in idx if r.kind is PriceKind.BAR_5M and r.session_date == date(2026, 9, 17)]
    assert len(regular) == 78 and max(r.interval_end for r in regular) == ny("2026-09-17", "16:00")


def test_official_close_diagnostic_is_separate_from_the_bar_close_series():
    fut, idx = real_records(OFFICIAL_0917)
    ends = {e.session.local_date: e for e in pair_sessions(fut, idx, REAL_SESSIONS, as_of_s=AS_OF).endpoints}
    e = ends[date(2026, 9, 17)]
    assert e.official_close.index == 29446.98046875 and e.official_close.index_kind is PriceKind.OFFICIAL_CLOSE
    assert e.official_close.futures == 29744.50  # 结束于现金收盘的期货线，不是 16:00—16:05 线
    assert e.close.index == 29442.1171875  # 主序列仍是普通线，两口径不混同


def test_close_record_stays_unverified_without_or_against_official_source():
    fut, idx = real_records()
    e = pair_sessions(fut, idx, REAL_SESSIONS, as_of_s=AS_OF).endpoints[1]
    assert e.official_close is None and "INDEX_CLOSE_RECORD_UNVERIFIED" in e.official_reasons
    assert "INDEX_CLOSE_RECORD_UNVERIFIED" not in e.reasons  # 诊断口径的缺口不混入主序列
    wrong = {date(2026, 9, 17): OfficialClose(date(2026, 9, 17), "29440.00", "test")}
    idx2, log = verify_official_closes(classify(recorded(IDX), REAL_SESSIONS, cash_index=True), REAL_SESSIONS, wrong)
    assert all(r.kind is not PriceKind.OFFICIAL_CLOSE for r in idx2)
    assert log[0]["status"] == "MISMATCH"


def test_official_diagnostic_without_enough_official_records_is_no_data():
    fut, idx = real_records(OFFICIAL_0917)
    res = pair_sessions(fut, idx, REAL_SESSIONS, as_of_s=AS_OF, rule=OFFICIAL_RULE)
    assert res.status == "NO_DATA" and res.samples == ()
    assert any(x.reason == "PREV_SESSION_OFFICIAL_CLOSE_MISSING" for x in res.exclusions)


def test_cross_session_sample_is_timed_by_interval_ends():
    fut, idx = real_records()
    res = pair_sessions(fut, idx, REAL_SESSIONS, as_of_s=AS_OF)
    assert res.rule == PAIRING_RULE and res.status == "OK" and len(res.samples) == 1
    s = res.samples[0]
    assert s.start.time_s == ny("2026-09-16", "16:00") and s.end.time_s == ny("2026-09-17", "09:35")
    assert s.elapsed_hours == pytest.approx(17 + 35 / 60)
    assert s.gap is GapKind.NORMAL
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))["series"]
    t = dict(zip(raw[FUT]["timestamp"], raw[FUT]["close"], strict=True))
    i = dict(zip(raw[IDX]["timestamp"], raw[IDX]["close"], strict=True))
    b0 = t[ny("2026-09-16", "15:55")] / i[ny("2026-09-16", "15:55")] - 1
    b1 = t[ny("2026-09-17", "09:30")] / i[ny("2026-09-17", "09:30")] - 1
    assert s.basis_drift == b1 - b0  # 未舍入


def test_off_grid_latest_record_is_not_treated_as_a_bar():
    fut, _ = real_records()
    last = fut[-1]
    assert last.raw_ts % 300 != 0 and last.kind is PriceKind.UNKNOWN


def test_session_still_open_at_fetch_contributes_no_close_endpoint():
    fut, idx = real_records()
    res = pair_sessions(fut, idx, REAL_SESSIONS, as_of_s=ny("2026-09-17", "12:00"))
    e = res.endpoints[1]
    assert e.close is None and "SESSION_INCOMPLETE_AT_FETCH" in e.reasons


# ---------- 合成序列（synthetic） ----------

def synthetic(days: list[tuple[str, str]], basis_bp=lambda day, k: 100.0, *, received_s: float = FAR_FUTURE,
              drop: tuple[tuple[str, str, str], ...] = (), override: dict | None = None):
    """synthetic：每个 (日期, 收盘时刻) 生成完整五分钟线。期货多给一根收盘后的线，指数多给一条收盘时刻记录。"""
    fts, fcs, its, ics = [], [], [], []
    for d, close_hm in days:
        t, end, k = ny(d, "09:30"), ny(d, close_hm), 0
        while t <= end:
            index = 20000.0 + 10 * k + (0 if t < end else 1)
            futures = index * (1 + basis_bp(d, k) / 1e4)
            if (FUT, d, _hm(t)) not in drop:
                fts.append(t)
                fcs.append((override or {}).get((FUT, t), futures))
            if (IDX, d, _hm(t)) not in drop:
                its.append(t)
                ics.append((override or {}).get((IDX, t), index))
            t, k = t + 300, k + 1
    return (RawSeries(FUT, tuple(fts), tuple(fcs), "synthetic:fut", received_s),
            RawSeries(IDX, tuple(its), tuple(ics), "synthetic:idx", received_s))


def _hm(t: int) -> str:
    return datetime.fromtimestamp(t, NY).strftime("%H:%M")


def run(days, sessions, **kw):
    f, i = synthetic(days, **{k: v for k, v in kw.items() if k != "as_of_s"})
    return pair_sessions(classify(f, sessions, cash_index=False), classify(i, sessions, cash_index=True), sessions,
                         as_of_s=kw.get("as_of_s", FAR_FUTURE))


def test_early_close_uses_the_bar_ending_at_1300():
    days = [("2026-11-27", "13:00"), ("2026-11-30", "16:00")]
    sessions = (session("2026-11-27", close_hm="13:00"), session("2026-11-30"))
    res = run(days, sessions)
    close = res.endpoints[0].close
    assert close.time_s == ny("2026-11-27", "13:00") and close.index_kind is PriceKind.BAR_5M
    s = res.samples[0]
    assert s.early_close_from is True and s.gap is GapKind.WEEKEND


def test_winter_and_summer_sessions_are_paired_by_their_own_utc_instants():
    days = [("2026-10-30", "16:00"), ("2026-11-02", "16:00")]  # 美国 11-01 结束夏令时
    res = run(days, (session("2026-10-30"), session("2026-11-02")))
    s = res.samples[0]
    assert datetime.fromtimestamp(s.start.time_s, ZoneInfo("UTC")).hour == 20  # 16:00 EDT
    assert datetime.fromtimestamp(s.end.time_s, ZoneInfo("UTC")).strftime("%H:%M") == "14:35"  # 09:35 EST
    assert s.elapsed_hours == pytest.approx(65 + 35 / 60 + 1)


def test_holiday_takes_precedence_over_weekend():
    days = [("2026-09-04", "16:00"), ("2026-09-08", "16:00")]  # 09-07 劳动节
    res = run(days, (session("2026-09-04"), session("2026-09-08")))
    s = res.samples[0]
    assert s.gap is GapKind.HOLIDAY_EXTENDED and s.non_session_weekdays == (date(2026, 9, 7),)


def test_missing_trading_day_is_a_data_gap_not_a_long_overnight():
    days = [("2026-09-14", "16:00"), ("2026-09-16", "16:00")]  # 周二 09-15 无数据
    sessions = (session("2026-09-14"), session("2026-09-15"), session("2026-09-16"))
    res = run(days, sessions)
    assert res.samples == () and res.status == "NO_DATA"
    reasons = {(x.key, x.reason) for x in res.exclusions if x.scope == "CROSS_SESSION"}
    assert ("2026-09-14→2026-09-15", "NEXT_SESSION_FIRST_BAR_MISSING") in reasons
    assert ("2026-09-15→2026-09-16", "PREV_SESSION_LAST_BAR_MISSING") in reasons


def test_missing_first_or_last_bar_excludes_only_the_affected_pair():
    days = [("2026-09-14", "16:00"), ("2026-09-15", "16:00"), ("2026-09-16", "16:00")]
    sessions = tuple(session(d) for d, _ in days)
    res = run(days, sessions, drop=((IDX, "2026-09-15", "09:30"),))
    assert [(s.from_date.isoformat(), s.to_date.isoformat()) for s in res.samples] == [("2026-09-15", "2026-09-16")]


def test_unknown_calendar_yields_no_samples():
    f, i = synthetic([("2026-09-14", "16:00"), ("2026-09-15", "16:00")])
    res = pair_sessions(classify(f, None, cash_index=False), classify(i, None, cash_index=True), None,
                        as_of_s=FAR_FUTURE)
    assert res.status == "CALENDAR_UNCERTAIN" and res.samples == ()


def test_empty_and_single_session_inputs_do_not_crash():
    empty = RawSeries(FUT, (), (), "synthetic:empty", FAR_FUTURE)
    res = pair_sessions(classify(empty, (), cash_index=False), classify(empty, (), cash_index=True), (),
                        as_of_s=FAR_FUTURE)
    assert res.status == "NO_DATA" and res.samples == ()
    single = run([("2026-09-14", "16:00")], (session("2026-09-14"),))
    assert single.status == "NO_DATA" and single.samples == ()


def test_bar_not_finished_at_receipt_is_partial():
    f, _ = synthetic([("2026-09-14", "16:00")], received_s=ny("2026-09-14", "10:02"))
    recs = {r.raw_ts: r for r in classify(f, (session("2026-09-14"),), cash_index=False)}
    assert recs[ny("2026-09-14", "09:55")].kind is PriceKind.BAR_5M  # 10:00 结束，已完整
    assert recs[ny("2026-09-14", "10:00")].kind is PriceKind.PARTIAL_BAR  # 10:05 才结束


@pytest.mark.parametrize(("value", "quality"), [
    (None, Quality.MISSING), (0.0, Quality.NON_POSITIVE), (-1.0, Quality.NON_POSITIVE),
    (math.nan, Quality.NON_FINITE), (math.inf, Quality.NON_FINITE), ("29744.5", Quality.NON_NUMERIC),
    (True, Quality.NON_NUMERIC),
])
def test_invalid_close_values_are_flagged_and_break_the_endpoint(value, quality):
    days = [("2026-09-14", "16:00"), ("2026-09-15", "16:00")]
    sessions = (session("2026-09-14"), session("2026-09-15"))
    t = ny("2026-09-14", "15:55")
    f, i = synthetic(days, override={(FUT, t): value})
    fut = classify(f, sessions, cash_index=False)
    assert next(r for r in fut if r.raw_ts == t).quality is quality
    res = pair_sessions(fut, classify(i, sessions, cash_index=True), sessions, as_of_s=FAR_FUTURE)
    assert res.samples == () and "FUTURES_LAST_BAR_MISSING" in res.endpoints[0].reasons


def test_duplicate_timestamps_identical_kept_once_conflicting_excluded():
    base = RawSeries(IDX, (ny("2026-09-14", "15:55"),) * 2, (100.0, 100.0), "synthetic:dup", FAR_FUTURE)
    recs = classify(base, (session("2026-09-14"),), cash_index=True)
    assert [r.quality for r in recs] == [Quality.OK, Quality.DUPLICATE_IDENTICAL]
    conflict = RawSeries(IDX, (ny("2026-09-14", "15:55"),) * 2, (100.0, 101.0), "synthetic:dup", FAR_FUTURE)
    assert {r.quality for r in classify(conflict, (session("2026-09-14"),), cash_index=True)} == {
        Quality.DUPLICATE_CONFLICT}


def test_index_record_outside_the_cash_session_is_unknown():
    s = RawSeries(IDX, (ny("2026-09-14", "09:25"), ny("2026-09-13", "10:00")), (1.0, 1.0), "synthetic:x", FAR_FUTURE)
    assert {r.kind for r in classify(s, (session("2026-09-14"),), cash_index=True)} == {PriceKind.UNKNOWN}


# ---------- 响应解析 ----------

def chart(ts, closes, symbol=FUT):
    return json.dumps({"chart": {"result": [{"meta": {"symbol": symbol}, "timestamp": ts,
                                             "indicators": {"quote": [{"close": closes}]}}], "error": None}}).encode()


def test_parse_chart_reads_symbol_and_columns():
    s = parse_chart(chart([1, 2], [3.0, None]), source_ref="m", received_s=9.0)
    assert (s.instrument, s.timestamps, s.closes, s.source_ref, s.received_s) == (FUT, (1, 2), (3.0, None), "m", 9.0)


@pytest.mark.parametrize(("body", "code"), [
    (b"<html>", "JSON_INVALID"),
    (json.dumps({"chart": {"result": None, "error": {"code": "Not Found"}}}).encode(), "NO_RESULT"),
    (chart([1, 2], [3.0]), "LENGTH_MISMATCH"),
    (json.dumps({"chart": {"result": [{"meta": {}}]}}).encode(), "NO_RESULT"),
])
def test_parse_chart_errors_are_explicit(body, code):
    with pytest.raises(ChartError) as err:
        parse_chart(body, source_ref="m", received_s=0.0)
    assert err.value.code == code


# ---------- 日历调度（工具 I/O 层，调用现有 CalendarProvider） ----------

def load_tool():
    spec = importlib.util.spec_from_file_location("futures_basis_intraday", REPO / "tools" / "futures_basis_intraday.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_tool_sessions_follow_the_calendar_across_dst_early_close_and_holidays():
    from qdii.io.calendars import CalendarProvider

    tool = load_tool()
    cal = CalendarProvider(REPO / "config" / "calendar_overrides.toml")
    got = {s.local_date: s for s in tool.expected_sessions(cal, date(2026, 9, 3), date(2026, 12, 1))}
    utc = ZoneInfo("UTC")
    assert datetime.fromtimestamp(got[date(2026, 9, 17)].open_s, utc).strftime("%H:%M") == "13:30"  # 夏令时
    assert datetime.fromtimestamp(got[date(2026, 12, 1)].open_s, utc).strftime("%H:%M") == "14:30"  # 冬令时
    early = got[date(2026, 11, 27)]
    assert early.early_close and datetime.fromtimestamp(early.close_s, NY).strftime("%H:%M") == "13:00"
    assert date(2026, 9, 7) not in got and date(2026, 11, 26) not in got  # 劳动节、感恩节
    assert tool.holiday_names(cal, date(2026, 9, 3), date(2026, 12, 1))[date(2026, 9, 7)] == "Labor Day"


def test_tool_sessions_are_none_when_the_calendar_is_uncertain(tmp_path):
    from qdii.io.calendars import CalendarProvider

    bad = tmp_path / "overrides.toml"
    bad.write_text("[nonsense]\nx = 1\n", encoding="utf-8")
    tool = load_tool()
    assert tool.expected_sessions(CalendarProvider(bad), date(2026, 9, 3), date(2026, 9, 18)) is None
    assert tool.expected_sessions(CalendarProvider(REPO / "config" / "calendar_overrides.toml"),
                                  date(2026, 9, 3), date(2028, 1, 5)) is None  # 超出日历覆盖范围


def test_single_day_offset_helper_is_sane():
    assert ny("2026-09-17", "16:00") - ny("2026-09-17", "15:55") == 300
    assert timedelta(seconds=ny("2026-09-17", "09:35") - ny("2026-09-16", "16:00")) == timedelta(hours=17, minutes=35)


# ================= 任务 3：统计只回答可观测问题 =================

def test_nearest_rank_positions_are_exact():
    xs = [float(v) for v in range(1, 22)]  # 21 个样本
    q = nearest_rank(xs, 0.95)
    assert q == {"method": "NEAREST_RANK", "p": 0.95, "n": 21, "rank": 20, "index": 19, "value": 20.0}
    assert nearest_rank([5.0, 1.0, 3.0, 2.0], 0.5)["value"] == 2.0  # ceil(0.5×4)=2
    assert nearest_rank(list(map(float, range(1, 21))), 0.95)["rank"] == 19  # p·n 为整数时不取下一位
    assert nearest_rank([], 0.95)["value"] is None


def test_summary_is_computed_before_any_rounding():
    s = summarize([0.04, -0.04, 0.04])  # 如先舍入到 0.1 会全部变 0
    assert s["signed_mean"] == pytest.approx(0.04 / 3) and s["abs_max"] == 0.04
    assert s["signed_variance"] == pytest.approx(((0.04 - 0.04 / 3) ** 2 * 2 + (0.04 + 0.04 / 3) ** 2) / 2)
    assert summarize([])["n"] == 0 and summarize([])["abs_p95"]["value"] is None


def test_direction_example_from_section_1():
    """b(c)=0.01、b(t)=0.0095：基差收窄 5bp → 代理净值相对偏低约 4.9505bp；真实溢价 10% 时估算溢价偏高约 5.4482bp。"""
    start = BasisObs(0, 101.0, 100.0, PriceKind.BAR_5M, "a", "b")
    end = BasisObs(3600, 100.95 * 1.02, 100.0 * 1.02, PriceKind.BAR_5M, "c", "d")
    x = CrossSession(PAIRING_RULE, date(2026, 1, 1), date(2026, 1, 2), start, end, GapKind.NORMAL, (), False)
    assert x.basis_drift == pytest.approx(-0.0005)
    assert x.relative_return_error * 1e4 == pytest.approx(-4.9505, abs=5e-5)
    assert x.relative_return_error == pytest.approx(x.basis_drift / (1 + start.basis))
    assert x.return_difference == pytest.approx((end.index / start.index) * x.basis_drift / (1 + start.basis))
    bias = estimated_premium_given_error(0.10, x.relative_return_error) - 0.10
    assert bias * 1e4 == pytest.approx(5.4482, abs=5e-5)


def windows_for(days, sessions, **kw):
    f, i = synthetic(days, **kw)
    ends = pair_sessions(classify(f, sessions, cash_index=False), classify(i, sessions, cash_index=True), sessions,
                         as_of_s=FAR_FUTURE)
    fut, idx = classify(f, sessions, cash_index=False), classify(i, sessions, cash_index=True)
    return fut, idx, ends


def test_windows_start_at_the_first_bar_close_and_do_not_overlap():
    days = [("2026-09-14", "16:00")]
    sessions = (session("2026-09-14"),)
    fut, idx, _ = windows_for(days, sessions)
    ws, excl = horizon_windows(fut, idx, sessions, hours=1, as_of_s=FAR_FUTURE)
    assert [(_hm(w.start.time_s), _hm(w.end.time_s)) for w in ws] == [
        ("09:35", "10:35"), ("10:35", "11:35"), ("11:35", "12:35"), ("12:35", "13:35"), ("13:35", "14:35"),
        ("14:35", "15:35")]
    assert excl == ()
    assert [(_hm(w.start.time_s), _hm(w.end.time_s)) for w in horizon_windows(fut, idx, sessions, hours=6,
                                                                              as_of_s=FAR_FUTURE)[0]] == [
        ("09:35", "15:35")]
    early = (session("2026-11-27", close_hm="13:00"),)
    fe, ie, _ = windows_for([("2026-11-27", "13:00")], early)
    assert len(horizon_windows(fe, ie, early, hours=3, as_of_s=FAR_FUTURE)[0]) == 1
    assert horizon_windows(fe, ie, early, hours=6, as_of_s=FAR_FUTURE)[0] == ()


def test_window_with_any_missing_interior_bar_is_excluded_not_filled():
    sessions = (session("2026-09-14"),)
    fut, idx, _ = windows_for([("2026-09-14", "16:00")], sessions, drop=((IDX, "2026-09-14", "10:00"),))
    ws, excl = horizon_windows(fut, idx, sessions, hours=1, as_of_s=FAR_FUTURE)
    assert [_hm(w.start.time_s) for w in ws] == ["10:35", "11:35", "12:35", "13:35", "14:35"]  # 09:35—10:35 缺内部线
    assert [(x.key, x.reason) for x in excl] == [("2026-09-14 open+5m/1h", "INTERIOR_BAR_MISSING")]


def test_same_start_comparison_uses_one_set_of_complete_sessions():
    days = [("2026-09-14", "16:00"), ("2026-09-15", "16:00")]
    sessions = tuple(session(d) for d, _ in days)
    fut, idx, _ = windows_for(days, sessions, drop=((FUT, "2026-09-15", "14:00"),))  # 09-15 的 6 小时窗口不完整
    cmp = same_start_comparison(fut, idx, sessions, hours=(1, 2, 3, 6), as_of_s=FAR_FUTURE)
    assert cmp["sessions"] == ["2026-09-14"]
    assert all(v["n"] == 1 for v in cmp["by_horizon"].values())


def test_endpoint_statistics_never_produce_an_asia_bound():
    """反例：两个端点只差 5bp，区间中途偏离 20bp。端点统计再小也不能给出亚洲界限。"""
    def bp(day, k):
        if day == "2026-09-15":
            return 105.0
        return 120.0 if 30 <= k <= 40 else 100.0

    days = [("2026-09-14", "16:00"), ("2026-09-15", "16:00")]
    sessions = tuple(session(d) for d, _ in days)
    f, i = synthetic(days, basis_bp=bp)
    out = analyze(f, i, sessions, as_of_s=FAR_FUTURE)
    assert out["asia_decision_error_status"] == "UNIDENTIFIED"
    assert out["asia_decision_error_bound_bp"] is None and out["decision_grade"] is False
    assert out["cross_session"]["values_bp"]["basis_drift"] == [pytest.approx(5.0, abs=1e-6)]
    interior = out["intraday"]["by_horizon"]["1h"]["summary"]["abs_max"]
    assert interior == pytest.approx(20.0, abs=1e-6)  # 端点 5bp 不约束区间内部的 20bp 偏离


def test_unknown_status_is_fixed_whatever_the_sample_size():
    days = [(d.isoformat(), "16:00") for d in (date(2026, 3, 2) + timedelta(days=k) for k in range(60))
            if d.weekday() < 5]
    sessions = tuple(session(d) for d, _ in days)
    f, i = synthetic(days)
    out = analyze(f, i, sessions, as_of_s=FAR_FUTURE, bootstrap_reps=50)
    assert {k: out[k] for k in ANALYSIS_UNKNOWNS} == {
        "asia_decision_error_status": "UNIDENTIFIED", "asia_decision_error_bound_bp": None, "decision_grade": False}
    assert out["cross_session"]["summary"]["abs_p95"]["value"] == pytest.approx(0.0, abs=1e-9)


def test_cross_session_is_stratified_by_gap_kind_with_elapsed_hours():
    days = [("2026-09-03", "16:00"), ("2026-09-04", "16:00"), ("2026-09-08", "16:00"), ("2026-09-09", "16:00")]
    sessions = tuple(session(d) for d, _ in days)
    f, i = synthetic(days)
    out = analyze(f, i, sessions, as_of_s=FAR_FUTURE)
    strata = out["cross_session"]["by_gap"]
    assert strata["NORMAL"]["n"] == 2 and strata["HOLIDAY_EXTENDED"]["n"] == 1 and strata["WEEKEND"]["n"] == 0
    hol = next(s for s in out["cross_session"]["samples"] if s["gap"] == "HOLIDAY_EXTENDED")
    assert hol["non_session_weekdays"] == ["2026-09-07"] and hol["elapsed_hours"] == pytest.approx(89 + 35 / 60)


def test_resampling_is_reproducible_and_shared_across_horizons():
    a = resample_session_indices(22, reps=5, seed=20260918)
    assert a == resample_session_indices(22, reps=5, seed=20260918)
    assert a != resample_session_indices(22, reps=5, seed=1)
    assert all(len(r) == 22 and all(0 <= k < 22 for k in r) for r in a)


def test_day_block_bootstrap_keeps_each_session_whole():
    by_session = [[1.0, 1.0, 1.0], [5.0, 5.0, 5.0]]
    seen = []
    ci = block_bootstrap_ci({"h": by_session}, lambda xs: seen.append(sorted(xs)) or sum(xs) / len(xs),
                            reps=200, seed=7, min_sessions=2)
    assert ci["h"]["status"] == "OK"
    assert all(len(xs) == 6 and xs.count(1.0) in (0, 3, 6) for xs in seen)  # 日内样本整块进出


def test_bootstrap_is_withheld_below_thirty_sessions():
    ci = block_bootstrap_ci({"h": [[1.0]] * 29}, lambda xs: sum(xs), reps=10, seed=1)
    assert ci["h"] == {"status": "INSUFFICIENT_SESSIONS", "sessions": 29, "ci": None}


def test_acf_within_sessions_is_null_for_constant_or_short_input():
    const = within_session_acf([[3.0] * 10, [3.0] * 10], lags=(1,))
    assert const[0]["acf"] is None and const[0]["pairs"] == 18 and const[0]["sessions"] == 2
    assert within_session_acf([[1.0]], lags=(1,))[0]["acf"] is None
    alt = within_session_acf([[1.0, -1.0] * 5], lags=(1,))
    assert alt[0]["acf"] == pytest.approx(-0.9) and alt[0]["input"] == "BASIS_LEVEL_MINUS_SESSION_MEAN"
    split = within_session_acf([[1.0, 2.0], [3.0, 4.0]], lags=(1,))
    assert split[0]["pairs"] == 2  # 不跨会话拼接
