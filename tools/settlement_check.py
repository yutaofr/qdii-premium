"""D4 证据：昨结算的结算日映射、合约身份与换月一致性核查（勘误 E8 门槛①②③）。

三条独立线索，全部只从原始日志重解析，不发请求：

1. **结算日映射**：每个 A 股交易时段观察到的昨结算，与该时段之前最近一个美股交易日 c 的 NDX 收盘比较。
   若字段确实是 c 日结算，基差应该小且随到期临近收敛；若落后一天，基差会随指数日变动整体平移（约 ±1%）。
2. **合约身份**：同一响应里的连续代码 hf_NQ 与身份已知的 hf_NQYYMM 逐字段比对，直接读出连续代码当前是哪个月份，
   以及它在哪一天切换。这是身份证据；日线基差只能提示、不能证明身份。
3. **日线基差变化的历史分布（探索性）**：连续代码日线收盘 / NDX 收盘的基差逐日变化。
   新浪日线收盘与指数收盘**不同刻**，该错位既可能放大也可能抵消偏差，因此这既不是误差上界，
   也不能推出"盘中区间误差更小"。仅用于观察换月跳变的量级与时点。

用法：uv run python tools/settlement_check.py [YYYY-MM-DD]
输出：reports/mvp/evidence/settlement-check-<date>.json
"""

from __future__ import annotations

import itertools
import json
import re
import statistics
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from qdii.apps.relative_snapshot import new_state
from qdii.contracts import index_history_v1, sina_hf_quarterly_v1, sina_hf_v2
from qdii.core.anchor import in_roll_window, third_friday
from qdii.core.types import MarketQuote, PriceType
from qdii.io import rawlog

REPO = Path(__file__).resolve().parents[1]
DATA = Path.home() / "qdii-data"
SH = ZoneInfo("Asia/Shanghai")
KLINE_ENDPOINTS = ("sina.nq_daily_kline", "research.sina.nq_daily_kline")
CONTINUOUS = "CONTINUOUS"  # 月份未知的连续代码 hf_NQ
ROLL_JUMP_BP = 50.0
HISTORY_DAYS = 30


def index_closes() -> dict[date, float]:
    """NDX 收盘：生产采集 + 研究抓取，同一契约解析。"""
    out: dict[date, float] = {}
    for root, days in ((DATA, HISTORY_DAYS), (DATA / "research", 3)):
        dates = [(date.today() - timedelta(days=i)).isoformat() for i in range(days, -1, -1)]  # noqa: DTZ011
        for msg in rawlog.iter_messages(root, dates=dates):
            if "nasdaq" not in msg.endpoint_id or msg.status != 200:
                continue
            for rec in index_history_v1.parse_nasdaq(msg).records:
                if getattr(rec, "close", None) is not None:
                    out[rec.trade_date] = float(rec.close)
    return out


def kline() -> dict[date, float]:
    dates = [(date.today() - timedelta(days=i)).isoformat() for i in range(HISTORY_DAYS, -1, -1)]  # noqa: DTZ011
    rows: dict[date, float] = {}
    for root in (DATA, DATA / "research"):
        for msg in rawlog.iter_messages(root, dates=dates):
            if msg.endpoint_id not in KLINE_ENDPOINTS or msg.status != 200:
                continue
            body = msg.body.decode("utf-8", "replace")
            m = re.search(r"=\((\[.*\])\);?", body, re.DOTALL)
            if m is None:
                continue
            for r in json.loads(m.group(1)):
                if float(r["close"]) > 0:
                    rows[date.fromisoformat(r["date"])] = float(r["close"])
    return rows


def identity_observations() -> list[dict]:
    """连续代码与身份已知季月合约的逐字段比对：连续代码此刻就是哪个月份（D4 身份证据）。"""
    dates = [(date.today() - timedelta(days=i)).isoformat() for i in range(HISTORY_DAYS, -1, -1)]  # noqa: DTZ011
    seen: dict[tuple[str, str], dict] = {}
    for root in (DATA, DATA / "research"):
        for msg in rawlog.iter_messages(root, dates=dates):
            if msg.endpoint_id not in ("sina.hf_NQ", "research.sina.hf_NQ") or msg.status != 200:
                continue
            cont = {r.price_type: r.value for r in sina_hf_v2.parse(msg).records if isinstance(r, MarketQuote)}
            per: dict[str, dict] = {}
            for r in sina_hf_quarterly_v1.parse(msg).records:
                per.setdefault(r.contract_id or "?", {})[r.price_type] = r.value
            if not per or not cont:
                continue
            fields = (PriceType.BID, PriceType.ASK, PriceType.SETTLE)
            matches = [c for c, q in per.items() if all(q.get(f) == cont.get(f) for f in fields)]
            day = datetime.fromtimestamp(msg.received_utc_ns / 1e9, tz=SH).date().isoformat()
            key = (day, ",".join(sorted(matches)) or "NONE")
            row = seen.setdefault(key, {
                "bj_date": day, "continuous_matches": matches or None, "samples": 0,
                "quarterlies": {c: {k.value: str(v) for k, v in q.items()} for c, q in sorted(per.items())}})
            row["samples"] += 1
    return [seen[k] for k in sorted(seen)]


def settle_observations() -> dict[tuple[date, str], dict]:
    """每个北京日期 × 合约的昨结算与买卖价中点。

    按合约分开：连续代码 CONTINUOUS 的观测**不能算作某个月份合约的观测**（D4 复核要求）。
    生产估算使用的是身份已知的季月合约，其结算日映射需要该合约自己的样本。
    """
    dates = [(date.today() - timedelta(days=i)).isoformat() for i in range(HISTORY_DAYS, -1, -1)]  # noqa: DTZ011
    out: dict[tuple[date, str], dict] = {}
    for root in (DATA, DATA / "research"):
        for msg in rawlog.iter_messages(root, dates=dates):
            if msg.endpoint_id not in ("sina.hf_NQ", "research.sina.hf_NQ") or msg.status != 200:
                continue
            per: dict[str, dict] = {}
            for r in sina_hf_v2.parse(msg).records:
                if isinstance(r, MarketQuote):
                    per.setdefault(CONTINUOUS, {})[r.price_type] = r
            for r in sina_hf_quarterly_v1.parse(msg).records:
                per.setdefault(r.contract_id or "?", {})[r.price_type] = r
            for contract, recs in per.items():
                settle, bid, ask = recs.get(PriceType.SETTLE), recs.get(PriceType.BID), recs.get(PriceType.ASK)
                if settle is None or settle.value is None or bid is None or ask is None:
                    continue
                t = bid.provider_time.utc_ns
                if t is None:
                    continue
                day = datetime.fromtimestamp(t / 1e9, tz=SH).date()
                row = out.setdefault((day, contract), {
                    "settle": str(settle.value), "samples": 0, "first_bj": None, "last_bj": None,
                    "mid_first": None, "mid_last": None})
                mid = (bid.value + ask.value) / 2 if bid.value and ask.value else None
                stamp = datetime.fromtimestamp(t / 1e9, tz=SH).strftime("%H:%M:%S")
                row["samples"] += 1
                if row["first_bj"] is None:
                    row["first_bj"], row["mid_first"] = stamp, str(mid)
                row["last_bj"], row["mid_last"] = stamp, str(mid)
                if row["settle"] != str(settle.value):  # 同一北京日内昨结算变化：立即可见
                    row["settle_changed_within_day"] = str(settle.value)
    return out


def main(day: str) -> None:
    target = date.fromisoformat(day)
    idx, kl, obs = index_closes(), kline(), settle_observations()
    cal = new_state(REPO).cal

    def last_us_session(before: date) -> date | None:
        return cal.last_session_on_or_before("NASDAQ", before - timedelta(days=1))

    settle_rows = []
    prev_basis: dict[str, float] = {}  # 逐合约比较，不跨合约算变化
    for d, contract in sorted(obs):
        o = obs[(d, contract)]
        c = last_us_session(d)
        close = idx.get(c) if c else None
        basis = (float(o["settle"]) / close - 1) * 1e4 if close else None
        previous = prev_basis.get(contract)
        row = {"bj_date": d.isoformat(), "contract": contract, "c": c.isoformat() if c else None,
               "settle": o["settle"], "index_close_c": close,
               "basis_bp": None if basis is None else round(basis, 1),
               "delta_basis_bp": None if basis is None or previous is None else round(basis - previous, 1),
               "samples": o["samples"], "first_bj": o["first_bj"], "last_bj": o["last_bj"],
               "mid_first": o["mid_first"], "mid_last": o["mid_last"],
               "settle_changed_within_day": o.get("settle_changed_within_day"),
               "roll_window": in_roll_window(c) if c else None}
        if row["delta_basis_bp"] is not None and abs(row["delta_basis_bp"]) > ROLL_JUMP_BP:
            row["flag"] = "SETTLE_BASIS_JUMP"  # 换月或结算日映射异常（仅研究标记，生产不消费）
        settle_rows.append(row)
        if basis is not None:
            prev_basis[contract] = basis

    common = sorted(set(kl) & set(idx))
    kbasis = {d: (kl[d] / idx[d] - 1) * 1e4 for d in common}
    changes, all_abs, outside_roll_abs, large = [], [], [], []
    for a, b in itertools.pairwise(common):
        delta = kbasis[b] - kbasis[a]  # Δ(F/I) 的基差变化；与"期货收益 − 指数收益"近似但不相等
        r_idx, r_fut = idx[b] / idx[a] - 1, kl[b] / kl[a] - 1
        row = {"date": b.isoformat(), "basis_change_bp": round(delta, 1),
               "return_diff_bp": round((r_fut - r_idx) * 1e4, 1), "in_roll_window": in_roll_window(b)}
        changes.append(row)
        all_abs.append(abs(delta))
        if not row["in_roll_window"]:  # 排除标准与被测幅度无关：只看是否处于换月窗口
            outside_roll_abs.append(abs(delta))
        if abs(delta) > ROLL_JUMP_BP:  # 正负都记，避免单向截尾
            expiry = min((third_friday(b.year, m) for m in (3, 6, 9, 12)), key=lambda e: abs((e - b).days))
            large.append({**row, "nearest_quarterly_expiry": expiry.isoformat(),
                          "days_before_expiry": (expiry - b).days})
    suspected = [r for r in large if r["in_roll_window"] and r["basis_change_bp"] > 0]
    all_abs.sort()
    outside_roll_abs.sort()

    def stats(xs: list[float]) -> dict:
        return {"n": len(xs), "median": round(statistics.median(xs), 1) if xs else None,
                "p95": round(xs[int(0.95 * len(xs))], 1) if xs else None, "max": round(max(xs), 1) if xs else None}
    next_expiry = min((third_friday(target.year, m) for m in (3, 6, 9, 12) if third_friday(target.year, m) >= target),
                      default=third_friday(target.year + 1, 3))
    out = {
        "nature": "SETTLEMENT_AND_ROLL_CHECK (原始日志重解析，无外部请求)",
        "generated_utc": datetime.now(ZoneInfo("UTC")).isoformat(timespec="seconds"),
        "target_date": day,
        "next_quarterly_expiry": next_expiry.isoformat(),
        "in_roll_window_today": in_roll_window(target),
        "settlement_observations": settle_rows,
        "contract_identity": identity_observations(),
        "kline_basis_changes": {
            "note": "日线收盘与指数收盘不同刻；以下为探索性分布，既不是误差上界，也不能外推到盘中区间",
            "days": len(common), "from": common[0].isoformat() if common else None,
            "to": common[-1].isoformat() if common else None,
            "abs_basis_change_bp_full_sample": stats(all_abs),
            "abs_basis_change_bp_excluding_roll_windows": stats(outside_roll_abs),
            "excluded_criterion": "季月到期前 11 天至到期日（与被测幅度无关）",
            "suspected_roll_days": suspected,
            "large_changes_both_directions": large,
        },
    }
    path = REPO / "reports" / "mvp" / "evidence" / f"settlement-check-{day}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"下一个季月到期 {next_expiry}，今日换月窗口内：{out['in_roll_window_today']}")
    for r in settle_rows:
        print(f"  {r['bj_date']} {r['contract']:<10} c={r['c']} 昨结算 {r['settle']} 指数 {r['index_close_c']} "
              f"基差 {r['basis_bp']}bp Δ {r['delta_basis_bp']} {r.get('flag') or ''}")
    for r in out["contract_identity"]:
        print(f"  身份比对 {r['bj_date']}：连续代码 = {r['continuous_matches']}（{r['samples']} 条样本）")
    k = out["kline_basis_changes"]
    print(f"日线基差 {k['days']} 天（{k['from']}→{k['to']}）：疑似换月 {len(k['suspected_roll_days'])} 次 "
          f"{[(r['date'], r['basis_change_bp'], r['days_before_expiry']) for r in k['suspected_roll_days']]}")
    print(f"双向大变动（|Δ| > {ROLL_JUMP_BP:.0f}bp）共 {len(k['large_changes_both_directions'])} 次")
    print(f"基差日变化绝对值（探索性，非误差上界）：全样本 {k['abs_basis_change_bp_full_sample']}；"
          f"排除换月窗口 {k['abs_basis_change_bp_excluding_roll_windows']}")
    print(path)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else datetime.now(SH).date().isoformat())
