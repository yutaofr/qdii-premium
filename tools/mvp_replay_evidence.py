"""MVP 回放证据（评审 R5 / NFR01）：用生产原始日志按在线路径重新生成相对比较快照，并做两种回放校验。

性质：**重新生成的样本，不是生产当时写入的快照**。生产快照由采集器在窗口内写入
~/qdii-data/snapshots/relative/，用 `qdii replay relative --date <UTC日期> [--stream]` 校验。

用法：uv run python tools/mvp_replay_evidence.py 2026-09-15
输出：~/qdii-data/evidence/mvp/replay-<date>/（快照，仓库外）与 reports/mvp/evidence/replay-<date>.json（摘要）。
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from qdii.apps.relative_snapshot import new_state, relevant
from qdii.apps.replay_relative import verify
from qdii.core.relative_bundle import canonical_json, evaluate_bundle, snapshot_to_dict
from qdii.io import rawlog
from qdii.io.snapshot_store import SnapshotStore
from qdii.pipeline.relative_state import ETF_ENDPOINT

REPO = Path(__file__).resolve().parents[1]
DATA = Path.home() / "qdii-data"


def pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def main(day: str) -> None:
    target = date.fromisoformat(day)
    out = DATA / "evidence" / "mvp" / f"replay-{day}"
    if out.exists():
        shutil.rmtree(out)  # 不跟随其中的 raw 符号链接，生产原始日志不受影响
    out.mkdir(parents=True)
    (out / "raw").symlink_to(DATA / "raw")  # 只读引用生产原始日志

    state, store = new_state(REPO), SnapshotStore(out)
    lookback = [(target - timedelta(days=i)).isoformat() for i in range(10, -1, -1)]
    start_ns = int(datetime.combine(target, datetime.min.time(), tzinfo=UTC).timestamp() * 1e9)
    latencies_ms: list[float] = []
    for msg in rawlog.iter_messages(DATA, dates=lookback):
        if not relevant(msg.endpoint_id):
            continue
        t0 = time.perf_counter()
        state.ingest(msg)
        if msg.endpoint_id == ETF_ENDPOINT and msg.status == 200 and msg.received_utc_ns >= start_ns:
            bundle = state.bundle(msg.received_utc_ns)
            store.append(canonical_json(bundle), snapshot_to_dict(evaluate_bundle(bundle)), 0)
            latencies_ms.append((time.perf_counter() - t0) * 1e3)  # 单个触发点：解析+构包+计算+写盘

    files = sorted(store.dir.glob("*.jsonl"))
    dates = [f.stem for f in files]
    summary = {
        "nature": "REGENERATED_FROM_PRODUCTION_RAW_LOG (not production-written snapshots)",
        "generated_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "target_utc_date": day,
        "evidence_dir": str(out),
        "snapshot_files": {f.name: {"lines": sum(1 for _ in f.open()), "sha256": hashlib.sha256(f.read_bytes()).hexdigest()}
                           for f in files},
        "triggers": len(latencies_ms),
        "per_trigger_latency_ms": None if not latencies_ms else {
            "p50": round(pct(latencies_ms, 0.50), 3), "p95": round(pct(latencies_ms, 0.95), 3),
            "max": round(max(latencies_ms), 3), "nfr01_limit_ms": 1000},
        "verify_bundle": verify(out, REPO, dates),
        "verify_stream": verify(out, REPO, dates, stream=True),
    }
    report = REPO / "reports" / "mvp" / "evidence" / f"replay-{day}.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("triggers", "per_trigger_latency_ms")}, ensure_ascii=False))
    print({k: summary[k]["status"] for k in ("verify_bundle", "verify_stream")}, summary["verify_bundle"]["verified"],
          summary["verify_stream"]["verified"])
    print(report)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "2026-09-15")
