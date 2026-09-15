"""相对比较快照（VM-10 + 勘误 E1）：在原始日志上按指定知识截止时刻回放。

只使用 received_at ≤ cutoff 的记录（DS-11 as-of）。市场阶段来自交易日历，不用星期几推断。
- cutoff 处于连续交易：CURRENT，使用最新一批新浪快照，默认按卖一价（买入比较）。
- 其余阶段：CLOSING_REFERENCE。收盘后若有当日收盘后 10 分钟内的快照，用其最新价（收盘价）；
  否则用最后一个连续交易快照。结果不代表当前可交易。
"""

from __future__ import annotations

import itertools
import json
import math
import tomllib
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from qdii.contracts import eastmoney_lsjz_v1, sina_a_share_v1
from qdii.core.relative import GroupResult, MemberInput, PairBound, RelativeMode, evaluate_relative
from qdii.core.types import MarketQuote, NavObservation, PriceType, QuoteSide, RawMessage, Side, SideState
from qdii.io import rawlog
from qdii.io.calendars import CalendarProvider, MarketPhase

SHANGHAI = ZoneInfo("Asia/Shanghai")
CLOSE_SNAPSHOT_WINDOW_S = 600
NAV_ROUNDING_BP = 0.5  # 两只基金各 4 位小数净值约 0.25bp


@dataclass(frozen=True)
class Fund:
    exchange: str
    code: str
    name: str
    nav_fx_rule_status: str

    @property
    def symbol(self) -> str:
        return f"{self.exchange}:{self.code}"


def load_funds(path: Path) -> list[Fund]:
    doc = tomllib.loads(path.read_text(encoding="utf-8"))
    return [Fund(f["exchange"], f["code"], f["name"], f["nav_fx_rule_status"]) for f in doc["fund"]]


def is_reference_snapshot(cal: CalendarProvider, provider_ns: int) -> bool:
    """收盘参考可用的快照：连续交易中，或午休起点/收盘后 10 分钟内的冻结快照（勘误 E1）。

    实测：新浪上午最后快照时间戳恰为 11:30:00，收盘后为 15:00:0x；二者分别是上午与全天的最终状态。
    """
    phase, session, _ = cal.phase("SSE", provider_ns)
    if phase is MarketPhase.CONTINUOUS:
        return True
    if session is None:
        return False
    window = CLOSE_SNAPSHOT_WINDOW_S * 1e9
    if phase is MarketPhase.BREAK and session.break_start_utc_ns is not None:
        return 0 <= provider_ns - session.break_start_utc_ns <= window
    if phase is MarketPhase.CLOSED:
        return 0 <= provider_ns - session.close_utc_ns <= window
    return False


def _utc_dates(cutoff_ns: int, days: int) -> list[str]:
    end = datetime.fromtimestamp(cutoff_ns / 1e9, tz=UTC).date()
    return [(end - timedelta(days=i)).isoformat() for i in range(days, -1, -1)]


def _latest_nav(msgs: list[RawMessage], code: str) -> tuple[NavObservation | None, bool, list[str]]:
    """最新净值 + 自动校验（QS-05 普通校验：正值、无事件字段、与前一日增长率一致）。"""
    rows: dict[date, NavObservation] = {}
    for m in msgs:
        for rec in eastmoney_lsjz_v1.parse(m).records:
            if isinstance(rec, NavObservation) and rec.code == code:
                rows[rec.nav_date] = rec
    if not rows:
        return None, False, ["无净值记录"]
    ordered = [rows[d] for d in sorted(rows)]
    last = ordered[-1]
    notes = []
    ok = last.unit_nav is not None and not last.event_fields
    if last.event_fields:
        notes.append(f"事件字段 {last.event_fields}")
    if ok and len(ordered) >= 2 and last.growth_pct is not None and ordered[-2].unit_nav:
        implied = float(last.unit_nav) / float(ordered[-2].unit_nav) - 1  # type: ignore[arg-type]
        if abs(implied - float(last.growth_pct) / 100) > 0.0002:
            ok = False
            notes.append("日增长率与净值比不一致")
    return last, ok, notes


def build(
    data_root: Path,
    repo: Path,
    cutoff_utc_ns: int,
    basis: str | None = None,
) -> tuple[GroupResult, dict]:
    cal = CalendarProvider(repo / "config" / "calendar_overrides.toml")
    funds = load_funds(repo / "config" / "funds.toml")
    phase, _, covered = cal.phase("SSE", cutoff_utc_ns)
    mode = RelativeMode.CURRENT if phase is MarketPhase.CONTINUOUS else RelativeMode.CLOSING_REFERENCE
    context: dict = {"cutoff_phase": phase.value, "calendar": cal.version, "tzdb": cal.tzdb_version, "notes": []}
    if not covered:
        context["notes"].append("CALENDAR_UNCERTAIN：日历未覆盖该日期")

    etf_msgs, nav_msgs = [], []
    for msg in rawlog.iter_messages(data_root, dates=_utc_dates(cutoff_utc_ns, 10), end_utc_ns=cutoff_utc_ns + 1):
        if msg.status != 200:
            continue
        if msg.endpoint_id == "sina.etf_batch":
            etf_msgs.append(msg)
        elif msg.endpoint_id.startswith("eastmoney.lsjz."):
            nav_msgs.append(msg)

    # 选快照
    chosen: RawMessage | None = None
    quotes: dict[str, dict] = {}

    def parse_snapshot(m: RawMessage) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for rec in sina_a_share_v1.parse(m).records:
            row = out.setdefault(rec.symbol, {})
            if isinstance(rec, MarketQuote) and rec.price_type is PriceType.LAST:
                row["last"], row["t"] = rec.value, rec.provider_time.utc_ns
            elif isinstance(rec, QuoteSide) and rec.side is Side.ASK:
                row["ask"] = rec.price if rec.state is SideState.VALID else None
        return out

    if mode is RelativeMode.CURRENT:
        if etf_msgs:
            chosen = etf_msgs[-1]
            quotes = parse_snapshot(chosen)
        basis = basis or "ASK"
        allowed = {MarketPhase.CONTINUOUS}
    else:
        basis = basis or "LAST"
        allowed = {MarketPhase.CONTINUOUS, MarketPhase.BREAK, MarketPhase.CLOSED}
        for m in reversed(etf_msgs):
            rows = parse_snapshot(m)
            ts = max((r["t"] for r in rows.values() if r.get("t")), default=None)
            if ts is not None and is_reference_snapshot(cal, ts):
                chosen, quotes = m, rows
                break
    context["snapshot_msg_id"] = chosen.msg_id if chosen else None
    context["price_basis"] = basis

    # 锚点后的 A 股交易日数，用于放大日终差分边界
    today = datetime.fromtimestamp(cutoff_utc_ns / 1e9, tz=SHANGHAI).date()
    members, nav_dates = [], []
    for f in funds:
        nav, verified, notes = _latest_nav([m for m in nav_msgs if m.endpoint_id.endswith(f.code)], f.code)
        if notes:
            context["notes"].append(f"{f.code}: {'; '.join(notes)}")
        row = quotes.get(f.symbol, {})
        price = row.get("ask") if basis == "ASK" else row.get("last")
        t = row.get("t")
        phase_ok = t is not None and (
            cal.phase("SSE", t)[0] in allowed if mode is RelativeMode.CURRENT else is_reference_snapshot(cal, t))
        if nav is not None:
            nav_dates.append(nav.nav_date)
        members.append(MemberInput(
            code=f.code, price=price, quote_time_utc_ns=t, phase_ok=phase_ok,
            nav=nav.unit_nav if nav else None, nav_date=nav.nav_date if nav else None, nav_verified=verified,
        ))
    context["provider_time_semantics"] = "PROVIDER_SNAPSHOT_UNVERIFIED"

    bounds = {}
    udiff_files = sorted((repo / "reports" / "phase0" / "history").glob("RESEARCH-*/pair_udiff.json"))
    if udiff_files and nav_dates:
        doc = json.loads(udiff_files[-1].read_text(encoding="utf-8"))
        anchor = min(nav_dates)
        k = max(1, len(cal.sessions_between("SSE", anchor, today)))
        for key, v in doc["pairs"].items():
            a, b = key.split("|")
            u_bp = v["p95_bp"] * math.sqrt(k) + NAV_ROUNDING_BP
            bounds[frozenset((a, b))] = PairBound(u_bp / 1e4, f"{doc['run_id']} P95×√{k}+{NAV_ROUNDING_BP}bp")
        context["udiff_source"] = str(udiff_files[-1].relative_to(repo))
        context["sessions_since_anchor"] = k

    group = evaluate_relative(members, mode=mode, price_basis=basis, cutoff_utc_ns=cutoff_utc_ns, bounds=bounds)
    context["names"] = {f.code: f.name for f in funds}
    context["nav_fx_rule_status"] = {f.code: f.nav_fx_rule_status for f in funds}
    return group, context


def _bj(ns: int | None) -> str:
    return "—" if ns is None else datetime.fromtimestamp(ns / 1e9, tz=SHANGHAI).strftime("%m-%d %H:%M:%S")


def render(group: GroupResult, ctx: dict) -> str:
    mode_cn = "当前（连续交易）" if group.mode is RelativeMode.CURRENT else "收盘后参考（不代表当前可交易）"
    basis_cn = {"ASK": "卖一价（买入比较）", "LAST": "最新价/收盘价"}[group.price_basis]
    lines = [
        f"相对比较 · {mode_cn} · 价格口径 {basis_cn}",
        (f"知识截止 {_bj(group.cutoff_utc_ns)} 北京（阶段 {ctx['cutoff_phase']}）；快照 τ {_bj(group.tau_utc_ns)}；"
         f"状态 {group.status.value}；锚点{'相同' if group.common_anchor else '不同'}"),
        "",
        f"{'排名':<4}{'代码':<8}{'名称':<20}{'价格':>8}{'单位净值':>10}{'净值日':>12}{'官方净值溢价':>12}{'相对最便宜':>12}",
    ]
    best = next((m.s for m in group.members if m.rank == 1), None)
    for m in group.members:
        name = ctx["names"].get(m.code, "")
        rel = "—" if m.s is None or best is None else f"{(m.s / best - 1) * 100:+.2f}%"
        prem = "—" if m.nav_premium is None else f"{m.nav_premium * 100:+.2f}%"
        lines.append(
            f"{(m.rank or '—')!s:<4}{m.code:<8}{name:<20}{m.price or '—'!s:>8}{m.nav or '—'!s:>10}"
            f"{m.nav_date or '—'!s:>12}{prem:>12}{rel:>12}"
            + ("" if m.eligible else f"  退出：{','.join(r.value for r in m.reasons)}")
        )
    ranked = [m.code for m in group.members if m.rank]
    if len(ranked) >= 2:
        lines += ["", "相邻排名成对判断（δ = S_i/S_j − 1；边界为日终历史差分 P95 放大后的情景值）："]
        for i, j in itertools.pairwise(ranked):
            p = next(p for p in group.pairs if {p.i, p.j} == {i, j})
            sign = 1 if p.i == i else -1
            u = "—" if p.u_diff is None else f"{p.u_diff * 1e4:.1f}bp"
            lines.append(f"  {i} vs {j}: δ={sign * p.delta * 100:+.3f}%  |lnR|={abs(p.ln_ratio) * 1e4:.1f}bp  "
                         f"边界 {u}  → {p.status.value}")
    lines += [
        "",
        (f"模型：M0 满仓假设（{','.join(r.value for r in group.reasons)}）；净值汇率规则状态："
         f"{sorted(set(ctx['nav_fx_rule_status'].values()))}；供应商时间语义 {ctx['provider_time_semantics']}。"),
        f"差分边界来源：{ctx.get('udiff_source', '无')}（锚点后 A 股交易日 {ctx.get('sessions_since_anchor', '—')}）。",
        f"日历 {ctx['calendar']}；{ctx['tzdb']}。机会提醒：{'允许' if group.opportunity_alert_allowed else '关闭'}。",
    ]
    lines += [f"注意：{n}" for n in ctx["notes"]]
    return "\n".join(lines)


def to_json(group: GroupResult, ctx: dict) -> str:
    def default(o: object) -> object:
        if isinstance(o, Decimal | date):
            return str(o)
        if isinstance(o, frozenset):
            return sorted(o)
        return str(o)

    return json.dumps({"group": asdict(group), "context": ctx}, ensure_ascii=False, indent=2, default=default)
