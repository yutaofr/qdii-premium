from datetime import UTC, date, datetime
from pathlib import Path

from qdii.io.calendars import CalendarProvider, MarketPhase

REPO = Path(__file__).resolve().parents[2]
CAL = CalendarProvider(REPO / "config" / "calendar_overrides.toml")


def ns(*args):
    return int(datetime(*args, tzinfo=UTC).timestamp()) * 10**9


def test_szse_alias_and_session_times():
    s = CAL.session("SZSE", date(2026, 9, 15))
    assert s is not None
    assert (s.open_utc_ns, s.break_start_utc_ns, s.break_end_utc_ns, s.close_utc_ns) == (
        ns(2026, 9, 15, 1, 30), ns(2026, 9, 15, 3, 30), ns(2026, 9, 15, 5, 0), ns(2026, 9, 15, 7, 0))


def test_phases_on_trading_day():
    cases = {
        ns(2026, 9, 15, 1, 20): MarketPhase.OPENING_AUCTION,  # 09:20
        ns(2026, 9, 15, 1, 27): MarketPhase.PREOPEN,  # 09:27
        ns(2026, 9, 15, 2, 0): MarketPhase.CONTINUOUS,  # 10:00
        ns(2026, 9, 15, 4, 0): MarketPhase.BREAK,  # 12:00
        ns(2026, 9, 15, 6, 58): MarketPhase.CLOSING_AUCTION,  # 14:58
        ns(2026, 9, 15, 7, 0): MarketPhase.CLOSED,  # 15:00
    }
    for t, expected in cases.items():
        assert CAL.phase("SSE", t)[0] is expected, t


def test_holidays_and_coverage_gap():
    assert CAL.phase("SSE", ns(2026, 10, 1, 2, 0))[0] is MarketPhase.CLOSED  # 国庆
    phase, _, covered = CAL.phase("SSE", ns(2027, 1, 4, 2, 0))  # AT56：库覆盖到 2026-12-31
    assert phase is MarketPhase.UNKNOWN and covered is False


def test_us_early_close_and_sessions_between():
    s = CAL.session("NASDAQ", date(2026, 11, 27))
    assert s is not None and s.close_utc_ns == ns(2026, 11, 27, 18, 0)  # 13:00 ET
    assert CAL.sessions_between("SSE", date(2026, 9, 30), date(2026, 10, 9)) == [date(2026, 10, 8), date(2026, 10, 9)]
    assert CAL.version.startswith("exchange_calendars==") and CAL.tzdb_version.startswith("tzdata==")
