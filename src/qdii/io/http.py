"""HTTP 采集（DS-07 / DS-08）。

- 每次请求（包括失败）都产出 RawMessage，失败率是 PH0-04 的证据。
- 超时、网络错误、5xx：按 5→15→30→60 秒退避。
- 403 / 429：立即把端点标记为 BLOCKED 并持久化；只有维护者执行 unblock 才恢复，不轮换身份。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from qdii.core.types import ClockStatus, RawMessage, RequestTemplate
from qdii.io.clock import Clock

BACKOFF_S = (5, 15, 30, 60)
BLOCKING_STATUSES = frozenset({403, 429})
KEPT_RESPONSE_HEADERS = ("age", "cache-control", "content-encoding", "content-type", "date", "etag",
                         "last-modified", "location", "server")


@dataclass(frozen=True)
class EndpointRequest:
    endpoint_id: str
    source_id: str
    method: str
    url: str
    headers: tuple[tuple[str, str], ...]
    body: bytes = b""


@dataclass
class EndpointState:
    consecutive_failures: int = 0
    blocked_reason: str | None = None
    last_status: int | None = None
    last_error: str | None = None
    last_ok_utc_ns: int | None = None
    last_attempt_utc_ns: int | None = None
    last_rtt_ms: float | None = None
    ok_count: int = 0
    fail_count: int = 0

    def backoff_s(self) -> float:
        if self.consecutive_failures == 0:
            return 0.0
        return BACKOFF_S[min(self.consecutive_failures, len(BACKOFF_S)) - 1]

    def record(self, msg: RawMessage) -> None:
        self.last_status, self.last_error = msg.status, msg.error
        self.last_attempt_utc_ns, self.last_rtt_ms = msg.received_utc_ns, msg.rtt_ms
        if msg.status in BLOCKING_STATUSES:
            self.blocked_reason = f"HTTP {msg.status} at {msg.received_utc_ns}"
            self.fail_count += 1
        elif msg.error is not None or msg.status is None or msg.status >= 400:
            self.consecutive_failures += 1
            self.fail_count += 1
        else:
            self.consecutive_failures = 0
            self.ok_count += 1
            self.last_ok_utc_ns = msg.received_utc_ns


@dataclass
class BlockList:
    """持久化的封禁端点表：<data_root>/state/blocked.json。"""

    path: Path
    entries: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> BlockList:
        entries = json.loads(path.read_text()) if path.exists() else {}
        return cls(path, entries)

    def block(self, endpoint_id: str, reason: str) -> None:
        self.entries[endpoint_id] = reason
        self._save()

    def unblock(self, endpoint_id: str) -> bool:
        removed = self.entries.pop(endpoint_id, None) is not None
        self._save()
        return removed

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.entries, ensure_ascii=False, indent=2))
        tmp.replace(self.path)


class Fetcher:
    def __init__(self, client: httpx.AsyncClient, clock: Clock, run_id: str) -> None:
        self.client = client
        self.clock = clock
        self.run_id = run_id
        self.clock_status = ClockStatus.unverified()

    async def fetch(self, req: EndpointRequest, seq: int) -> RawMessage:
        t0 = self.clock.monotonic_ns()
        status: int | None = None
        error: str | None = None
        body = b""
        final_url: str | None = None
        redirects: tuple[tuple[int, str], ...] = ()
        headers: tuple[tuple[str, str], ...] = ()
        try:
            resp = await self.client.request(
                req.method, req.url, headers=dict(req.headers), content=req.body or None
            )
            status, body, final_url = resp.status_code, resp.content, str(resp.url)
            redirects = tuple((h.status_code, h.headers.get("location", "")) for h in resp.history)
            headers = tuple((k, resp.headers[k]) for k in KEPT_RESPONSE_HEADERS if k in resp.headers)
        except httpx.HTTPError as exc:
            error = f"{type(exc).__name__}: {exc}"
        t1 = self.clock.monotonic_ns()
        received = self.clock.now_utc_ns()
        return RawMessage(
            msg_id=f"{req.source_id}:{received}:{seq}",
            run_id=self.run_id,
            source_id=req.source_id,
            endpoint_id=req.endpoint_id,
            request=RequestTemplate(req.method, req.url, tuple(sorted(req.headers)), req.body),
            status=status,
            error=error,
            body=body,
            body_sha256=hashlib.sha256(body).hexdigest(),
            received_utc_ns=received,
            monotonic_ns=t1,
            rtt_ms=round((t1 - t0) / 1e6, 1),
            clock=self.clock_status,
            final_url=final_url,
            redirects=redirects,
            response_headers=headers,
        )
