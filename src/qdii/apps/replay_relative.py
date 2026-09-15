"""相对比较确定性回放校验（FR11 / NFR02，ADD-0 §6）。

- 输入包模式：对每条已存快照，校验 bundle_id 与包内容一致（完整性），再用 evaluate_bundle 重算并按 ADR-011 比较。
- 流模式（--stream）：从原始日志按序 ingest，在每个已存快照的触发时刻重建输入包，比较 bundle_id；
  证明在线与回放走同一路径（ADR-009）。规则/配置版本变化会导致包不同，报告中单列。
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

from qdii.apps.relative_snapshot import new_state, relevant
from qdii.core.relative_bundle import (
    bundle_from_dict,
    bundle_id,
    canonical_json,
    diff_plain,
    evaluate_bundle,
    snapshot_to_dict,
)
from qdii.io import rawlog
from qdii.io.snapshot_store import SnapshotStore
from qdii.pipeline.relative_state import ETF_ENDPOINT

LOOKBACK_DAYS = 10
MAX_EXAMPLES = 5


def verify(data_root: Path, repo: Path, dates: list[str], *, stream: bool = False) -> dict[str, Any]:
    store = SnapshotStore(data_root)
    records = list(store.iter(sorted(dates)))
    report: dict[str, Any] = {"mode": "stream" if stream else "bundle", "dates": sorted(dates),
                              "snapshots": len(records), "integrity_failures": 0, "mismatches": 0,
                              "version_changes": 0, "missing_triggers": 0, "examples": []}

    def example(kind: str, rec: dict, detail: Any) -> None:
        if len(report["examples"]) < MAX_EXAMPLES:
            report["examples"].append({"kind": kind, "bundle_id": rec["bundle_id"][:16],
                                       "cutoff_utc_ns": rec["bundle"]["cutoff_utc_ns"], "detail": detail})

    if not stream:
        for rec in records:
            b = bundle_from_dict(rec["bundle"])
            if bundle_id(b) != rec["bundle_id"]:
                report["integrity_failures"] += 1
                example("INTEGRITY", rec, "bundle content does not hash to stored bundle_id")
                continue
            snap = snapshot_to_dict(evaluate_bundle(b))
            diffs = diff_plain({"result": snap["result"], "quality": snap["quality"]},
                               {"result": rec["result"], "quality": rec["quality"]})
            if diffs:
                report["mismatches"] += 1
                example("RESULT", rec, diffs[:5])
        return report

    by_cutoff = {rec["bundle"]["cutoff_utc_ns"]: rec for rec in records}
    if not by_cutoff:
        return report
    first = min(date.fromisoformat(d) for d in dates) - timedelta(days=LOOKBACK_DAYS)
    last = max(date.fromisoformat(d) for d in dates) + timedelta(days=1)
    span = [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]
    state = new_state(repo)
    seen = set()
    for msg in rawlog.iter_messages(data_root, dates=span, end_utc_ns=max(by_cutoff) + 1):
        if not relevant(msg.endpoint_id):
            continue
        state.ingest(msg)
        if msg.endpoint_id != ETF_ENDPOINT or msg.received_utc_ns not in by_cutoff:
            continue
        rec = by_cutoff[msg.received_utc_ns]
        seen.add(msg.received_utc_ns)
        rebuilt = state.bundle(msg.received_utc_ns)
        if bundle_id(rebuilt) == rec["bundle_id"]:
            continue
        stored = rec["bundle"]
        import json

        rebuilt_plain = json.loads(canonical_json(rebuilt))
        if rebuilt_plain["versions"] != stored["versions"]:
            report["version_changes"] += 1
            example("VERSIONS", rec, diff_plain(rebuilt_plain["versions"], stored["versions"])[:5])
        else:
            report["mismatches"] += 1
            example("BUNDLE", rec, diff_plain(rebuilt_plain, stored)[:5])
    report["missing_triggers"] = len(set(by_cutoff) - seen)
    report["mismatches"] += report["missing_triggers"]
    return report
