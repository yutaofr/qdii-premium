"""主机可用窗口组合（ADD-0 §15 修订）：A 股固定窗口 + 按交易日历计算的美股收盘锚点窗口。

收盘窗口不写死巴黎时间：对每个美股实际交易日取日历收盘 c（含提前收盘），窗口为 [c − before, c + after)。
所有窗口实现同一接口：active / overlap_s / next_bounds，供采集门控、缺口分类与状态页使用。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from qdii.io.calendars import CalendarProvider


class AvailabilityWindow(Protocol):
    def active(self, utc_ns: int) -> bool: ...
    def overlap_s(self, from_ns: int, to_ns: int) -> float: ...
    def next_bounds(self, utc_ns: int) -> tuple[int, int]: ...


@dataclass(frozen=True)
class CloseWindow:
    cal: CalendarProvider
    market: str = "NASDAQ"
    before_s: float = 300.0  # DS-10：c − 5 分钟
    after_s: float = 180.0  # c + 2 分钟缺口检查，再留 1 分钟

    def closes_between(self, from_ns: int, to_ns: int) -> list[int]:
        """收盘时刻 c 满足 from_ns < c ≤ to_ns（按日历，日历未覆盖的日期不产生收盘）。"""
        start = datetime.fromtimestamp(from_ns / 1e9, tz=UTC).date() - timedelta(days=2)
        end = datetime.fromtimestamp(to_ns / 1e9, tz=UTC).date() + timedelta(days=1)
        out = []
        d = start
        while d <= end:
            s = self.cal.session(self.market, d) if self.cal.covers(self.market, d) else None
            if s is not None and from_ns < s.close_utc_ns <= to_ns:
                out.append(s.close_utc_ns)
            d += timedelta(days=1)
        return sorted(out)

    def _bounds_near(self, utc_ns: int, days_back: float, days_fwd: float) -> list[tuple[int, int]]:
        lo, hi = int(utc_ns - days_back * 86400e9), int(utc_ns + days_fwd * 86400e9)
        return [(int(c - self.before_s * 1e9), int(c + self.after_s * 1e9)) for c in self.closes_between(lo, hi)]

    def active(self, utc_ns: int) -> bool:
        return any(s <= utc_ns < e for s, e in self._bounds_near(utc_ns, 1, 1))

    def overlap_s(self, from_ns: int, to_ns: int) -> float:
        if to_ns <= from_ns:
            return 0.0
        span_days = (to_ns - from_ns) / 86400e9
        total = 0
        for s, e in self._bounds_near(from_ns, 1, span_days + 1):
            total += max(0, min(e, to_ns) - max(s, from_ns))
        return total / 1e9

    def next_bounds(self, utc_ns: int) -> tuple[int, int]:
        for s, e in self._bounds_near(utc_ns, 1, 15):
            if utc_ns < e:
                return s, e
        raise ValueError("no US close window within 15 days (calendar coverage?)")


@dataclass(frozen=True)
class CompositeWindow:
    parts: tuple[AvailabilityWindow, ...]

    def active(self, utc_ns: int) -> bool:
        return any(p.active(utc_ns) for p in self.parts)

    def overlap_s(self, from_ns: int, to_ns: int) -> float:
        return sum(p.overlap_s(from_ns, to_ns) for p in self.parts)  # 各部分在时间上不重叠

    def next_bounds(self, utc_ns: int) -> tuple[int, int]:
        candidates = []
        for p in self.parts:
            try:
                candidates.append(p.next_bounds(utc_ns))
            except ValueError:
                continue
        if not candidates:
            raise ValueError("no availability window")
        return min(candidates)
