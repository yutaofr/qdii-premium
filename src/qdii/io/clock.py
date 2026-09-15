"""时钟抽象（ADR-006）：墙钟与单调钟都通过注入获得，回放使用虚拟时钟。"""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    def now_utc_ns(self) -> int: ...
    def monotonic_ns(self) -> int: ...


class SystemClock:
    def now_utc_ns(self) -> int:
        return time.time_ns()

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()


class ReplayClock:
    """回放时由消息驱动推进；不允许倒退。"""

    def __init__(self, start_utc_ns: int) -> None:
        self._now = start_utc_ns

    def advance_to(self, utc_ns: int) -> None:
        self._now = max(self._now, utc_ns)

    def now_utc_ns(self) -> int:
        return self._now

    def monotonic_ns(self) -> int:
        return self._now
