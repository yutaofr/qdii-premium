"""采集守护进程（ADD-0 §4.1、§15、§16）：录制原始报文、维护状态，并在线生成相对比较快照（MVP）。

主机可用窗口（HostWindow）之外不发任何请求、不持有防睡眠断言，主机可以正常睡眠；
窗口外的缺口记为 OFF_WINDOW_GAP，窗口内的缺口才是 COLLECTOR_GAP。
每批新浪 ETF 行情到达时，以该消息 received_at 为知识截止构建不可变输入包并计算相对比较（ADR-010），
快照只追加写入 snapshots/relative/（ADR-018）；状态页首屏按请求时刻即时计算（窗口外为收盘参考）。
"""

from __future__ import annotations

import asyncio
import errno
import itertools
import json
import logging
import logging.handlers
import os
import signal
import subprocess
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from qdii.apps.health import EventLog, Health
from qdii.apps.relative_snapshot import full_bundle, new_state, relevant, replay_into, view
from qdii.apps.status import BundleCache, serve_status
from qdii.contracts.registry import get_parser
from qdii.core.relative_bundle import canonical_json, evaluate_bundle, snapshot_to_dict
from qdii.core.types import ClockStatus, RawMessage
from qdii.io import host, rawlog
from qdii.io.anchor_store import AnchorStore
from qdii.io.calendars import CalendarProvider
from qdii.io.clock import Clock, SystemClock
from qdii.io.http import BlockList, EndpointState, Fetcher
from qdii.io.snapshot_store import SnapshotStore
from qdii.pipeline.anchor_capture import HF_ENDPOINT, AnchorTracker
from qdii.pipeline.host_windows import AvailabilityWindow, CloseWindow, CompositeWindow
from qdii.pipeline.relative_state import ETF_ENDPOINT, RelativeState
from qdii.pipeline.sources import EndpointConfig, load_sources, render_request
from qdii.pipeline.windows import WEEKDAYS, HostWindow, parse_hhmm

log = logging.getLogger("qdii.collect")

# 所有等待都不超过该秒数再重读墙钟：macOS 睡眠期间单调钟可能不前进，长等待会在唤醒后迟到
MAX_WAIT_S = 15.0


@dataclass(frozen=True)
class CollectorConfig:
    data_root: Path
    run_label: str
    heartbeat_interval_s: float
    gap_threshold_s: float
    host_probe_interval_s: float
    min_disk_free_gb: float
    max_clock_offset_ms: float
    status_enabled: bool
    status_interface: str
    status_port: int
    status_lan_enabled: bool = False
    host_window: AvailabilityWindow | None = None  # None = 全天可用；可为 A 股窗口与美股收盘窗口的组合
    host_probe_enabled: bool = True
    anchor_window: CloseWindow | None = None  # 启用时捕获美股收盘期货锚点（DS-09/10）


def load_collector_config(path: Path, data_root_override: Path | None = None) -> CollectorConfig:
    d = tomllib.loads(path.read_text(encoding="utf-8"))
    hb, hp, st, hw = d.get("heartbeat", {}), d.get("host_probe", {}), d.get("status", {}), d.get("host_window")
    window: AvailabilityWindow | None = None
    if hw is not None:
        window = HostWindow(
            start_tz=hw["start_tz"], start=parse_hhmm(hw["start"]),
            end_tz=hw["end_tz"], end=parse_hhmm(hw["end"]),
            weekdays=frozenset(hw.get("weekdays", sorted(WEEKDAYS))),
        )
    aw = d.get("anchor_window", {})
    anchor_window = None
    if aw.get("enabled", False):
        # 2026-09-15 维护者同意：按美股收盘期货锚点调整采集窗口（ADD-0 §15.3）
        anchor_window = CloseWindow(CalendarProvider(path.parent / "calendar_overrides.toml"),
                                    market=aw.get("market", "NASDAQ"), before_s=float(aw.get("before_s", 300)),
                                    after_s=float(aw.get("after_s", 180)))
        window = anchor_window if window is None else CompositeWindow((window, anchor_window))
    return CollectorConfig(
        data_root=(data_root_override or Path(d["data_root"])).expanduser(),
        run_label=d.get("run_label", "host"),
        heartbeat_interval_s=float(hb.get("interval_s", 30)),
        gap_threshold_s=float(hb.get("gap_threshold_s", 90)),
        host_probe_interval_s=float(hp.get("interval_s", 600)),
        min_disk_free_gb=float(hp.get("min_disk_free_gb", 20)),
        max_clock_offset_ms=float(hp.get("max_clock_offset_ms", 2000)),
        status_enabled=bool(st.get("enabled", True)),
        status_interface=st.get("interface", "en0"),
        status_port=int(st.get("port", 8787)),
        status_lan_enabled=bool(st.get("lan_enabled", False)),
        host_window=window,
        anchor_window=anchor_window,
    )


class DiskFull(RuntimeError):
    pass


class Collector:
    def __init__(self, cfg: CollectorConfig, endpoints: list[EndpointConfig], timeout_s: float,
                 clock: Clock | None = None, repo_root: Path | None = None,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.cfg = cfg
        self.endpoints = [ep for ep in endpoints if ep.enabled]
        self.timeout_s = timeout_s
        self.clock = clock or SystemClock()
        self.stop = asyncio.Event()
        self.window_open = asyncio.Event()
        self.seq = itertools.count()
        now = self.clock.now_utc_ns()
        stamp = datetime.fromtimestamp(now / 1e9, tz=UTC).strftime("%Y%m%dT%H%M%SZ")
        self.run_id = f"LIVE-{stamp}-{cfg.run_label}"
        root = cfg.data_root
        self.events = EventLog(root)
        self.health = Health(
            run_id=self.run_id, started_utc_ns=now,
            blocklist=BlockList.load(root / "state" / "blocked.json"), events=self.events,
            thresholds={"min_disk_free_gb": cfg.min_disk_free_gb, "max_clock_offset_ms": cfg.max_clock_offset_ms},
            lan_status_enabled=cfg.status_enabled and cfg.status_lan_enabled,
            host_window=cfg.host_window,
            anchor_window=cfg.anchor_window,
        )
        self.heartbeat_path = root / "state" / "heartbeat.json"
        self.writer: rawlog.RawLogWriter | None = None
        self.fetcher: Fetcher | None = None
        self.caffeinate: subprocess.Popen[bytes] | None = None
        self.exit_code = 0
        self.transport = transport
        self.relative: RelativeState | None = None
        self.page_bundles = BundleCache()  # 状态页展示过的完整输入包（二审 F4）
        self.names: dict[str, str] = {}
        if repo_root is not None and (repo_root / "config" / "funds.toml").exists():
            self.relative = new_state(repo_root, root, update_ledger=True)
            self.names = {f.code: f.name for f in self.relative.funds}
        self.snapshots = SnapshotStore(root)
        self.anchor_store = AnchorStore(root)
        self.anchors = AnchorTracker(cfg.anchor_window) if cfg.anchor_window is not None else None
        self.anchor_check_interval_s = 5.0  # 到期（c+2 分钟）后最多延迟该秒数评估

    # ---------- 生命周期 ----------

    async def run(self) -> int:
        root = self.cfg.data_root
        now = self.clock.now_utc_ns()
        self._detect_gap_since_last_run(now)
        for fix in rawlog.recover(root, now_utc_ns=now):
            self.events.emit("TAIL_REPAIR", path=str(fix.path), truncated_bytes=fix.truncated_bytes)
        self.writer = rawlog.RawLogWriter(root)
        if self.relative is not None:
            for err in self.relative.cal.errors:  # AT58/60：日历覆盖问题必须可见
                self.events.emit("CALENDAR_UNCERTAIN", error=err)
            n = await asyncio.to_thread(replay_into, self.relative, root, now)
            self.events.emit("RELATIVE_BOOTSTRAP", messages=n)
        if self.anchors is not None:
            n = await asyncio.to_thread(self._bootstrap_anchors, root, now)
            self.health.anchors_recent = self.anchor_store.recent()
            self.events.emit("ANCHOR_BOOTSTRAP", samples=n)
        self.events.emit("START", run_id=self.run_id, pid=os.getpid(),
                         endpoints=[ep.request.endpoint_id for ep in self.endpoints])

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.stop.set)

        limits = httpx.Limits(max_connections=8, max_keepalive_connections=4)
        # 跟随重定向（浏览器正常访问行为），最终 URL 与跳转链写入原始消息；不读取环境代理，保证访问路径固定
        async with httpx.AsyncClient(timeout=self.timeout_s, follow_redirects=True, max_redirects=5,
                                     limits=limits, trust_env=False, transport=self.transport) as client:
            self.fetcher = Fetcher(client, self.clock, self.run_id)
            tasks = [asyncio.create_task(self._window_manager(), name="window")]
            tasks += [asyncio.create_task(self._poll(ep), name=ep.request.endpoint_id) for ep in self.endpoints]
            tasks.append(asyncio.create_task(self._heartbeat(), name="heartbeat"))
            if self.anchors is not None:
                tasks.append(asyncio.create_task(self._anchor_capture(), name="anchor_capture"))
            tasks.append(asyncio.create_task(self._host_probe(), name="host_probe"))
            if self.cfg.status_enabled:
                tasks.append(asyncio.create_task(serve_status(
                    self._status_snapshot,
                    interface=self.cfg.status_interface, port=self.cfg.status_port, stop=self.stop,
                    lan_enabled=self.cfg.status_lan_enabled, bundles=self.page_bundles,
                ), name="status"))
            for t in tasks:
                t.add_done_callback(self._on_task_done)
            await self.stop.wait()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        self.writer.close()
        self._release_sleep_assertion()
        self._write_heartbeat()
        self.events.emit("STOP", run_id=self.run_id, exit_code=self.exit_code)
        return self.exit_code

    def _on_task_done(self, task: asyncio.Task[None]) -> None:
        """任何后台任务意外结束都要可见；采集相关任务结束则整体退出，交给 launchd 重启。"""
        if task.cancelled() or self.stop.is_set():
            return
        exc = task.exception()
        self.events.emit("TASK_ENDED", task=task.get_name(), error=repr(exc) if exc else None)
        log.error("task %s ended unexpectedly: %r", task.get_name(), exc)
        if task.get_name() != "status":
            self.exit_code = self.exit_code or 1
            self.stop.set()

    # ---------- 主机可用窗口 ----------

    def _in_window(self, utc_ns: int) -> bool:
        w = self.cfg.host_window
        return True if w is None else w.active(utc_ns)

    async def _window_manager(self) -> None:
        while not self.stop.is_set():
            now = self.clock.now_utc_ns()
            active = self._in_window(now)
            self.health.window_active = active
            if active and not self.window_open.is_set():
                self._hold_sleep_assertion()
                bounds = self.cfg.host_window.next_bounds(now) if self.cfg.host_window else None
                self.events.emit("WINDOW_OPEN", bounds_utc_ns=bounds)
                self.window_open.set()
            elif not active and self.window_open.is_set():
                self.window_open.clear()
                self._release_sleep_assertion()
                self.events.emit("WINDOW_CLOSE", relative_snapshots=self.health.relative_snapshots,
                                 last_bundle_id=self.health.relative_last_bundle_id,
                                 counts={eid: [s.ok_count, s.fail_count]
                                                         for eid, s in sorted(self.health.endpoints.items())})
            await self._sleep(MAX_WAIT_S if not active else 5.0)

    def _hold_sleep_assertion(self) -> None:
        if self.caffeinate is None or self.caffeinate.poll() is not None:
            self.caffeinate = host.hold_sleep_assertion()

    def _release_sleep_assertion(self) -> None:
        if self.caffeinate is not None and self.caffeinate.poll() is None:
            self.caffeinate.terminate()
        self.caffeinate = None

    async def _wait_window(self) -> bool:
        """窗口未开时短暂等待；返回当前是否在窗口内。"""
        if self.window_open.is_set() and self._in_window(self.clock.now_utc_ns()):
            return True
        await self._sleep(MAX_WAIT_S)
        return False

    # ---------- 轮询 ----------

    async def _poll(self, ep: EndpointConfig) -> None:
        req = ep.request
        state = self.health.endpoints.setdefault(req.endpoint_id, EndpointState())
        parser = get_parser(ep.contract) if ep.contract else None
        try:
            while not self.stop.is_set():
                if not await self._wait_window():
                    self.health.intervals_now[req.endpoint_id] = None
                    continue
                if req.endpoint_id in self.health.blocklist.entries:
                    self.health.intervals_now[req.endpoint_id] = None
                    await self._sleep(60)
                    continue
                interval = ep.schedule.interval_at(self.clock.now_utc_ns())
                self.health.intervals_now[req.endpoint_id] = interval
                if interval is None:
                    await self._sleep(MAX_WAIT_S)
                    continue
                started = self.clock.monotonic_ns()
                assert self.fetcher is not None
                msg = await self.fetcher.fetch(render_request(req, self.clock.now_utc_ns()), next(self.seq))
                self._write(msg)
                state.record(msg)
                if state.blocked_reason and req.endpoint_id not in self.health.blocklist.entries:
                    self.health.blocklist.block(req.endpoint_id, state.blocked_reason)
                    self.events.emit("ENDPOINT_BLOCKED", endpoint=req.endpoint_id, reason=state.blocked_reason)
                if parser is not None and msg.status == 200:
                    self.health.absorb_parse(parser(msg))
                self._on_relative_input(msg)
                if self.anchors is not None:
                    self.anchors.ingest(msg)
                delay = max(interval, state.backoff_s())
                await self._sleep_until_next(started, delay)
        except DiskFull:
            self.exit_code = 2
            self.stop.set()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 单个端点故障不拖垮进程
            log.exception("poll loop crashed: %s", req.endpoint_id)
            self.events.emit("POLL_LOOP_CRASH", endpoint=req.endpoint_id, error=repr(exc))
            self.exit_code = 1
            self.stop.set()  # 交给 launchd 重启，避免静默半残运行

    async def _sleep_until_next(self, started_mono: int, delay_s: float) -> None:
        """长间隔（如净值 900 秒）分段等待，每段后检查窗口是否已关闭。"""
        while not self.stop.is_set():
            remaining = delay_s - (self.clock.monotonic_ns() - started_mono) / 1e9
            if remaining <= 0 or not self._in_window(self.clock.now_utc_ns()):
                return
            await self._sleep(min(remaining, MAX_WAIT_S))

    # ---------- 美股收盘期货锚点（DS-09/10，VM-03） ----------

    def _bootstrap_anchors(self, root: Path, now: int) -> int:
        assert self.anchors is not None
        days = [(datetime.fromtimestamp(now / 1e9, tz=UTC).date() - timedelta(days=i)).isoformat() for i in range(4, -1, -1)]
        n = 0
        for msg in rawlog.iter_messages(root, sources=["sina"], dates=days, end_utc_ns=now + 1):
            if msg.endpoint_id == HF_ENDPOINT and self.anchors.ingest(msg):
                n += 1
        return n

    async def _anchor_capture(self) -> None:
        assert self.anchors is not None
        done = await asyncio.to_thread(self.anchor_store.done, "sina:hf_NQ")
        while not self.stop.is_set():
            for result in self.anchors.due(self.clock.now_utc_ns(), done):
                self.anchor_store.append(result, self.clock.now_utc_ns())
                done.add(result.c_utc_ns)
                self.events.emit("ANCHOR_CAPTURE", c_utc_ns=result.c_utc_ns, status=result.status, value=result.value,
                                 lag_s=result.lag_s, candidates=result.candidates, roll_window=result.roll_window,
                                 reasons=list(result.reason_codes))
                self.health.anchors_recent = self.anchor_store.recent()
            await self._sleep(self.anchor_check_interval_s)

    # ---------- 相对比较（MVP） ----------

    def _on_relative_input(self, msg: RawMessage) -> None:
        if self.relative is None or not relevant(msg.endpoint_id):
            return
        self.relative.ingest(msg)
        if msg.endpoint_id != ETF_ENDPOINT or msg.status != 200:
            return
        try:
            bundle = self.relative.bundle(msg.received_utc_ns)  # ADR-010：知识截止 = 触发消息的 received_at
            snap = evaluate_bundle(bundle)
            self.snapshots.append(canonical_json(bundle), snapshot_to_dict(snap), self.clock.now_utc_ns())
            self.health.relative_last_bundle_id = snap.bundle_id
            self.health.relative_snapshots += 1
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                self.events.emit("DISK_FULL", error=str(exc))
                raise DiskFull from exc
            raise

    def _status_snapshot(self) -> dict:
        now = self.clock.now_utc_ns()
        snap = self.health.snapshot(now)
        if self.relative is not None:
            try:
                bundle = self.relative.bundle(now)
                rel_snap = evaluate_bundle(bundle)
                snap["relative"] = view(rel_snap, bundle, self.names)
                snap["relative"]["persisted"] = False  # 页面输入包按请求时刻生成，不写入快照库（ADR-020）
                snap["relative"]["last_persisted_bundle_id"] = self.health.relative_last_bundle_id
                self.page_bundles.put(full_bundle(rel_snap, bundle))  # 二审 F4：下载链接取回本次展示的包
            except Exception as exc:  # 状态页计算失败不影响采集，但必须留下记录
                log.exception("relative view failed")
                snap["relative"] = {"error": repr(exc)}
        return snap

    def _write(self, msg: RawMessage) -> None:
        assert self.writer is not None
        try:
            self.writer.append(msg)
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                self.events.emit("DISK_FULL", error=str(exc))
                raise DiskFull from exc
            raise

    # ---------- 心跳与缺口 ----------

    def _emit_gap(self, from_ns: int, to_ns: int, reason: str, previous_run_id: str | None) -> None:
        seconds = (to_ns - from_ns) / 1e9
        w = self.cfg.host_window
        in_window = seconds if w is None else w.overlap_s(from_ns, to_ns)
        kind = "COLLECTOR_GAP" if in_window > self.cfg.gap_threshold_s else "OFF_WINDOW_GAP"
        self.events.emit(kind, from_utc_ns=from_ns, to_utc_ns=to_ns, seconds=round(seconds, 1),
                         in_window_seconds=round(in_window, 1), previous_run_id=previous_run_id, reason=reason)
        if kind == "COLLECTOR_GAP":
            self.health.in_window_gap_s += in_window

    def _detect_gap_since_last_run(self, now: int) -> None:
        if not self.heartbeat_path.exists():
            return
        try:
            last = json.loads(self.heartbeat_path.read_text())
        except ValueError:
            self.events.emit("HEARTBEAT_UNREADABLE", path=str(self.heartbeat_path))
            return
        if (now - last["utc_ns"]) / 1e9 > self.cfg.gap_threshold_s:
            self._emit_gap(last["utc_ns"], now, "PROCESS_NOT_RUNNING", last.get("run_id"))

    def _write_heartbeat(self) -> None:
        now = self.clock.now_utc_ns()
        self.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.heartbeat_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"utc_ns": now, "run_id": self.run_id, "pid": os.getpid()}))
        tmp.replace(self.heartbeat_path)
        self.health.last_heartbeat_utc_ns = now

    async def _heartbeat(self) -> None:
        interval = min(self.cfg.heartbeat_interval_s, MAX_WAIT_S * 2)
        prev = self.clock.now_utc_ns()
        self._write_heartbeat()
        while not self.stop.is_set():
            await self._sleep(interval)
            now = self.clock.now_utc_ns()
            if (now - prev) / 1e9 > interval + self.cfg.gap_threshold_s:
                # 进程还在，但墙钟跳过了一段：系统睡眠或时钟跳变
                self._emit_gap(prev, now, "SLEEP_OR_CLOCK_JUMP", self.run_id)
            prev = now
            self._write_heartbeat()

    # ---------- 主机探测（仅窗口内） ----------

    async def _host_probe(self) -> None:
        if not self.cfg.host_probe_enabled:
            await self.stop.wait()  # 禁用时保持存活：提前返回会被 _on_task_done 视为任务崩溃
            return
        last_power: str | None = None
        while not self.stop.is_set():
            if not await self._wait_window():
                continue
            power = await asyncio.to_thread(host.power_source)
            offset = await asyncio.to_thread(host.ntp_offset_ms)
            free = await asyncio.to_thread(host.disk_free_gb, self.cfg.data_root)
            fw_blocks = await asyncio.to_thread(host.firewall_blocks_all_incoming)
            alive = self.caffeinate is not None and self.caffeinate.poll() is None
            now = self.clock.now_utc_ns()
            self.health.host = host.HostFacts(power, offset, free, alive, fw_blocks)
            self.health.host_checked_utc_ns = now
            synced = None if offset is None else abs(offset) <= self.cfg.max_clock_offset_ms
            if self.fetcher is not None:
                self.fetcher.clock_status = ClockStatus(synced, offset, now)
            if power != last_power:
                self.events.emit("POWER_SOURCE", source=power)
                last_power = power
            if offset is not None and not synced:
                self.events.emit("CLOCK_SKEW", offset_ms=offset)
            if free < self.cfg.min_disk_free_gb:
                self.events.emit("DISK_LOW", free_gb=round(free, 1))
            started = self.clock.monotonic_ns()
            await self._sleep_until_next(started, self.cfg.host_probe_interval_s)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=seconds)
        except TimeoutError:
            pass


def setup_logging(data_root: Path) -> None:
    logs = data_root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(logs / "collector.log", maxBytes=20_000_000, backupCount=5)
    handler.setFormatter(logging.Formatter('{"t":"%(asctime)s","lvl":"%(levelname)s","log":"%(name)s","msg":%(message)r}'))
    # launchd 的 stdout/stderr 文件不轮转：只让 WARNING 以上进入 stderr；逐请求记录已在原始日志中，压低 httpx
    stream = logging.StreamHandler()
    stream.setLevel(logging.WARNING)
    logging.basicConfig(level=logging.INFO, handlers=[handler, stream])
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def main(config_dir: Path, data_root: Path | None = None) -> int:
    cfg = load_collector_config(config_dir / "collector.toml", data_root)
    _, timeout_s, endpoints = load_sources(config_dir / "sources.toml")
    setup_logging(cfg.data_root)
    return asyncio.run(Collector(cfg, endpoints, timeout_s, repo_root=config_dir.parent).run())
