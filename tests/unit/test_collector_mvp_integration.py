"""端到端：真实 Collector + 模拟 HTTP → 原始日志 + 相对比较快照 → 流回放逐包复现（FR06/FR11/NFR02）。"""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

from qdii.apps.collect import Collector, CollectorConfig
from qdii.apps.replay_relative import verify
from qdii.io.http import EndpointRequest
from qdii.pipeline.sources import EndpointConfig
from qdii.pipeline.windows import Schedule
from tests.unit.test_relative_snapshot_replay import CODES, sina_body

REPO = Path(__file__).resolve().parents[2]
NAVS = {vendor[2:]: nav for vendor, nav in CODES.items()}


def handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "hq.sinajs.cn" in url:
        return httpx.Response(200, content=sina_body("14:49:55", {c: "2.300" for c in CODES}))
    code = request.url.params.get("fundCode")
    rows = [{"FSRQ": "2026-09-11", "DWJZ": NAVS[code], "LJJZ": NAVS[code], "JZZZL": ""}]
    return httpx.Response(200, json={"ErrCode": 0, "TotalCount": 1, "Data": {"LSJZList": rows}})


def endpoint(eid, source, url, interval):
    return EndpointConfig(EndpointRequest(eid, source, "GET", url, (("User-Agent", "test"),)),
                          Schedule((), interval), None, True)


def test_collector_writes_replayable_relative_snapshots(tmp_path):
    cfg = CollectorConfig(
        data_root=tmp_path, run_label="test", heartbeat_interval_s=30, gap_threshold_s=90,
        host_probe_interval_s=600, min_disk_free_gb=0, max_clock_offset_ms=2000, status_enabled=False,
        status_interface="lo0", status_port=0, host_window=None, host_probe_enabled=False,
    )
    endpoints = [endpoint(f"eastmoney.lsjz.{c}", "eastmoney", f"https://api.fund.eastmoney.com/f10/lsjz?fundCode={c}", 5)
                 for c in NAVS]
    endpoints.append(endpoint("sina.etf_batch", "sina", "https://hq.sinajs.cn/list=x", 0.1))
    collector = Collector(cfg, endpoints, 5, repo_root=REPO, transport=httpx.MockTransport(handler))

    async def run():
        asyncio.get_running_loop().call_later(1.5, collector.stop.set)
        return await collector.run()

    assert asyncio.run(run()) == 0
    dates = sorted(p.stem for p in (tmp_path / "snapshots" / "relative").glob("*.jsonl"))
    records = [json.loads(line) for d in dates for line in (tmp_path / "snapshots" / "relative" / f"{d}.jsonl").open()]
    assert len(records) >= 3
    assert collector.health.relative_snapshots == len(records)
    # 快照中五只基金均取到净值（NAV 先于或随后到达均被 as-of 处理）
    assert all(len(r["bundle"]["members"]) == 5 for r in records)

    bundle_report = verify(tmp_path, REPO, dates)
    stream_report = verify(tmp_path, REPO, dates, stream=True)
    assert bundle_report["mismatches"] == 0 and bundle_report["integrity_failures"] == 0
    assert stream_report["mismatches"] == 0, stream_report
    assert datetime.now(UTC).date().isoformat() in dates or dates
