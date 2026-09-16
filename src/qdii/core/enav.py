"""盘中估算净值 E-NAV（VM M0，勘误 E8：昨结算作美股收盘期货锚点）。纯函数。

  E-NAV(t) = N0 × I(c)/I(a) × F(t)/F(c) × X(t)/X0
  估算溢价 = 价格 / E-NAV − 1

- N0：最近已披露单位净值（净值日 a）；I(a)：a 对应的 NDX 收盘；X0：a 当日中间价（L0_FX_T）。
- c：截止时刻之前最近一个已收盘的美股交易日；I(c)：c 日 NDX 收盘。
- F(c)：新浪 hf_NQ 昨结算（CME 日结算与 16:00 ET 收盘同刻）；F(t)：同一行情行的买卖价中点，按成员报价时刻 as-of 取。
- X(t)：CFETS USD/CNY 即期买卖价中点，同样按成员报价时刻 as-of 取。

合约身份与换月（VM-07，D4 专项 F1）：F(t) 与 F(c) 取自同一合约的同一行情行。
- 合约身份已知（hf_NQYYMM）：换月不会污染比值，换月窗口内只作披露（ROLL_WINDOW）。
- 身份未知（连续代码兜底）：换月窗口内 F(t) 与 F(c) 可能分属不同月份，且净变动无法拆分出合约价差
  （例：合约价差 +1% 与行情 −0.6% 相抵后净变动仅 +0.4%），因此**一律不出估算**（ROLL_ANCHOR_MISSING）。
`futures_move_max` 只是异常熔断（明显超出单日可能的变动），不作为合约身份凭据。

结果三态（四审 D1）：
- PROXY_ANCHOR：连续交易中、按知识截止时刻检查 ETF/期货/汇率都够新，可作"当前"盘中代理估算（结算日未验证）；
- REFERENCE：收盘/午休参考快照上的估算，只是历史参考，不代表当前可交易；
- UNAVAILABLE：缺输入、过期、阶段或日历不满足，给出原因。
满仓假设（FULL_EXPOSURE_ASSUMPTION）、合约月份未知（CONTRACT_UNKNOWN）、结算锚点为代理始终随结果披露。
独立于相对比较的准入：同日期子集退出等 R 条件不影响 E。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from qdii.core.types import ReasonCode

SETTLE_PUBLISH_LAG_NS = 2 * 3600 * 1_000_000_000  # CME 18:00 ET 重新开盘后，行情行的昨结算才指向 c


@dataclass(frozen=True, slots=True)
class EnavPolicy:
    version: str = "EPOL-0.2-PROXY"
    quote_max_age_s: float = 60.0  # 当前估算：ETF 报价相对知识截止时刻（与相对比较 max_age_s 一致）
    futures_max_age_s: float = 60.0  # 期货样本相对成员报价时刻，当前估算时同时相对知识截止时刻
    fx_max_age_s: float = 120.0  # CFETS 每 30 秒轮询
    basis_min: float = -0.005  # 昨结算 / 指数收盘 − 1 的合理范围（持有成本为正，近到期趋近 0）
    basis_max: float = 0.03
    futures_move_max: float = 0.03  # 期货段单次变动熔断（异常检测，非身份凭据）


@dataclass(frozen=True, slots=True)
class EnavInput:
    code: str
    price: float | None  # 比较口径价格（连续交易为卖一，否则最新价）
    quote_time_utc_ns: int | None
    cutoff_utc_ns: int
    current: bool  # 输入包为连续交易（CURRENT）模式
    phase_ok: bool  # 报价所处阶段合格（连续交易，或收盘参考冻结快照）
    calendar_ok: bool  # 截止日与该成员锚点日期都在日历覆盖范围内
    roll_window: bool  # c 所在美东日期处于季月换月窗口
    contract_known: bool  # 期货取自身份已知的季月合约（而非月份未知的连续代码）
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
    status: str  # PROXY_ANCHOR / REFERENCE / UNAVAILABLE
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
    if not m.calendar_ok:
        missing.append(ReasonCode.CALENDAR_UNCERTAIN)
    if not m.phase_ok:
        missing.append(ReasonCode.SESSION_BOUNDARY)
    if m.current:  # 四审 D1：当前估算按知识截止时刻检查各输入时效，旧行情停更后不得继续显示为可用
        for t, limit, reason in ((m.quote_time_utc_ns, policy.quote_max_age_s, ReasonCode.TIME_SKEW),
                                 (m.futures_time_utc_ns, policy.futures_max_age_s, ReasonCode.TIME_SKEW),
                                 (m.fx_time_utc_ns, policy.fx_max_age_s, ReasonCode.FX_STALE)):
            if t is not None and (m.cutoff_utc_ns - t) / 1e9 > limit:
                missing.append(reason)
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
    if m.roll_window and not m.contract_known:
        missing.append(ReasonCode.ROLL_ANCHOR_MISSING)  # 身份未知 + 换月窗口：无法确认 F(t) 与 F(c) 同一合约
    if futures_move is not None and abs(futures_move - 1) > policy.futures_move_max:
        missing.append(ReasonCode.SOURCE_ANCHOR_MISMATCH)  # 异常熔断：变动超出单日可能范围
    fx_move = m.fx_spot / m.fx_at_anchor if _pos(m.fx_spot) and _pos(m.fx_at_anchor) else None
    if missing:
        return EnavResult(m.code, "UNAVAILABLE", None, None, index_move, futures_move, fx_move, basis, f_age, x_age,
                          tuple(dict.fromkeys(missing)))
    enav = m.nav * index_move * futures_move * fx_move  # type: ignore[operator]
    return EnavResult(
        m.code, "PROXY_ANCHOR" if m.current else "REFERENCE", enav, m.price / enav - 1,  # type: ignore[operator]
        index_move, futures_move, fx_move, basis, f_age, x_age,
        (ReasonCode.SETTLEMENT_ANCHOR_PROXY, ReasonCode.FULL_EXPOSURE_ASSUMPTION)
        + ((ReasonCode.ROLL_WINDOW,) if m.roll_window else ())
        + (() if m.contract_known else (ReasonCode.CONTRACT_UNKNOWN,)),
    )
