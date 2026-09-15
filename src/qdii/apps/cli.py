"""qdii 命令行入口。"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from qdii.contracts.registry import PARSERS
from qdii.core.types import MarketQuote, PriceType, QuoteSide
from qdii.io import rawlog
from qdii.io.http import BlockList

SHANGHAI = ZoneInfo("Asia/Shanghai")
DEFAULT_DATA_ROOT = Path("~/qdii-data").expanduser()


def _bj(utc_ns: int | None) -> str:
    if utc_ns is None:
        return "—"
    return datetime.fromtimestamp(utc_ns / 1e9, tz=UTC).astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")


def cmd_collect(args: argparse.Namespace) -> int:
    from qdii.apps.collect import main as collect_main

    return collect_main(Path(args.config_dir).resolve(), Path(args.data_root).expanduser() if args.data_root else None)


def cmd_raw_stats(args: argparse.Namespace) -> int:
    root = Path(args.data_root).expanduser()
    per: dict[str, dict] = defaultdict(lambda: {"n": 0, "status": Counter(), "bytes": 0, "first": None, "last": None,
                                                 "rtt": []})
    for msg in rawlog.iter_messages(root, dates=args.date):
        s = per[msg.endpoint_id]
        s["n"] += 1
        s["status"][str(msg.status) if msg.status is not None else (msg.error or "ERR").split(":")[0]] += 1
        s["bytes"] += len(msg.body)
        s["first"] = s["first"] or msg.received_utc_ns
        s["last"] = msg.received_utc_ns
        if msg.rtt_ms is not None:
            s["rtt"].append(msg.rtt_ms)
    print(f"{'endpoint':34} {'n':>6} {'MB':>6} {'rtt_p50':>8}  status  first(BJ) → last(BJ)")
    for eid, s in sorted(per.items()):
        rtt = sorted(s["rtt"])
        p50 = rtt[len(rtt) // 2] if rtt else float("nan")
        print(f"{eid:34} {s['n']:>6} {s['bytes'] / 1e6:>6.2f} {p50:>8.0f}  {dict(s['status'])}  "
              f"{_bj(s['first'])} → {_bj(s['last'])}")
    return 0


def cmd_raw_parse(args: argparse.Namespace) -> int:
    """在原始日志上重跑解析器：流回放路径的最小形态（ADD-0 §6）。"""
    parser = PARSERS.get(args.contract)
    if parser is None:
        print(f"unknown contract {args.contract}; known: {sorted(PARSERS)}", file=sys.stderr)
        return 2
    root = Path(args.data_root).expanduser()
    n_msg = 0
    issues: Counter[str] = Counter()
    side_states: Counter[str] = Counter()
    provider_lag: list[float] = []
    for msg in rawlog.iter_messages(root, dates=args.date):
        if msg.endpoint_id != args.endpoint:
            continue
        n_msg += 1
        result = parser(msg)
        issues.update(i.code.value for i in result.issues)
        for rec in result.records:
            if isinstance(rec, QuoteSide):
                side_states[f"{rec.side.value}:{rec.state.value}"] += 1
            elif isinstance(rec, MarketQuote) and rec.price_type is PriceType.LAST and rec.provider_time.utc_ns:
                provider_lag.append((rec.received_utc_ns - rec.provider_time.utc_ns) / 1e9)
    provider_lag.sort()
    q = (lambda p: provider_lag[min(len(provider_lag) - 1, int(p * len(provider_lag)))]) if provider_lag else None
    print(json.dumps({
        "messages": n_msg,
        "issues": dict(issues),
        "side_states": dict(side_states),
        "received_minus_provider_time_s": None if q is None else
        {"p50": q(0.5), "p95": q(0.95), "max": provider_lag[-1], "min": provider_lag[0]},
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_unblock(args: argparse.Namespace) -> int:
    bl = BlockList.load(Path(args.data_root).expanduser() / "state" / "blocked.json")
    print("unblocked" if bl.unblock(args.endpoint) else "not blocked")
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    path = Path(args.data_root).expanduser() / "events" / "collector.jsonl"
    if not path.exists():
        return 0
    for line in path.read_text(encoding="utf-8").splitlines()[-args.tail:]:
        ev = json.loads(line)
        rest = {k: v for k, v in ev.items() if k not in ("utc_ns", "type")}
        print(_bj(ev["utc_ns"]), ev["type"], json.dumps(rest, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="qdii")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("collect", help="运行采集守护进程")
    c.add_argument("--config-dir", default="config")
    c.add_argument("--data-root", default=None)
    c.set_defaults(func=cmd_collect)

    raw = sub.add_parser("raw", help="原始日志工具").add_subparsers(dest="raw_cmd", required=True)
    s = raw.add_parser("stats")
    s.add_argument("--date", action="append", required=True, help="UTC 日期 YYYY-MM-DD，可重复")
    s.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT))
    s.set_defaults(func=cmd_raw_stats)
    r = raw.add_parser("parse")
    r.add_argument("--contract", required=True)
    r.add_argument("--endpoint", required=True)
    r.add_argument("--date", action="append", required=True)
    r.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT))
    r.set_defaults(func=cmd_raw_parse)

    u = sub.add_parser("unblock", help="维护者确认后解除端点封禁（DS-08）")
    u.add_argument("endpoint")
    u.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT))
    u.set_defaults(func=cmd_unblock)

    e = sub.add_parser("events", help="查看事件日志")
    e.add_argument("--tail", type=int, default=30)
    e.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT))
    e.set_defaults(func=cmd_events)

    rel = sub.add_parser("relative", help="相对比较快照（VM-10）：按知识截止时刻在原始日志上回放")
    rel.add_argument("--at", default=None, help="ISO 时间，含时区，如 2026-09-15T14:59:30+08:00；默认现在")
    rel.add_argument("--basis", choices=["ASK", "LAST"], default=None)
    rel.add_argument("--json", action="store_true")
    rel.add_argument("--save", action="store_true", help="把本次输入包与结果保存到 snapshots/decisions/，便于事后复盘")
    rel.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT))
    rel.set_defaults(func=cmd_relative)

    rp = sub.add_parser("replay", help="确定性回放校验（FR11/NFR02）").add_subparsers(dest="replay_cmd", required=True)
    rr = rp.add_parser("relative", help="重算已存相对比较快照；--stream 从原始日志重建输入包再比对")
    rr.add_argument("--date", action="append", required=True, help="快照 UTC 日期 YYYY-MM-DD，可重复")
    rr.add_argument("--stream", action="store_true")
    rr.add_argument("--kind", choices=["relative", "decisions"], default="relative")
    rr.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT))
    rr.set_defaults(func=cmd_replay_relative)

    ra = rp.add_parser("anchors", help="从原始日志重算美股收盘期货锚点并与锚点库比较")
    ra.add_argument("--date", action="append", required=True, help="美东日期 YYYY-MM-DD，可重复")
    ra.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT))
    ra.set_defaults(func=cmd_replay_anchors)

    an = sub.add_parser("anchors", help="查看最近的美股收盘期货锚点")
    an.add_argument("--tail", type=int, default=10)
    an.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT))
    an.set_defaults(func=cmd_anchors)

    rs = sub.add_parser("research", help="Phase 0 研究工具").add_subparsers(dest="research_cmd", required=True)
    f = rs.add_parser("fetch-history", help="抓取 NAV / NDX / 中间价历史到 research 原始日志")
    f.add_argument("--since", required=True, type=date.fromisoformat)
    f.add_argument("--until", default=None, type=date.fromisoformat)
    f.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT))
    f.set_defaults(func=cmd_research_fetch)
    fr = rs.add_parser("fund-rules", help="PH0-06：下载最新招募说明书（仓库外）并提取估值汇率条款候选句")
    fr.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT))
    fr.add_argument("--out", default="reports/phase0/fund_rules")
    fr.set_defaults(func=cmd_research_fund_rules)
    a = rs.add_parser("nav-fx", help="PH0-07：净值日期对齐与汇率规则对照报告")
    a.add_argument("--run", default="latest")
    a.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT))
    a.add_argument("--out", default="reports/phase0/history")
    a.set_defaults(func=cmd_research_nav_fx)
    return p


def cmd_relative(args: argparse.Namespace) -> int:
    from qdii.apps.relative_snapshot import build, render, to_json

    if args.at:
        at = datetime.fromisoformat(args.at)
        if at.tzinfo is None:
            print("--at 必须带时区", file=sys.stderr)
            return 2
        cutoff = int(at.timestamp() * 1e9)
    else:
        cutoff = int(datetime.now(UTC).timestamp() * 1e9)
    repo = Path(__file__).resolve().parents[3]
    data_root = Path(args.data_root).expanduser()
    snap, bundle, names = build(data_root, repo, cutoff, args.basis)
    print(to_json(snap, bundle, names) if args.json else render(snap, bundle, names))
    if args.save:
        from qdii.core.relative_bundle import canonical_json, snapshot_to_dict
        from qdii.io.snapshot_store import SnapshotStore

        path = SnapshotStore(data_root, "decisions").append(
            canonical_json(bundle), snapshot_to_dict(snap), int(datetime.now(UTC).timestamp() * 1e9))
        print(f"决策快照已保存：{path}（bundle_id {snap.bundle_id}）", file=sys.stderr)
    return 0


def cmd_replay_relative(args: argparse.Namespace) -> int:
    from qdii.apps.replay_relative import verify

    repo = Path(__file__).resolve().parents[3]
    report = verify(Path(args.data_root).expanduser(), repo, args.date, stream=args.stream, kind=args.kind)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return {"PASSED": 0, "NO_DATA": 3, "VERSION_CHANGED": 4}.get(report["status"], 1)


def cmd_replay_anchors(args: argparse.Namespace) -> int:
    from qdii.apps.replay_anchors import verify

    repo = Path(__file__).resolve().parents[3]
    report = verify(Path(args.data_root).expanduser(), repo, args.date)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return {"PASSED": 0, "NO_DATA": 3}.get(report["status"], 1)


def cmd_anchors(args: argparse.Namespace) -> int:
    from qdii.io.anchor_store import AnchorStore

    for rec in AnchorStore(Path(args.data_root).expanduser()).recent(args.tail):
        a = rec["anchor"]
        lag = "—" if a["lag_s"] is None else f"{a['lag_s']:.1f}s"
        print(f"{_bj(a['c_utc_ns'])} 北京  {a['status']:<8} {a['value'] or '—':>12}  距c {lag:>6}  样本 {a['candidates']:>3}"
              f"  换月窗口 {'是' if a['roll_window'] else '否'}  {','.join(a['reason_codes'])}")
    return 0


def cmd_research_fetch(args: argparse.Namespace) -> int:
    import asyncio

    from qdii.apps.research_nav import fetch_history

    until = args.until or datetime.now(UTC).date()
    run_id = asyncio.run(fetch_history(Path(args.data_root).expanduser() / "research", args.since, until))
    print(run_id)
    return 0


def cmd_research_fund_rules(args: argparse.Namespace) -> int:
    import asyncio

    from qdii.apps.research_rules import run

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    print(asyncio.run(run(Path(args.data_root).expanduser(), Path(args.out) / stamp)))
    return 0


def cmd_research_nav_fx(args: argparse.Namespace) -> int:
    from qdii.apps.research_nav import analyze, latest_run

    root = Path(args.data_root).expanduser() / "research"
    run_id = latest_run(root) if args.run == "latest" else args.run
    if run_id is None:
        print("no research run found; run `qdii research fetch-history` first", file=sys.stderr)
        return 2
    print(analyze(root, run_id, Path(args.out) / run_id))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
