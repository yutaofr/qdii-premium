import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

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


# ---------- AT58 / AT60 ----------

BASE = (REPO / "config" / "calendar_overrides.toml").read_text(encoding="utf-8")
CLOSURE = """
[[closure]]
market = "SSE"
date = "2026-09-16"
source = "https://example.invalid/temporary-closure"
"""


def test_valid_closure_override_applies_and_is_ledgered(tmp_path):
    ov, ledger = tmp_path / "ov.toml", tmp_path / "ledger.json"
    ov.write_text(BASE + CLOSURE, encoding="utf-8")
    cal = CalendarProvider(ov, ledger_path=ledger, update_ledger=True)
    assert cal.errors == [] and cal.phase("SZSE", ns(2026, 9, 16, 2, 0))[0] is MarketPhase.CLOSED
    assert json.loads(ledger.read_text())["XSHG"] == ["2026-09-16"]


def test_at58_corrupt_override_not_half_loaded(tmp_path):
    ov = tmp_path / "ov.toml"
    ov.write_text(BASE + CLOSURE.replace("2026-09-16", "2026-13-40"), encoding="utf-8")
    cal = CalendarProvider(ov)
    assert cal.errors and "+UNCERTAIN" in cal.version
    phase, _, covered = cal.phase("SSE", ns(2026, 9, 15, 2, 0))
    assert phase is MarketPhase.UNKNOWN and covered is False
    assert cal.sessions_between("SSE", date(2026, 9, 11), date(2026, 9, 15)) == []


def test_at58_unknown_keys_or_missing_source_rejected(tmp_path):
    ov = tmp_path / "ov.toml"
    ov.write_text(BASE + CLOSURE.replace('source = "https://example.invalid/temporary-closure"\n', ""), encoding="utf-8")
    assert CalendarProvider(ov).errors
    ov.write_text(BASE + "\n[open_day]\nmarket = 'SSE'\n", encoding="utf-8")
    assert CalendarProvider(ov).errors


def test_at60_rollback_removing_known_closure_rejected(tmp_path):
    ov, ledger = tmp_path / "ov.toml", tmp_path / "ledger.json"
    ov.write_text(BASE + CLOSURE, encoding="utf-8")
    CalendarProvider(ov, ledger_path=ledger, update_ledger=True)
    ov.write_text(BASE, encoding="utf-8")  # 回退到不含该临时休市的旧版本
    cal = CalendarProvider(ov, ledger_path=ledger, update_ledger=True)
    assert any("rollback rejected" in e for e in cal.errors)
    assert cal.phase("SSE", ns(2026, 9, 16, 2, 0))[1:] == (None, False)
    assert json.loads(ledger.read_text())["XSHG"] == ["2026-09-16"]  # 台账不被不一致版本改写


BAD_OVERRIDES = {
    "alias_scalar": "alias = 1\n",
    "alias_entry_not_table": '[alias]\nSSE = "XSHG"\n',
    "alias_extra_key": BASE.replace('[alias.SSE]\ncalendar = "XSHG"', '[alias.SSE]\ncalendar = "XSHG"\nnote = 1', 1),
    "opening_single_time": BASE.replace('opening = ["09:15", "09:25"]', 'opening = ["09:15"]', 1),
    "opening_reversed": BASE.replace('opening = ["09:15", "09:25"]', 'opening = ["09:25", "09:15"]', 1),
    "opening_not_string": BASE.replace('opening = ["09:15", "09:25"]', "opening = [915, 925]", 1),
    "auction_unknown_key": BASE.replace('tz = "Asia/Shanghai"', 'tz = "Asia/Shanghai"\nclsoed = false', 1),
    "auction_unknown_market": BASE + '\n[auction.XXXX]\ntz = "UTC"\nopening = ["09:00", "09:10"]\n',
    "closure_nested_unknown_key": BASE + CLOSURE + "clsoed = false\n",
    "closure_toml_date": BASE + CLOSURE.replace('date = "2026-09-16"', "date = 2026-09-16"),
    "closure_not_array": BASE + "\nclosure = 1\n",
    "closure_empty_source": BASE + CLOSURE.replace('"https://example.invalid/temporary-closure"', '""'),
}


@pytest.mark.parametrize("name", sorted(BAD_OVERRIDES))
def test_f2_invalid_override_structure_rejected_without_exception(tmp_path, name):
    ov = tmp_path / "ov.toml"
    ov.write_text(BAD_OVERRIDES[name], encoding="utf-8")
    cal = CalendarProvider(ov)  # 不得抛出
    assert cal.errors and "+UNCERTAIN" in cal.version
    phase, session, covered = cal.phase("SSE", ns(2026, 9, 15, 2, 0))
    assert (phase, session, covered) == (MarketPhase.UNKNOWN, None, False)
    assert not cal.covers("NASDAQ", date(2026, 9, 14))
    assert cal.last_session_on_or_before("NASDAQ", date(2026, 9, 14)) is None


def test_f2_full_closure_record_with_optional_fields_accepted(tmp_path):
    ov = tmp_path / "ov.toml"
    ov.write_text(BASE + CLOSURE + 'received_at = "2026-09-15T08:00:00Z"\nreason = "台风"\n', encoding="utf-8")
    cal = CalendarProvider(ov)
    assert cal.errors == [] and cal.phase("SSE", ns(2026, 9, 16, 2, 0))[0] is MarketPhase.CLOSED


def test_last_session_on_or_before_uses_calendar_not_weekdays():
    assert CAL.last_session_on_or_before("NASDAQ", date(2026, 9, 7)) == date(2026, 9, 4)  # 劳工节
    assert CAL.last_session_on_or_before("NASDAQ", date(2026, 9, 14)) == date(2026, 9, 14)
    assert CAL.last_session_on_or_before("SSE", date(2027, 6, 1)) is None  # 超出覆盖范围
