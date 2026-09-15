"""四审 D2：唤醒与关键输入缺口时尽快刷新，不继续消耗睡前的长轮询等待。

macOS 睡眠期间单调钟不前进、墙钟前进；这里用注入时钟模拟，不宣称是真实睡眠实验。
"""

import asyncio
from datetime import time
from pathlib import Path

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


def test_wake_next_day_five_minutes_before_close_refreshes_immediately(tmp_path):
    clock = SleepyClock(bj(13, 31, day=15))  # 巴黎 07:31 首次取数
    slept = []

    def on_sleep(collector, clock, seen, seconds):
        if seen and not slept and clock.wall >= seen[0] + 60 * 10**9:
            slept.append(True)  # 运行 60 秒后合盖；次日北京 15:00（收盘前 5 分钟）唤醒，睡眠期间单调钟不走
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
