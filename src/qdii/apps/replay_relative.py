"""相对比较确定性回放校验（FR11 / NFR02，ADD-0 §6）。

- 输入包模式：对每条已存快照，校验 bundle_id 与包内容一致（完整性），再用 evaluate_bundle 重算并按 ADR-011 比较。
- 流模式（--stream）：从原始日志按序 ingest，在每个已存快照的触发时刻重建输入包，比较 bundle_id；
  证明在线与回放走同一路径（ADR-009）。规则/配置版本变化会导致包不同，报告中单列。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from qdii.apps.relative_snapshot import new_state, relevant
from qdii.core.relative_bundle import (
    SCHEMA_VERSION,
    bundle_from_dict,
    bundle_id,
    canonical_json,
    diff_plain,
    evaluate_bundle,
    snapshot_to_dict,
)
from qdii.io import rawlog
from qdii.io.snapshot_store import SnapshotStore

LOOKBACK_DAYS = 10
MAX_EXAMPLES = 5


def verify(data_root: Path, repo: Path, dates: list[str], *, stream: bool = False,
           kind: str = "relative") -> dict[str, Any]:
    store = SnapshotStore(data_root, kind)
    records = list(store.iter(sorted(dates)))
    report: dict[str, Any] = {"mode": "stream" if stream else "bundle", "dates": sorted(dates),
                              "store": str(store.dir), "snapshots": len(records), "verified": 0,
                              "integrity_failures": 0, "mismatches": 0, "version_changes": 0,
                              "legacy_integrity_ok": 0, "legacy_integrity_failures": 0,
                              "missing_triggers": 0, "examples": [], "status": "NO_DATA"}
    if not records:  # 评审 R5：零样本不是成功
        return report

    def example(kind: str, rec: dict, detail: Any) -> None:
        if len(report["examples"]) < MAX_EXAMPLES:
            report["examples"].append({"kind": kind, "bundle_id": rec["bundle_id"][:16],
                                       "cutoff_utc_ns": rec["bundle"]["cutoff_utc_ns"], "detail": detail})

    current = [r for r in records if r["bundle"].get("schema") == SCHEMA_VERSION]
    for rec in records:
        # 旧结构输入包无法按当前代码复算：列为版本变更，不崩溃也不冒充通过。
        # 但仍可核验它作为历史记录未被篡改：内容重新规范化后的哈希应等于当时写下的 bundle_id。
        # 这与"用当前规则重新评估历史输入"是两件事，后者可能因规则收紧而不可用，不改写原始记录。
        if rec["bundle"].get("schema") != SCHEMA_VERSION:
            report["version_changes"] += 1
            digest = hashlib.sha256(json.dumps(rec["bundle"], sort_keys=True, separators=(",", ":"),
                                               ensure_ascii=False).encode("utf-8")).hexdigest()
            if digest == rec["bundle_id"]:
                report["legacy_integrity_ok"] += 1
            else:
                report["legacy_integrity_failures"] += 1
                example("LEGACY_INTEGRITY", rec, "stored bundle does not hash to its bundle_id")
            example("SCHEMA", rec, {"stored": rec["bundle"].get("schema"), "current": SCHEMA_VERSION})
    records = current
    if not records:
        return _finish(report)

    if not stream:
        for rec in records:
            b = bundle_from_dict(rec["bundle"])
            if bundle_id(b) != rec["bundle_id"]:
                report["integrity_failures"] += 1
                example("INTEGRITY", rec, "bundle content does not hash to stored bundle_id")
                continue
            snap = snapshot_to_dict(evaluate_bundle(b))
            diffs = diff_plain({"result": snap["result"], "quality": snap["quality"], "enav": snap["enav"]},
                               {"result": rec["result"], "quality": rec["quality"], "enav": rec.get("enav")})
            if diffs:
                report["mismatches"] += 1
                example("RESULT", rec, diffs[:5])
            else:
                report["verified"] += 1
        return _finish(report)

    # 流模式：对每条快照，先按 (received_at, msg_id) 顺序喂入截止时刻及之前收到的原始消息，
    # 再在该截止时刻重建输入包比较 bundle_id（适用于触发快照与主动保存的决策快照）
    ordered = sorted(records, key=lambda r: r["bundle"]["cutoff_utc_ns"])
    first = min(date.fromisoformat(d) for d in dates) - timedelta(days=LOOKBACK_DAYS)
    last = max(date.fromisoformat(d) for d in dates) + timedelta(days=1)
    span = [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]
    state = new_state(repo, data_root)
    messages = iter(rawlog.iter_messages(data_root, dates=span, end_utc_ns=ordered[-1]["bundle"]["cutoff_utc_ns"] + 1))
    pending = next(messages, None)
    for rec in ordered:
        cutoff = rec["bundle"]["cutoff_utc_ns"]
        while pending is not None and pending.received_utc_ns <= cutoff:
            if relevant(pending.endpoint_id):
                state.ingest(pending)
            pending = next(messages, None)
        rebuilt = state.bundle(cutoff, rec["bundle"]["price_basis"] if kind == "decisions" else None)
        if bundle_id(rebuilt) == rec["bundle_id"]:
            report["verified"] += 1
            continue
        rebuilt_plain, stored = json.loads(canonical_json(rebuilt)), rec["bundle"]
        if rebuilt_plain["versions"] != stored["versions"]:
            report["version_changes"] += 1
            example("VERSIONS", rec, diff_plain(rebuilt_plain["versions"], stored["versions"])[:5])
        else:
            report["mismatches"] += 1
            example("BUNDLE", rec, diff_plain(rebuilt_plain, stored)[:5])
    return _finish(report)


def _finish(report: dict[str, Any]) -> dict[str, Any]:
    if report["integrity_failures"] or report["mismatches"]:
        report["status"] = "FAILED"
    elif report["version_changes"]:
        report["status"] = "VERSION_CHANGED"
    else:
        report["status"] = "PASSED" if report["verified"] == report["snapshots"] else "FAILED"
    return report
