"""覆盖文件模糊测试（三审 C1/C2）：对合法覆盖文件做随机键、值、表头变异。

不变式：CalendarProvider 构造与所有公开查询永不抛出；覆盖文件被拒绝时，所有市场的所有查询都降级为未覆盖。
"""

import tempfile
from datetime import UTC, date, datetime
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from qdii.io.calendars import CalendarProvider, MarketPhase

REPO = Path(__file__).resolve().parents[2]
VALID = (REPO / "config" / "calendar_overrides.toml").read_text(encoding="utf-8") + """
[[closure]]
market = "SSE"
date = "2026-09-16"
source = "https://example.invalid/temporary-closure"
"""
LINES = VALID.splitlines()
ASSIGN = [i for i, line in enumerate(LINES) if "=" in line and not line.lstrip().startswith("#")]
HEADERS = [i for i, line in enumerate(LINES) if line.startswith("[")]

LITERALS = [
    "1", "0", "-1", "1.5", "true", "false", '""', '" "', '"X"', '"SSE"', '"XSHG"', '"XSHE"', '"UTC"', '"America"',
    '"Asia/Shanghai "', '"../etc"', "2026-09-16", '"2026-09-16"', '"2026-02-30"', '"2026-09-15T08:00:00Z"',
    '"09:15"', '"09:15+01:00"', '"09:15:00"', '"9:15"', '"24:00"', '"23:59"', "[]", '["09:15"]',
    '["09:15", "09:25"]', '["09:25", "09:15"]', '["09:15+01:00", "09:25"]', '["09:15", "09:15"]', "[915, 925]",
    '["09:15", "09:25", "09:30"]', "{}", '{calendar = "XSHG"}', '{tz = "UTC"}', "[[1]]",
]
KEYS = ["calendar", "tz", "opening", "closing", "market", "date", "source", "received_at", "reason", "clsoed",
        "alias", "auction", "closure", "SSE", "SZSE"]
TABLES = ["[alias]", "[alias.SSE]", "[alias.NOPE]", "[auction]", "[auction.SSE]", "[auction.XXXX]", "[[closure]]",
          "[[alias]]", "[closure]", "[open_day]"]

ops = st.one_of(
    st.tuples(st.just("value"), st.sampled_from(ASSIGN), st.sampled_from(LITERALS)),
    st.tuples(st.just("key"), st.sampled_from(ASSIGN), st.sampled_from(KEYS)),
    st.tuples(st.just("insert"), st.sampled_from(HEADERS),
              st.tuples(st.sampled_from(KEYS), st.sampled_from(LITERALS)).map(lambda kv: f"{kv[0]} = {kv[1]}")),
    st.tuples(st.just("header"), st.sampled_from(HEADERS), st.sampled_from(TABLES)),
    st.tuples(st.just("delete"), st.sampled_from(ASSIGN + HEADERS), st.just("")),
)


def mutate(mutations) -> str:
    lines = list(LINES)
    inserts: dict[int, list[str]] = {}
    for kind, i, payload in mutations:
        if kind in ("value", "key") and "=" not in lines[i]:
            continue  # 该行已被前一变异删除或替换为表头
        if kind == "value":
            lines[i] = lines[i].split("=", 1)[0] + "= " + payload
        elif kind == "key":
            lines[i] = payload + " =" + lines[i].split("=", 1)[1]
        elif kind == "insert":
            inserts.setdefault(i, []).append(payload)
        elif kind == "header":
            lines[i] = payload
        else:
            lines[i] = ""
    out = []
    for i, line in enumerate(lines):
        out.append(line)
        out.extend(inserts.get(i, []))
    return "\n".join(out) + "\n"


NOW = int(datetime(2026, 9, 15, 2, 0, tzinfo=UTC).timestamp() * 1e9)


@settings(max_examples=250, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(ops, min_size=1, max_size=4))
def test_mutated_overrides_never_raise_and_rejection_degrades_every_query(mutations):
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "ov.toml"
        path.write_text(mutate(mutations), encoding="utf-8")
        cal = CalendarProvider(path)
        rejected = "+UNCERTAIN" in cal.version
        for market in ("SSE", "SZSE", "NASDAQ", "XSHG", "NOPE"):
            phase = cal.phase(market, NOW)
            cal.session(market, date(2026, 9, 14))
            between = cal.sessions_between(market, date(2026, 9, 11), date(2026, 9, 15))
            cal.last_session_on_or_before(market, date(2026, 9, 14))
            if rejected:
                assert phase == (MarketPhase.UNKNOWN, None, False)
                assert between == [] and not cal.covers(market, date(2026, 9, 14))
