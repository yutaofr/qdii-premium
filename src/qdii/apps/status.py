"""局域网只读状态页（ADR-014）。

只绑定指定接口的 IPv4 私网地址，不监听 0.0.0.0 / IPv6；来源地址不在该子网内直接拒绝。
只处理 GET：/（HTML）、/health.json、/relative.json、/relative/bundle/<bundle_id>.json。

二审 F4：页面展示的输入包按 bundle_id 放入有界缓存，下载链接取回的正是该包，不按新请求时刻重新构包。
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import logging
import re
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from qdii.apps.relative_snapshot import DECISION_NOTICE, RELATIVE_NOTICE, pair_text
from qdii.io import host

log = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")
BUNDLE_PATH = re.compile(r"^/relative/bundle/([0-9a-f]{64})\.json$")


class BundleCache:
    """最近展示过的完整输入包（bundle_id → 完整材料）。只存于内存，超出容量按最早展示淘汰。"""

    def __init__(self, capacity: int = 64) -> None:
        self.capacity = capacity
        self._items: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def put(self, material: dict[str, Any]) -> str:
        bid = material["bundle_id"]
        self._items[bid] = material
        self._items.move_to_end(bid)
        while len(self._items) > self.capacity:
            self._items.popitem(last=False)
        return bid

    def get(self, bid: str) -> dict[str, Any] | None:
        return self._items.get(bid)


def _fmt(utc_ns: int | None) -> str:
    if not utc_ns:
        return "—"
    t = datetime.fromtimestamp(utc_ns / 1e9, tz=UTC)
    bj = t.astimezone(SHANGHAI).strftime("%m-%d %H:%M:%S")
    local = t.astimezone().strftime("%H:%M:%S %Z")  # 仅展示用
    return f"{bj} 北京 / {local}"


MODE_CN = {"CURRENT": "当前比较（连续交易，按卖一价，买入口径）", "CLOSING_REFERENCE": "收盘/午间参考（按最新价/收盘价，不代表当前可交易）"}
PAIR_CN = {"ROBUST_DIFFERENCE": "超出情景边界", "UNRESOLVED": "未能区分", "MODEL_REFERENCE": "仅模型参考"}
ENAV_CN = {"PROXY_ANCHOR": "盘中代理估算（结算日未验证，误差未验证）", "REFERENCE": "收盘参考估算（非当前）"}
DECISION_CN = {"UNVALIDATED_ERROR": "误差未验证，不能确认是否满足买入条件", "REFERENCE_ONLY": "只有历史参考估算",
               "NO_ESTIMATE": "无估算"}
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
        x = r.get("enav") or {}
        if x.get("premium") is not None:
            p = x["premium"]
            label = ENAV_CN.get(x["status"], x["status"])
            strong = ("b", "b") if x["status"] == "PROXY_ANCHOR" else ("span class='muted'", "span")
            roll = "；<b>换月窗口</b>" if "ROLL_WINDOW" in x["reasons"] else ""
            roll += f"；合约 {e(str(rel.get('futures_contract') or '—'))}"
            enav_cell = (f"<{strong[0]}>{'溢价' if p >= 0 else '折价'} {abs(p) * 100:.2f}%</{strong[1]}>"
                         f"<br><span class='small'>{label}；估算净值 {x['value']:.4f}{roll}</span>")
        else:
            enav_cell = f"—<br><span class='small'>{e(', '.join(x.get('reasons', [])) or '未计算')}</span>"
        enav_detail = ("" if x.get("value") is None else
                       f"估算：指数 {(x['index_move'] - 1) * 100:+.2f}% × 期货 {(x['futures_move'] - 1) * 100:+.2f}%"
                       f"（{e(str(x['futures_mid']))} / 昨结算 {e(str(x['futures_settle']))}，基差 {x['basis'] * 1e4:.1f}bp，"
                       f"样本距报价 {x['futures_age_s']:.0f}s）× 汇率 {(x['fx_move'] - 1) * 100:+.2f}%"
                       f"（即期 {e(str(x['fx_spot']))}，{x['fx_age_s']:.0f}s）；原因 {e(', '.join(x['reasons']))}；")
        details = (f"<details><summary>详情</summary><div class='small'>"
                   f"{enav_detail}单位净值 {e(str(r['nav']))}（{e(str(r['nav_date']))}）；快照时间 {_fmt(r['quote_time_utc_ns'])}；"
                   f"年龄 {age_txt}；"
                   f"锚点后 {r['anchor_sessions']} 个交易日（{e(str(r['anchor_health']))}）；"
                   f"因子日期 指数 {e(str(r['index_date'] or '—'))} / 中间价 {e(str(r['fx_date'] or '—'))}；"
                   f"原因 {e(', '.join(r['reasons']) or '无')}；行情消息 {e(str(r['snapshot_msg_id']))}；"
                   f"净值消息 {e(str(r['nav_msg_id']))}</div></details>")
        rows.append(
            f"<tr class='{'' if r['eligible'] else 'muted'}'><td>{r['rank'] or '—'}</td>"
            f"<td><b>{e(r['code'])}</b><br><span class='small'>{e(r['name'])}</span></td>"
            f"<td>{e(str(r['price'] or '—'))}<br><span class='small'>{e(str(r['volume'] or ''))}</span></td>"
            f"<td>{enav_cell}</td><td>{_pct(r['rel_to_best'])}</td><td>{judge}</td><td>{prem_txt}</td>"
            f"<td>{e(str(r['nav_date'] or '—'))}</td><td>{FRESH_CN.get(r['freshness'], r['freshness'])}</td>"
            f"<td>{details}</td></tr>")
    reasons = ", ".join(rel["reasons"]) or "无"
    bid = e(rel["bundle_id"])
    persisted = ("本页输入包按请求时刻即时生成，<b>未写入快照库</b>；"
                 f"<a href='/relative/bundle/{bid}.json'>下载本页完整输入包</a>（含成对边界与来源；"
                 "内存中保留最近展示的输入包，采集器重启或被较新页面挤出后链接失效），"
                 "或用 <code>qdii relative --save</code> 另存一个新的决策快照。最近持久化快照："
                 f"{e(str(rel.get('last_persisted_bundle_id') or '无'))}")
    decision = rel.get("absolute_decision_status", "NO_ESTIMATE")
    bound = rel.get("absolute_error_bound_bp")
    decision_txt = (f"绝对买入判断：<b>{e(DECISION_CN.get(decision, decision))}</b>（{e(decision)}；"
                    f"有估算 {rel.get('estimate_available_count', 0)}/{rel.get('member_count', 0)} 只；"
                    f"误差界限 {'—' if bound is None else e(str(bound))}）。{e(DECISION_NOTICE)}")
    return f"""<h2>估算溢价与相对比较 · {e(MODE_CN.get(rel['mode'], rel['mode']))}</h2>
<p class="notice">{decision_txt}</p>
<p class="notice">{e(rel['scope_notice'])}</p>
<p>知识截止 {_fmt(rel['cutoff_utc_ns'])}；快照 {_fmt(rel['tau_utc_ns'])}；比较状态 <b>{e(rel['status'])}</b>；
参与比较成员锚点最旧 {rel['max_anchor_sessions']} 个交易日（{e(q['anchor_health'])}）；机会提醒 {'允许' if rel['opportunity_alert_allowed'] else '关闭'}</p>
<div class="wrap"><table>
<tr><th>排名</th><th>基金</th><th>价格<br><span class='small'>卖一量</span></th><th>估算溢价<br><span class='small'>昨结算代理锚点，结算日与误差均未验证</span></th><th>相对最便宜</th><th>与下一名</th>
<th>官方净值对照<br><span class='small'>最新价/已披露净值</span></th><th>净值日</th><th>新鲜度</th><th></th></tr>{''.join(rows)}</table></div>
<p class="small">{e(RELATIVE_NOTICE)}相对价差 = (价格/单位净值) 之比 − 1，不是绝对溢价百分点；"官方净值对照"用的是已披露的旧净值，不是估算净值。
模型 M0 满仓假设（{e(q['provenance_confidence'])}，{e(q['model_status'])}）；延迟 {e(q['delay_status'])}；原因 {e(reasons)}。</p>
<p class="small">成对判断的"情景边界" = 日终历史差分 P95×√交易日数 + 报价错位情景（VM-11 波动假设）+ 净值舍入；
未经盘中实测，"超出情景边界"只表示超出该声明边界，不是统计显著性。</p>
<details><summary>输入包与版本</summary><div class="small">{persisted}<br>bundle_id {e(rel['bundle_id'])}<br>
{e(json.dumps(rel['versions'], ensure_ascii=False))}<br>{e(', '.join(rel['notes']))}</div></details>"""


ANCHOR_CN = {"READY": "已捕获", "DEGRADED": "降级（距收盘 30—60 秒）", "FAILED": "失败（距收盘超过 60 秒）",
             "MISSING": "漏采"}


def render_anchors(anchors: dict[str, Any] | None) -> str:
    """美股收盘期货锚点（DS-10：界面持续显示最近成功 c、合约身份、缺口）。"""
    e = html.escape
    if not anchors:
        return ""
    nxt = anchors.get("next_window") or {}
    nxt_txt = ("日历不确定" if "error" in nxt else
               f"收盘 {_fmt(nxt.get('close_utc_ns'))}；采集窗口 {_fmt(nxt.get('start_utc_ns'))} → {_fmt(nxt.get('end_utc_ns'))}"
               if nxt else "—")
    row_list = []
    for a in reversed(anchors.get("recent") or []):
        lag_txt = "—" if a["lag_s"] is None else f"{a['lag_s']:.0f}s"
        row_list.append(
            f"<tr class='{'' if a['status'] == 'READY' else 'bad'}'><td>{_fmt(a['c_utc_ns'])}</td>"
            f"<td>{e(ANCHOR_CN.get(a['status'], a['status']))}</td><td>{e(str(a['value'] or '—'))}</td>"
            f"<td>{lag_txt}</td><td>{a['candidates']}</td><td>{'是' if a['roll_window'] else '否'}</td>"
            f"<td class='small'>{e(', '.join(a['reason_codes']))}</td></tr>")
    rows = "".join(row_list)
    return f"""<h2>美股收盘期货锚点（新浪 hf_NQ，合约月份未知）</h2>
<p>下一次：{nxt_txt}。窗口期间请保持 Mac 开盖联网。</p>
<div class="wrap"><table><tr><th>美股收盘 c</th><th>状态</th><th>买卖中间价</th><th>距 c</th><th>候选样本</th>
<th>换月窗口</th><th>原因</th></tr>{rows or "<tr><td colspan='7'>尚无记录</td></tr>"}</table></div>
<p class="small">锚点用于后续绝对估算（E 路径）；合约月份未知（CONTRACT_UNKNOWN），换月窗口内的锚点不能跨序列使用。</p>"""


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
{render_anchors(snap.get('anchors'))}
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
    bundles: BundleCache | None = None,
) -> None:
    """本机回环始终提供（应用防火墙不拦回环）；局域网 IPv4 仅在 lan_enabled 时绑定，地址变化时重绑。"""
    loopback = await asyncio.start_server(
        _handler(snapshot, ipaddress.ip_network("127.0.0.0/8"), bundles), host="127.0.0.1", port=port
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
                    _handler(snapshot, ipaddress.ip_network(cidr), bundles), host=ip, port=port
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


def _handler(snapshot: Callable[[], dict[str, Any]], allowed: ipaddress.IPv4Network | ipaddress.IPv6Network,
             bundles: BundleCache | None = None):
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
            elif (match := BUNDLE_PATH.match(request_line[1])) is not None:
                material = bundles.get(match.group(1)) if bundles is not None else None
                if material is None:  # 未展示过或已被淘汰：不重新构包冒充（二审 F4）
                    await _respond(writer, 404, "text/plain; charset=utf-8",
                                   b"bundle not cached; reload the page or use `qdii relative --save`")
                else:
                    body = json.dumps(material, ensure_ascii=False, default=str).encode()
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
