"""期货锚点回放校验：从原始日志按序 ingest，在每条锚点记录的截止时刻重算并比较（FR11/NFR02）。"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from qdii.core.relative_bundle import diff_plain, to_plain
from qdii.io import rawlog
from qdii.io.anchor_store import AnchorStore
from qdii.io.calendars import CalendarProvider
from qdii.pipeline.anchor_capture import HF_ENDPOINT, AnchorTracker
from qdii.pipeline.host_windows import CloseWindow


def verify(data_root: Path, repo: Path, et_dates: list[str]) -> dict[str, Any]:
    records = sorted(AnchorStore(data_root).iter(et_dates), key=lambda r: r["anchor"]["c_utc_ns"])
    report: dict[str, Any] = {"dates": et_dates, "anchors": len(records), "verified": 0, "mismatches": 0,
                              "examples": [], "status": "NO_DATA"}
    if not records:
        return report
    tracker = AnchorTracker(CloseWindow(CalendarProvider(repo / "config" / "calendar_overrides.toml")))
    first = min(date.fromisoformat(d) for d in et_dates) - timedelta(days=1)
    last = max(date.fromisoformat(d) for d in et_dates) + timedelta(days=2)
    span = [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]
    messages = iter(rawlog.iter_messages(data_root, sources=["sina"], dates=span))
    pending = next(messages, None)
    for rec in records:
        c = rec["anchor"]["c_utc_ns"]
        cutoff = rec["anchor"]["cutoff_utc_ns"]
        while pending is not None and pending.received_utc_ns <= cutoff:
            if pending.endpoint_id == HF_ENDPOINT:
                tracker.ingest(pending)
            pending = next(messages, None)
        diffs = diff_plain(to_plain(asdict(tracker.evaluate(c))), rec["anchor"])
        if diffs:
            report["mismatches"] += 1
            if len(report["examples"]) < 5:
                report["examples"].append({"c_utc_ns": c, "diffs": diffs[:5]})
        else:
            report["verified"] += 1
    report["status"] = "PASSED" if report["mismatches"] == 0 else "FAILED"
    return report
