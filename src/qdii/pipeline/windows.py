"""采集窗口（纯函数）。

采集窗口刻意取交易时段的超集：多录无害，漏录无法补回（ADD-0 D4）。
窗口只决定轮询频率，不表达“是否交易日/是否开市”——那由日历模块判断，不能用星期几代替。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

WEEKDAYS = frozenset(range(5))


@dataclass(frozen=True)
class Window:
    tz: str
    start: time
    end: time  # end <= start 表示跨午夜，属于 start 所在日
    interval_s: float
    weekdays: frozenset[int] = WEEKDAYS  # 以窗口起始日的当地星期计

    def contains(self, utc_ns: int) -> bool:
        local = datetime.fromtimestamp(utc_ns / 1e9, tz=UTC).astimezone(ZoneInfo(self.tz))
        t = local.timetz().replace(tzinfo=None)
        if self.start < self.end:
            return local.weekday() in self.weekdays and self.start <= t < self.end
        # 跨午夜：今天 start 之后，或昨天开始的窗口延续到今天 end 之前
        if t >= self.start:
            return local.weekday() in self.weekdays
        if t < self.end:
            return (local - timedelta(days=1)).weekday() in self.weekdays
        return False


@dataclass(frozen=True)
class Schedule:
    windows: tuple[Window, ...]
    idle_interval_s: float | None  # None = 窗口外不轮询

    def interval_at(self, utc_ns: int) -> float | None:
        active = [w.interval_s for w in self.windows if w.contains(utc_ns)]
        if active:
            return min(active)
        return self.idle_interval_s


@dataclass(frozen=True)
class HostWindow:
    """主机可用窗口：起点与终点可以位于不同时区（ADD-0 §15）。

    以起点时区的当地日期 D 为准：[D start@start_tz, D end@end_tz)。窗口外允许主机睡眠，
    窗口外的缺口不算事故。要求同一日期内终点晚于起点，否则拒绝配置。
    """

    start_tz: str
    start: time
    end_tz: str
    end: time
    weekdays: frozenset[int] = WEEKDAYS

    def bounds(self, d: date) -> tuple[int, int]:
        s = datetime.combine(d, self.start, tzinfo=ZoneInfo(self.start_tz))
        e = datetime.combine(d, self.end, tzinfo=ZoneInfo(self.end_tz))
        if e <= s:
            raise ValueError(f"host window end {e.isoformat()} is not after start {s.isoformat()}")
        return int(s.timestamp() * 1e9), int(e.timestamp() * 1e9)

    def _local_date(self, utc_ns: int) -> date:
        return datetime.fromtimestamp(utc_ns / 1e9, tz=UTC).astimezone(ZoneInfo(self.start_tz)).date()

    def active(self, utc_ns: int) -> bool:
        d = self._local_date(utc_ns)
        if d.weekday() not in self.weekdays:
            return False
        s, e = self.bounds(d)
        return s <= utc_ns < e

    def overlap_s(self, from_ns: int, to_ns: int) -> float:
        if to_ns <= from_ns:
            return 0.0
        total = 0
        d, last = self._local_date(from_ns) - timedelta(days=1), self._local_date(to_ns) + timedelta(days=1)
        while d <= last:
            if d.weekday() in self.weekdays:
                s, e = self.bounds(d)
                total += max(0, min(e, to_ns) - max(s, from_ns))
            d += timedelta(days=1)
        return total / 1e9

    def next_bounds(self, utc_ns: int) -> tuple[int, int]:
        """当前所在窗口，或之后第一个窗口的 (start, end)。"""
        d = self._local_date(utc_ns)
        for _ in range(15):
            if d.weekday() in self.weekdays:
                s, e = self.bounds(d)
                if utc_ns < e:
                    return s, e
            d += timedelta(days=1)
        raise ValueError("no host window within 15 days; check weekdays")


def parse_hhmm(text: str) -> time:
    hh, mm = text.split(":")
    return time(int(hh), int(mm))
