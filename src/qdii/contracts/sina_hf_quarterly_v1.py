"""新浪外盘期货单一月份合约契约 v1（DS-09 身份已知，D4 专项 F1）。

请求：GET https://hq.sinajs.cn/list=hf_NQ2609,hf_NQ2612（与连续代码同一请求），须带 Referer。
每行 `var hq_str_hf_NQ<YYMM>="...";`，字段与连续代码一致：0 价（CALCULATED）、2 买、3 卖、6 时间、7 昨结算、12 日期、13 名称。

与 `sina_hf_v2` 的区别只有一点，却是关键：**合约月份来自代码本身**，因此 contract_id 已知，
不再带 CONTRACT_UNKNOWN。估算用固定月份合约取 F(t) 与 F(c)，二者必然同一合约，换月不再污染比值（VM-07）。
2026-09-16 实测：hf_NQ2609 的买卖价、昨结算与 hf_NQ 完全一致，hf_NQ2612 高约 1.0%（持有成本）。
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
CONTRACT_VERSION = "sina_hf_quarterly_v1"
MIN_FIELDS = 14
TICK = Decimal("0.25")
SHANGHAI = ZoneInfo("Asia/Shanghai")
_LINE = re.compile(r'var hq_str_hf_(NQ(\d{2})(\d{2}))="(.*?)";')


def series_id(contract: str) -> str:
    return f"sina:hf_{contract}"


def parse(msg: RawMessage) -> ParseResult:
    failed = http_failure(msg)
    if failed:
        return failed
    text = msg.body.decode("gb18030", errors="replace")
    records: list[MarketQuote] = []
    issues: list[ParseIssue] = []
    for match in _LINE.finditer(text):
        contract, fields = match.group(1), match.group(4).split(",")
        symbol = f"CME:{contract}"
        if len(fields) < MIN_FIELDS:
            issues.append(ParseIssue(ReasonCode.QUOTE_INVALID, f"{len(fields)} fields < {MIN_FIELDS}", symbol))
            continue
        try:
            local = datetime.strptime(f"{fields[12]} {fields[6]}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=SHANGHAI)
            provider = Instant(int(local.timestamp()) * 1_000_000_000, TimeResolution.S,
                               TimeSemantics.PROVIDER_SNAPSHOT_UNVERIFIED)
        except ValueError:
            provider = Instant.unknown()
            issues.append(ParseIssue(ReasonCode.TIME_UNVERIFIED, f"date/time={fields[12]!r} {fields[6]!r}", symbol))
        for idx, ptype in ((0, PriceType.CALCULATED), (2, PriceType.BID), (3, PriceType.ASK),
                           (7, PriceType.SETTLE)):
            value, err = parse_decimal(fields[idx])
            if value is not None and value <= 0:
                value, err = None, ReasonCode.ZERO_PRICE_OR_VOLUME
            codes: list[ReasonCode] = []
            if err:
                codes.append(err)
            elif value is not None and ptype in (PriceType.BID, PriceType.ASK, PriceType.SETTLE) and value % TICK != 0:
                codes.append(ReasonCode.TICK_MISMATCH)
            if ptype is PriceType.SETTLE:
                codes.append(ReasonCode.SETTLEMENT_ANCHOR_PROXY)  # 结算日映射仍未经供应商证实（勘误 E8）
            records.append(MarketQuote(
                msg_id=msg.msg_id, source_id=SOURCE_ID, series_id=series_id(contract), symbol=symbol,
                contract_id=contract, price_type=ptype, raw_value=fields[idx], value=value,
                event_time=Instant.unknown(), provider_time=provider, received_utc_ns=msg.received_utc_ns,
                contract_version=CONTRACT_VERSION, reason_codes=tuple(codes),
            ))
    if not records and not issues:
        issues.append(ParseIssue(ReasonCode.QUOTE_MISSING, "no hf_NQ<YYMM> line"))
    return ParseResult(tuple(records), tuple(issues))
