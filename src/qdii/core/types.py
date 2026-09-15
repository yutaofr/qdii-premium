"""核心数据类型：时间、原始消息、规范化记录、枚举与理由码（QS-01 / QS-07）。

本模块只放不可变数据结构，不做 I/O、不读时钟。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import NewType

UtcNs = NewType("UtcNs", int)


class TimeResolution(StrEnum):
    NS = "NS"
    MS = "MS"
    S = "S"
    MINUTE = "MINUTE"
    DATE = "DATE"
    UNKNOWN = "UNKNOWN"


class TimeSemantics(StrEnum):
    """时间字段的语义；未经盘中验证的供应商时间不得当作事件时间。"""

    EVENT = "EVENT"
    PROVIDER_SNAPSHOT_UNVERIFIED = "PROVIDER_SNAPSHOT_UNVERIFIED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class Instant:
    utc_ns: int | None  # None 表示无法确定，不猜日期
    resolution: TimeResolution
    semantics: TimeSemantics = TimeSemantics.UNKNOWN

    @staticmethod
    def unknown() -> Instant:
        return Instant(None, TimeResolution.UNKNOWN, TimeSemantics.UNKNOWN)


class SideState(StrEnum):  # QS-01 side_state
    VALID = "VALID"
    EMPTY_CONFIRMED = "EMPTY_CONFIRMED"
    MISSING = "MISSING"
    INVALID = "INVALID"
    UNKNOWN = "UNKNOWN"


class Side(StrEnum):
    BID = "BID"
    ASK = "ASK"


class PriceType(StrEnum):
    LAST = "LAST"
    OPEN = "OPEN"
    PREV_CLOSE = "PREV_CLOSE"
    BID = "BID"
    ASK = "ASK"
    MID = "MID"
    SETTLE = "SETTLE"
    CALCULATED = "CALCULATED"
    UNKNOWN = "UNKNOWN"


class ReasonCode(StrEnum):
    """QS-07 理由码登记表的唯一实现。新增代码须同步修改 QS 版本与测试。"""

    NAV_MISSING = "NAV_MISSING"
    NAV_REJECTED = "NAV_REJECTED"
    NAV_FX_RULE_UNKNOWN = "NAV_FX_RULE_UNKNOWN"
    PENDING_VERIFY = "PENDING_VERIFY"
    CORPORATE_ACTION_PENDING = "CORPORATE_ACTION_PENDING"
    NONSEPARABLE_EVENT = "NONSEPARABLE_EVENT"
    FACTOR_GROUP_MISMATCH = "FACTOR_GROUP_MISMATCH"
    CONTRACT_UNKNOWN = "CONTRACT_UNKNOWN"
    TICK_MISMATCH = "TICK_MISMATCH"
    FUTURE_ANCHOR_MISSING = "FUTURE_ANCHOR_MISSING"
    SETTLEMENT_ANCHOR_PROXY = "SETTLEMENT_ANCHOR_PROXY"  # 勘误 E8：以昨结算作美股收盘期货锚点，未验证
    SETTLEMENT_BASIS_SUSPECT = "SETTLEMENT_BASIS_SUSPECT"  # 昨结算相对指数收盘的基差超出合理范围
    INDEX_CLOSE_MISSING = "INDEX_CLOSE_MISSING"  # 最近美股交易日的指数收盘未到
    ROLL_ANCHOR_MISSING = "ROLL_ANCHOR_MISSING"
    SOURCE_ANCHOR_MISMATCH = "SOURCE_ANCHOR_MISMATCH"
    FX_MISSING = "FX_MISSING"
    FX_STALE = "FX_STALE"
    CNH_PROXY = "CNH_PROXY"
    KNOWN_DELAY = "KNOWN_DELAY"
    SUSPECTED_DELAY = "SUSPECTED_DELAY"
    TIME_SKEW = "TIME_SKEW"
    TIME_UNVERIFIED = "TIME_UNVERIFIED"
    FUTURE_TIMESTAMP = "FUTURE_TIMESTAMP"
    SESSION_BOUNDARY = "SESSION_BOUNDARY"
    SESSION_NOT_ADVANCED = "SESSION_NOT_ADVANCED"
    FUTURES_CLOSED = "FUTURES_CLOSED"
    RECOVERY_PENDING = "RECOVERY_PENDING"
    CLOCK_SKEW = "CLOCK_SKEW"
    CLOCK_UNVERIFIED = "CLOCK_UNVERIFIED"
    CALENDAR_UNCERTAIN = "CALENDAR_UNCERTAIN"
    CALENDAR_SYNC_DEGRADED = "CALENDAR_SYNC_DEGRADED"
    ANCHOR_AGED = "ANCHOR_AGED"
    ANCHOR_EXTENDED = "ANCHOR_EXTENDED"
    ANCHOR_INVALID = "ANCHOR_INVALID"
    EMPTY_CONFIRMED = "EMPTY_CONFIRMED"
    QUOTE_MISSING = "QUOTE_MISSING"
    QUOTE_INVALID = "QUOTE_INVALID"
    ZERO_PRICE_OR_VOLUME = "ZERO_PRICE_OR_VOLUME"
    NONFINITE_NUMBER = "NONFINITE_NUMBER"
    NONPOSITIVE_NAV = "NONPOSITIVE_NAV"
    PRICE_AT_LIMIT = "PRICE_AT_LIMIT"
    LIMIT_UP_LOCKED = "LIMIT_UP_LOCKED"
    LIMIT_DOWN_LOCKED = "LIMIT_DOWN_LOCKED"
    ONE_SIDED_BOOK = "ONE_SIDED_BOOK"
    BOOK_INCOMPLETE = "BOOK_INCOMPLETE"
    SOURCE_CONFLICT = "SOURCE_CONFLICT"
    SOURCE_ACCESS_BLOCKED = "SOURCE_ACCESS_BLOCKED"
    MODEL_UNCALIBRATED = "MODEL_UNCALIBRATED"
    FULL_EXPOSURE_ASSUMPTION = "FULL_EXPOSURE_ASSUMPTION"
    DIFFERENTIAL_UNCALIBRATED = "DIFFERENTIAL_UNCALIBRATED"
    DIFFERENCE_UNRESOLVED = "DIFFERENCE_UNRESOLVED"
    STRESS_OUTSIDE_BUDGET = "STRESS_OUTSIDE_BUDGET"
    PCF_FX_PROXY = "PCF_FX_PROXY"
    PCF_FX_REFERENCE_LAG = "PCF_FX_REFERENCE_LAG"
    PCF_FX_UNUSABLE = "PCF_FX_UNUSABLE"
    ANCHOR_CAPTURE_FAILED = "ANCHOR_CAPTURE_FAILED"
    BACKFILL_FAILED = "BACKFILL_FAILED"
    HISTORICAL_RESEARCH_ONLY = "HISTORICAL_RESEARCH_ONLY"
    REPLAY_INPUT_MISSING = "REPLAY_INPUT_MISSING"
    SYNTHETIC_FIXTURE = "SYNTHETIC_FIXTURE"


# ---------- 原始消息 ----------

@dataclass(frozen=True, slots=True)
class RequestTemplate:
    method: str
    url: str
    headers: tuple[tuple[str, str], ...] = ()  # 无凭据；按名称排序后保存
    body: bytes = b""


@dataclass(frozen=True, slots=True)
class ClockStatus:
    synced: bool | None
    offset_ms: float | None
    checked_utc_ns: int | None

    @staticmethod
    def unverified() -> ClockStatus:
        return ClockStatus(None, None, None)


@dataclass(frozen=True, slots=True)
class RawMessage:
    msg_id: str  # f"{source_id}:{received_utc_ns}:{seq}"，确定性
    run_id: str  # LIVE-* / SYN-* / PROBE-* / RESEARCH-*
    source_id: str
    endpoint_id: str
    request: RequestTemplate
    status: int | None  # 最终响应状态；None = 网络层失败
    error: str | None
    body: bytes
    body_sha256: str
    received_utc_ns: int
    monotonic_ns: int
    rtt_ms: float | None
    clock: ClockStatus
    final_url: str | None = None
    redirects: tuple[tuple[int, str], ...] = ()  # (状态码, Location)
    response_headers: tuple[tuple[str, str], ...] = ()  # 白名单子集，含服务器 Date

    @property
    def is_synthetic(self) -> bool:
        return self.run_id.startswith("SYN-")


# ---------- 规范化记录 ----------

@dataclass(frozen=True, slots=True)
class MarketQuote:
    msg_id: str
    source_id: str
    series_id: str
    symbol: str  # 交易所前缀 + 代码，如 SSE:513100
    contract_id: str | None  # None = 身份未知，不得冒充固定月份
    price_type: PriceType
    raw_value: str | None
    value: Decimal | None  # 无效为 None，不为 0
    event_time: Instant
    provider_time: Instant
    received_utc_ns: int
    contract_version: str
    reason_codes: tuple[ReasonCode, ...] = ()


@dataclass(frozen=True, slots=True)
class QuoteSide:
    snapshot_id: str
    msg_id: str
    symbol: str
    side: Side
    level: int
    raw_price: str | None
    raw_volume: str | None
    price: Decimal | None  # 仅 VALID 时非 None
    volume: Decimal | None
    state: SideState
    received_utc_ns: int
    contract_version: str
    reason_codes: tuple[ReasonCode, ...] = ()


@dataclass(frozen=True, slots=True)
class NavObservation:
    """来源层的单位净值观测；带 revision 的 NavRecord 由存储层生成（DS-11）。"""

    msg_id: str
    source_id: str
    code: str
    nav_date: date
    nav_type: str  # UNIT
    currency: str  # 契约假设，见各解析器说明
    unit_nav: Decimal | None
    cum_nav: Decimal | None
    growth_pct: Decimal | None  # 来源给出的日增长率（%），用于识别分红拆分断点
    event_fields: tuple[tuple[str, str], ...]  # 非空的分红/拆分原文字段
    received_utc_ns: int
    contract_version: str
    reason_codes: tuple[ReasonCode, ...] = ()


@dataclass(frozen=True, slots=True)
class IndexClose:
    msg_id: str
    source_id: str
    index_code: str  # NDX
    trade_date: date  # 指数所在市场的交易日
    close: Decimal | None
    received_utc_ns: int
    contract_version: str
    reason_codes: tuple[ReasonCode, ...] = ()


@dataclass(frozen=True, slots=True)
class FxFixing:
    msg_id: str
    source_id: str
    pair: str  # USD/CNY
    publish_date: date  # 中国日期
    rate: Decimal | None  # 每 1 美元兑人民币
    received_utc_ns: int
    contract_version: str
    reason_codes: tuple[ReasonCode, ...] = ()


Record = MarketQuote | QuoteSide | NavObservation | IndexClose | FxFixing


@dataclass(frozen=True, slots=True)
class ParseIssue:
    code: ReasonCode
    detail: str
    symbol: str | None = None


@dataclass(frozen=True, slots=True)
class ParseResult:
    records: tuple[Record, ...]
    issues: tuple[ParseIssue, ...]
