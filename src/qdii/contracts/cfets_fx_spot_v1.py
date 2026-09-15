"""中国货币网人民币外汇即期报价契约 v1（DS 汇率主源）。

请求：GET https://www.chinamoney.com.cn/r/cms/www/chinamoney/data/fx/rfx-sp-quot.json
响应：head.rep_code="200"；data.showDateCN 为该批报价的北京时间（2026-09-15 实测盘中与接收时间相差约 2 秒，
非交易时段停在前一时刻）；records[] 每个货币对含 bidPrc / askPrc，无报价时为 "---"。
本契约只取 USD/CNY（每 1 美元），输出买、卖两条报价；主值由使用方取同一快照中点。
"""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from qdii.contracts._common import http_failure, positive_decimal
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

SOURCE_ID = "cfets"
CONTRACT_VERSION = "cfets_fx_spot_v1"
SERIES_ID = "cfets:USD/CNY:spot"
PAIR = "USD/CNY"
SHANGHAI = ZoneInfo("Asia/Shanghai")


def parse(msg: RawMessage) -> ParseResult:
    failed = http_failure(msg)
    if failed:
        return failed
    try:
        doc = json.loads(msg.body)
        code, data, rows = doc["head"]["rep_code"], doc["data"], doc["records"]
    except (ValueError, KeyError, TypeError):
        return ParseResult((), (ParseIssue(ReasonCode.QUOTE_INVALID, "not CFETS spot JSON"),))
    if code != "200":
        return ParseResult((), (ParseIssue(ReasonCode.QUOTE_MISSING, f"rep_code={code!r}"),))
    try:
        local = datetime.strptime(str(data.get("showDateCN")), "%Y-%m-%d %H:%M:%S").replace(tzinfo=SHANGHAI)
        provider = Instant(int(local.timestamp()) * 1_000_000_000, TimeResolution.S,
                           TimeSemantics.PROVIDER_SNAPSHOT_UNVERIFIED)
    except ValueError:
        return ParseResult((), (ParseIssue(ReasonCode.TIME_UNVERIFIED, f"showDateCN={data.get('showDateCN')!r}"),))
    row = next((r for r in rows if isinstance(r, dict) and r.get("ccyPair") == PAIR), None)
    if row is None:
        return ParseResult((), (ParseIssue(ReasonCode.QUOTE_MISSING, f"{PAIR} not in records"),))
    records = []
    for key, ptype in (("bidPrc", PriceType.BID), ("askPrc", PriceType.ASK)):
        raw = row.get(key)
        value, err = positive_decimal(None if raw in (None, "---", "") else str(raw))
        codes = (err,) if err else (() if value is not None else (ReasonCode.QUOTE_MISSING,))
        records.append(MarketQuote(
            msg_id=msg.msg_id, source_id=SOURCE_ID, series_id=SERIES_ID, symbol=PAIR, contract_id=None,
            price_type=ptype, raw_value=str(raw), value=value, event_time=Instant.unknown(), provider_time=provider,
            received_utc_ns=msg.received_utc_ns, contract_version=CONTRACT_VERSION, reason_codes=codes,
        ))
    return ParseResult(tuple(records), ())
