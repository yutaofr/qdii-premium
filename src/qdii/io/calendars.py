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
from datetime import date, datetime, time, timedelta
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
ALIAS_KEYS = frozenset({"calendar"})
AUCTION_KEYS = frozenset({"tz", "opening", "closing"})
CLOSURE_REQUIRED = frozenset({"market", "date", "source"})
CLOSURE_KEYS = CLOSURE_REQUIRED | {"received_at", "reason"}


@dataclass(frozen=True)
class Overrides:
    """已完整校验的覆盖规则；构造器只消费这个对象，不再读取原始 TOML 结构（二审 F2）。"""

    aliases: dict[str, str]
    closures: tuple[tuple[str, date], ...]
    auctions: dict[str, AuctionRule]


def _table(value: object, where: str, allowed: frozenset[str], required: frozenset[str]) -> dict:
    if not isinstance(value, dict):
        raise CalendarOverrideError(f"{where}: expected table, got {type(value).__name__}")
    unknown = set(value) - allowed
    if unknown:
        raise CalendarOverrideError(f"{where}: unknown keys {sorted(unknown)}")
    missing = required - set(value)
    if missing:
        raise CalendarOverrideError(f"{where}: missing keys {sorted(missing)}")
    return value


def _text(value: object, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CalendarOverrideError(f"{where}: expected non-empty string")
    return value


def _interval(value: object, where: str) -> tuple[time, time]:
    if not isinstance(value, list) or len(value) != 2:
        raise CalendarOverrideError(f"{where}: expected [\"HH:MM\", \"HH:MM\"]")
    try:
        start, end = (time.fromisoformat(_text(x, where)) for x in value)
    except ValueError as exc:
        raise CalendarOverrideError(f"{where}: bad time ({exc})") from exc
    if not start < end:
        raise CalendarOverrideError(f"{where}: start must be before end")
    return start, end


def _parse_overrides(raw: bytes) -> Overrides:
    """结构与语义完整校验；任一处非法抛 CalendarOverrideError，整份不加载（AT58/60，评审 R6，二审 F2）。"""
    try:
        doc = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise CalendarOverrideError(f"unreadable overrides: {exc}") from exc
    _table(doc, "overrides", ALLOWED_TOP_KEYS, frozenset())
    known_calendars = set(xcals.get_calendar_names(include_aliases=True))

    aliases: dict[str, str] = {}
    raw_alias = doc.get("alias", {})
    if not isinstance(raw_alias, dict):
        raise CalendarOverrideError("alias: expected table")
    for market, a in raw_alias.items():
        cal = _text(_table(a, f"alias.{market}", ALIAS_KEYS, ALIAS_KEYS)["calendar"], f"alias.{market}.calendar")
        if cal not in known_calendars:
            raise CalendarOverrideError(f"alias.{market}: unknown calendar {cal!r}")
        aliases[market] = cal
    markets = set(aliases) | known_calendars

    closures: list[tuple[str, date]] = []
    items = doc.get("closure", [])
    if not isinstance(items, list):
        raise CalendarOverrideError("closure: expected array of tables ([[closure]])")
    for n, item in enumerate(items):
        where = f"closure[{n}]"
        _table(item, where, CLOSURE_KEYS, CLOSURE_REQUIRED)
        market = _text(item["market"], f"{where}.market")
        if market not in markets:
            raise CalendarOverrideError(f"{where}: unknown market {market!r}")
        try:
            day = date.fromisoformat(_text(item["date"], f"{where}.date"))
        except ValueError as exc:
            raise CalendarOverrideError(f"{where}: bad date ({exc})") from exc
        _text(item["source"], f"{where}.source")
        if "received_at" in item:
            try:
                datetime.fromisoformat(_text(item["received_at"], f"{where}.received_at"))
            except ValueError as exc:
                raise CalendarOverrideError(f"{where}: bad received_at ({exc})") from exc
        if "reason" in item:
            _text(item["reason"], f"{where}.reason")
        closures.append((market, day))

    auctions: dict[str, AuctionRule] = {}
    raw_auction = doc.get("auction", {})
    if not isinstance(raw_auction, dict):
        raise CalendarOverrideError("auction: expected table")
    for market, a in raw_auction.items():
        where = f"auction.{market}"
        if market not in markets:
            raise CalendarOverrideError(f"{where}: unknown market")
        _table(a, where, AUCTION_KEYS, frozenset({"tz", "opening"}))
        tz = _text(a["tz"], f"{where}.tz")
        try:
            ZoneInfo(tz)
        except (ValueError, KeyError) as exc:  # ZoneInfoNotFoundError 是 KeyError 子类
            raise CalendarOverrideError(f"{where}.tz: unknown zone {tz!r}") from exc
        opening = _interval(a["opening"], f"{where}.opening")
        closing = _interval(a["closing"], f"{where}.closing") if "closing" in a else None
        auctions[market] = AuctionRule(tz, opening, closing)
    return Overrides(aliases, tuple(closures), auctions)


class CalendarProvider:
    def __init__(self, overrides_path: Path | None = None, *, ledger_path: Path | None = None,
                 update_ledger: bool = False) -> None:
        self._cals: dict[str, xcals.ExchangeCalendar] = {}
        self.errors: list[str] = []
        self._uncertain: set[str] = set()  # 日历名；"*" 表示全部
        raw = overrides_path.read_bytes() if overrides_path else b""
        empty = Overrides({}, (), {})
        try:
            ov = _parse_overrides(raw) if raw else empty
        except CalendarOverrideError as exc:
            self.errors.append(str(exc))
            self._uncertain.add("*")
            ov = empty
        self._aliases: dict[str, str] = dict(ov.aliases)
        self._closed: dict[str, set[date]] = {}
        for market, day in ov.closures:
            self._closed.setdefault(self._name(market), set()).add(day)
        self._auction: dict[str, AuctionRule] = dict(ov.auctions)
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

    def last_session_on_or_before(self, market: str, d: date, max_back_days: int = 14) -> date | None:
        """不晚于 d 的最近交易日；途经任一未覆盖日期或窗口内无交易日时返回 None（不按工作日推断）。"""
        for back in range(max_back_days + 1):
            day = d - timedelta(days=back)
            if not self.covers(market, day):
                return None
            if self.session(market, day) is not None:
                return day
        return None

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
