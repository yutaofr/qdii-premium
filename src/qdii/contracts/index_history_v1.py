"""NDX 日收盘历史契约 v1：FRED CSV 与 Nasdaq 官方历史 API。

- FRED：GET https://fred.stlouisfed.org/graph/fredgraph.csv?id=NASDAQ100&cosd=YYYY-MM-DD
  两列 observation_date,NASDAQ100；缺值为 "." 或空。
- Nasdaq：GET https://api.nasdaq.com/api/quote/NDX/historical?assetclass=index&fromdate=..&todate=..&limit=N
  data.tradesTable.rows[].date=MM/DD/YYYY，close="29,127.16"。
两者用于交叉核对（2026-09-14 实测同值）；FRED 数据源自 Nasdaq，不构成完全独立证据。
"""

from __future__ import annotations

import json

from qdii.contracts._common import http_failure, parse_date, positive_decimal
from qdii.core.types import IndexClose, ParseIssue, ParseResult, RawMessage, ReasonCode

FRED_VERSION = "fred_series_csv_v1"
NASDAQ_VERSION = "nasdaq_index_historical_v1"
FRED_SERIES_TO_INDEX = {"NASDAQ100": "NDX"}


def parse_fred(msg: RawMessage) -> ParseResult:
    failed = http_failure(msg)
    if failed:
        return failed
    lines = msg.body.decode("utf-8", errors="replace").strip().splitlines()
    if not lines or "," not in lines[0]:
        return ParseResult((), (ParseIssue(ReasonCode.QUOTE_INVALID, "missing CSV header"),))
    series = lines[0].split(",")[1].strip()
    index_code = FRED_SERIES_TO_INDEX.get(series)
    if index_code is None:
        return ParseResult((), (ParseIssue(ReasonCode.QUOTE_INVALID, f"unknown FRED series {series}"),))
    records, issues = [], []
    for line in lines[1:]:
        parts = line.split(",")
        d = parse_date(parts[0]) if parts else None
        if d is None or len(parts) != 2:
            issues.append(ParseIssue(ReasonCode.QUOTE_INVALID, f"bad row {line!r}"))
            continue
        value, err = positive_decimal(parts[1] if parts[1] not in (".", "") else None)
        records.append(IndexClose(msg.msg_id, "fred", index_code, d, value, msg.received_utc_ns, FRED_VERSION,
                                  (err,) if err else ()))
    return ParseResult(tuple(records), tuple(issues))


def parse_nasdaq(msg: RawMessage) -> ParseResult:
    failed = http_failure(msg)
    if failed:
        return failed
    try:
        data = json.loads(msg.body).get("data") or {}
        symbol = data.get("symbol")
        rows = (data.get("tradesTable") or {}).get("rows")
    except (ValueError, AttributeError):
        return ParseResult((), (ParseIssue(ReasonCode.QUOTE_INVALID, "unexpected JSON"),))
    if symbol != "NDX" or rows is None:
        return ParseResult((), (ParseIssue(ReasonCode.QUOTE_INVALID, f"symbol={symbol!r} rows={rows is not None}"),))
    records, issues = [], []
    for row in rows:
        d = parse_date(row.get("date", ""), "%m/%d/%Y")
        if d is None:
            issues.append(ParseIssue(ReasonCode.QUOTE_INVALID, f"date={row.get('date')!r}"))
            continue
        value, err = positive_decimal(row.get("close"))
        records.append(IndexClose(msg.msg_id, "nasdaq", "NDX", d, value, msg.received_utc_ns, NASDAQ_VERSION,
                                  (err,) if err else ()))
    return ParseResult(tuple(records), tuple(issues))
