from hypothesis import given
from hypothesis import strategies as st

from qdii.core.quote_norm import classify_side
from qdii.core.types import SideState

numeric_like = st.one_of(
    st.text(max_size=12),
    st.decimals(allow_nan=True, allow_infinity=True).map(str),
    st.sampled_from(["0", "0.000", "-0", "", "---", "NaN", "inf", "-Infinity", "1e400", " 2.19 "]),
    st.none(),
)


@given(numeric_like, numeric_like)
def test_never_raises_and_only_valid_carries_numbers(price, volume):
    c = classify_side(price, volume)
    if c.state is SideState.VALID:
        assert c.price is not None and c.volume is not None
        assert c.price.is_finite() and c.volume.is_finite()
        assert c.price > 0 and c.volume > 0
        assert c.reason_codes == ()
    else:
        # 非 VALID 时不能留下可进入公式的数值，也必须带理由（否则会出现 −100% 假折价）
        assert c.price is None and c.volume is None
        assert c.reason_codes
