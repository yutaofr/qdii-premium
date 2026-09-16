"""读取 config/sources.toml → 端点请求与采集计划。"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from qdii.core.anchor import third_friday
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


def render_request(req: EndpointRequest, now_utc_ns: int) -> EndpointRequest:
    """URL 占位符在请求时刻填入；原始日志记录填入后的实际 URL，回放不依赖渲染时刻。

    - {from_date}（30 天前）、{to_date}（今天），按 UTC 日期；
    - {nq_quarterlies}：最近两个季月 NQ 合约代码（如 hf_NQ2609,hf_NQ2612），按美东日期计算，
      使估算能固定在身份已知的单一合约上（DS-09，D4 专项 F1）。
    """
    if "{" not in req.url:
        return req
    today = datetime.fromtimestamp(now_utc_ns / 1e9, tz=UTC).date()
    url = req.url.replace("{to_date}", today.isoformat()).replace(
        "{from_date}", (today - timedelta(days=30)).isoformat())
    if "{nq_quarterlies}" in url:
        url = url.replace("{nq_quarterlies}", ",".join(f"hf_{c}" for c in nq_quarterlies(today)))
    return replace(req, url=url)


def nq_quarterlies(day: date, count: int = 2) -> list[str]:
    """不早于该日期到期的最近 count 个季月合约代码（NQYYMM）。"""
    out = []
    year, month = day.year, ((day.month - 1) // 3) * 3 + 3
    while len(out) < count:
        if third_friday(year, month) >= day:
            out.append(f"NQ{year % 100:02d}{month:02d}")
        month += 3
        if month > 12:
            year, month = year + 1, 3
    return out
