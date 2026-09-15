from datetime import UTC, datetime

import pytest

from qdii.io import rawlog
from tests.helpers import make_msg


def ns(*args):
    return int(datetime(*args, tzinfo=UTC).timestamp()) * 10**9


def live(body: bytes, t: int, seq: int, source: str = "sina"):
    return make_msg(body, received_utc_ns=t, seq=seq, source_id=source, run_id="LIVE-test")


def test_roundtrip_preserves_bytes():
    msg = live("纳指ETF".encode("gb18030") + b"\x00\xff", ns(2026, 9, 15, 1, 30), 7)
    assert rawlog.decode(rawlog.encode(msg)) == msg


def test_roundtrip_preserves_redirect_chain_and_headers():
    from dataclasses import replace

    msg = replace(
        live(b"{}", ns(2026, 9, 15, 1, 30), 8),
        final_url="https://push2.example/api?x=1",
        redirects=((302, "https://push2.example/api?x=1"),),
        response_headers=(("content-type", "application/json"), ("date", "Mon, 14 Sep 2026 23:35:00 GMT")),
    )
    assert rawlog.decode(rawlog.encode(msg)) == msg


def test_hour_rotation_compresses_and_reader_merges_in_order(tmp_path):
    w = rawlog.RawLogWriter(tmp_path)
    msgs = [
        live(b"a", ns(2026, 9, 15, 1, 59, 58), 0, "sina"),
        live(b"b", ns(2026, 9, 15, 1, 59, 59), 1, "tencent"),
        live(b"c", ns(2026, 9, 15, 2, 0, 1), 2, "sina"),  # 触发 sina 01 时段轮转
    ]
    for m in msgs:
        w.append(m)
    w.close()
    assert (tmp_path / "raw/sina/2026-09-15/01.jsonl.gz").exists()
    assert (tmp_path / "raw/sina/2026-09-15/02.jsonl").exists()
    out = list(rawlog.iter_messages(tmp_path, dates=["2026-09-15"]))
    assert [m.body for m in out] == [b"a", b"b", b"c"]


def test_synthetic_messages_rejected_by_live_writer(tmp_path):
    with pytest.raises(ValueError):
        rawlog.RawLogWriter(tmp_path).append(make_msg(b"x", run_id="SYN-x"))


def test_recover_truncates_partial_tail_and_compresses_old_hours(tmp_path):
    w = rawlog.RawLogWriter(tmp_path)
    t_old = ns(2026, 9, 15, 1, 0, 0)
    path = w.append(live(b"ok", t_old, 0))
    w.close()
    with path.open("a") as fh:
        fh.write('{"v":1,"msg_id":"partial')  # 模拟崩溃时写了半行
    repairs = rawlog.recover(tmp_path, now_utc_ns=ns(2026, 9, 15, 3, 0, 0))
    assert len(repairs) == 1 and repairs[0].truncated_bytes > 0
    out = list(rawlog.iter_messages(tmp_path))
    assert [m.body for m in out] == [b"ok"]
    assert path.with_suffix(".jsonl.gz").exists()


def test_recover_keeps_current_hour_plain(tmp_path):
    t = ns(2026, 9, 15, 3, 10, 0)
    w = rawlog.RawLogWriter(tmp_path)
    path = w.append(live(b"x", t, 0))
    w.close()
    assert rawlog.recover(tmp_path, now_utc_ns=t + 10**9) == []
    assert path.exists()
