from datetime import UTC, datetime
from decimal import Decimal

from qdii.contracts import sina_a_share_v1 as sina
from qdii.core.types import MarketQuote, PriceType, QuoteSide, ReasonCode, Side, SideState, TimeSemantics
from tests.helpers import make_msg, recorded_sina

ROW_513100 = (
    "纳指ETF国泰,2.195,2.201,2.191,2.195,2.187,2.191,2.192,108306329,237192700.000,"
    "148100,2.191,250200,2.190,509571,2.189,536900,2.188,460800,2.187,"
    "852600,2.192,1561200,2.193,160200,2.194,779600,2.195,102100,2.196,"
    "2026-09-14,15:34:59,00,D|194400|425930.40"
)


def _body(rows: dict[str, str]) -> bytes:
    return "\n".join(f'var hq_str_{k}="{v}";' for k, v in rows.items()).encode("gb18030")


def _by(result, symbol):
    quotes = {r.price_type: r for r in result.records if isinstance(r, MarketQuote) and r.symbol == symbol}
    sides = {r.side: r for r in result.records if isinstance(r, QuoteSide) and r.symbol == symbol}
    return quotes, sides


def test_recorded_probe_sample_parses_all_five():
    result = sina.parse(recorded_sina("etf_batch", "referer"))
    assert result.issues == ()
    symbols = {r.symbol for r in result.records}
    assert symbols == {"SSE:513100", "SZSE:159696", "SZSE:159501", "SZSE:159660", "SSE:513390"}
    assert len(result.records) == 5 * 5

    quotes, sides = _by(result, "SSE:513100")
    assert quotes[PriceType.LAST].value == Decimal("2.191")
    assert quotes[PriceType.PREV_CLOSE].value == Decimal("2.201")
    assert sides[Side.BID].state is SideState.VALID
    assert (sides[Side.BID].price, sides[Side.BID].volume) == (Decimal("2.191"), Decimal(148100))
    assert (sides[Side.ASK].price, sides[Side.ASK].volume) == (Decimal("2.192"), Decimal(852600))

    pt = quotes[PriceType.LAST].provider_time
    assert pt.utc_ns == int(datetime(2026, 9, 14, 7, 34, 59, tzinfo=UTC).timestamp()) * 10**9
    assert pt.semantics is TimeSemantics.PROVIDER_SNAPSHOT_UNVERIFIED
    # 供应商时间不是成交事件时间（DS-03 / AT29）
    assert quotes[PriceType.LAST].event_time.utc_ns is None


def test_recorded_403_without_referer_is_access_blocked():
    result = sina.parse(recorded_sina("etf_batch", "no_referer"))
    assert result.records == ()
    assert [i.code for i in result.issues] == [ReasonCode.SOURCE_ACCESS_BLOCKED]


def test_zero_ask_does_not_erase_valid_bid():
    f = ROW_513100.split(",")
    f[7], f[20], f[21] = "0.000", "0", "0.000"
    result = sina.parse(make_msg(_body({"sh513100": ",".join(f)})))
    _, sides = _by(result, "SSE:513100")
    assert sides[Side.ASK].state is SideState.UNKNOWN
    assert sides[Side.ASK].price is None
    assert ReasonCode.ZERO_PRICE_OR_VOLUME in sides[Side.ASK].reason_codes
    assert sides[Side.BID].state is SideState.VALID


def test_zero_last_price_is_null_not_zero():
    f = ROW_513100.split(",")
    f[3] = "0.000"
    quotes, _ = _by(sina.parse(make_msg(_body({"sh513100": ",".join(f)}))), "SSE:513100")
    assert quotes[PriceType.LAST].value is None
    assert quotes[PriceType.LAST].reason_codes == (ReasonCode.ZERO_PRICE_OR_VOLUME,)


def test_duplicate_price_mismatch_flagged():
    f = ROW_513100.split(",")
    f[6] = "2.180"  # 冗余买一价与 f11 不一致
    result = sina.parse(make_msg(_body({"sh513100": ",".join(f)})))
    _, sides = _by(result, "SSE:513100")
    assert ReasonCode.QUOTE_INVALID in sides[Side.BID].reason_codes
    assert any(i.code is ReasonCode.QUOTE_INVALID for i in result.issues)


def test_short_row_and_bad_time_reported_without_crash():
    short = sina.parse(make_msg(_body({"sh513100": "纳指ETF国泰,2.195,2.201"})))
    assert short.records == ()
    assert short.issues[0].code is ReasonCode.QUOTE_INVALID

    f = ROW_513100.split(",")
    f[31] = "25:61:00"
    result = sina.parse(make_msg(_body({"sh513100": ",".join(f)})))
    quotes, _ = _by(result, "SSE:513100")
    assert quotes[PriceType.LAST].provider_time.utc_ns is None
    assert any(i.code is ReasonCode.TIME_UNVERIFIED for i in result.issues)


def test_unsupported_market_code_is_not_mapped():
    result = sina.parse(make_msg(_body({"hk00700": ROW_513100})))
    assert result.records == ()
    assert result.issues[0].code is ReasonCode.QUOTE_INVALID


def test_network_failure_is_missing():
    result = sina.parse(make_msg(b"", status=None, error="ConnectTimeout: timed out"))
    assert result.records == () and result.issues[0].code is ReasonCode.QUOTE_MISSING
