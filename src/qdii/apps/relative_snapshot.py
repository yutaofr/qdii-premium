"""相对比较快照（VM-10 + 勘误 E1）：命令行回放、首屏视图与文本渲染。

回放与在线共用 pipeline.relative_state.RelativeState 和 core.relative_bundle.evaluate_bundle（ADR-009）：
- CURRENT（连续交易）：最新一批新浪快照，默认卖一价（买入比较）。
- CLOSING_REFERENCE（其他阶段）：午休起点/收盘后 10 分钟内的冻结快照或最后连续交易快照，按最新价/收盘价；
  不代表当前可交易。
"""

from __future__ import annotations

import itertools
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from qdii.core.relative import RelativeMode
from qdii.core.relative_bundle import (
    RelativeBundle,
    RelativeSnapshot,
    canonical_json,
    evaluate_bundle,
    snapshot_to_dict,
)
from qdii.io import rawlog
from qdii.io.calendars import CalendarProvider
from qdii.pipeline.relative_state import (
    ETF_ENDPOINT,
    NAV_ENDPOINT_PREFIX,
    RelativeState,
    load_funds,
    load_latest_udiff,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
LOOKBACK_DAYS = 10


def new_state(repo: Path) -> RelativeState:
    return RelativeState(
        cal=CalendarProvider(repo / "config" / "calendar_overrides.toml"),
        funds=load_funds(repo / "config" / "funds.toml"),
        udiff=load_latest_udiff(repo),
    )


def relevant(endpoint_id: str) -> bool:
    return endpoint_id == ETF_ENDPOINT or endpoint_id.startswith(NAV_ENDPOINT_PREFIX)


def utc_dates(cutoff_ns: int, days: int = LOOKBACK_DAYS) -> list[str]:
    end = datetime.fromtimestamp(cutoff_ns / 1e9, tz=UTC).date()
    return [(end - timedelta(days=i)).isoformat() for i in range(days, -1, -1)]


def replay_into(state: RelativeState, data_root: Path, cutoff_utc_ns: int) -> int:
    """把 received_at ≤ cutoff 的相关原始消息按序喂给状态（as-of）。返回喂入条数。"""
    n = 0
    for msg in rawlog.iter_messages(data_root, dates=utc_dates(cutoff_utc_ns), end_utc_ns=cutoff_utc_ns + 1):
        if relevant(msg.endpoint_id) and state.ingest(msg):
            n += 1
    return n


def build(data_root: Path, repo: Path, cutoff_utc_ns: int, basis: str | None = None
          ) -> tuple[RelativeSnapshot, RelativeBundle, dict[str, str]]:
    state = new_state(repo)
    replay_into(state, data_root, cutoff_utc_ns)
    bundle = state.bundle(cutoff_utc_ns, basis)
    return evaluate_bundle(bundle), bundle, {f.code: f.name for f in state.funds}


# ---------- 视图 ----------

def _bj(ns: int | None) -> str:
    return "—" if ns is None else datetime.fromtimestamp(ns / 1e9, tz=SHANGHAI).strftime("%m-%d %H:%M:%S")


def view(snap: RelativeSnapshot, bundle: RelativeBundle, names: dict[str, str]) -> dict[str, Any]:
    """首屏视图模型（FR02/FR03/FR12）：人读字段 + 可展开的完整输入与原因。"""
    g, q = snap.result, snap.quality
    specs = {m.code: m for m in bundle.members}
    best = next((m.s for m in g.members if m.rank == 1), None)
    ranked = [m.code for m in g.members if m.rank]
    pair_to_next = {}
    for i, j in itertools.pairwise(ranked):
        p = next(p for p in g.pairs if {p.i, p.j} == {i, j})
        sign = 1 if p.i == i else -1
        pair_to_next[i] = {"next": j, "delta": sign * p.delta, "abs_ln_bp": abs(p.ln_ratio) * 1e4,
                           "u_diff_bp": None if p.u_diff is None else p.u_diff * 1e4, "status": p.status.value}
    mq = {m.code: m for m in q.members}
    rows = []
    for m in g.members:
        s = specs[m.code]
        rows.append({
            "rank": m.rank, "code": m.code, "name": names.get(m.code, ""), "eligible": m.eligible,
            "price": s.price, "volume": s.volume, "nav": s.nav, "nav_date": s.nav_date,
            "nav_premium": m.nav_premium, "rel_to_best": None if m.s is None or best is None else m.s / best - 1,
            "quote_time_utc_ns": s.quote_time_utc_ns, "age_s": mq[m.code].age_s, "freshness": mq[m.code].freshness,
            "reasons": [r.value for r in m.reasons], "to_next": pair_to_next.get(m.code),
            "snapshot_msg_id": s.snapshot_msg_id, "nav_msg_id": s.nav_msg_id,
        })
    return {
        "bundle_id": snap.bundle_id, "mode": g.mode.value, "price_basis": g.price_basis,
        "cutoff_utc_ns": g.cutoff_utc_ns, "tau_utc_ns": g.tau_utc_ns, "status": g.status.value,
        "common_anchor": g.common_anchor, "opportunity_alert_allowed": g.opportunity_alert_allowed,
        "reasons": [r.value for r in g.reasons], "quality": snapshot_to_dict(snap)["quality"],
        "sessions_since_anchor": bundle.sessions_since_anchor, "versions": dict(bundle.versions),
        "notes": list(bundle.notes), "rows": rows,
    }


def render(snap: RelativeSnapshot, bundle: RelativeBundle, names: dict[str, str]) -> str:
    v = view(snap, bundle, names)
    q = v["quality"]
    mode_cn = "当前（连续交易）" if v["mode"] == RelativeMode.CURRENT.value else "收盘参考（不代表当前可交易）"
    basis_cn = {"ASK": "卖一价（买入比较）", "LAST": "最新价/收盘价"}[v["price_basis"]]
    lines = [
        f"相对比较 · {mode_cn} · 价格口径 {basis_cn}",
        (f"知识截止 {_bj(v['cutoff_utc_ns'])} 北京；快照 τ {_bj(v['tau_utc_ns'])}；状态 {v['status']}；"
         f"新鲜度 {q['freshness']}；锚点 {q['anchor_health']}（{v['sessions_since_anchor']} 个交易日）"),
        "",
        f"{'排名':<4}{'代码':<8}{'名称':<20}{'价格':>8}{'单位净值':>10}{'净值日':>12}{'官方净值溢价':>12}{'相对最便宜':>12}",
    ]
    for r in v["rows"]:
        rel = "—" if r["rel_to_best"] is None else f"{r['rel_to_best'] * 100:+.2f}%"
        prem = "—" if r["nav_premium"] is None else f"{r['nav_premium'] * 100:+.2f}%"
        lines.append(f"{(r['rank'] or '—')!s:<4}{r['code']:<8}{r['name']:<20}{r['price'] or '—'!s:>8}"
                     f"{r['nav'] or '—'!s:>10}{r['nav_date'] or '—'!s:>12}{prem:>12}{rel:>12}"
                     + ("" if r["eligible"] else f"  退出：{','.join(r['reasons'])}"))
    pairs = [(r["code"], r["to_next"]) for r in v["rows"] if r["to_next"]]
    if pairs:
        lines += ["", "相邻排名成对判断（δ = S_i/S_j − 1；边界为日终历史差分 P95 放大后的情景值）："]
        for code, p in pairs:
            u = "—" if p["u_diff_bp"] is None else f"{p['u_diff_bp']:.1f}bp"
            lines.append(f"  {code} vs {p['next']}: δ={p['delta'] * 100:+.3f}%  |lnR|={p['abs_ln_bp']:.1f}bp  "
                         f"边界 {u}  → {p['status']}")
    lines += [
        "",
        (f"质量：来源可信度 {q['provenance_confidence']}；延迟 {q['delay_status']}；模型 {q['model_status']}；"
         f"对齐 {q['alignment']}；原因 {','.join(v['reasons']) or '无'}。"),
        f"输入包 {v['bundle_id'][:16]}；版本 {json.dumps(v['versions'], ensure_ascii=False)}。",
        f"机会提醒：{'允许' if v['opportunity_alert_allowed'] else '关闭'}。",
    ]
    return "\n".join(lines)


def to_json(snap: RelativeSnapshot, bundle: RelativeBundle, names: dict[str, str]) -> str:
    return json.dumps({"view": view(snap, bundle, names), "bundle": json.loads(canonical_json(bundle)),
                       "snapshot": snapshot_to_dict(snap)}, ensure_ascii=False, indent=2)
