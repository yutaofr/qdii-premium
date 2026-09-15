from decimal import Decimal

import pytest

from qdii.core.quote_norm import classify_side, parse_decimal
from qdii.core.types import ReasonCode as R
from qdii.core.types import SideState as S


@pytest.mark.parametrize(
    ("price", "volume", "state", "codes"),
    [
        ("2.191", "148100", S.VALID, ()),
        # AT51：零值组合不可执行，未经契约确认不得判为空盘
        ("0", "0", S.UNKNOWN, (R.ZERO_PRICE_OR_VOLUME,)),
        ("0.000", "0", S.UNKNOWN, (R.ZERO_PRICE_OR_VOLUME,)),
        ("2.200", "0", S.UNKNOWN, (R.ZERO_PRICE_OR_VOLUME,)),
        ("0.000", "500", S.UNKNOWN, (R.ZERO_PRICE_OR_VOLUME,)),
        # AT52：缺失、非法、非有限、负数
        ("", "100", S.MISSING, (R.QUOTE_MISSING,)),
        ("---", "100", S.MISSING, (R.QUOTE_MISSING,)),
        (None, None, S.MISSING, (R.QUOTE_MISSING,)),
        ("2.1", "", S.MISSING, (R.QUOTE_MISSING,)),
        ("abc", "100", S.INVALID, (R.QUOTE_INVALID,)),
        ("NaN", "100", S.INVALID, (R.NONFINITE_NUMBER,)),
        ("2.1", "Infinity", S.INVALID, (R.NONFINITE_NUMBER,)),
        ("-2.1", "100", S.INVALID, (R.QUOTE_INVALID,)),
        ("2.1", "-5", S.INVALID, (R.QUOTE_INVALID,)),
        ("", "abc", S.INVALID, (R.QUOTE_MISSING, R.QUOTE_INVALID)),
    ],
)
def test_classify_side(price, volume, state, codes):
    c = classify_side(price, volume)
    assert c.state is state
    assert c.reason_codes == codes
    if state is S.VALID:
        assert c.price == Decimal(price) and c.volume == Decimal(volume)
    else:
        assert c.price is None and c.volume is None


def test_empty_encoding_only_when_contract_confirms():
    # AT53：同样的零值报文，契约确认空盘编码时才是 EMPTY_CONFIRMED
    confirmed = classify_side("0.000", "0", empty_encoding=lambda p, v: p == 0 and v == 0)
    unknown = classify_side("0.000", "0")
    assert confirmed.state is S.EMPTY_CONFIRMED and confirmed.reason_codes == (R.EMPTY_CONFIRMED,)
    assert unknown.state is S.UNKNOWN


def test_empty_encoding_never_overrides_valid_or_invalid():
    always = lambda p, v: True
    assert classify_side("2.1", "100", empty_encoding=always).state is S.VALID
    assert classify_side("-1", "0", empty_encoding=always).state is S.INVALID


def test_parse_decimal_keeps_exact_text():
    assert parse_decimal(" 2.190 ") == (Decimal("2.190"), None)
