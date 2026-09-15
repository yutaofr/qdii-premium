"""VM-03 美股收盘期货锚点选取（纯函数）。

在每个美股实际收盘 c，对每个来源/序列：只使用 provider_time ≤ c 且 received_at ≤ cutoff（通常 c+2 分钟）的样本，
取最后一个有效双边报价的中间价。
- 距 c ≤ 30 秒：READY；30—60 秒：DEGRADED（TIME_SKEW）；> 60 秒：FAILED（不能当作合格锚点）；无样本：MISSING。
- 不得使用 c 之后的样本、日 K 收盘或结算价（AT72）；本函数只接收逐笔快照样本。
- 身份：聚合序列无合约月份时始终带 CONTRACT_UNKNOWN；换月窗口内另标 roll_window（VM-07，DS-09）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from qdii.core.types import ReasonCode


@dataclass(frozen=True, slots=True)
class FuturesSample:
    msg_id: str
    provider_utc_ns: int | None
    received_utc_ns: int
    bid: Decimal | None
    ask: Decimal | None
    tick_ok: bool
    contract_known: bool = False


@dataclass(frozen=True, slots=True)
class AnchorPolicy:
    version: str = "APOL-1.0"
    ok_within_s: float = 30.0
    degraded_within_s: float = 60.0


@dataclass(frozen=True, slots=True)
class AnchorResult:
    series_id: str
    c_utc_ns: int
    cutoff_utc_ns: int
    status: str  # READY / DEGRADED / FAILED / MISSING
    value: str | None  # 中间价（十进制字符串）
    price_type: str  # MID
    lag_s: float | None
    sample_msg_id: str | None
    sample_provider_utc_ns: int | None
    candidates: int
    roll_window: bool
    reason_codes: tuple[str, ...]
    policy_version: str


def third_friday(year: int, month: int) -> date:
    d = date(year, month, 15)
    return d + timedelta(days=(4 - d.weekday()) % 7)


def in_roll_window(et_date: date) -> bool:
    """季月合约到期（第三个周五）前 11 天至到期日：覆盖 CME 现行（周一）与旧（周四前 8 天）换月惯例。"""
    for month in (3, 6, 9, 12):
        expiry = third_friday(et_date.year, month)
        if expiry - timedelta(days=11) <= et_date <= expiry:
            return True
    return False


def select_anchor(
    samples: list[FuturesSample],
    *,
    series_id: str,
    c_utc_ns: int,
    cutoff_utc_ns: int,
    et_date: date,
    policy: AnchorPolicy | None = None,
) -> AnchorResult:
    policy = policy or AnchorPolicy()
    valid = [
        s for s in samples
        if s.received_utc_ns <= cutoff_utc_ns and s.provider_utc_ns is not None and s.provider_utc_ns <= c_utc_ns
        and s.bid is not None and s.ask is not None and math.isfinite(float(s.bid)) and math.isfinite(float(s.ask))
        and 0 < s.bid <= s.ask
    ]
    roll = in_roll_window(et_date)
    base: list[str] = []
    if valid and not all(s.contract_known for s in valid):
        base.append(ReasonCode.CONTRACT_UNKNOWN.value)
    if not valid:
        return AnchorResult(series_id, c_utc_ns, cutoff_utc_ns, "MISSING", None, "MID", None, None, None, 0, roll,
                            (ReasonCode.FUTURE_ANCHOR_MISSING.value, ReasonCode.ANCHOR_CAPTURE_FAILED.value),
                            policy.version)
    best = max(valid, key=lambda s: (s.provider_utc_ns, s.received_utc_ns, s.msg_id))
    lag = (c_utc_ns - best.provider_utc_ns) / 1e9  # type: ignore[operator]
    reasons = list(base)
    if not best.tick_ok:
        reasons.append(ReasonCode.TICK_MISMATCH.value)
    if lag <= policy.ok_within_s:
        status = "READY"
    elif lag <= policy.degraded_within_s:
        status = "DEGRADED"
        reasons.append(ReasonCode.TIME_SKEW.value)
    else:
        status = "FAILED"
        reasons += [ReasonCode.FUTURE_ANCHOR_MISSING.value, ReasonCode.ANCHOR_CAPTURE_FAILED.value]
    mid = (best.bid + best.ask) / 2  # type: ignore[operator]
    return AnchorResult(series_id, c_utc_ns, cutoff_utc_ns, status, str(mid), "MID", lag, best.msg_id,
                        best.provider_utc_ns, len(valid), roll, tuple(dict.fromkeys(reasons)), policy.version)
