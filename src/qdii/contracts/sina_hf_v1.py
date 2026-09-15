"""新浪外盘期货 hf_NQ 契约 v1（DS-05）。

请求：GET https://hq.sinajs.cn/list=hf_NQ，须带 Referer: https://finance.sina.com.cn/
字段（下标从 0，AKShare 适配源码与 2026-09-15 探测一致）：0 price，2 bid，3 ask，6 time，12 date，13 name。
实测：date/time 为北京时间（与接收时间相差约 1 秒）；bid/ask 落在 0.25 点格点，price 不在格点且可低于 bid，
因此 price 不是最后成交价（记为 CALCULATED），锚点使用 bid/ask 中间价（MID）。
报文不含合约月份：contract_id=None，理由 CONTRACT_UNKNOWN（DS-09）。
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from qdii.contracts._common import http_failure
from qdii.core.quote_norm import parse_decimal
from qdii.core.types import (
    Instant,
    MarketQuote,
    ParseIssue,
    ParseResult,
    PriceType,
    RawMessage,
    ReasonCode,
    TimeResolution,
    TimeSemantics,
)

SOURCE_ID = "sina"
CONTRACT_VERSION = "sina_hf_v1"
SERIES_ID = "sina:hf_NQ"
SYMBOL = "CME:NQ"
MIN_FIELDS = 14
TICK = Decimal("0.25")
SHANGHAI = ZoneInfo("Asia/Shanghai")
_LINE = re.compile(r'var hq_str_hf_NQ="(.*?)";')


def _positive(raw: str) -> tuple[Decimal | None, ReasonCode | None]:
    value, err = parse_decimal(raw)
    if err:
        return None, err
    if value is not None and value <= 0:
        return None, ReasonCode.ZERO_PRICE_OR_VOLUME
    return value, None


def parse(msg: RawMessage) -> ParseResult:
    failed = http_failure(msg)
    if failed:
        return failed
    m = _LINE.search(msg.body.decode("gb18030", errors="replace"))
    if m is None:
        return ParseResult((), (ParseIssue(ReasonCode.QUOTE_INVALID, "no hq_str_hf_NQ"),))
    f = m.group(1).split(",")
    if len(f) < MIN_FIELDS:
        return ParseResult((), (ParseIssue(ReasonCode.QUOTE_INVALID, f"{len(f)} fields < {MIN_FIELDS}", SYMBOL),))
    issues: list[ParseIssue] = []
    try:
        local = datetime.strptime(f"{f[12]} {f[6]}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=SHANGHAI)
        provider = Instant(int(local.timestamp()) * 1_000_000_000, TimeResolution.S,
                           TimeSemantics.PROVIDER_SNAPSHOT_UNVERIFIED)
    except ValueError:
        provider = Instant.unknown()
        issues.append(ParseIssue(ReasonCode.TIME_UNVERIFIED, f"date/time={f[12]!r} {f[6]!r}", SYMBOL))

    records = []
    for idx, ptype in ((0, PriceType.CALCULATED), (2, PriceType.BID), (3, PriceType.ASK)):
        value, err = _positive(f[idx])
        codes: list[ReasonCode] = [ReasonCode.CONTRACT_UNKNOWN]
        if err:
            codes.append(err)
        elif value is not None and ptype in (PriceType.BID, PriceType.ASK) and value % TICK != 0:
            codes.append(ReasonCode.TICK_MISMATCH)
        records.append(MarketQuote(
            msg_id=msg.msg_id, source_id=SOURCE_ID, series_id=SERIES_ID, symbol=SYMBOL, contract_id=None,
            price_type=ptype, raw_value=f[idx], value=value, event_time=Instant.unknown(), provider_time=provider,
            received_utc_ns=msg.received_utc_ns, contract_version=CONTRACT_VERSION, reason_codes=tuple(codes),
        ))
    return ParseResult(tuple(records), tuple(issues))
