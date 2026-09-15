from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from qdii.core.types import ClockStatus, RawMessage, RequestTemplate

FIXTURES = Path(__file__).parent / "fixtures"


def make_msg(
    body: bytes,
    *,
    status: int | None = 200,
    source_id: str = "sina",
    endpoint_id: str = "sina.etf_batch",
    received_utc_ns: int = 1_789_427_546_000_000_000,
    seq: int = 0,
    run_id: str = "SYN-test",
    error: str | None = None,
) -> RawMessage:
    return RawMessage(
        msg_id=f"{source_id}:{received_utc_ns}:{seq}",
        run_id=run_id,
        source_id=source_id,
        endpoint_id=endpoint_id,
        request=RequestTemplate("GET", "https://example.invalid/"),
        status=status,
        error=error,
        body=body,
        body_sha256=hashlib.sha256(body).hexdigest(),
        received_utc_ns=received_utc_ns,
        monotonic_ns=0,
        rtt_ms=1.0,
        clock=ClockStatus.unverified(),
    )


def recorded_sina(endpoint_id: str, variant: str) -> RawMessage:
    doc = json.loads((FIXTURES / "recorded" / "sina_probe_20260914T231217Z.json").read_text(encoding="utf-8"))
    for m in doc["messages"]:
        if m["endpoint_id"] == endpoint_id and m["variant"] == variant:
            return make_msg(
                base64.b64decode(m["body_b64"]),
                status=m["status"],
                received_utc_ns=m["received_utc_ns"],
                run_id=m["run_id"],
                endpoint_id=f"sina.{endpoint_id}",
            )
    raise KeyError((endpoint_id, variant))
