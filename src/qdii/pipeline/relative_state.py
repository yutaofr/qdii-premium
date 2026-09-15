"""相对比较的增量输入状态（ADR-009：在线与回放共用同一路径）。

在线：采集器每收到一条消息就 ingest；ETF 批次到达时以该消息 received_at 为知识截止生成输入包（ADR-010）。
回放：按 (received_at, msg_id) 顺序把原始日志逐条 ingest，得到与在线相同的输入包。
"""

from __future__ import annotations

import math
import tomllib
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from qdii.contracts import chinamoney_ccpr_his_v1, eastmoney_lsjz_v1, index_history_v1, sina_a_share_v1
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
REFERENCE_WINDOW_S = 600  # 阶段边界后冻结快照的有效期
NAV_ROUNDING_BP = 0.5
GROWTH_TOLERANCE = 0.0002
ETF_ENDPOINT = "sina.etf_batch"
NAV_ENDPOINT_PREFIX = "eastmoney.lsjz."
INDEX_ENDPOINT = "nasdaq.ndx_history"  # 跨日归一化因子 I(a)（评审 R4）
FIXING_ENDPOINT = "chinamoney.ccpr"  # 跨日归一化因子 X0（L0_FX_T）
FACTOR_ENDPOINTS = frozenset({INDEX_ENDPOINT, FIXING_ENDPOINT})
CONTRACTS = (sina_a_share_v1.CONTRACT_VERSION, eastmoney_lsjz_v1.CONTRACT_VERSION,
             index_history_v1.NASDAQ_VERSION, chinamoney_ccpr_his_v1.CONTRACT_VERSION)


def is_relative_input(endpoint_id: str) -> bool:
    return endpoint_id in (ETF_ENDPOINT, *FACTOR_ENDPOINTS) or endpoint_id.startswith(NAV_ENDPOINT_PREFIX)


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
    navs: dict[str, dict[date, list[_NavValue]]] = field(default_factory=dict)
    index_closes: dict[date, dict[Decimal, str]] = field(default_factory=dict)  # 日期 → {数值: msg_id}
    fixings: dict[date, dict[Decimal, str]] = field(default_factory=dict)
    last_received_utc_ns: int = 0

    # ---------- ingest ----------

    def ingest(self, msg: RawMessage) -> bool:
        """返回是否被相对比较使用。原始消息全部保留在原始日志中；这里只维护当前有效输入。"""
        if msg.status != 200:
            return False
        self.last_received_utc_ns = max(self.last_received_utc_ns, msg.received_utc_ns)
        if msg.endpoint_id == ETF_ENDPOINT:
            for symbol, quote in self._parse_etf(msg).items():
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
        return False

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

    def _anchor_factors(self, nav_date: date) -> tuple[Decimal | None, Decimal | None, tuple[str, ...]]:
        """L0_FX_T：I(a) = 不晚于净值日的最近 NDX 收盘；X0 = 净值日当日中间价。"""
        idx_dates = [d for d in self.index_closes if d <= nav_date]
        idx = self._unambiguous(self.index_closes[max(idx_dates)]) if idx_dates else None
        fx = self._unambiguous(self.fixings.get(nav_date))
        if idx is None or fx is None:
            return None, None, ()
        return idx[0], fx[0], (idx[1], fx[1])

    # ---------- 输入包 ----------

    def bundle(self, cutoff_utc_ns: int, basis: str | None = None, versions: dict[str, str] | None = None
               ) -> RelativeBundle:
        phase, _, covered = self.cal.phase("SSE", cutoff_utc_ns)
        mode = RelativeMode.CURRENT if phase is MarketPhase.CONTINUOUS else RelativeMode.CLOSING_REFERENCE
        notes: list[str] = [f"cutoff_phase={phase.value}"]
        quotes = self.latest if mode is RelativeMode.CURRENT else self.reference
        basis = basis or ("ASK" if mode is RelativeMode.CURRENT else "LAST")

        members: list[MemberSpec] = []
        nav_dates: list[date] = []
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
            idx = fx = None
            factor_ids: tuple[str, ...] = ()
            if nav_date:
                nav_dates.append(nav_date)
                idx, fx, factor_ids = self._anchor_factors(nav_date)
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
                index_at_anchor=str(idx) if idx is not None else None,
                fx_at_anchor=str(fx) if fx is not None else None,
                anchor_factor_msg_ids=factor_ids,
            ))

        today = datetime.fromtimestamp(cutoff_utc_ns / 1e9, tz=SHANGHAI).date()
        # 评审 R6：日历未覆盖截止日或锚点日时不查询交易日区间，结果以 CALENDAR_UNCERTAIN 降级
        covered = covered and all(self.cal.covers("SSE", d) for d in nav_dates)
        if not covered:
            notes.append(ReasonCode.CALENDAR_UNCERTAIN.value)
        sessions = None
        bounds: list[tuple[str, str, float, float, str]] = []
        if nav_dates and covered:
            sessions = len(self.cal.sessions_between("SSE", min(nav_dates), today))
            if self.udiff:
                k = max(1, sessions)
                for key, v in sorted(self.udiff["pairs"].items()):
                    i, j = sorted(key.split("|"))
                    bounds.append((i, j, v["p95_bp"] * math.sqrt(k), NAV_ROUNDING_BP,
                                   f"{self.udiff['run_id']} 日终差分P95×√{k}（历史情景，未经盘中实测）"))

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
            sessions_since_anchor=sessions, model_status="HISTORICAL_VALIDATED",
            members=tuple(members), bounds=tuple(bounds), versions=tuple(sorted(all_versions.items())),
            notes=tuple(notes),
        )


def load_latest_udiff(repo: Path) -> dict | None:
    import json

    files = sorted((repo / "reports" / "phase0" / "history").glob("RESEARCH-*/pair_udiff.json"))
    return json.loads(files[-1].read_text(encoding="utf-8")) if files else None
