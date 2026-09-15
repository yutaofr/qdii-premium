"""PH0-07 历史经济验证（ADD-0 §7）：抓取 → 原始日志 → 契约解析 → 规则对照报告。

抓取结果与在线采集分开存放：<data_root>/research/，run_id 以 RESEARCH- 开头。
分析只从原始日志重解析，不直接使用抓取时的响应对象（与在线/回放同一解析路径）。
"""

from __future__ import annotations

import asyncio
import csv
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import httpx

from qdii.contracts import chinamoney_ccpr_his_v1, eastmoney_lsjz_v1, index_history_v1
from qdii.core import history_validation as hv
from qdii.core.types import FxFixing, IndexClose, NavObservation, RawMessage
from qdii.io import rawlog
from qdii.io.clock import SystemClock
from qdii.io.http import EndpointRequest, Fetcher

FUNDS = ("513100", "159696", "159501", "159660", "513390")
UA = "qdii-premium-research/0.1 (personal research)"
PAUSE_S = 1.0


# ---------- 抓取 ----------

async def fetch_history(root: Path, since: date, until: date, funds: tuple[str, ...] = FUNDS) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"RESEARCH-{stamp}"
    writer = rawlog.RawLogWriter(root)
    seq = iter(range(10**9))
    counts: dict[str, int] = defaultdict(int)

    async with httpx.AsyncClient(timeout=30, follow_redirects=True, trust_env=False) as client:
        fetcher = Fetcher(client, SystemClock(), run_id)

        async def get(endpoint_id: str, source_id: str, url: str, params: dict, headers: dict | None = None
                      ) -> RawMessage:
            req = EndpointRequest(endpoint_id, source_id, "GET", f"{url}?{urlencode(params)}",
                                  tuple(sorted({"User-Agent": UA, **(headers or {})}.items())))
            msg = await fetcher.fetch(req, next(seq))
            writer.append(msg)
            counts[f"{endpoint_id}:{msg.status or msg.error}"] += 1
            await asyncio.sleep(PAUSE_S)
            return msg

        await get("research.fred.NASDAQ100", "fred", "https://fred.stlouisfed.org/graph/fredgraph.csv",
                  {"id": "NASDAQ100", "cosd": since.isoformat(), "coed": until.isoformat()})
        await get("research.nasdaq.NDX", "nasdaq", "https://api.nasdaq.com/api/quote/NDX/historical",
                  {"assetclass": "index", "fromdate": since.isoformat(), "todate": until.isoformat(), "limit": 9999})

        chunk_start = since
        while chunk_start <= until:  # 中间价按 180 天分段，避免单次范围过大
            chunk_end = min(until, chunk_start + timedelta(days=179))
            page, pages = 1, 1
            while page <= pages:
                m = await get("research.chinamoney.ccpr", "chinamoney",
                              "https://www.chinamoney.com.cn/ags/ms/cm-u-bk-ccpr/CcprHisNew",
                              {"startDate": chunk_start.isoformat(), "endDate": chunk_end.isoformat(),
                               "currency": "USD/CNY", "pageNum": page, "pageSize": 50})
                pages = chinamoney_ccpr_his_v1.page_total(m) or 0 if page == 1 else pages
                page += 1
            chunk_start = chunk_end + timedelta(days=1)

        for code in funds:
            page, pages = 1, 1
            while page <= pages:
                m = await get(f"research.eastmoney.lsjz.{code}", "eastmoney", "https://api.fund.eastmoney.com/f10/lsjz",
                              {"fundCode": code, "pageIndex": page, "pageSize": 20,
                               "startDate": since.isoformat(), "endDate": until.isoformat()},
                              {"Referer": "https://fundf10.eastmoney.com/"})
                if page == 1:
                    total = eastmoney_lsjz_v1.total_count(m) or 0
                    pages = (total + 19) // 20
                page += 1

    writer.close()
    manifest = root / "manifests" / f"{run_id}.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"run_id": run_id, "since": since.isoformat(), "until": until.isoformat(),
                                    "funds": funds, "counts": counts}, ensure_ascii=False, indent=2))
    return run_id


def latest_run(root: Path) -> str | None:
    files = sorted((root / "manifests").glob("RESEARCH-*.json"))
    return files[-1].stem if files else None


# ---------- 分析 ----------

@dataclass
class Loaded:
    navs: dict[str, list[NavObservation]]
    fred: dict[date, float]
    nasdaq: dict[date, float]
    fixings: dict[date, float]
    issues: dict[str, int]
    http: dict[str, int]


def load_run(root: Path, run_id: str) -> Loaded:
    navs: dict[str, dict[date, NavObservation]] = defaultdict(dict)
    fred, nasdaq, fixings = {}, {}, {}
    issues: dict[str, int] = defaultdict(int)
    http: dict[str, int] = defaultdict(int)
    parsers = {
        "research.fred.": index_history_v1.parse_fred,
        "research.nasdaq.": index_history_v1.parse_nasdaq,
        "research.chinamoney.ccpr": chinamoney_ccpr_his_v1.parse,
        "research.eastmoney.lsjz.": eastmoney_lsjz_v1.parse,
    }
    for msg in rawlog.iter_messages(root):
        if msg.run_id != run_id:
            continue
        http[f"{msg.endpoint_id.rsplit('.', 1)[0] if 'lsjz' in msg.endpoint_id else msg.endpoint_id}:"
             f"{msg.status or 'ERR'}"] += 1
        parser = next((p for prefix, p in parsers.items() if msg.endpoint_id.startswith(prefix)), None)
        if parser is None:
            continue
        result = parser(msg)
        for issue in result.issues:
            issues[f"{msg.endpoint_id.split('.')[1]}:{issue.code.value}"] += 1
        for rec in result.records:
            if isinstance(rec, NavObservation):
                navs[rec.code][rec.nav_date] = rec  # 同日期后收到的覆盖先收到的（研究用途）
            elif isinstance(rec, IndexClose) and rec.close is not None:
                (fred if rec.source_id == "fred" else nasdaq)[rec.trade_date] = float(rec.close)
            elif isinstance(rec, FxFixing) and rec.rate is not None:
                fixings[rec.publish_date] = float(rec.rate)
    return Loaded({k: list(v.values()) for k, v in navs.items()}, fred, nasdaq, fixings, dict(issues), dict(http))


PAIR_RULE = "L0_FX_T"
PAIR_WINDOW = 60


def pair_udiff(clean_res: dict[str, dict[date, float]], run_id: str) -> dict:
    """成对差分：两基金同一净值区间残差之差（VM-10 σ_diff 的经验替代，只作情景边界）。"""
    import itertools
    import math

    pairs = {}
    for a, b in itertools.combinations(sorted(clean_res), 2):
        common = sorted(set(clean_res[a]) & set(clean_res[b]))[-PAIR_WINDOW:]
        diffs = sorted(abs(clean_res[a][d] - clean_res[b][d]) * 1e4 for d in common)
        if not diffs:
            continue
        rank = max(0, math.ceil(0.95 * len(diffs)) - 1)
        pairs[f"{a}|{b}"] = {"n": len(diffs), "median_bp": diffs[len(diffs) // 2], "p95_bp": diffs[rank],
                             "max_bp": diffs[-1], "last_d1": common[-1].isoformat()}
    return {"run_id": run_id, "rule": PAIR_RULE, "window": f"last {PAIR_WINDOW} common clean intervals",
            "nature": "HISTORICAL_RESEARCH_ONLY", "pairs": pairs}


def _fmt(v: float | None, nd: int = 1) -> str:
    return "—" if v is None else f"{v:.{nd}f}"


def analyze(root: Path, run_id: str, out_dir: Path) -> Path:
    data = load_run(root, run_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    common = sorted(set(data.fred) & set(data.nasdaq))
    diffs = [abs(data.fred[d] - data.nasdaq[d]) for d in common]
    index = data.nasdaq or data.fred

    lines = [
        f"# PH0-07 历史经济验证：净值日期对齐与汇率规则（{run_id}）",
        "",
        (f"生成：{datetime.now(UTC).isoformat(timespec='seconds')}。性质：**历史经济拟合**（HISTORICAL_RESEARCH_ONLY）。"
         "没有历史获知时间，不证明披露及时性；误差最低不等于基金合同采用该规则，正式结论以 PH0-06 条款核验为准（VM-12）。"),
        "",
        "## 数据",
        "",
        (f"- NDX 收盘：Nasdaq {len(data.nasdaq)} 天、FRED {len(data.fred)} 天；重叠 {len(common)} 天，"
         f"最大绝对差 {_fmt(max(diffs) if diffs else None, 4)} 点。分析使用 {'Nasdaq' if data.nasdaq else 'FRED'}。"),
        f"- USD/CNY 中间价（中国货币网）：{len(data.fixings)} 个发布日"
        + (f"，{min(data.fixings)} 至 {max(data.fixings)}。" if data.fixings else "。"),
        f"- HTTP：{json.dumps(data.http, ensure_ascii=False)}",
        f"- 解析问题：{json.dumps(data.issues, ensure_ascii=False) if data.issues else '无'}",
        "",
        "## 假设",
        "",
        "| 名称 | 净值日期对应的 NDX 收盘 | 汇率 |",
        "|---|---|---|",
        "| L0_FX_T | 同一美股交易日 | 最近一个发布日 ≤ 净值日的中间价 |",
        "| L0_FX_T+1 | 同一美股交易日 | 净值日之后第一个发布日的中间价 |",
        "| L1_FX_T | 前一个美股交易日 | 最近一个发布日 ≤ 净值日的中间价 |",
        "| L1_FX_T+1 | 前一个美股交易日 | 净值日之后第一个发布日的中间价 |",
        "| L0_NOFX | 同一美股交易日 | 不计汇率（对照组） |",
        "",
        "残差 = 实际净值比 / M0 预测比 − 1，单位 bp。M0 假设 100% 未对冲美元纳指敞口，残差包含现金拖累、费用、跟踪偏差。",
        "",
    ]

    csv_path = out_dir / "intervals.csv"
    summary_rows = []
    clean_res: dict[str, dict[date, float]] = {}
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["code", "d0", "d1", "nav0", "nav1", "actual_ratio", "excluded",
                    *[f"res_bp_{h.name}" for h in hv.HYPOTHESES]])
        for code in FUNDS:
            navs = data.navs.get(code, [])
            intervals = hv.build_intervals(navs, index, data.fixings)
            for iv in intervals:
                w.writerow([iv.code, iv.d0, iv.d1, iv.nav0, iv.nav1, f"{iv.actual_ratio:.8f}", iv.excluded or "",
                            *["" if iv.residuals[h.name] is None else f"{iv.residuals[h.name] * 1e4:.3f}"
                              for h in hv.HYPOTHESES]])
            cleaned = hv.clean(intervals)
            clean_res[code] = {iv.d1: iv.residuals[PAIR_RULE] for iv in cleaned}  # type: ignore[misc]
            sel = hv.select_rule(cleaned)
            excluded = defaultdict(int)
            for iv in intervals:
                if iv.excluded:
                    excluded[iv.excluded] += 1
                elif iv not in cleaned:
                    excluded["UNCOMPUTABLE"] += 1
            summary_rows.append((code, len(navs), len(intervals), dict(excluded), sel))

    lines += ["## 各基金结果", ""]
    for code, n_nav, n_iv, excluded, sel in summary_rows:
        lines += [
            f"### {code}",
            "",
            f"净值 {n_nav} 条，区间 {n_iv} 个，干净区间 {sel.n_clean} 个；排除 {json.dumps(excluded, ensure_ascii=False) or '无'}。"
            + (f" **样本不足：距 60+60 目标差 {sel.shortfall} 个。**" if sel.shortfall else ""),
            "",
            f"仅用开发集选出：**{sel.selected or '无法选择'}**。",
            "",
            "| 假设 | 开发集 n | 开发 MAE | 封存 n | 封存均值 | 封存 MAE | 封存 P95 | 封存最大 |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for h in hv.HYPOTHESES:
            d, o = sel.dev[h.name], sel.holdout[h.name]
            mark = " ←" if h.name == sel.selected else ""
            lines.append(f"| {h.name}{mark} | {d.n} | {_fmt(d.mae_bp)} | {o.n} | {_fmt(o.mean_bp)} | "
                         f"{_fmt(o.mae_bp)} | {_fmt(o.p95_bp)} | {_fmt(o.max_bp)} |")
        lines.append("")

    # 相对比较前提（VM-10）：同一净值日期覆盖
    by_date: dict[date, int] = defaultdict(int)
    for code in FUNDS:
        for n in data.navs.get(code, []):
            by_date[n.nav_date] += 1
    all_five = sum(1 for c in by_date.values() if c == len(FUNDS))
    lines += [
        "## 相对比较前提：净值日期对齐",
        "",
        f"出现过的净值日期 {len(by_date)} 个，其中五只同时有净值的 {all_five} 个。",
        "",
        f"逐区间明细：[intervals.csv]({csv_path.name})。",
        "",
    ]
    udiff = pair_udiff(clean_res, run_id)
    (out_dir / "pair_udiff.json").write_text(json.dumps(udiff, ensure_ascii=False, indent=2), encoding="utf-8")
    lines += [
        "## 成对差分误差（供 VM-10 情景边界）",
        "",
        (f"规则 {PAIR_RULE}；每对基金取最近 {PAIR_WINDOW} 个共同干净区间，统计两基金日残差之差的绝对值。"
         "这是日终历史分布，不是盘中误差证明；使用时须按锚点后的交易日数放大并注明来源。"),
        "",
        "| 基金对 | n | 中位数 bp | P95 bp | 最大 bp |",
        "|---|---|---|---|---|",
        *[f"| {k} | {v['n']} | {v['median_bp']:.2f} | {v['p95_bp']:.2f} | {v['max_bp']:.2f} |"
          for k, v in udiff["pairs"].items()],
        "",
    ]
    report = out_dir / "nav_fx_rule.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    return report
