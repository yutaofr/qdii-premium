"""相对比较快照存储（ADR-018：MVP 用只追加 JSONL，SQLite 推迟到出现查询需求时）。

布局：<root>/snapshots/<kind>/<YYYY-MM-DD>.jsonl（按 cutoff 的 UTC 日期）。
kind=relative：采集器在 ETF 批次触发时写入；kind=decisions：维护者主动保存的页面/命令行决策快照（评审口径 3）。
每行：{"v":1, "bundle_id", "bundle"(规范输入包), "result", "quality", "computed_at_utc_ns"}。
computed_at 只是元数据，不参与确定性比较（ADR-010）。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = 1


class SnapshotStore:
    def __init__(self, root: Path, kind: str = "relative") -> None:
        if kind not in ("relative", "decisions"):
            raise ValueError(kind)
        self.dir = root / "snapshots" / kind

    def path_for(self, cutoff_utc_ns: int) -> Path:
        d = datetime.fromtimestamp(cutoff_utc_ns / 1e9, tz=UTC).date().isoformat()
        return self.dir / f"{d}.jsonl"

    def append(self, bundle_json: str, snapshot: dict[str, Any], computed_at_utc_ns: int) -> Path:
        bundle = json.loads(bundle_json)
        path = self.path_for(bundle["cutoff_utc_ns"])
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"v": SCHEMA, "bundle_id": snapshot["bundle_id"], "bundle": bundle,
                           "result": snapshot["result"], "quality": snapshot["quality"],
                           "computed_at_utc_ns": computed_at_utc_ns}, ensure_ascii=False, separators=(",", ":"))
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return path

    def iter(self, dates: Iterable[str]) -> Iterator[dict[str, Any]]:
        for d in dates:
            path = self.dir / f"{d}.jsonl"
            if not path.exists():
                continue
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        rec = json.loads(line)
                        if rec.get("v") != SCHEMA:
                            raise ValueError(f"unsupported snapshot schema in {path}")
                        yield rec
