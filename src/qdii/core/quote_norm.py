"""盘口一侧价量归一化（QS-04，AT51—AT53）。

规则：只有有限正价格与有限正数量才构成 VALID；零值、非有限数、负数、缺失都不得进入公式。
是否把某种零值组合视为“明确无挂单”只能由来源契约声明，本模块不做推断。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from qdii.core.types import ReasonCode, SideState

MISSING_TOKENS = frozenset({"", "-", "--", "---"})

# 来源契约提供：给定（已解析的有限非负）价格与数量，返回该组合是否被契约确认为“无挂单”
EmptyEncoding = Callable[[Decimal, Decimal], bool]


@dataclass(frozen=True, slots=True)
class SideClassification:
    price: Decimal | None
    volume: Decimal | None
    state: SideState
    reason_codes: tuple[ReasonCode, ...]


def parse_decimal(raw: str | None) -> tuple[Decimal | None, ReasonCode | None]:
    """原始字符串 → 有限 Decimal；失败返回 (None, 理由码)。不接受 float 输入以免精度假象。"""
    if raw is None:
        return None, ReasonCode.QUOTE_MISSING
    text = raw.strip()
    if text in MISSING_TOKENS:
        return None, ReasonCode.QUOTE_MISSING
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        return None, ReasonCode.QUOTE_INVALID
    if not value.is_finite():
        return None, ReasonCode.NONFINITE_NUMBER
    return value, None


def classify_side(
    raw_price: str | None,
    raw_volume: str | None,
    *,
    empty_encoding: EmptyEncoding | None = None,
) -> SideClassification:
    price, price_err = parse_decimal(raw_price)
    volume, volume_err = parse_decimal(raw_volume)

    errors = tuple(dict.fromkeys(e for e in (price_err, volume_err) if e is not None))
    if errors:
        state = SideState.MISSING if set(errors) == {ReasonCode.QUOTE_MISSING} else SideState.INVALID
        return SideClassification(None, None, state, errors)

    assert price is not None and volume is not None
    if price < 0 or volume < 0:
        return SideClassification(None, None, SideState.INVALID, (ReasonCode.QUOTE_INVALID,))

    if price > 0 and volume > 0:
        return SideClassification(price, volume, SideState.VALID, ())

    # 至少一项为零：不可执行
    if empty_encoding is not None and empty_encoding(price, volume):
        return SideClassification(None, None, SideState.EMPTY_CONFIRMED, (ReasonCode.EMPTY_CONFIRMED,))
    return SideClassification(None, None, SideState.UNKNOWN, (ReasonCode.ZERO_PRICE_OR_VOLUME,))
