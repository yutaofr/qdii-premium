"""四审 D2：唤醒与关键输入缺口时尽快刷新，不继续消耗睡前的长轮询等待。

macOS 睡眠期间单调钟不前进、墙钟前进；这里用注入时钟模拟，不宣称是真实睡眠实验。
"""

import asyncio
import json
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from qdii.apps.collect import Collector, CollectorConfig
from qdii.io import rawlog
from qdii.io.http import EndpointRequest, Fetcher
from qdii.pipeline.sources import EndpointConfig
from qdii.pipeline.windows import HostWindow, Schedule
from tests.unit.test_relative_bundle_state import bj

HOST = HostWindow("Europe/Paris", time(7, 30), "Asia/Shanghai", time(15, 5))
NDX = EndpointConfig(EndpointRequest("nasdaq.ndx_history", "nasdaq", "GET", "https://api.nasdaq.invalid/ndx", ()),
                     Schedule((), 3600), None, True)


class SleepyClock:
    def __init__(self, wall: int) -> None:
        self.wall, self.mono = wall, 0

    def now_utc_ns(self) -> int:
        return self.wall

    def monotonic_ns(self) -> int:
        return self.mono


def run_poll(tmp_path: Path, clock: SleepyClock, on_sleep, statuses=(200, 200)) -> list[int]:
    cfg = CollectorConfig(
        data_root=tmp_path, run_label="test", heartbeat_interval_s=30, gap_threshold_s=90,
        host_probe_interval_s=600, min_disk_free_gb=0, max_clock_offset_ms=2000, status_enabled=False,
        status_interface="lo0", status_port=0, host_window=HOST, host_probe_enabled=False,
    )
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(clock.wall)
        return httpx.Response(statuses[min(len(seen), len(statuses)) - 1], json={"data": None})

    collector = Collector(cfg, [NDX], 5, clock=clock)

    async def fake_sleep(seconds: float) -> None:
        on_sleep(collector, clock, seen, seconds)
        if len(seen) >= 2 or clock.wall > bj(15, 5, day=16) or (clock.wall > bj(15, 5, day=15) and clock.wall < bj(7, 0, day=16)):
            collector.stop.set()
        await asyncio.sleep(0)

    collector._sleep = fake_sleep

    async def main() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            collector.writer = rawlog.RawLogWriter(tmp_path)
            collector.fetcher = Fetcher(client, clock, collector.run_id)
            collector.window_open.set()
            await collector._poll(NDX)
            collector.writer.close()

    asyncio.run(main())
    return seen


def advance(clock: SleepyClock, seconds: float, *, mono: bool = True) -> None:
    clock.wall += int(seconds * 1e9)
    if mono:
        clock.mono += int(seconds * 1e9)


def test_wake_after_a_share_close_still_refreshes_within_the_window(tmp_path):
    """收盘参考场景：次日北京 15:00（A 股已收盘，采集窗口到 15:05）才唤醒，仍应立即刷新而非补等睡前间隔。

    收盘前可交易时段的完整路径见 test_e2_*。
    """
    clock = SleepyClock(bj(13, 31, day=15))  # 巴黎 07:31 首次取数
    slept = []

    def on_sleep(collector, clock, seen, seconds):
        if seen and not slept and clock.wall >= seen[0] + 60 * 10**9:
            slept.append(True)  # 运行 60 秒后合盖；次日北京 15:00 唤醒，睡眠期间单调钟不走
            clock.wall = bj(15, 0, day=16)
            return
        advance(clock, seconds)

    seen = run_poll(tmp_path, clock, on_sleep)
    assert len(seen) == 2, "唤醒后没有在窗口结束前刷新"
    assert seen[1] - bj(15, 0, day=16) <= 15 * 10**9  # 一个等待分段内重新取数


def test_critical_input_gap_retries_soon_instead_of_hourly(tmp_path):
    clock = SleepyClock(bj(13, 31, day=15))

    def on_sleep(collector, clock, seen, seconds):
        collector.input_gaps = {"nasdaq.ndx_history"}  # 估算需要的收盘数据仍缺
        advance(clock, seconds)

    seen = run_poll(tmp_path, clock, on_sleep, statuses=(500, 200))
    assert len(seen) == 2 and 60 * 10**9 <= seen[1] - seen[0] <= 135 * 10**9


def test_without_gap_or_sleep_normal_interval_is_kept(tmp_path):
    clock = SleepyClock(bj(14, 10, day=15))

    def on_sleep(collector, clock, seen, seconds):
        advance(clock, seconds)

    seen = run_poll(tmp_path, clock, on_sleep)
    assert len(seen) == 1  # 1 小时周期内窗口（北京 15:05）先结束，不额外请求


def test_host_probe_loop_polls_and_waits(tmp_path, monkeypatch):
    """2026-09-16 事故：主机探测里的一处调用未随等待逻辑一起修改，整窗口反复崩溃重启。"""
    from qdii.io import host as host_mod

    clock = SleepyClock(bj(14, 0, day=16))
    cfg = CollectorConfig(
        data_root=tmp_path, run_label="test", heartbeat_interval_s=30, gap_threshold_s=90,
        host_probe_interval_s=600, min_disk_free_gb=0, max_clock_offset_ms=2000, status_enabled=False,
        status_interface="lo0", status_port=0, host_window=HOST, host_probe_enabled=True,
    )
    monkeypatch.setattr(host_mod, "power_source", lambda: "AC Power")
    monkeypatch.setattr(host_mod, "ntp_offset_ms", lambda: 1.0)
    monkeypatch.setattr(host_mod, "disk_free_gb", lambda p: 100.0)
    monkeypatch.setattr(host_mod, "firewall_blocks_all_incoming", lambda: True)
    collector = Collector(cfg, [], 5, clock=clock)
    rounds = []

    async def fake_sleep(seconds: float) -> None:
        rounds.append(seconds)
        advance(clock, seconds)
        if len(rounds) >= 3:
            collector.stop.set()
        await asyncio.sleep(0)

    collector._sleep = fake_sleep
    collector.window_open.set()
    asyncio.run(collector._host_probe())
    assert collector.health.host is not None and collector.health.host.power_source == "AC Power"
    assert rounds and all(s <= 15 for s in rounds)  # 分段等待，无异常


def test_auxiliary_task_crash_does_not_stop_collection(tmp_path):
    cfg = CollectorConfig(
        data_root=tmp_path, run_label="test", heartbeat_interval_s=30, gap_threshold_s=90,
        host_probe_interval_s=600, min_disk_free_gb=0, max_clock_offset_ms=2000, status_enabled=False,
        status_interface="lo0", status_port=0, host_window=HOST, host_probe_enabled=False,
    )
    collector = Collector(cfg, [], 5, clock=SleepyClock(bj(14, 0, day=16)))

    async def boom() -> None:
        raise RuntimeError("probe bug")

    async def main() -> None:
        for name, stops in (("host_probe", False), ("heartbeat", True)):
            task = asyncio.create_task(boom(), name=name)
            task.add_done_callback(collector._on_task_done)
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.sleep(0)
            assert collector.stop.is_set() is stops, name
        assert "host_probe" in collector.health.degraded
        assert any("host_probe" in w for w in collector.health.warnings(0))

    asyncio.run(main())


def test_e1_status_task_recovers_after_port_is_freed(tmp_path):
    """五审 E1：端口暂时被占用时状态页失败，采集继续；端口释放后状态页自行恢复。"""
    import socket

    import httpx as httpx_mod

    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    cfg = CollectorConfig(
        data_root=tmp_path, run_label="test", heartbeat_interval_s=30, gap_threshold_s=90,
        host_probe_interval_s=600, min_disk_free_gb=0, max_clock_offset_ms=2000, status_enabled=True,
        status_interface="lo0", status_port=port, host_window=HOST, host_probe_enabled=False,
    )
    clock = SleepyClock(bj(14, 0, day=16))
    collector = Collector(cfg, [], 5, clock=clock)

    async def fast_sleep(seconds: float) -> None:
        advance(clock, seconds)  # 退避按注入时钟计，测试不真的等 5 秒
        await asyncio.sleep(0.02)

    collector._sleep = fast_sleep

    async def main() -> tuple[str, int, int]:
        from qdii.apps.status import serve_status

        task = collector._supervised("status", lambda: serve_status(
            collector._status_snapshot, interface="lo0", port=port, stop=collector.stop, lan_enabled=False))
        for _ in range(50):  # 等第一次绑定失败并记录降级
            await asyncio.sleep(0.02)
            if "status" in collector.health.degraded:
                break
        first_error = collector.health.degraded.get("status", "")
        assert not collector.stop.is_set()  # 采集不受影响
        blocker.close()
        body = ""
        for _ in range(100):  # 端口释放后自行恢复
            await asyncio.sleep(0.05)
            try:
                async with httpx_mod.AsyncClient(timeout=1) as client:
                    body = (await client.get(f"http://127.0.0.1:{port}/health.json")).text
                break
            except httpx_mod.HTTPError:
                continue
        restarts = collector.health.task_restarts.get("status", 0)
        collector.stop.set()
        task.cancel()
        return first_error, restarts, len(body)

    first_error, restarts, body_len = asyncio.run(main())
    assert "OSError" in first_error or "Error" in first_error
    assert restarts >= 1 and body_len > 0
    assert "status" not in collector.health.degraded  # 恢复后清除降级标记


# ---------- 五审 E2：收盘前唤醒的完整路径 ----------

REPO = Path(__file__).resolve().parents[2]
WAKE = bj(14, 50, day=16)  # A 股 15:00 收盘前 10 分钟（巴黎 08:50）；采集窗口到 15:05
CLOSE = bj(15, 0, day=16)
NAV_DATE = "2026-09-14"  # 唤醒当天（09-16）可取到的最新净值日
OLD_NAV_DATE = "2026-09-11"  # 前一天缓存里的净值日
NAVS = {"513100": "1.9655", "159696": "1.8297", "159501": "1.8462", "159660": "2.1475", "513390": "2.2064"}
OLD_NAVS = {c: f"{float(v) * 0.99:.4f}" for c, v in NAVS.items()}
PRICES = {"sh513100": "2.210", "sz159696": "2.005", "sz159501": "2.092", "sz159660": "2.339", "sh513390": "2.406"}


def full_endpoints():
    from qdii.pipeline.windows import Window

    def ep(eid, source, url, interval):
        return EndpointConfig(EndpointRequest(eid, source, "GET", url, ()),
                              Schedule((Window("Asia/Shanghai", time(9, 10), time(15, 10), interval),), interval),
                              None, True)

    out = [ep("sina.etf_batch", "sina", "https://hq.sinajs.invalid/etf", 5),
           ep("sina.hf_NQ", "sina", "https://hq.sinajs.invalid/nq", 5),
           ep("cfets.fx_spot_quot", "cfets", "https://chinamoney.invalid/spot", 30),
           ep("nasdaq.ndx_history", "nasdaq", "https://api.nasdaq.invalid/ndx", 3600),
           ep("chinamoney.ccpr", "chinamoney", "https://chinamoney.invalid/ccpr", 3600)]
    out += [ep(f"eastmoney.lsjz.{c}", "eastmoney", f"https://api.fund.invalid/lsjz?fundCode={c}", 900) for c in NAVS]
    return out


def bodies(clock: SleepyClock, calls: list[str], fail_ndx_call: int):
    """按注入时钟生成各来源报文：前一天只能取到当时已存在的数据（旧净值、无 09-15 收盘）。

    第 fail_ndx_call 次指数请求返回 500，模拟关键请求失败。
    """
    from tests.unit.test_enav import cfets_body, hf
    from tests.unit.test_relative_snapshot_replay import sina_body

    def handler(request: httpx.Request) -> httpx.Response:
        url, now = str(request.url), clock.wall
        local = datetime.fromtimestamp(now / 1e9, tz=ZoneInfo("Asia/Shanghai"))
        if "etf" in url:
            calls.append("etf")
            return httpx.Response(200, content=sina_body(local.strftime("%H:%M:%S"), PRICES,
                                                         day=local.strftime("%Y-%m-%d")))
        if "nq" in url:
            calls.append("nq")
            return httpx.Response(200, content=hf(now, "29046.25", "29046.75", "28955.00", now, 0).body)
        if "spot" in url:
            calls.append("spot")
            return httpx.Response(200, content=cfets_body(local.strftime("%Y-%m-%d %H:%M:%S"), "6.7075", "6.7078"))
        if "ndx" in url:
            calls.append("ndx")
            if calls.count("ndx") == fail_ndx_call:
                return httpx.Response(500, json={"error": "boom"})
            rows = [{"date": "09/11/2026", "close": "29,368.44"}, {"date": "09/14/2026", "close": "29,127.16"}]
            if now >= bj(6, 0, day=16):  # 09-15 美股收盘（北京 09-16 04:00）之后才发布
                rows.append({"date": "09/15/2026", "close": "28,937.84"})
            return httpx.Response(200, json={"data": {"symbol": "NDX", "tradesTable": {"rows": rows}}})
        if "ccpr" in url:
            calls.append("ccpr")
            records = [{"date": OLD_NAV_DATE, "values": ["6.7743"]}, {"date": NAV_DATE, "values": ["6.7698"]}]
            return httpx.Response(200, json={"data": {"searchlist": ["USD/CNY"]}, "records": records})
        code = request.url.params.get("fundCode")
        calls.append(f"nav:{code}")
        d, table = ((NAV_DATE, NAVS) if now >= bj(0, 0, day=16) else (OLD_NAV_DATE, OLD_NAVS))
        rows = [{"FSRQ": d, "DWJZ": table[code], "LJJZ": table[code], "JZZZL": ""}]
        return httpx.Response(200, json={"ErrCode": 0, "TotalCount": 1, "Data": {"LSJZList": rows}})

    return handler


def run_wake_scenario(tmp_path: Path, fail_ndx_call: int) -> tuple[dict | None, list[str], SleepyClock]:
    """前一天窗口内各端点各取一次 → 合盖 → 次日 14:50 唤醒 → 直到出现五只全部可用的快照或窗口结束。

    注入时钟按并发任务数分摊推进，近似"多个任务同时等待"，避免模拟时间跑得比真实情况快。
    """
    cfg = CollectorConfig(
        data_root=tmp_path, run_label="test", heartbeat_interval_s=30, gap_threshold_s=90,
        host_probe_interval_s=600, min_disk_free_gb=0, max_clock_offset_ms=2000, status_enabled=False,
        status_interface="lo0", status_port=0, host_window=HOST, host_probe_enabled=False,
    )
    clock = SleepyClock(bj(14, 0, day=15))
    calls: list[str] = []
    collector = Collector(cfg, full_endpoints(), 5, clock=clock, repo_root=REPO)
    state: dict = {"slept": False, "done": None}
    snap_path = tmp_path / "snapshots" / "relative" / "2026-09-16.jsonl"
    share = len(collector.endpoints)

    def ready() -> dict | None:
        if not snap_path.exists():
            return None
        for line in reversed(snap_path.read_text(encoding="utf-8").splitlines()):
            rec = json.loads(line)
            if rec["enav"] and all(x["status"] == "PROXY_ANCHOR" for x in rec["enav"]):
                return rec
        return None

    async def fake_sleep(seconds: float) -> None:
        if not state["slept"] and len({c.split(":")[0] for c in calls}) >= 5 and len(calls) >= share:
            state["slept"] = True  # 合盖：睡眠期间单调钟不前进，墙钟到次日 14:50
            clock.wall = WAKE
            await asyncio.sleep(0)
            return
        advance(clock, seconds / share)
        if state["slept"] and (rec := ready()) is not None:
            state["done"] = rec
            collector.stop.set()
        if clock.wall > bj(15, 5, day=16):
            collector.stop.set()
        await asyncio.sleep(0)

    collector._sleep = fake_sleep

    async def main() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(bodies(clock, calls, fail_ndx_call))) as client:
            collector.writer = rawlog.RawLogWriter(tmp_path)
            collector.fetcher = Fetcher(client, clock, collector.run_id)
            collector.window_open.set()
            tasks = [asyncio.create_task(collector._poll(ep)) for ep in collector.endpoints]
            await asyncio.wait_for(collector.stop.wait(), timeout=60)
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            collector.writer.close()

    asyncio.run(main())
    return state["done"], calls, clock


def test_e2_wake_ten_minutes_before_close_produces_a_usable_estimate_snapshot(tmp_path):
    """睡眠跨日后在 A 股收盘前 10 分钟唤醒：刷新全部关键输入 → 五只成功估算 → 快照落盘 → 回放通过。"""
    from qdii.apps.replay_relative import verify

    rec, calls, _ = run_wake_scenario(tmp_path, fail_ndx_call=0)
    assert rec is not None, f"收盘前没有得到可用估算；calls={calls[-10:]}"
    cutoff = rec["bundle"]["cutoff_utc_ns"]
    assert 0 <= cutoff - WAKE <= 60 * 10**9 and cutoff < CLOSE  # 唤醒后一分钟内，且仍在可交易时段
    assert rec["bundle"]["mode"] == "CURRENT" and rec["bundle"]["price_basis"] == "ASK"
    assert rec["bundle"]["us_close_date"] == "2026-09-15" and rec["bundle"]["us_close_index"] == "28937.84"
    assert {m["nav_date"] for m in rec["bundle"]["members"]} == {NAV_DATE}
    assert all(m["futures_settle"] == "28955.00" and m["fx_spot"] is not None for m in rec["bundle"]["members"])
    assert all(x["premium"] is not None and 0.05 < x["premium"] < 0.25 for x in rec["enav"])
    # 跨日旧缓存必须被刷新：净值、指数、中间价在唤醒后都重新取过
    assert calls.count("ndx") >= 2 and calls.count("ccpr") >= 2 and calls.count("nav:513100") >= 2
    assert verify(tmp_path, REPO, ["2026-09-16"])["status"] == "PASSED"


def test_e2_critical_request_failing_after_wake_recovers_before_close(tmp_path):
    """唤醒后第一次取指数失败：按缺口重试恢复，收盘前仍得到可用估算。"""
    rec, calls, _ = run_wake_scenario(tmp_path, fail_ndx_call=2)  # 第 2 次（唤醒后首次）失败
    assert rec is not None, f"失败后没有恢复；calls={calls[-10:]}"
    cutoff = rec["bundle"]["cutoff_utc_ns"]
    assert cutoff < CLOSE and (cutoff - WAKE) <= 5 * 60 * 10**9  # 收盘前恢复，且不需要等满一小时
    assert calls.count("ndx") >= 3
    assert all(x["status"] == "PROXY_ANCHOR" for x in rec["enav"])
