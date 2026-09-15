"""中国货币网人民币汇率中间价历史契约 v1。

请求：GET https://www.chinamoney.com.cn/ags/ms/cm-u-bk-ccpr/CcprHisNew
      ?startDate=YYYY-MM-DD&endDate=YYYY-MM-DD&currency=USD/CNY&pageNum=n&pageSize=m
响应：data.searchlist 给出 values 的币种顺序；records[].date 为中国发布日，values[i] 为中间价字符串。
USD/CNY 以每 1 美元报价（2026-09-15 实测 6.7670）；100JPY/CNY 等非 1 单位报价不在本契约范围。
"""

from __future__ import annotations

import json

from qdii.contracts._common import http_failure, parse_date, positive_decimal
from qdii.core.types import FxFixing, ParseIssue, ParseResult, RawMessage, ReasonCode

SOURCE_ID = "chinamoney"
CONTRACT_VERSION = "chinamoney_ccpr_his_v1"
SUPPORTED_PAIRS = frozenset({"USD/CNY"})


def parse(msg: RawMessage) -> ParseResult:
    failed = http_failure(msg)
    if failed:
        return failed
    try:
        doc = json.loads(msg.body)
        data, rows = doc.get("data") or {}, doc.get("records")
    except (ValueError, AttributeError):
        return ParseResult((), (ParseIssue(ReasonCode.QUOTE_INVALID, "not JSON"),))
    pairs = data.get("searchlist") or []
    if rows is None or not pairs:
        return ParseResult((), (ParseIssue(ReasonCode.QUOTE_MISSING, "records/searchlist missing"),))
    records, issues = [], []
    for row in rows:
        d = parse_date(row.get("date", ""))
        values = row.get("values") or []
        if d is None or len(values) != len(pairs):
            issues.append(ParseIssue(ReasonCode.QUOTE_INVALID, f"bad row {row!r}"))
            continue
        for pair, raw in zip(pairs, values, strict=True):
            if pair not in SUPPORTED_PAIRS:
                continue
            rate, err = positive_decimal(raw)
            records.append(FxFixing(msg.msg_id, SOURCE_ID, pair, d, rate, msg.received_utc_ns, CONTRACT_VERSION,
                                    (err,) if err else ()))
    return ParseResult(tuple(records), tuple(issues))


def page_total(msg: RawMessage) -> int | None:
    try:
        return int((json.loads(msg.body).get("data") or {}).get("pageTotal"))
    except (ValueError, TypeError, AttributeError):
        return None
