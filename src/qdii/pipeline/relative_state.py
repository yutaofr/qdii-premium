"""相对比较与盘中估算净值的增量输入状态（ADR-009：在线与回放共用同一路径）。

在线：采集器每收到一条消息就 ingest；ETF 批次到达时以该消息 received_at 为知识截止生成输入包（ADR-010）。
回放：按 (received_at, msg_id) 顺序把原始日志逐条 ingest，得到与在线相同的输入包。
"""

from __future__ import annotations

import tomllib
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from qdii.contracts import (
    cfets_fx_spot_v1,
    chinamoney_ccpr_his_v1,
    eastmoney_lsjz_v1,
    index_history_v1,
    sina_a_share_v1,
    sina_hf_v2,
)
from qdii.core.enav import EnavPolicy
from qdii.core.relative import RelativeMode, RelativePolicy
from qdii.core.relative_bundle import SCHEMA_VERSION, MemberSpec, RelativeBundle
from qdii.core.types import (
    FxFixing,
    IndexClose,
    MarketQuote,
    NavObservation,
    PriceType,
    QuoteSide,
    RawMessage,
    ReasonCode,
    Side,
    SideState,
)
from qdii.io.calendars import CalendarProvider, MarketPhase

SHANGHAI = ZoneInfo("Asia/Shanghai")
NEW_YORK = ZoneInfo("America/New_York")
REFERENCE_WINDOW_S = 600  # 阶段边界后冻结快照的有效期
NAV_ROUNDING_BP = 0.5
GROWTH_TOLERANCE = 0.0002
ETF_ENDPOINT = "sina.etf_batch"
NAV_ENDPOINT_PREFIX = "eastmoney.lsjz."
INDEX_ENDPOINT = "nasdaq.ndx_history"  # 跨日归一化因子 I(a)（评审 R4）
FIXING_ENDPOINT = "chinamoney.ccpr"  # 跨日归一化因子 X0（L0_FX_T）
INDEX_MARKET = "NASDAQ"  # I(a) 的交易日由该日历确定（二审 F1）
FUTURE_TOLERANCE_NS = 2_000_000_000  # 供应商时间晚于接收时间超过该容差视为未来异常（二审 F3，与 core.relative 的 FUTURE_TIMESTAMP 容差一致）
FUTURES_ENDPOINT = "sina.hf_NQ"  # E-NAV：F(t) 与昨结算 F(c)（勘误 E8）
FX_SPOT_ENDPOINT = "cfets.fx_spot_quot"  # E-NAV：X(t)
FACTOR_ENDPOINTS = frozenset({INDEX_ENDPOINT, FIXING_ENDPOINT})
ENAV_ENDPOINTS = frozenset({FUTURES_ENDPOINT, FX_SPOT_ENDPOINT})
TICK_RETENTION_NS = 30 * 60 * 1_000_000_000  # 期货/汇率样本保留最近 30 分钟，供成员按报价时刻 as-of 取值
CONTRACTS = (sina_a_share_v1.CONTRACT_VERSION, eastmoney_lsjz_v1.CONTRACT_VERSION,
             index_history_v1.NASDAQ_VERSION, chinamoney_ccpr_his_v1.CONTRACT_VERSION,
             sina_hf_v2.CONTRACT_VERSION, cfets_fx_spot_v1.CONTRACT_VERSION)


def is_relative_input(endpoint_id: str) -> bool:
    return (endpoint_id in (ETF_ENDPOINT, *FACTOR_ENDPOINTS, *ENAV_ENDPOINTS)
            or endpoint_id.startswith(NAV_ENDPOINT_PREFIX))


@dataclass(frozen=True)
class Fund:
    exchange: str
    code: str
    name: str
    nav_fx_rule: str
    nav_fx_rule_status: str
    factor_group: str = "NDX_USD_UNHEDGED"

    @property
    def symbol(self) -> str:
        return f"{self.exchange}:{self.code}"


def load_funds(path: Path) -> list[Fund]:
    doc = tomllib.loads(path.read_text(encoding="utf-8"))
    return [Fund(f["exchange"], f["code"], f["name"], f["nav_fx_rule"], f["nav_fx_rule_status"], f["factor_group"])
            for f in doc["fund"]]


def is_reference_snapshot(cal: CalendarProvider, provider_ns: int) -> bool:
    """收盘参考可用：连续交易中，或午休起点/收盘后 10 分钟内的冻结快照（勘误 E1）。"""
    phase, session, _ = cal.phase("SSE", provider_ns)
    if phase is MarketPhase.CONTINUOUS:
        return True
    if session is None:
        return False
    window = REFERENCE_WINDOW_S * 1e9
    if phase is MarketPhase.BREAK and session.break_start_utc_ns is not None:
        return 0 <= provider_ns - session.break_start_utc_ns <= window
    if phase is MarketPhase.CLOSED:
        return 0 <= provider_ns - session.close_utc_ns <= window
    return False


@dataclass(frozen=True)
class _Quote:
    """单只基金的一条市场快照（按供应商快照时间判断新旧，评审 R3）。"""

    t: int
    received_utc_ns: int
    msg_id: str
    last: Decimal | None
    ask: Decimal | None
    ask_volume: Decimal | None

    def newer_than(self, other: _Quote | None) -> bool:
        if other is None:
            return True
        return (self.t, self.received_utc_ns) > (other.t, other.received_utc_ns)


@dataclass(frozen=True)
class _NavValue:
    """同一净值日的一条完整经济事实；任一字段不同即为新证据（评审 R2）。"""

    unit_nav: Decimal
    cum_nav: Decimal | None
    growth_pct: Decimal | None
    event_fields: tuple[tuple[str, str], ...]
    msg_id: str

    def fact(self) -> tuple:
        return (self.unit_nav, self.cum_nav, self.growth_pct, self.event_fields)


@dataclass(frozen=True)
class _Factors:
    index: Decimal
    index_date: date
    fx: Decimal
    fx_date: date
    msg_ids: tuple[str, ...]


@dataclass(frozen=True)
class _Tick:
    """期货或汇率的一条双边快照：mid 为买卖价中点；ref 为同一行的昨结算（仅期货）。"""

    t: int
    msg_id: str
    mid: Decimal
    ref: Decimal | None = None


def _mid(bid: Decimal | None, ask: Decimal | None) -> Decimal | None:
    return (bid + ask) / 2 if bid is not None and ask is not None and ask >= bid else None


@dataclass
class RelativeState:
    cal: CalendarProvider
    funds: list[Fund]
    udiff: dict | None = None
    policy: RelativePolicy = field(default_factory=RelativePolicy)
    factor_group: str = "NDX_USD_UNHEDGED"  # VM-10：只在同一因子组内比较
    latest: dict[str, _Quote] = field(default_factory=dict)  # symbol → 最新市场快照
    reference: dict[str, _Quote] = field(default_factory=dict)  # symbol → 最新可作收盘参考的快照
    stale_rejected: int = 0  # 较旧行情被拒绝的次数（审计）
    future_rejected: int = 0  # 供应商时间明显晚于接收时间、未进入有效指针的行情次数（审计，原文仍在原始日志）
    navs: dict[str, dict[date, list[_NavValue]]] = field(default_factory=dict)
    index_closes: dict[date, dict[Decimal, str]] = field(default_factory=dict)  # 日期 → {数值: msg_id}
    fixings: dict[date, dict[Decimal, str]] = field(default_factory=dict)
    enav_policy: EnavPolicy = field(default_factory=EnavPolicy)
    futures: deque[_Tick] = field(default_factory=deque)  # 按供应商时间递增
    fx_spot: deque[_Tick] = field(default_factory=deque)
    last_received_utc_ns: int = 0

    # ---------- ingest ----------

    def ingest(self, msg: RawMessage) -> bool:
        """返回是否被相对比较使用。原始消息全部保留在原始日志中；这里只维护当前有效输入。"""
        if msg.status != 200:
            return False
        self.last_received_utc_ns = max(self.last_received_utc_ns, msg.received_utc_ns)
        if msg.endpoint_id == ETF_ENDPOINT:
            for symbol, quote in self._parse_etf(msg).items():
                # 二审 F3：先按接收时间校验，未来时间的异常行情不得写入有效指针，否则会挡住后续正常行情
                if quote.t - msg.received_utc_ns > FUTURE_TOLERANCE_NS:
                    self.future_rejected += 1
                    continue
                # 评审 R3：按成员比较供应商快照时间，较旧行情（即使接收更晚）不得覆盖；缺失成员保留原值
                if quote.newer_than(self.latest.get(symbol)):
                    self.latest[symbol] = quote
                else:
                    self.stale_rejected += 1
                if is_reference_snapshot(self.cal, quote.t) and quote.newer_than(self.reference.get(symbol)):
                    self.reference[symbol] = quote
            return True
        if msg.endpoint_id.startswith(NAV_ENDPOINT_PREFIX):
            for rec in eastmoney_lsjz_v1.parse(msg).records:
                if isinstance(rec, NavObservation) and rec.unit_nav is not None:
                    values = self.navs.setdefault(rec.code, {}).setdefault(rec.nav_date, [])
                    value = _NavValue(rec.unit_nav, rec.cum_nav, rec.growth_pct, rec.event_fields, msg.msg_id)
                    if all(v.fact() != value.fact() for v in values):  # 完全相同的事实才算重复（AT50）
                        values.append(value)
            return True
        if msg.endpoint_id == INDEX_ENDPOINT:
            for rec in index_history_v1.parse_nasdaq(msg).records:
                if isinstance(rec, IndexClose) and rec.close is not None:
                    self.index_closes.setdefault(rec.trade_date, {}).setdefault(rec.close, msg.msg_id)
            return True
        if msg.endpoint_id == FIXING_ENDPOINT:
            for rec in chinamoney_ccpr_his_v1.parse(msg).records:
                if isinstance(rec, FxFixing) and rec.rate is not None and rec.pair == "USD/CNY":
                    self.fixings.setdefault(rec.publish_date, {}).setdefault(rec.rate, msg.msg_id)
            return True
        if msg.endpoint_id == FUTURES_ENDPOINT:
            self._push(self.futures, msg, sina_hf_v2.parse(msg).records, with_settle=True)
            return True
        if msg.endpoint_id == FX_SPOT_ENDPOINT:
            self._push(self.fx_spot, msg, cfets_fx_spot_v1.parse(msg).records, with_settle=False)
            return True
        return False

    def _push(self, ticks: deque[_Tick], msg: RawMessage, records: tuple, *, with_settle: bool) -> None:
        by_type = {r.price_type: r for r in records if isinstance(r, MarketQuote)}
        bid, ask = by_type.get(PriceType.BID), by_type.get(PriceType.ASK)
        if bid is None or ask is None or bid.provider_time.utc_ns is None:
            return
        if any(ReasonCode.TICK_MISMATCH in r.reason_codes for r in (bid, ask)):
            return
        t = bid.provider_time.utc_ns
        mid = _mid(bid.value, ask.value)
        settle = by_type.get(PriceType.SETTLE)
        ref = settle.value if settle is not None else None
        if mid is None or (with_settle and ref is None):
            return
        if t - msg.received_utc_ns > FUTURE_TOLERANCE_NS:  # 与 ETF 相同：未来时间不进入有效样本（二审 F3）
            self.future_rejected += 1
            return
        if ticks and t <= ticks[-1].t:
            return  # 供应商时间未前进（重复快照或乱序）：保留先到的样本
        ticks.append(_Tick(t, msg.msg_id, mid, ref))
        while ticks and ticks[0].t < t - TICK_RETENTION_NS:
            ticks.popleft()

    @staticmethod
    def _as_of(ticks: deque[_Tick], t: int | None) -> _Tick | None:
        """报价时刻 t（含未来容差）之前最后一条样本。"""
        if t is None:
            return None
        return next((x for x in reversed(ticks) if x.t <= t + FUTURE_TOLERANCE_NS), None)

    def _us_close(self, cutoff_utc_ns: int) -> tuple[date | None, int | None, Decimal | None, str | None]:
        """估值时点之前最近一个已收盘的美股交易日 c，及 c 日 NDX 收盘（必须精确对应，不回退旧值）。"""
        d = datetime.fromtimestamp(cutoff_utc_ns / 1e9, tz=NEW_YORK).date()
        for _ in range(3):
            c = self.cal.last_session_on_or_before(INDEX_MARKET, d)
            if c is None:
                return None, None, None, None
            session = self.cal.session(INDEX_MARKET, c)
            if session is not None and session.close_utc_ns <= cutoff_utc_ns:
                idx = self._unambiguous(self.index_closes.get(c))
                return c, session.close_utc_ns, (idx[0] if idx else None), (idx[1] if idx else None)
            d = c - timedelta(days=1)
        return None, None, None, None
    @staticmethod
    def _parse_etf(msg: RawMessage) -> dict[str, _Quote]:
        rows: dict[str, dict] = {}
        for rec in sina_a_share_v1.parse(msg).records:
            row = rows.setdefault(rec.symbol, {})
            if isinstance(rec, MarketQuote) and rec.price_type is PriceType.LAST:
                row["last"], row["t"] = rec.value, rec.provider_time.utc_ns
            elif isinstance(rec, QuoteSide) and rec.side is Side.ASK:
                valid = rec.state is SideState.VALID
                row["ask"], row["ask_volume"] = (rec.price, rec.volume) if valid else (None, None)
        return {
            symbol: _Quote(r["t"], msg.received_utc_ns, msg.msg_id, r.get("last"), r.get("ask"), r.get("ask_volume"))
            for symbol, r in rows.items() if r.get("t") is not None  # 无法判断时间的行情不进入比较
        }

    # ---------- 净值选择（QS-05 首版三态：自动普通校验 / 待复核隔离） ----------

    def _nav_for(self, code: str) -> tuple[_NavValue | None, date | None, bool, tuple[str, ...]]:
        by_date = self.navs.get(code)
        if not by_date:
            return None, None, False, ()
        dates = sorted(by_date)
        d = dates[-1]
        values = by_date[d]
        latest = values[-1]
        reasons: list[str] = []
        if len({v.unit_nav for v in values}) > 1 or len({v.growth_pct for v in values if v.growth_pct is not None}) > 1:
            reasons.append(ReasonCode.PENDING_VERIFY.value)  # 同日数值或增长率被修订（AT46）
        if any(v.event_fields for v in values):
            reasons.append(ReasonCode.CORPORATE_ACTION_PENDING.value)  # 后补事件字段同样隔离（评审 R2）
        if not reasons and len(dates) >= 2 and latest.growth_pct is not None:
            prev = by_date[dates[-2]][-1].unit_nav
            implied = float(latest.unit_nav) / float(prev) - 1
            if abs(implied - float(latest.growth_pct) / 100) > GROWTH_TOLERANCE:
                reasons.append(ReasonCode.PENDING_VERIFY.value)
        return latest, d, not reasons, tuple(dict.fromkeys(reasons))

    @staticmethod
    def _unambiguous(values: dict[Decimal, str] | None) -> tuple[Decimal, str] | None:
        if not values or len(values) != 1:
            return None  # 缺失或同日多个不同数值：不提供因子
        return next(iter(values.items()))

    def _anchor_factors(self, nav_date: date) -> tuple[_Factors | None, str | None]:
        """L0_FX_T：I(a) = 净值日应有的 NDX 收盘，X0 = 净值日当日中间价。返回 (因子, 缺失说明)。

        二审 F1：先由美股日历求“不晚于净值日的最近交易日”a，再精确取 a 日收盘。
        真实休市（如 9 月劳工节）回退到前一交易日；a 本身是交易日但数据未到/缺行时不回退到旧值，
        该成员不提供跨日因子（随后按同日期子集规则处理）。
        """
        a = self.cal.last_session_on_or_before(INDEX_MARKET, nav_date)
        if a is None:
            return None, f"index_date_uncertain:{nav_date.isoformat()}"
        idx = self._unambiguous(self.index_closes.get(a))
        if idx is None:
            return None, f"index_close_missing:{a.isoformat()}"
        fx = self._unambiguous(self.fixings.get(nav_date))
        if fx is None:
            return None, f"fixing_missing:{nav_date.isoformat()}"
        return _Factors(idx[0], a, fx[0], nav_date, (idx[1], fx[1])), None

    # ---------- 输入包 ----------

    def bundle(self, cutoff_utc_ns: int, basis: str | None = None, versions: dict[str, str] | None = None
               ) -> RelativeBundle:
        phase, _, covered = self.cal.phase("SSE", cutoff_utc_ns)
        mode = RelativeMode.CURRENT if phase is MarketPhase.CONTINUOUS else RelativeMode.CLOSING_REFERENCE
        notes: list[str] = [f"cutoff_phase={phase.value}"]
        quotes = self.latest if mode is RelativeMode.CURRENT else self.reference
        basis = basis or ("ASK" if mode is RelativeMode.CURRENT else "LAST")

        today = datetime.fromtimestamp(cutoff_utc_ns / 1e9, tz=SHANGHAI).date()
        # 评审 R6：日历未覆盖截止日时整组以 CALENDAR_UNCERTAIN 降级；成员锚点覆盖按成员判断（二审 F5）
        if not covered:
            notes.append(ReasonCode.CALENDAR_UNCERTAIN.value)
        # 估值时点 = 所用行情的最新报价时刻（收盘参考在夜间查看时仍是 15:00 快照，不能用之后才发生的美股收盘）
        valuation_ns = max((q.t for q in (quotes.get(f.symbol) for f in self.funds) if q), default=cutoff_utc_ns)
        us_close_date, us_close_ns, us_close_index, us_close_msg = self._us_close(min(valuation_ns, cutoff_utc_ns))
        if us_close_date is not None and us_close_index is None:
            notes.append(f"enav:index_close_missing:{us_close_date.isoformat()}")
        members: list[MemberSpec] = []
        for f in self.funds:
            q = quotes.get(f.symbol)
            last_q = self.latest.get(f.symbol) if mode is RelativeMode.CURRENT else q
            price = (q.ask if basis == "ASK" else q.last) if q else None
            volume = q.ask_volume if (q and basis == "ASK") else None
            t = q.t if q else None
            if t is None:
                phase_ok = False
            elif mode is RelativeMode.CURRENT:
                phase_ok = self.cal.phase("SSE", t)[0] is MarketPhase.CONTINUOUS
            else:
                phase_ok = is_reference_snapshot(self.cal, t)
            nav, nav_date, verified, reasons = self._nav_for(f.code)
            factors: _Factors | None = None
            anchor_covered, sessions = True, None
            if nav_date:
                factors, missing = self._anchor_factors(nav_date)
                if missing:
                    notes.append(f"{f.code}:{missing}")
                # 二审 F5：锚点交易日数与日历覆盖按成员计算，被剔除成员的旧锚点不影响其他成员
                anchor_covered = self.cal.covers("SSE", nav_date)
                if covered and anchor_covered:
                    sessions = len(self.cal.sessions_between("SSE", nav_date, today))
            fut, fx = self._as_of(self.futures, t), self._as_of(self.fx_spot, t)
            if f.nav_fx_rule_status != "VERIFIED":
                reasons = (*reasons, ReasonCode.NAV_FX_RULE_UNKNOWN.value)
            if f.factor_group != self.factor_group:  # AT69：不同因子组不混排
                reasons = (*reasons, ReasonCode.FACTOR_GROUP_MISMATCH.value)
            members.append(MemberSpec(
                code=f.code,
                price=str(price) if price is not None else None,
                volume=str(volume) if volume is not None else None,
                quote_time_utc_ns=t,
                phase_ok=phase_ok,
                nav=str(nav.unit_nav) if nav else None,
                nav_date=nav_date.isoformat() if nav_date else None,
                nav_verified=verified,
                nav_reasons=reasons,
                snapshot_msg_id=q.msg_id if q else None,
                nav_msg_id=nav.msg_id if nav else None,
                last_price=str(last_q.last) if last_q and last_q.last is not None else None,
                index_at_anchor=str(factors.index) if factors else None,
                fx_at_anchor=str(factors.fx) if factors else None,
                anchor_factor_msg_ids=factors.msg_ids if factors else (),
                index_date=factors.index_date.isoformat() if factors else None,
                fx_date=factors.fx_date.isoformat() if factors else None,
                anchor_sessions=sessions,
                anchor_calendar_covered=anchor_covered,
                futures_mid=str(fut.mid) if fut else None,
                futures_settle=str(fut.ref) if fut else None,
                futures_time_utc_ns=fut.t if fut else None,
                futures_msg_id=fut.msg_id if fut else None,
                fx_spot=str(fx.mid) if fx else None,
                fx_time_utc_ns=fx.t if fx else None,
                fx_msg_id=fx.msg_id if fx else None,
            ))

        # 边界只存单日 P95；按成员对 k = max(k_i, k_j) 放大在纯函数中完成（二审 F5）
        bounds: list[tuple[str, str, float, float, str]] = []
        if self.udiff and covered:
            source = f"{self.udiff['run_id']} 日终差分P95×√k，k=两成员锚点后交易日数较大者（历史情景，未经盘中实测）"
            for key, v in sorted(self.udiff["pairs"].items()):
                i, j = sorted(key.split("|"))
                bounds.append((i, j, v["p95_bp"], NAV_ROUNDING_BP, source))

        all_versions = {
            "calendar": self.cal.version,
            "tzdb": self.cal.tzdb_version,
            "contracts": ",".join(CONTRACTS),
            "udiff": self.udiff["run_id"] if self.udiff else "none",
            "nav_fx_rules": ",".join(f"{f.code}:{f.nav_fx_rule}:{f.nav_fx_rule_status}" for f in self.funds),
            "factor_group": self.factor_group,
            **(versions or {}),
        }
        return RelativeBundle(
            schema=SCHEMA_VERSION, cutoff_utc_ns=cutoff_utc_ns, mode=mode.value, price_basis=basis,
            policy=tuple(sorted(asdict(self.policy).items())), calendar_covered=covered,
            model_status="HISTORICAL_VALIDATED",
            members=tuple(members), bounds=tuple(bounds), versions=tuple(sorted(all_versions.items())),
            notes=tuple(notes),
            us_close_date=us_close_date.isoformat() if us_close_date else None,
            us_close_utc_ns=us_close_ns,
            us_close_index=str(us_close_index) if us_close_index is not None else None,
            us_close_msg_id=us_close_msg,
            enav_policy=tuple(sorted(asdict(self.enav_policy).items())),
        )


def load_latest_udiff(repo: Path) -> dict | None:
    import json

    files = sorted((repo / "reports" / "phase0" / "history").glob("RESEARCH-*/pair_udiff.json"))
    return json.loads(files[-1].read_text(encoding="utf-8")) if files else None
