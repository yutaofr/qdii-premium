"""美股收盘期货锚点：hf_NQ 契约、VM-03 选取、日历收盘窗口、跟踪器与锚点库、回放校验。"""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from qdii.apps.collect import Collector, CollectorConfig
from qdii.apps.replay_anchors import verify
from qdii.contracts import sina_hf_v1
from qdii.core.anchor import FuturesSample, in_roll_window, select_anchor
from qdii.core.types import PriceType, ReasonCode
from qdii.io import rawlog
from qdii.io.anchor_store import AnchorStore
from qdii.io.calendars import CalendarProvider
from qdii.io.http import EndpointRequest
from qdii.pipeline.anchor_capture import AnchorTracker
from qdii.pipeline.host_windows import CloseWindow, CompositeWindow
from qdii.pipeline.sources import EndpointConfig
from qdii.pipeline.windows import HostWindow, Schedule
from tests.helpers import make_msg, recorded_sina

REPO = Path(__file__).resolve().parents[2]
CAL = CalendarProvider(REPO / "config" / "calendar_overrides.toml")
C = int(datetime(2026, 9, 15, 20, 0, tzinfo=UTC).timestamp()) * 10**9  # 2026-09-15 16:00 ET


def ns(*args):
    return int(datetime(*args, tzinfo=UTC).timestamp()) * 10**9


def hf_body(provider_utc_ns, bid="29167.25", ask="29168.50", price="29165.800"):
    from zoneinfo import ZoneInfo

    local = datetime.fromtimestamp(provider_utc_ns / 1e9, tz=ZoneInfo("Asia/Shanghai"))
    f = [price, "", bid, ask, "", "", local.strftime("%H:%M:%S"), "", "", "", "", "", local.strftime("%Y-%m-%d"),
         "纳斯达克指数期货", ""]
    return f'var hq_str_hf_NQ="{",".join(f)}";'.encode("gb18030")


def hf_msg(provider_utc_ns, received_utc_ns, seq, **kw):
    return make_msg(hf_body(provider_utc_ns, **kw), received_utc_ns=received_utc_ns, seq=seq, run_id="LIVE-test",
                    endpoint_id="sina.hf_NQ")


# ---------- 契约 ----------

def test_recorded_hf_nq_sample():
    msg = recorded_sina("hf_NQ", "referer")
    recs = {r.price_type: r for r in sina_hf_v1.parse(replace(msg, endpoint_id="sina.hf_NQ")).records}
    assert recs[PriceType.BID].value == Decimal("29167.250") and recs[PriceType.ASK].value == Decimal("29168.500")
    assert recs[PriceType.CALCULATED].value == Decimal("29165.800")
    assert all(ReasonCode.CONTRACT_UNKNOWN in r.reason_codes for r in recs.values())
    assert ReasonCode.TICK_MISMATCH not in recs[PriceType.BID].reason_codes
    # time 07:12:43 北京 = 2026-09-14 23:12:43 UTC，接收时间约 1 秒后
    assert abs(recs[PriceType.BID].provider_time.utc_ns - msg.received_utc_ns) < 5e9


def test_hf_403_is_blocked():
    r = sina_hf_v1.parse(make_msg(b"Forbidden", status=403))
    assert r.records == () and r.issues[0].code is ReasonCode.SOURCE_ACCESS_BLOCKED


# ---------- VM-03 选取 ----------

def sample(sec_before_c, received_after_c=1, bid="29000.00", ask="29000.25", msg="m", tick_ok=True):
    return FuturesSample(msg, C - sec_before_c * 10**9, C + received_after_c * 10**9, Decimal(bid), Decimal(ask), tick_ok)


def select(samples):
    return select_anchor(samples, series_id="s", c_utc_ns=C, cutoff_utc_ns=C + 120 * 10**9, et_date=date(2026, 9, 15))


def test_ready_uses_last_sample_at_or_before_c_mid_price():
    r = select([sample(40, msg="a"), sample(5, bid="29100.00", ask="29100.50", msg="b"),
                FuturesSample("future", C + 3 * 10**9, C + 4 * 10**9, Decimal(1), Decimal(2), True)])
    assert (r.status, r.value, r.sample_msg_id, r.lag_s) == ("READY", "29100.25", "b", 5.0)
    assert "CONTRACT_UNKNOWN" in r.reason_codes and r.roll_window is True  # 2026-09-15 在 9 月合约换月窗口内


@pytest.mark.parametrize(("sec", "status"), [(30, "READY"), (31, "DEGRADED"), (60, "DEGRADED"), (61, "FAILED")])
def test_lag_thresholds(sec, status):
    assert select([sample(sec)]).status == status


def test_missing_when_only_future_late_or_invalid_samples():
    late = sample(5, received_after_c=121)  # c+2 分钟之后才收到
    crossed = sample(3, bid="29001", ask="29000")  # bid > ask
    r = select([late, crossed])
    assert r.status == "MISSING" and "FUTURE_ANCHOR_MISSING" in r.reason_codes


def test_tick_mismatch_flag_and_roll_window():
    assert "TICK_MISMATCH" in select([sample(5, tick_ok=False)]).reason_codes
    assert in_roll_window(date(2026, 9, 14)) and in_roll_window(date(2026, 12, 18))
    assert not in_roll_window(date(2026, 9, 22)) and not in_roll_window(date(2026, 10, 15))


# ---------- 日历收盘窗口 ----------

def test_close_window_follows_calendar_and_early_close():
    w = CloseWindow(CAL)
    assert w.active(C - 300 * 10**9) and w.active(C + 179 * 10**9) and not w.active(C + 180 * 10**9)
    assert not w.active(ns(2026, 9, 19, 20, 0))  # 周六
    early = ns(2026, 11, 27, 18, 0)  # 感恩节后提前收盘 13:00 ET
    assert w.active(early) and not w.active(ns(2026, 11, 27, 21, 0))
    assert w.next_bounds(ns(2026, 9, 18, 21, 0))[0] == ns(2026, 9, 21, 19, 55)  # 周五收盘后 → 周一
    assert w.overlap_s(ns(2026, 9, 15, 0, 0), ns(2026, 9, 16, 0, 0)) == 480


def test_composite_window_with_ashare_window():
    comp = CompositeWindow((HostWindow("Europe/Paris", datetime.min.time().replace(hour=7, minute=30),
                                       "Asia/Shanghai", datetime.min.time().replace(hour=15, minute=5)),
                            CloseWindow(CAL)))
    assert comp.active(ns(2026, 9, 15, 6, 0)) and comp.active(C) and not comp.active(ns(2026, 9, 15, 12, 0))
    assert comp.next_bounds(ns(2026, 9, 15, 12, 0))[0] == C - 300 * 10**9
    assert comp.overlap_s(ns(2026, 9, 15, 0, 0), ns(2026, 9, 16, 0, 0)) == 95 * 60 + 480


# ---------- 跟踪器、锚点库与回放 ----------

def test_tracker_due_store_and_replay(tmp_path):
    writer = rawlog.RawLogWriter(tmp_path)
    msgs = [hf_msg(C - 12 * 10**9, C - 11 * 10**9, 1), hf_msg(C - 2 * 10**9, C - 1 * 10**9, 2, bid="29200.00", ask="29200.50"),
            hf_msg(C + 3 * 10**9, C + 4 * 10**9, 3, bid="1.00", ask="2.00")]
    tracker = AnchorTracker(CloseWindow(CAL))
    for m in msgs:
        writer.append(m)
        tracker.ingest(m)
    writer.close()
    assert all(r.c_utc_ns != C for r in tracker.due(C + 60 * 10**9, set()))  # c+2 分钟之前不评估当天收盘
    tracker = AnchorTracker(CloseWindow(CAL))
    for m in msgs:
        tracker.ingest(m)
    results = tracker.due(C + 121 * 10**9, set())
    today = [r for r in results if r.c_utc_ns == C]
    assert len(today) == 1 and (today[0].status, today[0].value, today[0].lag_s) == ("READY", "29200.25", 2.0)
    store = AnchorStore(tmp_path)
    for r in results:
        store.append(r, 0)
    assert C in store.done("sina:hf_NQ")
    report = verify(tmp_path, REPO, ["2026-09-15", "2026-09-14", "2026-09-11"])
    assert report["status"] == "PASSED" and report["verified"] == len(results)


def test_collector_captures_anchor_end_to_end(tmp_path):
    """真实 Collector：窗口门控 + 轮询 + 到期评估 + 锚点库；时钟注入为收盘附近。"""

    class FakeClock:
        def __init__(self):
            import time
            self.offset = C + 118 * 10**9 - time.time_ns()  # 从 c+118 秒开始：先收到样本，约 2 秒后到期评估

        def now_utc_ns(self):
            import time
            return time.time_ns() + self.offset

        def monotonic_ns(self):
            import time
            return time.monotonic_ns()

    clock = FakeClock()

    def handler(request):
        return httpx.Response(200, content=hf_body(C - 7 * 10**9, bid="29300.00", ask="29300.25"))

    cfg = CollectorConfig(
        data_root=tmp_path, run_label="test", heartbeat_interval_s=30, gap_threshold_s=90, host_probe_interval_s=600,
        min_disk_free_gb=0, max_clock_offset_ms=2000, status_enabled=False, status_interface="lo0", status_port=0,
        host_window=CloseWindow(CAL), host_probe_enabled=False, anchor_window=CloseWindow(CAL),
    )
    ep = EndpointConfig(EndpointRequest("sina.hf_NQ", "sina", "GET", "https://hq.sinajs.cn/list=hf_NQ", ()),
                        Schedule((), 0.2), None, True)
    collector = Collector(cfg, [ep], 5, clock=clock, transport=httpx.MockTransport(handler))
    collector.anchor_check_interval_s = 0.2

    async def run():
        asyncio.get_running_loop().call_later(3.0, collector.stop.set)
        return await collector.run()

    assert asyncio.run(run()) == 0
    anchors = [r["anchor"] for r in AnchorStore(tmp_path).iter() if r["anchor"]["c_utc_ns"] == C]
    assert len(anchors) == 1 and anchors[0]["status"] == "READY" and anchors[0]["value"] == "29300.125"
    events = [json.loads(line)["type"] for line in (tmp_path / "events" / "collector.jsonl").open()]
    assert "ANCHOR_CAPTURE" in events
