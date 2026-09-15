"""局域网只读状态页（ADR-014）。

只绑定指定接口的 IPv4 私网地址，不监听 0.0.0.0 / IPv6；来源地址不在该子网内直接拒绝。
只处理 GET /（HTML）与 GET /health.json。
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from qdii.apps.relative_snapshot import pair_text
from qdii.io import host

log = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")


def _fmt(utc_ns: int | None) -> str:
    if not utc_ns:
        return "—"
    t = datetime.fromtimestamp(utc_ns / 1e9, tz=UTC)
    bj = t.astimezone(SHANGHAI).strftime("%m-%d %H:%M:%S")
    local = t.astimezone().strftime("%H:%M:%S %Z")  # 仅展示用
    return f"{bj} 北京 / {local}"


MODE_CN = {"CURRENT": "当前比较（连续交易，按卖一价，买入口径）", "CLOSING_REFERENCE": "收盘/午间参考（按最新价/收盘价，不代表当前可交易）"}
PAIR_CN = {"ROBUST_DIFFERENCE": "超出情景边界", "UNRESOLVED": "未能区分", "MODEL_REFERENCE": "仅模型参考"}
FRESH_CN = {"CURRENT": "新", "RECENT": "较新", "AGING": "变旧", "STALE": "过期", "UNKNOWN": "未知", "NOT_APPLICABLE": "—"}


def _pct(x: float | None, digits: int = 2) -> str:
    return "—" if x is None else f"{x * 100:+.{digits}f}%"


def render_relative(rel: dict[str, Any] | None) -> str:
    """首屏（SRD §4、FR02/FR03/FR12）：排名、卖一及可见数量、相对价差与判断、净值日期；技术细节折叠在详情中。"""
    e = html.escape
    if not rel:
        return "<h2>相对比较</h2><p>未启用（缺少基金配置）。</p>"
    if "error" in rel:
        return f"<h2>相对比较</h2><p class='bad'>计算失败：{e(rel['error'])}</p>"
    q = rel["quality"]
    rows = []
    for r in rel["rows"]:
        nxt = r.get("to_next")
        judge = (f"比 {e(nxt['next'])} 便宜 {abs(nxt['delta']) * 100:.2f}%：<b>{PAIR_CN.get(nxt['status'], nxt['status'])}</b>"
                 f"<br><span class='small'>{e(pair_text(nxt))}</span>"
                 if nxt else ("—" if r["eligible"] else "未参与比较"))
        premium = r["nav_premium"]
        prem_txt = "—" if premium is None else (f"溢价 {premium * 100:.2f}%" if premium >= 0 else f"折价 {-premium * 100:.2f}%")
        prem_txt += f"<br><span class='small'>最新价 {e(str(r['last_price'] or '—'))}</span>"
        age_txt = "—" if r["age_s"] is None else f"{r['age_s']:.0f}s"
        details = (f"<details><summary>详情</summary><div class='small'>"
                   f"单位净值 {e(str(r['nav']))}（{e(str(r['nav_date']))}）；快照时间 {_fmt(r['quote_time_utc_ns'])}；"
                   f"年龄 {age_txt}；"
                   f"原因 {e(', '.join(r['reasons']) or '无')}；行情消息 {e(str(r['snapshot_msg_id']))}；"
                   f"净值消息 {e(str(r['nav_msg_id']))}</div></details>")
        rows.append(
            f"<tr class='{'' if r['eligible'] else 'muted'}'><td>{r['rank'] or '—'}</td>"
            f"<td><b>{e(r['code'])}</b><br><span class='small'>{e(r['name'])}</span></td>"
            f"<td>{e(str(r['price'] or '—'))}<br><span class='small'>{e(str(r['volume'] or ''))}</span></td>"
            f"<td>{_pct(r['rel_to_best'])}</td><td>{judge}</td><td>{prem_txt}</td>"
            f"<td>{e(str(r['nav_date'] or '—'))}</td><td>{FRESH_CN.get(r['freshness'], r['freshness'])}</td>"
            f"<td>{details}</td></tr>")
    reasons = ", ".join(rel["reasons"]) or "无"
    persisted = ("本页输入包按请求时刻即时生成，<b>未写入快照库</b>；"
                 f"<a href='/relative/bundle.json'>下载本页完整输入包</a>（含成对边界与来源），"
                 f"或用 <code>qdii relative --save</code> 保存决策快照。最近持久化快照："
                 f"{e(str(rel.get('last_persisted_bundle_id') or '无'))}")
    return f"""<h2>相对比较 · {e(MODE_CN.get(rel['mode'], rel['mode']))}</h2>
<p class="notice">{e(rel['scope_notice'])}</p>
<p>知识截止 {_fmt(rel['cutoff_utc_ns'])}；快照 {_fmt(rel['tau_utc_ns'])}；比较状态 <b>{e(rel['status'])}</b>；
锚点距今 {rel['sessions_since_anchor']} 个交易日（{e(q['anchor_health'])}）；机会提醒 {'允许' if rel['opportunity_alert_allowed'] else '关闭'}</p>
<div class="wrap"><table>
<tr><th>排名</th><th>基金</th><th>价格<br><span class='small'>卖一量</span></th><th>相对最便宜</th><th>与下一名</th>
<th>官方净值对照<br><span class='small'>最新价/已披露净值</span></th><th>净值日</th><th>新鲜度</th><th></th></tr>{''.join(rows)}</table></div>
<p class="small">相对价差 = (价格/单位净值) 之比 − 1，不是绝对溢价百分点；"官方净值对照"用的是已披露的旧净值，不是估算净值。
模型 M0 满仓假设（{e(q['provenance_confidence'])}，{e(q['model_status'])}）；延迟 {e(q['delay_status'])}；原因 {e(reasons)}。</p>
<p class="small">成对判断的"情景边界" = 日终历史差分 P95×√交易日数 + 报价错位情景（VM-11 波动假设）+ 净值舍入；
未经盘中实测，"超出情景边界"只表示超出该声明边界，不是统计显著性。</p>
<details><summary>输入包与版本</summary><div class="small">{persisted}<br>bundle_id {e(rel['bundle_id'])}<br>
{e(json.dumps(rel['versions'], ensure_ascii=False))}<br>{e(', '.join(rel['notes']))}</div></details>"""


def render_html(snap: dict[str, Any]) -> str:
    e = html.escape
    warn = "".join(f"<li>{e(w)}</li>" for w in snap["warnings"]) or "<li>无</li>"
    host_info = snap["host"] or {}
    ep_rows = "".join(
        f"<tr class='{'bad' if ep['blocked'] or ep['consecutive_failures'] else ''}'>"
        f"<td>{e(ep['id'])}</td><td>{ep['interval_now_s'] if ep['interval_now_s'] is not None else '停'}</td>"
        f"<td>{ep['last_status'] or e(str(ep['last_error'] or '—'))}</td>"
        f"<td>{_fmt(ep['last_ok_utc_ns'])}</td><td>{ep['last_rtt_ms'] or '—'}</td>"
        f"<td>{ep['ok']}/{ep['fail']}</td><td>{e(ep['blocked'] or '')}</td></tr>"
        for ep in snap["endpoints"]
    )

    def side(d: dict[str, Any] | None) -> str:
        if not d:
            return "—"
        if d["state"] == "VALID":
            return f"{d['price']} × {d['volume']}"
        return f"{d['state']} {' '.join(d['reasons'])}"

    etf_rows = "".join(
        f"<tr><td>{e(sym)}</td><td>{row.get('last') or '—'}</td><td>{e(side(row.get('bid')))}</td>"
        f"<td>{e(side(row.get('ask')))}</td><td>{_fmt(row.get('provider_utc_ns'))}</td>"
        f"<td>{_fmt(row.get('received_utc_ns'))}</td></tr>"
        for sym, row in sorted(snap["etf"].items())
    )
    events = "".join(
        f"<tr><td>{_fmt(ev['utc_ns'])}</td><td>{e(ev['type'])}</td>"
        f"<td>{e(json.dumps({k: v for k, v in ev.items() if k not in ('utc_ns', 'type')}, ensure_ascii=False))}</td></tr>"
        for ev in reversed(snap["events_recent"])
    )
    return f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="15">
<title>纳指100 QDII ETF 相对比较</title>
<style>body{{font:14px -apple-system,sans-serif;margin:16px;color:#222}}table{{border-collapse:collapse;margin:8px 0 20px}}
td,th{{border:1px solid #ddd;padding:4px 8px;text-align:left;white-space:nowrap}}th{{background:#f4f4f4}}
.bad td{{background:#fff1f0}}.wrap{{overflow-x:auto}}h2{{font-size:16px;margin-top:20px}}
.small{{font-size:12px;color:#666;white-space:normal}}.notice{{background:#fff8e1;padding:8px;border-left:3px solid #f0a000}}.muted td{{color:#999}}details{{white-space:normal}}</style></head><body>
<h1 style="font-size:18px">纳指100 QDII ETF 相对比较</h1>
{render_relative(snap.get('relative'))}
<h2>采集状态</h2>
<p>运行 {e(snap['run_id'])}；启动 {_fmt(snap['started_utc_ns'])}；心跳 {_fmt(snap['last_heartbeat_utc_ns'])}</p>
<p>采集窗口：{'进行中' if snap['window']['active'] else '未开'}；
{_fmt(snap['window'].get('start_utc_ns'))} → {_fmt(snap['window'].get('end_utc_ns'))}</p>
<h2>告警</h2><ul>{warn}</ul>
<h2>主机</h2><p>电源 {e(str(host_info.get('power_source')))}；NTP 偏移 {host_info.get('ntp_offset_ms')} ms；
磁盘余量 {host_info.get('disk_free_gb')} GB；防睡眠 {host_info.get('sleep_assertion_alive')}；检查于 {_fmt(host_info.get('checked_utc_ns'))}</p>
<h2>ETF（新浪，只做结构解析，未做质量判定）</h2><div class="wrap"><table>
<tr><th>代码</th><th>最新</th><th>买一</th><th>卖一</th><th>供应商时间（未验证）</th><th>接收</th></tr>{etf_rows}</table></div>
<h2>端点</h2><div class="wrap"><table>
<tr><th>端点</th><th>当前间隔 s</th><th>最近状态</th><th>最近成功</th><th>RTT ms</th><th>成功/失败</th><th>封禁</th></tr>{ep_rows}</table></div>
<p>解析问题计数：{e(json.dumps(snap['parse_issues'], ensure_ascii=False))}</p>
<h2>最近事件</h2><div class="wrap"><table><tr><th>时间</th><th>类型</th><th>详情</th></tr>{events}</table></div>
</body></html>"""


async def serve_status(
    snapshot: Callable[[], dict[str, Any]],
    *,
    interface: str,
    port: int,
    stop: asyncio.Event,
    lan_enabled: bool = False,
) -> None:
    """本机回环始终提供（应用防火墙不拦回环）；局域网 IPv4 仅在 lan_enabled 时绑定，地址变化时重绑。"""
    loopback = await asyncio.start_server(
        _handler(snapshot, ipaddress.ip_network("127.0.0.0/8")), host="127.0.0.1", port=port
    )
    log.info("status page on http://127.0.0.1:%d", port)
    try:
        if not lan_enabled:
            await stop.wait()
        while lan_enabled and not stop.is_set():
            lan = await asyncio.to_thread(host.lan_ipv4, interface)
            if lan is None:
                log.warning("status: no private IPv4 on %s; retry in 60s", interface)
                await _wait(stop, 60)
                continue
            ip, cidr = lan
            try:
                server = await asyncio.start_server(
                    _handler(snapshot, ipaddress.ip_network(cidr)), host=ip, port=port
                )
            except OSError as exc:  # 端口占用等：局域网监听失败不得影响回环状态页
                log.warning("status: cannot bind %s:%d (%s); retry in 60s", ip, port, exc)
                await _wait(stop, 60)
                continue
            log.info("status page on http://%s:%d (subnet %s)", ip, port, cidr)
            try:
                while not stop.is_set():  # 每 5 分钟检查地址是否变化（DHCP）
                    await _wait(stop, 300)
                    if await asyncio.to_thread(host.lan_ipv4, interface) != lan:
                        log.info("status: LAN address changed, rebinding")
                        break
            finally:
                server.close()
                await server.wait_closed()
    finally:
        loopback.close()
        await loopback.wait_closed()


def _handler(snapshot: Callable[[], dict[str, Any]], allowed: ipaddress.IPv4Network | ipaddress.IPv6Network):
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            peer = writer.get_extra_info("peername")
            if not peer or ipaddress.ip_address(peer[0]) not in allowed:
                await _respond(writer, 403, "text/plain", b"forbidden")
                return
            request_line = (await asyncio.wait_for(reader.readline(), 5)).decode("latin-1").split()
            while (await asyncio.wait_for(reader.readline(), 5)) not in (b"\r\n", b"\n", b""):
                pass
            if len(request_line) < 2 or request_line[0] != "GET":
                await _respond(writer, 405, "text/plain", b"method not allowed")
            elif request_line[1] == "/health.json":
                body = json.dumps(snapshot(), ensure_ascii=False, default=str).encode()
                await _respond(writer, 200, "application/json; charset=utf-8", body)
            elif request_line[1] == "/relative/bundle.json":
                body = json.dumps(snapshot().get("relative_bundle"), ensure_ascii=False, default=str).encode()
                await _respond(writer, 200, "application/json; charset=utf-8", body)
            elif request_line[1] == "/relative.json":
                body = json.dumps(snapshot().get("relative"), ensure_ascii=False, default=str).encode()
                await _respond(writer, 200, "application/json; charset=utf-8", body)
            elif request_line[1] == "/":
                await _respond(writer, 200, "text/html; charset=utf-8", render_html(snapshot()).encode())
            else:
                await _respond(writer, 404, "text/plain", b"not found")
        except (TimeoutError, OSError, UnicodeDecodeError, ValueError):
            pass  # 客户端断开、防火墙切断等；状态页故障不得影响采集
        finally:
            writer.close()

    return handle


async def _respond(writer: asyncio.StreamWriter, code: int, ctype: str, body: bytes) -> None:
    reason = {200: "OK", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed"}[code]
    head = (f"HTTP/1.1 {code} {reason}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
            "Cache-Control: no-store\r\nConnection: close\r\n\r\n").encode()
    writer.write(head + body)
    await writer.drain()


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass
