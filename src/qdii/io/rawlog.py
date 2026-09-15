"""原始日志（ADR-003，ADD-0 §5.1）。

布局：<root>/raw/<source_id>/<YYYY-MM-DD>/<HH>.jsonl（UTC）。当前小时明文追加，
轮转后压缩为 .jsonl.gz。每条写入后 flush，并按条数或时间 fsync。
崩溃后启动时把不完整的尾行截断，并返回截断信息供记录。
"""

from __future__ import annotations

import base64
import gzip
import heapq
import json
import os
import shutil
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

from qdii.core.types import ClockStatus, RawMessage, RequestTemplate

SCHEMA_VERSION = 1


# ---------- 编解码 ----------

def encode(msg: RawMessage) -> str:
    doc = {
        "v": SCHEMA_VERSION,
        "msg_id": msg.msg_id,
        "run_id": msg.run_id,
        "source_id": msg.source_id,
        "endpoint_id": msg.endpoint_id,
        "request": {
            "method": msg.request.method,
            "url": msg.request.url,
            "headers": [list(h) for h in msg.request.headers],
            "body_b64": base64.b64encode(msg.request.body).decode(),
        },
        "status": msg.status,
        "error": msg.error,
        "body_b64": base64.b64encode(msg.body).decode(),
        "body_sha256": msg.body_sha256,
        "received_utc_ns": msg.received_utc_ns,
        "monotonic_ns": msg.monotonic_ns,
        "rtt_ms": msg.rtt_ms,
        "clock": {
            "synced": msg.clock.synced,
            "offset_ms": msg.clock.offset_ms,
            "checked_utc_ns": msg.clock.checked_utc_ns,
        },
        "final_url": msg.final_url,
        "redirects": [list(r) for r in msg.redirects],
        "response_headers": [list(h) for h in msg.response_headers],
    }
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":"))


def decode(line: str) -> RawMessage:
    d = json.loads(line)
    if d.get("v") != SCHEMA_VERSION:
        raise ValueError(f"unsupported raw log schema {d.get('v')}")
    req = d["request"]
    return RawMessage(
        msg_id=d["msg_id"],
        run_id=d["run_id"],
        source_id=d["source_id"],
        endpoint_id=d["endpoint_id"],
        request=RequestTemplate(
            method=req["method"],
            url=req["url"],
            headers=tuple((k, v) for k, v in req["headers"]),
            body=base64.b64decode(req["body_b64"]),
        ),
        status=d["status"],
        error=d["error"],
        body=base64.b64decode(d["body_b64"]),
        body_sha256=d["body_sha256"],
        received_utc_ns=d["received_utc_ns"],
        monotonic_ns=d["monotonic_ns"],
        rtt_ms=d["rtt_ms"],
        clock=ClockStatus(**d["clock"]),
        final_url=d.get("final_url"),
        redirects=tuple((int(s), loc) for s, loc in d.get("redirects", [])),
        response_headers=tuple((k, v) for k, v in d.get("response_headers", [])),
    )


# ---------- 路径 ----------

def segment_path(root: Path, source_id: str, utc_ns: int) -> Path:
    t = datetime.fromtimestamp(utc_ns / 1e9, tz=UTC)
    return root / "raw" / source_id / t.strftime("%Y-%m-%d") / f"{t:%H}.jsonl"


@dataclass
class _Open:
    path: Path
    fh: IO[str]
    unsynced: int = 0
    last_sync: float = 0.0


@dataclass(frozen=True)
class TailRepair:
    path: Path
    truncated_bytes: int


class RawLogWriter:
    def __init__(self, root: Path, *, fsync_every: int = 20, fsync_interval_s: float = 1.0) -> None:
        self.root = root
        self.fsync_every = fsync_every
        self.fsync_interval_s = fsync_interval_s
        self._open: dict[str, _Open] = {}

    def append(self, msg: RawMessage) -> Path:
        if msg.is_synthetic:
            raise ValueError("synthetic messages must not be written to the live raw log")
        path = segment_path(self.root, msg.source_id, msg.received_utc_ns)
        cur = self._open.get(msg.source_id)
        if cur is None or cur.path != path:
            if cur is not None:
                self._close(cur)
                compress_segment(cur.path)
            path.parent.mkdir(parents=True, exist_ok=True)
            cur = _Open(path, path.open("a", encoding="utf-8"), last_sync=time.monotonic())
            self._open[msg.source_id] = cur
        cur.fh.write(encode(msg) + "\n")
        cur.fh.flush()
        cur.unsynced += 1
        now = time.monotonic()
        if cur.unsynced >= self.fsync_every or now - cur.last_sync >= self.fsync_interval_s:
            os.fsync(cur.fh.fileno())
            cur.unsynced, cur.last_sync = 0, now
        return path

    def close(self) -> None:
        for cur in self._open.values():
            self._close(cur)
        self._open.clear()

    @staticmethod
    def _close(cur: _Open) -> None:
        cur.fh.flush()
        os.fsync(cur.fh.fileno())
        cur.fh.close()


def compress_segment(path: Path) -> Path:
    gz = path.with_suffix(".jsonl.gz")
    with path.open("rb") as src, gzip.open(gz, "wb") as dst:
        shutil.copyfileobj(src, dst)
    path.unlink()
    return gz


def repair_tail(path: Path) -> TailRepair | None:
    """截断不完整或无法解析的最后一行；完整文件返回 None。"""
    data = path.read_bytes()
    if not data:
        return None
    end = len(data)
    if not data.endswith(b"\n"):
        end = data.rfind(b"\n") + 1
    else:
        last_start = data.rfind(b"\n", 0, len(data) - 1) + 1
        try:
            json.loads(data[last_start:])
        except ValueError:
            end = last_start
    if end == len(data):
        return None
    with path.open("r+b") as fh:
        fh.truncate(end)
    return TailRepair(path, len(data) - end)


def recover(root: Path, *, now_utc_ns: int) -> list[TailRepair]:
    """启动恢复：修复所有明文段尾行；非当前小时的明文段压缩。"""
    repairs: list[TailRepair] = []
    raw = root / "raw"
    if not raw.exists():
        return repairs
    for path in sorted(raw.glob("*/*/*.jsonl")):
        fix = repair_tail(path)
        if fix:
            repairs.append(fix)
        source_id = path.parts[-3]
        if path != segment_path(root, source_id, now_utc_ns):
            compress_segment(path)
    return repairs


# ---------- 读取 ----------

def _read_segment(path: Path) -> Iterator[RawMessage]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:  # type: ignore[operator]
        for line in fh:
            if line.strip():
                yield decode(line)


def segments(root: Path, *, sources: Iterable[str] | None = None, dates: Iterable[str] | None = None) -> list[Path]:
    raw = root / "raw"
    wanted_sources = set(sources) if sources else None
    wanted_dates = set(dates) if dates else None
    out = []
    for path in raw.glob("*/*/*.jsonl*"):
        source_id, date = path.parts[-3], path.parts[-2]
        if wanted_sources and source_id not in wanted_sources:
            continue
        if wanted_dates and date not in wanted_dates:
            continue
        out.append(path)
    return sorted(out)


def iter_messages(
    root: Path,
    *,
    sources: Iterable[str] | None = None,
    dates: Iterable[str] | None = None,
    start_utc_ns: int | None = None,
    end_utc_ns: int | None = None,
) -> Iterator[RawMessage]:
    """按 (received_utc_ns, msg_id) 全局有序输出，供流回放使用。"""
    streams = [_read_segment(p) for p in segments(root, sources=sources, dates=dates)]
    for msg in heapq.merge(*streams, key=lambda m: (m.received_utc_ns, m.msg_id)):
        if start_utc_ns is not None and msg.received_utc_ns < start_utc_ns:
            continue
        if end_utc_ns is not None and msg.received_utc_ns >= end_utc_ns:
            continue
        yield msg
