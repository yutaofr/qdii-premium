"""估算净值分母的日频误差：用模型从上一净值日推算下一净值日，与官方净值对照。

检验的是 E-NAV 里“指数 × 汇率”这一段（VM M0 的跨日部分，L0_FX_T 规则）：
    预测 NAV(b) = NAV(a) × I(b)/I(a) × X(b)/X(a)
其中 I 为对应美股交易日的 NDX 收盘、X 为该净值日的人民币中间价，日期均由日历确定（与生产同一路径）。
剩余误差来自基金费用计提、跟踪偏离与估值细则；不覆盖盘中期货那一段（F(t)/F(c)，勘误 E8 仍未验证）。

只用生产原始日志，不发请求。用法：uv run python tools/nav_model_check.py [YYYY-MM-DD]
输出：reports/mvp/evidence/nav-model-check-<date>.json
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from qdii.apps.relative_snapshot import new_state, relevant
from qdii.io import rawlog

REPO = Path(__file__).resolve().parents[1]
DATA = Path.home() / "qdii-data"
LOOKBACK_DAYS = 10


def main(day: str) -> None:
    state = new_state(REPO)
    dates = [(date.fromisoformat(day) - timedelta(days=i)).isoformat() for i in range(LOOKBACK_DAYS, -1, -1)]
    for msg in rawlog.iter_messages(DATA, dates=dates):
        if relevant(msg.endpoint_id):
            state.ingest(msg)

    rows = []
    for fund in state.funds:
        by_date = state.navs.get(fund.code) or {}
        if len(by_date) < 2:
            continue
        recent = sorted(by_date)[-2:]
        a, b = recent[0], recent[1]
        fa, fb = state._anchor_factors(a)[0], state._anchor_factors(b)[0]
        if fa is None or fb is None:
            continue
        nav_a, nav_b = by_date[a][-1].unit_nav, by_date[b][-1].unit_nav
        predicted = nav_a * (fb.index / fa.index) * (fb.fx / fa.fx)
        rows.append({
            "code": fund.code, "from": a.isoformat(), "to": b.isoformat(),
            "nav_from": str(nav_a), "nav_to_official": str(nav_b), "nav_to_predicted": f"{predicted:.6f}",
            "index_from": f"{fa.index}@{fa.index_date}", "index_to": f"{fb.index}@{fb.index_date}",
            "fx_from": f"{fa.fx}@{fa.fx_date}", "fx_to": f"{fb.fx}@{fb.fx_date}",
            "error_bp": round(float(predicted / nav_b - 1) * 1e4, 2),
        })
    if not rows:
        print("没有可比较的相邻净值日（需要两个净值日及其指数、中间价因子）")
        return
    errors = [abs(r["error_bp"]) for r in rows]
    out = {"nature": "MODEL_VS_OFFICIAL_NAV (跨日部分，不含盘中期货)", "generated_utc": datetime.now(
        ZoneInfo("UTC")).isoformat(timespec="seconds"), "target_date": day, "funds": len(rows),
        "mean_abs_error_bp": round(sum(errors) / len(errors), 2), "max_abs_error_bp": round(max(errors), 2),
        "rows": rows}
    path = REPO / "reports" / "mvp" / "evidence" / f"nav-model-check-{day}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: out[k] for k in ("funds", "mean_abs_error_bp", "max_abs_error_bp")}, ensure_ascii=False))
    for r in rows:
        print(f"  {r['code']} {r['from']}→{r['to']} 官方 {r['nav_to_official']} 预测 {r['nav_to_predicted']} "
              f"误差 {r['error_bp']:+.1f}bp")
    print(path)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat())
