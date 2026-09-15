"""盘中估算净值 E-NAV（VM M0，勘误 E8：昨结算作美股收盘期货锚点）。纯函数。

  E-NAV(t) = N0 × I(c)/I(a) × F(t)/F(c) × X(t)/X0
  估算溢价 = 价格 / E-NAV − 1

- N0：最近已披露单位净值（净值日 a）；I(a)：a 对应的 NDX 收盘；X0：a 当日中间价（L0_FX_T）。
- c：截止时刻之前最近一个已收盘的美股交易日；I(c)：c 日 NDX 收盘。
- F(c)：新浪 hf_NQ 昨结算（CME 日结算与 16:00 ET 收盘同刻）；F(t)：同一行情行的买卖价中点，按成员报价时刻 as-of 取。
- X(t)：CFETS USD/CNY 即期买卖价中点，同样按成员报价时刻 as-of 取。

结果只分两态：PROXY_ANCHOR（已算出，锚点为未验证代理）/ UNAVAILABLE（缺输入或检查失败，给出原因）。
满仓假设（FULL_EXPOSURE_ASSUMPTION）、合约月份未知（CONTRACT_UNKNOWN）始终随结果披露。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from qdii.core.types import ReasonCode

SETTLE_PUBLISH_LAG_NS = 2 * 3600 * 1_000_000_000  # CME 18:00 ET 重新开盘后，行情行的昨结算才指向 c


@dataclass(frozen=True, slots=True)
class EnavPolicy:
    version: str = "EPOL-0.1-PROXY"
    futures_max_age_s: float = 60.0  # 期货样本相对成员报价时刻
    fx_max_age_s: float = 120.0  # CFETS 每 30 秒轮询
    basis_min: float = -0.005  # 昨结算 / 指数收盘 − 1 的合理范围（持有成本为正，近到期趋近 0）
    basis_max: float = 0.03


@dataclass(frozen=True, slots=True)
class EnavInput:
    code: str
    price: float | None  # 比较口径价格（连续交易为卖一，否则最新价）
    quote_time_utc_ns: int | None
    nav: float | None
    nav_usable: bool  # 已通过净值校验且无事件/规则隔离
    index_date: date | None  # a 对应的美股交易日
    index_at_anchor: float | None  # I(a)
    fx_at_anchor: float | None  # X0
    us_close_date: date | None  # c
    us_close_utc_ns: int | None
    index_at_close: float | None  # I(c)
    futures_mid: float | None  # F(t)
    futures_settle: float | None  # F(c)
    futures_time_utc_ns: int | None
    fx_spot: float | None  # X(t)
    fx_time_utc_ns: int | None


@dataclass(frozen=True, slots=True)
class EnavResult:
    code: str
    status: str  # PROXY_ANCHOR / UNAVAILABLE
    enav: float | None
    premium: float | None
    index_move: float | None  # I(c)/I(a)
    futures_move: float | None  # F(t)/F(c)
    fx_move: float | None  # X(t)/X0
    basis: float | None  # F(c)/I(c) − 1
    futures_age_s: float | None
    fx_age_s: float | None
    reasons: tuple[ReasonCode, ...]


def _age_s(t: int | None, ref: int | None) -> float | None:
    return None if t is None or ref is None else (ref - t) / 1e9


def _pos(x: float | None) -> bool:
    return x is not None and x > 0


def evaluate_enav(m: EnavInput, policy: EnavPolicy) -> EnavResult:
    missing: list[ReasonCode] = []
    if not (_pos(m.nav) and m.nav_usable):
        missing.append(ReasonCode.NAV_REJECTED if _pos(m.nav) else ReasonCode.NAV_MISSING)
    if not (_pos(m.index_at_anchor) and _pos(m.fx_at_anchor) and m.index_date is not None):
        missing.append(ReasonCode.NAV_FX_RULE_UNKNOWN)  # 缺 I(a) 或 X0：不能把净值换算到共同基准
    if not (_pos(m.index_at_close) and m.us_close_date is not None and m.us_close_utc_ns is not None):
        missing.append(ReasonCode.INDEX_CLOSE_MISSING)
    elif m.index_date is not None and m.us_close_date < m.index_date:
        missing.append(ReasonCode.ANCHOR_INVALID)
    if not _pos(m.price) or m.quote_time_utc_ns is None:
        missing.append(ReasonCode.QUOTE_MISSING)

    f_age = _age_s(m.futures_time_utc_ns, m.quote_time_utc_ns)
    if not (_pos(m.futures_mid) and _pos(m.futures_settle)) or f_age is None:
        missing.append(ReasonCode.FUTURE_ANCHOR_MISSING)
    elif f_age > policy.futures_max_age_s:
        missing.append(ReasonCode.TIME_SKEW)
    elif m.us_close_utc_ns is not None and m.futures_time_utc_ns < m.us_close_utc_ns + SETTLE_PUBLISH_LAG_NS:
        missing.append(ReasonCode.FUTURE_ANCHOR_MISSING)  # 行情行的昨结算尚未滚动到 c

    x_age = _age_s(m.fx_time_utc_ns, m.quote_time_utc_ns)
    if not _pos(m.fx_spot) or x_age is None:
        missing.append(ReasonCode.FX_MISSING)
    elif x_age > policy.fx_max_age_s:
        missing.append(ReasonCode.FX_STALE)

    basis = (m.futures_settle / m.index_at_close - 1) if _pos(m.futures_settle) and _pos(m.index_at_close) else None
    if basis is not None and not policy.basis_min <= basis <= policy.basis_max:
        missing.append(ReasonCode.SETTLEMENT_BASIS_SUSPECT)

    index_move = m.index_at_close / m.index_at_anchor if _pos(m.index_at_close) and _pos(m.index_at_anchor) else None
    futures_move = m.futures_mid / m.futures_settle if _pos(m.futures_mid) and _pos(m.futures_settle) else None
    fx_move = m.fx_spot / m.fx_at_anchor if _pos(m.fx_spot) and _pos(m.fx_at_anchor) else None
    if missing:
        return EnavResult(m.code, "UNAVAILABLE", None, None, index_move, futures_move, fx_move, basis, f_age, x_age,
                          tuple(dict.fromkeys(missing)))
    enav = m.nav * index_move * futures_move * fx_move  # type: ignore[operator]
    return EnavResult(
        m.code, "PROXY_ANCHOR", enav, m.price / enav - 1,  # type: ignore[operator]
        index_move, futures_move, fx_move, basis, f_age, x_age,
        (ReasonCode.SETTLEMENT_ANCHOR_PROXY, ReasonCode.CONTRACT_UNKNOWN, ReasonCode.FULL_EXPOSURE_ASSUMPTION),
    )
