"""采集进程运行状态：供状态页读取，并追加写入事件日志。"""

from __future__ import annotations

import json
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from qdii.core.types import MarketQuote, ParseResult, PriceType, QuoteSide
from qdii.io.host import HostFacts
from qdii.io.http import BlockList, EndpointState
from qdii.pipeline.windows import HostWindow


class EventLog:
    """<data_root>/events/collector.jsonl：启动、停止、缺口、封禁、主机告警等。"""

    def __init__(self, root: Path, keep_recent: int = 50) -> None:
        self.path = root / "events" / "collector.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.recent: deque[dict[str, Any]] = deque(maxlen=keep_recent)

    def emit(self, kind: str, **detail: Any) -> None:
        event = {"utc_ns": time.time_ns(), "type": kind, **detail}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        self.recent.append(event)


@dataclass
class Health:
    run_id: str
    started_utc_ns: int
    blocklist: BlockList
    events: EventLog
    endpoints: dict[str, EndpointState] = field(default_factory=dict)
    intervals_now: dict[str, float | None] = field(default_factory=dict)
    host: HostFacts | None = None
    host_checked_utc_ns: int | None = None
    last_heartbeat_utc_ns: int | None = None
    etf_view: dict[str, dict[str, Any]] = field(default_factory=dict)
    parse_issues: Counter[str] = field(default_factory=Counter)
    thresholds: dict[str, float] = field(default_factory=dict)
    lan_status_enabled: bool = False
    host_window: HostWindow | None = None
    window_active: bool = False
    in_window_gap_s: float = 0.0

    def absorb_parse(self, result: ParseResult) -> None:
        for issue in result.issues:
            self.parse_issues[issue.code.value] += 1
        for rec in result.records:
            if isinstance(rec, MarketQuote) and rec.price_type is PriceType.LAST:
                row = self.etf_view.setdefault(rec.symbol, {})
                row["last"] = str(rec.value) if rec.value is not None else None
                row["provider_utc_ns"] = rec.provider_time.utc_ns
                row["received_utc_ns"] = rec.received_utc_ns
            elif isinstance(rec, QuoteSide):
                row = self.etf_view.setdefault(rec.symbol, {})
                row[rec.side.value.lower()] = {
                    "state": rec.state.value,
                    "price": str(rec.price) if rec.price is not None else None,
                    "volume": str(rec.volume) if rec.volume is not None else None,
                    "reasons": [c.value for c in rec.reason_codes],
                }

    def warnings(self, now_utc_ns: int) -> list[str]:
        out = []
        if self.blocklist.entries:
            out.append(f"端点已封禁：{', '.join(sorted(self.blocklist.entries))}")
        if self.in_window_gap_s > 0:
            out.append(f"本次运行窗口内缺口累计 {self.in_window_gap_s:.0f} 秒（详见事件 COLLECTOR_GAP）")
        if not self.window_active:
            return out  # 窗口外允许主机睡眠、不采集，主机类告警不适用
        h = self.host
        if h is None:
            out.append("主机状态尚未探测")
        else:
            if h.power_source != "AC Power":
                out.append(f"电源：{h.power_source or '未知'}（采集期间应接电源）")
            if not h.sleep_assertion_alive:
                out.append("防睡眠断言未生效")
            if self.lan_status_enabled and h.firewall_blocks_lan:
                out.append("macOS 防火墙处于“阻止所有传入连接”模式：局域网无法访问状态页（本机 127.0.0.1 可用）")
            if h.ntp_offset_ms is None:
                out.append("CLOCK_UNVERIFIED：无法取得 NTP 偏移")
            elif abs(h.ntp_offset_ms) > self.thresholds.get("max_clock_offset_ms", 2000):
                out.append(f"CLOCK_SKEW：偏移 {h.ntp_offset_ms:.0f} ms")
            if h.disk_free_gb < self.thresholds.get("min_disk_free_gb", 20):
                out.append(f"磁盘余量 {h.disk_free_gb:.1f} GB")
        if self.last_heartbeat_utc_ns and now_utc_ns - self.last_heartbeat_utc_ns > 90e9:
            out.append("心跳超过 90 秒未更新")
        return out

    def _window_info(self, now_utc_ns: int) -> dict[str, Any]:
        if self.host_window is None:
            return {"configured": False, "active": True}
        start, end = self.host_window.next_bounds(now_utc_ns)
        return {"configured": True, "active": self.window_active, "start_utc_ns": start, "end_utc_ns": end}

    def snapshot(self, now_utc_ns: int) -> dict[str, Any]:
        return {
            "now_utc_ns": now_utc_ns,
            "run_id": self.run_id,
            "started_utc_ns": self.started_utc_ns,
            "last_heartbeat_utc_ns": self.last_heartbeat_utc_ns,
            "warnings": self.warnings(now_utc_ns),
            "window": self._window_info(now_utc_ns),
            "host": None if self.host is None else {
                "power_source": self.host.power_source,
                "ntp_offset_ms": self.host.ntp_offset_ms,
                "disk_free_gb": round(self.host.disk_free_gb, 1),
                "sleep_assertion_alive": self.host.sleep_assertion_alive,
                "firewall_blocks_lan": self.host.firewall_blocks_lan,
                "checked_utc_ns": self.host_checked_utc_ns,
            },
            "endpoints": [
                {
                    "id": eid,
                    "interval_now_s": self.intervals_now.get(eid),
                    "blocked": self.blocklist.entries.get(eid),
                    "last_status": st.last_status,
                    "last_error": st.last_error,
                    "last_ok_utc_ns": st.last_ok_utc_ns,
                    "last_attempt_utc_ns": st.last_attempt_utc_ns,
                    "last_rtt_ms": st.last_rtt_ms,
                    "ok": st.ok_count,
                    "fail": st.fail_count,
                    "consecutive_failures": st.consecutive_failures,
                }
                for eid, st in sorted(self.endpoints.items())
            ],
            "etf": self.etf_view,
            "parse_issues": dict(self.parse_issues),
            "events_recent": list(self.events.recent),
        }
