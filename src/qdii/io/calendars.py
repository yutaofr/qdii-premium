"""交易日历（ADR-013，DS-10）。

实现：exchange_calendars（锁定版本）+ config/calendar_overrides.toml 维护者覆盖。
2026-09-15 实测（PH0-08）：
- XSHG 交易日与 5 只 ETF 在 2025-01-02—2026-09-11 的 412 个净值日期完全一致；
- XNYS 交易日与 NDX 425 个收盘日完全一致，含提前收盘日；
- 库无 XSHE：深交所按覆盖文件声明映射到 XSHG（证据见覆盖文件）；
- XSHG 覆盖到 2026-12-31，之后的日期必须标 CALENDAR_UNCERTAIN（AT56），不得按工作日推断。
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import tomllib
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import StrEnum
from pathlib import Path
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd


class MarketPhase(StrEnum):  # QS-01 market_phase
    PREOPEN = "PREOPEN"
    OPENING_AUCTION = "OPENING_AUCTION"
    CONTINUOUS = "CONTINUOUS"
    BREAK = "BREAK"
    CLOSING_AUCTION = "CLOSING_AUCTION"
    CLOSED = "CLOSED"
    HALTED = "HALTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Session:
    market: str
    local_date: date
    open_utc_ns: int
    close_utc_ns: int
    break_start_utc_ns: int | None
    break_end_utc_ns: int | None


@dataclass(frozen=True)
class AuctionRule:
    tz: str
    opening_auction: tuple[time, time]
    closing_auction: tuple[time, time] | None


def _ns(ts: pd.Timestamp) -> int:
    return int(ts.value)


class CalendarProvider:
    def __init__(self, overrides_path: Path | None = None) -> None:
        self._cals: dict[str, xcals.ExchangeCalendar] = {}
        doc = tomllib.loads(overrides_path.read_text(encoding="utf-8")) if overrides_path else {}
        self._aliases: dict[str, str] = {k: v["calendar"] for k, v in doc.get("alias", {}).items()}
        self._closed: dict[str, set[date]] = {}
        for item in doc.get("closure", []):
            self._closed.setdefault(item["market"], set()).add(date.fromisoformat(item["date"]))
        self._auction: dict[str, AuctionRule] = {}
        for market, a in doc.get("auction", {}).items():
            oa = (time.fromisoformat(a["opening"][0]), time.fromisoformat(a["opening"][1]))
            ca = (time.fromisoformat(a["closing"][0]), time.fromisoformat(a["closing"][1])) if "closing" in a else None
            self._auction[market] = AuctionRule(a["tz"], oa, ca)
        raw = overrides_path.read_bytes() if overrides_path else b""
        self.version = (f"exchange_calendars=={importlib.metadata.version('exchange_calendars')}"
                        f"+overrides:{hashlib.sha256(raw).hexdigest()[:12]}")
        self.tzdb_version = f"tzdata=={importlib.metadata.version('tzdata')}"

    def _cal(self, market: str) -> xcals.ExchangeCalendar:
        name = self._aliases.get(market, market)
        if name not in self._cals:
            self._cals[name] = xcals.get_calendar(name)
        return self._cals[name]

    def covers(self, market: str, d: date) -> bool:
        cal = self._cal(market)
        return cal.first_session.date() <= d <= cal.last_session.date()

    def session(self, market: str, d: date) -> Session | None:
        """d 为交易日则返回会话；非交易日返回 None。调用方须先确认 covers()。"""
        if not self.covers(market, d) or d in self._closed.get(market, set()):
            return None
        cal, ts = self._cal(market), pd.Timestamp(d)
        if not cal.is_session(ts):
            return None
        has_break = cal.has_break and not pd.isna(cal.session_break_start(ts))
        return Session(
            market, d, _ns(cal.session_open(ts)), _ns(cal.session_close(ts)),
            _ns(cal.session_break_start(ts)) if has_break else None,
            _ns(cal.session_break_end(ts)) if has_break else None,
        )

    def sessions_between(self, market: str, after: date, through: date) -> list[date]:
        """(after, through] 内的交易日。"""
        cal = self._cal(market)
        out = [s.date() for s in cal.sessions_in_range(pd.Timestamp(after), pd.Timestamp(through))
               if s.date() > after]
        closed = self._closed.get(market, set())
        return [d for d in out if d not in closed]

    def phase(self, market: str, utc_ns: int) -> tuple[MarketPhase, Session | None, bool]:
        """返回 (阶段, 当日会话, 日历是否覆盖)。未覆盖时阶段为 UNKNOWN（CALENDAR_UNCERTAIN）。"""
        cal = self._cal(market)
        local = datetime.fromtimestamp(utc_ns / 1e9, tz=ZoneInfo(str(cal.tz)))
        d = local.date()
        if not self.covers(market, d):
            return MarketPhase.UNKNOWN, None, False
        s = self.session(market, d)
        if s is None:
            return MarketPhase.CLOSED, None, True
        rule = self._auction.get(market)
        if rule is not None:
            def at(t: time) -> int:
                return int(datetime.combine(d, t, tzinfo=ZoneInfo(rule.tz)).timestamp() * 1e9)
            if at(rule.opening_auction[0]) <= utc_ns < at(rule.opening_auction[1]):
                return MarketPhase.OPENING_AUCTION, s, True
            if rule.closing_auction and at(rule.closing_auction[0]) <= utc_ns < at(rule.closing_auction[1]):
                return MarketPhase.CLOSING_AUCTION, s, True
        if utc_ns < s.open_utc_ns:
            return MarketPhase.PREOPEN, s, True
        if utc_ns >= s.close_utc_ns:
            return MarketPhase.CLOSED, s, True
        if s.break_start_utc_ns and s.break_start_utc_ns <= utc_ns < (s.break_end_utc_ns or 0):
            return MarketPhase.BREAK, s, True
        return MarketPhase.CONTINUOUS, s, True
