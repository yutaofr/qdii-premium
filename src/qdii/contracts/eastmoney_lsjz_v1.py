"""天天基金历史净值契约 v1（DS-04）。

请求：GET https://api.fund.eastmoney.com/f10/lsjz?fundCode=<code>&pageIndex=<n>&pageSize=20
      &startDate=YYYY-MM-DD&endDate=YYYY-MM-DD，须带 Referer: https://fundf10.eastmoney.com/
实测（2026-09-15，法国主机）：pageSize 上限 20；带日期范围可用；TotalCount 为范围内总行数。
字段：FSRQ 净值日期，DWJZ 单位净值，LJJZ 累计净值，JZZZL 日增长率(%)，
      FHSP/FHFCZ/FHFCBZ/FHFCZ10 分红拆分相关文本。
币种：五只标的均为人民币份额；来源未显式给出，按契约假设 CNY，身份核验见 PH0-06。
"""

from __future__ import annotations

import json

from qdii.contracts._common import http_failure, parse_date, positive_decimal, query_param
from qdii.core.quote_norm import parse_decimal
from qdii.core.types import NavObservation, ParseIssue, ParseResult, RawMessage, ReasonCode

SOURCE_ID = "eastmoney"
CONTRACT_VERSION = "eastmoney_lsjz_v1"
EVENT_KEYS = ("FHSP", "FHFCZ", "FHFCBZ", "FHFCZ10")


def parse(msg: RawMessage) -> ParseResult:
    failed = http_failure(msg)
    if failed:
        return failed
    code = query_param(msg.request.url, "fundCode")
    try:
        doc = json.loads(msg.body)
    except ValueError:
        return ParseResult((), (ParseIssue(ReasonCode.QUOTE_INVALID, "not JSON"),))
    if doc.get("ErrCode") != 0:
        return ParseResult((), (ParseIssue(ReasonCode.QUOTE_INVALID, f"ErrCode={doc.get('ErrCode')}"),))
    rows = (doc.get("Data") or {}).get("LSJZList")
    if rows is None:
        return ParseResult((), (ParseIssue(ReasonCode.QUOTE_MISSING, "Data.LSJZList missing"),))
    if code is None:
        return ParseResult((), (ParseIssue(ReasonCode.QUOTE_INVALID, "fundCode not in request URL"),))

    records, issues = [], []
    for row in rows:
        nav_date = parse_date(row.get("FSRQ") or "")
        if nav_date is None:
            issues.append(ParseIssue(ReasonCode.QUOTE_INVALID, f"FSRQ={row.get('FSRQ')!r}", code))
            continue
        unit_nav, unit_err = positive_decimal(row.get("DWJZ"))
        cum_nav, _ = positive_decimal(row.get("LJJZ"))
        growth, _ = parse_decimal(row.get("JZZZL"))
        codes = ()
        if unit_err:
            codes = (ReasonCode.NONPOSITIVE_NAV if unit_err is ReasonCode.ZERO_PRICE_OR_VOLUME else unit_err,)
        records.append(NavObservation(
            msg_id=msg.msg_id, source_id=SOURCE_ID, code=code, nav_date=nav_date, nav_type="UNIT",
            currency="CNY", unit_nav=unit_nav, cum_nav=cum_nav, growth_pct=growth,
            event_fields=tuple((k, str(row[k])) for k in EVENT_KEYS if row.get(k) not in (None, "")),
            received_utc_ns=msg.received_utc_ns, contract_version=CONTRACT_VERSION, reason_codes=codes,
        ))
    return ParseResult(tuple(records), tuple(issues))


def total_count(msg: RawMessage) -> int | None:
    try:
        return int(json.loads(msg.body).get("TotalCount"))
    except (ValueError, TypeError, AttributeError):
        return None
