"""交易日历（ADR-013，DS-10）。

实现：exchange_calendars（锁定版本）+ config/calendar_overrides.toml 维护者覆盖。
2026-09-15 实测（PH0-08）：
- XSHG 交易日与 5 只 ETF 在 2025-01-02—2026-09-11 的 412 个净值日期完全一致；
- XNYS 交易日与 NDX 425 个收盘日完全一致，含提前收盘日；
- 库无 XSHE：深交所按覆盖文件声明映射到 XSHG（证据见覆盖文件）；
- XSHG 覆盖到 2026-12-31，之后的日期必须标 CALENDAR_UNCERTAIN（AT56），不得按工作日推断。

覆盖文件安全（AT58/AT60，评审 R6）：
- 覆盖文件结构非法（未知键、日期/市场错误、缺来源）时整份不加载，所有市场视为未覆盖（不暴露半份规则）；
- 已激活休市台账（ledger）：曾经生效的临时休市日，若新版本覆盖文件删除了它且库本身仍视为交易日，
  判为不一致回退，受影响市场视为未覆盖；合法版本加载后才把新增休市写入台账。
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
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


class CalendarOverrideError(ValueError):
    pass


ALLOWED_TOP_KEYS = frozenset({"alias", "auction", "closure"})


def _parse_overrides(raw: bytes) -> dict:
    try:
        doc = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise CalendarOverrideError(f"unreadable overrides: {exc}") from exc
    unknown = set(doc) - ALLOWED_TOP_KEYS
    if unknown:
        raise CalendarOverrideError(f"unknown keys {sorted(unknown)}")
    known_calendars = set(xcals.get_calendar_names(include_aliases=True))
    for market, a in doc.get("alias", {}).items():
        if a.get("calendar") not in known_calendars:
            raise CalendarOverrideError(f"alias {market} -> unknown calendar {a.get('calendar')!r}")
    markets = set(doc.get("alias", {})) | known_calendars
    for item in doc.get("closure", []):
        if item.get("market") not in markets:
            raise CalendarOverrideError(f"closure for unknown market {item.get('market')!r}")
        try:
            date.fromisoformat(item["date"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CalendarOverrideError(f"closure with bad date {item!r}") from exc
        if not item.get("source"):
            raise CalendarOverrideError(f"closure without source {item!r}")
    for market, a in doc.get("auction", {}).items():
        try:
            ZoneInfo(a["tz"])
            [time.fromisoformat(x) for x in (*a["opening"], *a.get("closing", ()))]
        except Exception as exc:
            raise CalendarOverrideError(f"bad auction for {market}: {exc}") from exc
    return doc


class CalendarProvider:
    def __init__(self, overrides_path: Path | None = None, *, ledger_path: Path | None = None,
                 update_ledger: bool = False) -> None:
        self._cals: dict[str, xcals.ExchangeCalendar] = {}
        self.errors: list[str] = []
        self._uncertain: set[str] = set()  # 日历名；"*" 表示全部
        raw = overrides_path.read_bytes() if overrides_path else b""
        try:
            doc = _parse_overrides(raw) if raw else {}
        except CalendarOverrideError as exc:
            self.errors.append(str(exc))
            self._uncertain.add("*")
            doc = {}
        self._aliases: dict[str, str] = {k: v["calendar"] for k, v in doc.get("alias", {}).items()}
        self._closed: dict[str, set[date]] = {}
        for item in doc.get("closure", []):
            self._closed.setdefault(self._name(item["market"]), set()).add(date.fromisoformat(item["date"]))
        self._auction: dict[str, AuctionRule] = {}
        for market, a in doc.get("auction", {}).items():
            oa = (time.fromisoformat(a["opening"][0]), time.fromisoformat(a["opening"][1]))
            ca = (time.fromisoformat(a["closing"][0]), time.fromisoformat(a["closing"][1])) if "closing" in a else None
            self._auction[market] = AuctionRule(a["tz"], oa, ca)
        if ledger_path is not None and "*" not in self._uncertain:
            self._check_ledger(ledger_path, update_ledger)
        self.version = (f"exchange_calendars=={importlib.metadata.version('exchange_calendars')}"
                        f"+overrides:{hashlib.sha256(raw).hexdigest()[:12]}"
                        + ("+UNCERTAIN" if self._uncertain else ""))
        self.tzdb_version = f"tzdata=={importlib.metadata.version('tzdata')}"

    def _name(self, market: str) -> str:
        return self._aliases.get(market, market)

    def _check_ledger(self, path: Path, update: bool) -> None:
        ledger = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        for cal_name, days in ledger.items():
            cal = xcals.get_calendar(cal_name)
            for d_str in days:
                d = date.fromisoformat(d_str)
                still_closed = d in self._closed.get(cal_name, set())
                library_closed = (cal.first_session.date() <= d <= cal.last_session.date()
                                  and not cal.is_session(pd.Timestamp(d)))
                if not still_closed and not library_closed:
                    self.errors.append(f"known closure {cal_name} {d} removed by overrides (rollback rejected)")
                    self._uncertain.add(cal_name)
        if update and not self._uncertain:
            merged = {k: sorted(set(ledger.get(k, [])) | {d.isoformat() for d in v})
                      for k, v in {**{k: set() for k in ledger}, **self._closed}.items()}
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)

    def _cal(self, market: str) -> xcals.ExchangeCalendar:
        name = self._name(market)
        if name not in self._cals:
            self._cals[name] = xcals.get_calendar(name)
        return self._cals[name]

    def covers(self, market: str, d: date) -> bool:
        if "*" in self._uncertain or self._name(market) in self._uncertain:
            return False  # AT58/60：覆盖文件非法或不一致回退，受影响市场不确定
        cal = self._cal(market)
        return cal.first_session.date() <= d <= cal.last_session.date()

    def session(self, market: str, d: date) -> Session | None:
        """d 为交易日则返回会话；非交易日返回 None。调用方须先确认 covers()。"""
        if not self.covers(market, d) or d in self._closed.get(self._name(market), set()):
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
        if not (self.covers(market, after) and self.covers(market, through)):
            return []
        closed = self._closed.get(self._name(market), set())
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
