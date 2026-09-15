from datetime import UTC, datetime, time
from pathlib import Path

from qdii.io.http import BlockList, EndpointState
from qdii.pipeline.sources import load_sources
from qdii.pipeline.windows import Schedule, Window
from tests.helpers import make_msg

REPO = Path(__file__).resolve().parents[2]


def ns(*args):
    return int(datetime(*args, tzinfo=UTC).timestamp()) * 10**9


NY_CLOSE = Window("America/New_York", time(15, 55), time(16, 3), 5)
SH_SESSION = Window("Asia/Shanghai", time(9, 10), time(15, 10), 5)


def test_ny_close_window_follows_dst():
    # 2026-09-15（美东夏令时）16:00 ET = 20:00 UTC；2026-12-15（冬令时）= 21:00 UTC
    assert NY_CLOSE.contains(ns(2026, 9, 15, 20, 0))
    assert not NY_CLOSE.contains(ns(2026, 9, 15, 21, 0))
    assert NY_CLOSE.contains(ns(2026, 12, 15, 21, 0))
    assert not NY_CLOSE.contains(ns(2026, 12, 15, 20, 0))


def test_shanghai_window_and_weekend():
    assert SH_SESSION.contains(ns(2026, 9, 15, 1, 30))  # 周二 09:30 北京
    assert not SH_SESSION.contains(ns(2026, 9, 19, 1, 30))  # 周六
    assert not SH_SESSION.contains(ns(2026, 9, 15, 7, 30))  # 15:30 北京


def test_overnight_window_belongs_to_start_day():
    w = Window("Asia/Shanghai", time(21, 0), time(3, 0), 10)
    assert w.contains(ns(2026, 9, 18, 17, 0))  # 周六 01:00 北京，属于周五开始的窗口
    assert not w.contains(ns(2026, 9, 20, 17, 0))  # 周一 01:00 北京，属于周日开始的窗口


def test_schedule_prefers_smallest_active_interval_then_idle():
    s = Schedule((SH_SESSION, Window("Asia/Shanghai", time(9, 0), time(10, 0), 2)), idle_interval_s=60)
    assert s.interval_at(ns(2026, 9, 15, 1, 30)) == 2
    assert s.interval_at(ns(2026, 9, 15, 3, 0)) == 5
    assert s.interval_at(ns(2026, 9, 19, 3, 0)) == 60
    assert Schedule((), None).interval_at(ns(2026, 9, 15, 3, 0)) is None


def test_backoff_sequence_and_reset():
    st = EndpointState()
    fail = make_msg(b"", status=None, error="ReadTimeout")
    delays = []
    for _ in range(5):
        st.record(fail)
        delays.append(st.backoff_s())
    assert delays == [5, 15, 30, 60, 60]
    st.record(make_msg(b"ok"))
    assert st.backoff_s() == 0 and st.blocked_reason is None


def test_403_blocks_and_blocklist_persists(tmp_path):
    st = EndpointState()
    st.record(make_msg(b"Forbidden", status=403))
    assert st.blocked_reason is not None
    bl = BlockList.load(tmp_path / "blocked.json")
    bl.block("sina.etf_batch", st.blocked_reason)
    assert "sina.etf_batch" in BlockList.load(tmp_path / "blocked.json").entries
    assert bl.unblock("sina.etf_batch")
    assert BlockList.load(tmp_path / "blocked.json").entries == {}


def test_repo_sources_config_loads_and_sina_has_referer():
    _, _, endpoints = load_sources(REPO / "config" / "sources.toml")
    ids = {e.request.endpoint_id for e in endpoints}
    assert {"sina.etf_batch", "sina.hf_NQ", "eastmoney.lsjz.513390"} <= ids
    for e in endpoints:
        if e.request.source_id == "sina" and "hq.sinajs.cn" in e.request.url:
            assert dict(e.request.headers).get("Referer") == "https://finance.sina.com.cn/"
