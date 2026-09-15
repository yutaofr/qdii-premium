"""读取 config/sources.toml → 端点请求与采集计划。"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from qdii.io.http import EndpointRequest
from qdii.pipeline.windows import WEEKDAYS, Schedule, Window, parse_hhmm


@dataclass(frozen=True)
class EndpointConfig:
    request: EndpointRequest
    schedule: Schedule
    contract: str | None  # 已注册的解析器版本；None = 只录制不解析
    enabled: bool


def load_sources(path: Path) -> tuple[str, float, list[EndpointConfig]]:
    doc = tomllib.loads(path.read_text(encoding="utf-8"))
    defaults = doc.get("defaults", {})
    user_agent = defaults["user_agent"]
    timeout_s = float(defaults.get("timeout_s", 15))
    endpoints = []
    seen: set[str] = set()
    for item in doc.get("endpoint", []):
        endpoint_id = item["id"]
        if endpoint_id in seen:
            raise ValueError(f"duplicate endpoint id {endpoint_id}")
        seen.add(endpoint_id)
        windows = tuple(
            Window(
                tz=w["tz"],
                start=parse_hhmm(w["start"]),
                end=parse_hhmm(w["end"]),
                interval_s=float(w["interval_s"]),
                weekdays=frozenset(w.get("weekdays", sorted(WEEKDAYS))),
            )
            for w in item.get("windows", [])
        )
        headers = {"User-Agent": user_agent, **item.get("headers", {})}
        endpoints.append(EndpointConfig(
            request=EndpointRequest(
                endpoint_id=endpoint_id,
                source_id=item["source_id"],
                method=item.get("method", "GET"),
                url=item["url"],
                headers=tuple(sorted(headers.items())),
            ),
            schedule=Schedule(windows, item.get("idle_interval_s")),
            contract=item.get("contract"),
            enabled=item.get("enabled", True),
        ))
    return user_agent, timeout_s, endpoints
