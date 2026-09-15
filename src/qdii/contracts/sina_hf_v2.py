"""新浪外盘期货 hf_NQ 契约 v2 = v1 + 字段 7 昨结算价（勘误 E8）。

字段 7 为前一 CME 交易日的结算价（AKShare 适配源码字段映射；与同一行买卖价同属新浪连续合约）。
CME 股指期货日结算按美东 16:00 前 30 秒成交确定，与纳指收盘同一时刻；该对应关系及合约一致性尚待证据，
因此只作代理锚点（SETTLEMENT_ANCHOR_PROXY），不是 VM-03 的 c 时点样本。
"""

from __future__ import annotations

import re

from qdii.contracts import sina_hf_v1
from qdii.core.types import MarketQuote, ParseResult, PriceType, RawMessage, ReasonCode

SOURCE_ID = sina_hf_v1.SOURCE_ID
CONTRACT_VERSION = "sina_hf_v2"
SERIES_ID = sina_hf_v1.SERIES_ID
SYMBOL = sina_hf_v1.SYMBOL
_LINE = re.compile(r'var hq_str_hf_NQ="(.*?)";')


def parse(msg: RawMessage) -> ParseResult:
    base = sina_hf_v1.parse(msg)
    if not base.records:
        return base
    fields = _LINE.search(msg.body.decode("gb18030", errors="replace")).group(1).split(",")  # v1 已确认存在
    value, err = sina_hf_v1._positive(fields[7])
    first = base.records[0]
    settle = MarketQuote(
        msg_id=msg.msg_id, source_id=SOURCE_ID, series_id=SERIES_ID, symbol=SYMBOL, contract_id=None,
        price_type=PriceType.SETTLE, raw_value=fields[7], value=value, event_time=first.event_time,
        provider_time=first.provider_time, received_utc_ns=msg.received_utc_ns, contract_version=CONTRACT_VERSION,
        reason_codes=(ReasonCode.CONTRACT_UNKNOWN, ReasonCode.SETTLEMENT_ANCHOR_PROXY) + ((err,) if err else ()),
    )
    return ParseResult((*base.records, settle), base.issues)
