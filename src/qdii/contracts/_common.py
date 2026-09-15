"""契约解析器共用的小工具（纯函数）。"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal

from qdii.core.quote_norm import parse_decimal
from qdii.core.types import ParseIssue, ParseResult, RawMessage, ReasonCode


def http_failure(msg: RawMessage) -> ParseResult | None:
    """非 200 或空响应时返回带理由的空结果；否则返回 None。"""
    if msg.status in (403, 429):
        return ParseResult((), (ParseIssue(ReasonCode.SOURCE_ACCESS_BLOCKED, f"HTTP {msg.status}"),))
    if msg.status != 200 or not msg.body:
        detail = msg.error or f"HTTP {msg.status}, {len(msg.body)} bytes"
        return ParseResult((), (ParseIssue(ReasonCode.QUOTE_MISSING, detail),))
    return None


def positive_decimal(raw: str | None) -> tuple[Decimal | None, ReasonCode | None]:
    value, err = parse_decimal(raw.replace(",", "") if raw is not None else None)
    if err:
        return None, err
    if value is not None and value <= 0:
        return None, ReasonCode.ZERO_PRICE_OR_VOLUME
    return value, None


def parse_date(text: str, fmt: str = "%Y-%m-%d") -> date | None:
    try:
        return datetime.strptime(text.strip(), fmt).date()  # noqa: DTZ007 - 纯日期，无时刻语义
    except (ValueError, AttributeError):
        return None


def query_param(url: str, name: str) -> str | None:
    m = re.search(rf"[?&]{re.escape(name)}=([^&#]*)", url)
    return m.group(1) if m else None
