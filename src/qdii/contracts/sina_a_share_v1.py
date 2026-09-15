"""新浪 A 股批量行情契约 v1（DS-03）。

请求：GET https://hq.sinajs.cn/list=<codes>，须带 Referer: https://finance.sina.com.cn/
（2026-09-15 从法国主机实测：不带返回 403；DS-08 已接受）。
报文：GB18030；每行 var hq_str_<code>="f0,f1,...";，本次观测 34 个字段。

字段（下标从 0）：0 名称，1 今开，2 昨收，3 最新，6 买一价（冗余），7 卖一价（冗余），
8 成交量，9 成交额，10/11 买一量/价，20/21 卖一量/价，30 日期，31 时间。
时间为供应商快照时间（收盘后仍会更新，见探测证据），不是最后成交时间。
"""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from qdii.core.quote_norm import classify_side, parse_decimal
from qdii.core.types import (
    Instant,
    MarketQuote,
    ParseIssue,
    ParseResult,
    PriceType,
    QuoteSide,
    RawMessage,
    ReasonCode,
    Record,
    Side,
    TimeResolution,
    TimeSemantics,
)

SOURCE_ID = "sina"
CONTRACT_VERSION = "sina_a_share_v1"
MIN_FIELDS = 32
SHANGHAI = ZoneInfo("Asia/Shanghai")
EXCHANGE_PREFIX = {"sh": "SSE", "sz": "SZSE"}
_LINE = re.compile(r'var hq_str_(\w+)="(.*?)";')


def symbol_of(vendor_code: str) -> str | None:
    prefix, code = vendor_code[:2], vendor_code[2:]
    if prefix not in EXCHANGE_PREFIX or not re.fullmatch(r"\d{6}", code):
        return None
    return f"{EXCHANGE_PREFIX[prefix]}:{code}"


def _provider_time(date_s: str, time_s: str) -> tuple[Instant, ReasonCode | None]:
    try:
        local = datetime.strptime(f"{date_s} {time_s}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=SHANGHAI)
    except ValueError:
        return Instant.unknown(), ReasonCode.TIME_UNVERIFIED
    ns = int(local.timestamp()) * 1_000_000_000
    return Instant(ns, TimeResolution.S, TimeSemantics.PROVIDER_SNAPSHOT_UNVERIFIED), None


def parse(msg: RawMessage) -> ParseResult:
    if msg.status in (403, 429):
        return ParseResult((), (ParseIssue(ReasonCode.SOURCE_ACCESS_BLOCKED, f"HTTP {msg.status}"),))
    if msg.status != 200 or not msg.body:
        detail = msg.error or f"HTTP {msg.status}, {len(msg.body)} bytes"
        return ParseResult((), (ParseIssue(ReasonCode.QUOTE_MISSING, detail),))
    try:
        text = msg.body.decode("gb18030")
    except UnicodeDecodeError as exc:
        return ParseResult((), (ParseIssue(ReasonCode.QUOTE_INVALID, f"GB18030 decode: {exc}"),))

    records: list[Record] = []
    issues: list[ParseIssue] = []
    matches = _LINE.findall(text)
    if not matches:
        issues.append(ParseIssue(ReasonCode.QUOTE_INVALID, "no hq_str assignment found"))

    for vendor_code, payload in matches:
        symbol = symbol_of(vendor_code)
        if symbol is None:
            issues.append(ParseIssue(ReasonCode.QUOTE_INVALID, f"unsupported code {vendor_code}"))
            continue
        fields = payload.split(",")
        if len(fields) < MIN_FIELDS:
            issues.append(ParseIssue(ReasonCode.QUOTE_INVALID, f"{len(fields)} fields < {MIN_FIELDS}", symbol))
            continue
        records.extend(_parse_row(msg, symbol, fields, issues))

    return ParseResult(tuple(records), tuple(issues))


def _parse_row(msg: RawMessage, symbol: str, f: list[str], issues: list[ParseIssue]) -> list[Record]:
    provider_time, time_err = _provider_time(f[30], f[31])
    if time_err:
        issues.append(ParseIssue(time_err, f"date/time={f[30]!r} {f[31]!r}", symbol))
    series_id = f"{SOURCE_ID}:{symbol}:L1"
    snapshot_id = f"{msg.msg_id}:{symbol}"
    out: list[Record] = []

    for idx, price_type in ((3, PriceType.LAST), (1, PriceType.OPEN), (2, PriceType.PREV_CLOSE)):
        value, err = parse_decimal(f[idx])
        codes: tuple[ReasonCode, ...] = ()
        if err:
            codes = (err,)
        elif value is not None and value <= 0:
            value, codes = None, (ReasonCode.ZERO_PRICE_OR_VOLUME,)
        out.append(MarketQuote(
            msg_id=msg.msg_id, source_id=SOURCE_ID, series_id=series_id, symbol=symbol, contract_id=None,
            price_type=price_type, raw_value=f[idx], value=value, event_time=Instant.unknown(),
            provider_time=provider_time, received_utc_ns=msg.received_utc_ns,
            contract_version=CONTRACT_VERSION, reason_codes=codes,
        ))

    # 买一/卖一：量价取 10/11、20/21；6/7 为冗余价格，用于一致性检查
    for side, vol_idx, px_idx, dup_idx in ((Side.BID, 10, 11, 6), (Side.ASK, 20, 21, 7)):
        cls = classify_side(f[px_idx], f[vol_idx], empty_encoding=None)  # 空盘编码尚未核验
        codes = cls.reason_codes
        dup, _ = parse_decimal(f[dup_idx])
        main, _ = parse_decimal(f[px_idx])
        if dup is not None and main is not None and dup != main:
            codes = codes + (ReasonCode.QUOTE_INVALID,)
            issues.append(ParseIssue(ReasonCode.QUOTE_INVALID, f"{side} f{dup_idx}={f[dup_idx]} != f{px_idx}={f[px_idx]}", symbol))
        out.append(QuoteSide(
            snapshot_id=snapshot_id, msg_id=msg.msg_id, symbol=symbol, side=side, level=1,
            raw_price=f[px_idx], raw_volume=f[vol_idx], price=cls.price, volume=cls.volume, state=cls.state,
            received_utc_ns=msg.received_utc_ns, contract_version=CONTRACT_VERSION, reason_codes=codes,
        ))
    return out
