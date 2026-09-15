"""相对比较的增量输入状态（ADR-009：在线与回放共用同一路径）。

在线：采集器每收到一条消息就 ingest；ETF 批次到达时以该消息 received_at 为知识截止生成输入包（ADR-010）。
回放：按 (received_at, msg_id) 顺序把原始日志逐条 ingest，得到与在线相同的输入包。
"""

from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from qdii.contracts import eastmoney_lsjz_v1, sina_a_share_v1
from qdii.core.relative import RelativeMode, RelativePolicy
from qdii.core.relative_bundle import SCHEMA_VERSION, MemberSpec, RelativeBundle
from qdii.core.types import (
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


@dataclass
class _Snapshot:
    msg_id: str
    received_utc_ns: int
    rows: dict[str, dict]
    provider_max_ns: int | None


@dataclass
class _NavValue:
    unit_nav: Decimal
    growth_pct: Decimal | None
    event_fields: tuple[tuple[str, str], ...]
    msg_id: str


@dataclass
class RelativeState:
    cal: CalendarProvider
    funds: list[Fund]
    udiff: dict | None = None
    policy: RelativePolicy = field(default_factory=RelativePolicy)
    factor_group: str = "NDX_USD_UNHEDGED"  # VM-10：只在同一因子组内比较
    latest: _Snapshot | None = None
    reference: _Snapshot | None = None
    navs: dict[str, dict[date, list[_NavValue]]] = field(default_factory=dict)
    last_received_utc_ns: int = 0

    # ---------- ingest ----------

    def ingest(self, msg: RawMessage) -> bool:
        """返回是否被相对比较使用。乱序（早于已处理时刻）的消息保留审计但不倒退当前快照（QS-02）。"""
        if msg.status != 200:
            return False
        if msg.endpoint_id == ETF_ENDPOINT:
            snap = self._parse_etf(msg)
            if msg.received_utc_ns >= self.last_received_utc_ns:
                self.latest = snap
                if snap.provider_max_ns is not None and is_reference_snapshot(self.cal, snap.provider_max_ns):
                    self.reference = snap
                self.last_received_utc_ns = msg.received_utc_ns
            return True
        if msg.endpoint_id.startswith(NAV_ENDPOINT_PREFIX):
            for rec in eastmoney_lsjz_v1.parse(msg).records:
                if isinstance(rec, NavObservation) and rec.unit_nav is not None:
                    values = self.navs.setdefault(rec.code, {}).setdefault(rec.nav_date, [])
                    if all(v.unit_nav != rec.unit_nav for v in values):  # 重复报文不新增修订（AT50）
                        values.append(_NavValue(rec.unit_nav, rec.growth_pct, rec.event_fields, msg.msg_id))
            return True
        return False

    @staticmethod
    def _parse_etf(msg: RawMessage) -> _Snapshot:
        rows: dict[str, dict] = {}
        for rec in sina_a_share_v1.parse(msg).records:
            row = rows.setdefault(rec.symbol, {})
            if isinstance(rec, MarketQuote) and rec.price_type is PriceType.LAST:
                row["last"], row["t"] = rec.value, rec.provider_time.utc_ns
            elif isinstance(rec, QuoteSide) and rec.side is Side.ASK:
                valid = rec.state is SideState.VALID
                row["ask"], row["ask_volume"] = (rec.price, rec.volume) if valid else (None, None)
        ts = [r["t"] for r in rows.values() if r.get("t")]
        return _Snapshot(msg.msg_id, msg.received_utc_ns, rows, max(ts) if ts else None)

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
        verified = True
        if len(values) > 1:  # 同一净值日出现不同数值：修订，隔离直至复核（AT46）
            verified = False
            reasons.append(ReasonCode.PENDING_VERIFY.value)
        if latest.event_fields:
            verified = False
            reasons.append(ReasonCode.CORPORATE_ACTION_PENDING.value)
        if verified and len(dates) >= 2 and latest.growth_pct is not None:
            prev = by_date[dates[-2]][-1].unit_nav
            implied = float(latest.unit_nav) / float(prev) - 1
            if abs(implied - float(latest.growth_pct) / 100) > GROWTH_TOLERANCE:
                verified = False
                reasons.append(ReasonCode.PENDING_VERIFY.value)
        return latest, d, verified, tuple(dict.fromkeys(reasons))

    # ---------- 输入包 ----------

    def bundle(self, cutoff_utc_ns: int, basis: str | None = None, versions: dict[str, str] | None = None
               ) -> RelativeBundle:
        phase, _, covered = self.cal.phase("SSE", cutoff_utc_ns)
        mode = RelativeMode.CURRENT if phase is MarketPhase.CONTINUOUS else RelativeMode.CLOSING_REFERENCE
        notes: list[str] = [f"cutoff_phase={phase.value}"]
        if not covered:
            notes.append(ReasonCode.CALENDAR_UNCERTAIN.value)
        if mode is RelativeMode.CURRENT:
            snap, basis = self.latest, basis or "ASK"
        else:
            snap, basis = self.reference, basis or "LAST"

        members: list[MemberSpec] = []
        nav_dates: list[date] = []
        for f in self.funds:
            row = (snap.rows.get(f.symbol, {}) if snap else {})
            price = row.get("ask") if basis == "ASK" else row.get("last")
            volume = row.get("ask_volume") if basis == "ASK" else None
            t = row.get("t")
            if t is None:
                phase_ok = False
            elif mode is RelativeMode.CURRENT:
                phase_ok = self.cal.phase("SSE", t)[0] is MarketPhase.CONTINUOUS
            else:
                phase_ok = is_reference_snapshot(self.cal, t)
            nav, nav_date, verified, reasons = self._nav_for(f.code)
            if nav_date:
                nav_dates.append(nav_date)
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
                snapshot_msg_id=snap.msg_id if snap else None,
                nav_msg_id=nav.msg_id if nav else None,
            ))

        sessions = None
        bounds: list[tuple[str, str, float, str]] = []
        if nav_dates:
            today = datetime.fromtimestamp(cutoff_utc_ns / 1e9, tz=SHANGHAI).date()
            sessions = len(self.cal.sessions_between("SSE", min(nav_dates), today))
            if self.udiff:
                k = max(1, sessions)
                for key, v in sorted(self.udiff["pairs"].items()):
                    i, j = sorted(key.split("|"))
                    u_bp = v["p95_bp"] * math.sqrt(k) + NAV_ROUNDING_BP
                    bounds.append((i, j, u_bp / 1e4, f"{self.udiff['run_id']} P95×√{k}+{NAV_ROUNDING_BP}bp"))

        all_versions = {
            "calendar": self.cal.version,
            "tzdb": self.cal.tzdb_version,
            "contracts": f"{sina_a_share_v1.CONTRACT_VERSION},{eastmoney_lsjz_v1.CONTRACT_VERSION}",
            "udiff": self.udiff["run_id"] if self.udiff else "none",
            "nav_fx_rules": ",".join(f"{f.code}:{f.nav_fx_rule}:{f.nav_fx_rule_status}" for f in self.funds),
            "factor_group": self.factor_group,
            **(versions or {}),
        }
        return RelativeBundle(
            schema=SCHEMA_VERSION, cutoff_utc_ns=cutoff_utc_ns, mode=mode.value, price_basis=basis,
            policy_version=self.policy.version, max_age_s=self.policy.max_age_s, max_span_s=self.policy.max_span_s,
            sessions_since_anchor=sessions, model_status="HISTORICAL_VALIDATED",
            members=tuple(members), bounds=tuple(bounds), versions=tuple(sorted(all_versions.items())),
            notes=tuple(notes),
        )


def load_latest_udiff(repo: Path) -> dict | None:
    import json

    files = sorted((repo / "reports" / "phase0" / "history").glob("RESEARCH-*/pair_udiff.json"))
    return json.loads(files[-1].read_text(encoding="utf-8")) if files else None
