"""期货锚点库：只追加 JSONL，<root>/anchors/futures/<美东日期>.jsonl（FuturesAnchor，DS-11）。

每个 (series_id, c) 只写一次；MISSING / FAILED 也写入，使漏采可见（AT75）。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Iterator
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from qdii.core.anchor import AnchorResult

SCHEMA = 1
NEW_YORK = ZoneInfo("America/New_York")


class AnchorStore:
    def __init__(self, root: Path) -> None:
        self.dir = root / "anchors" / "futures"

    def path_for(self, c_utc_ns: int) -> Path:
        return self.dir / f"{datetime.fromtimestamp(c_utc_ns / 1e9, tz=NEW_YORK).date().isoformat()}.jsonl"

    def append(self, result: AnchorResult, computed_at_utc_ns: int, origin: str = "LIVE") -> Path:
        path = self.path_for(result.c_utc_ns)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"v": SCHEMA, "origin": origin, "anchor": asdict(result),
                           "computed_at_utc_ns": computed_at_utc_ns}, ensure_ascii=False, separators=(",", ":"))
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return path

    def iter(self, et_dates: Iterable[str] | None = None) -> Iterator[dict[str, Any]]:
        paths = ([self.dir / f"{d}.jsonl" for d in et_dates] if et_dates is not None
                 else sorted(self.dir.glob("*.jsonl")))
        for path in paths:
            if not path.exists():
                continue
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        rec = json.loads(line)
                        if rec.get("v") != SCHEMA:
                            raise ValueError(f"unsupported anchor schema in {path}")
                        yield rec

    def done(self, series_id: str) -> set[int]:
        return {r["anchor"]["c_utc_ns"] for r in self.iter() if r["anchor"]["series_id"] == series_id}

    def recent(self, n: int = 5) -> list[dict[str, Any]]:
        records = list(self.iter())
        return sorted(records, key=lambda r: r["anchor"]["c_utc_ns"])[-n:]
