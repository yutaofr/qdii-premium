"""PH0-08 最小可达性探测（ADD-0 §8.3 / §13 第一天上午）。

只用标准库；逐端点串行、间隔请求；响应原样保存为 RawMessage 形态的 JSONL，
另生成 summary.md。响应体只作为数据解析，不执行任何返回的脚本（NFR08）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import platform
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ETF_CODES = ["sh513100", "sz159696", "sz159501", "sz159660", "sh513390"]
USER_AGENT = "qdii-premium-probe/0.1 (personal research)"
TIMEOUT_S = 15
PAUSE_S = 1.5

Check = Callable[[bytes], tuple[bool, str]]


@dataclass
class Endpoint:
    source_id: str
    endpoint_id: str
    variant: str
    url: str
    check: Check
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    data: bytes | None = None


# ---------- 字段检查：只判断结构是否完整，不做业务结论 ----------

def _sina_vars(body: bytes) -> dict[str, str]:
    text = body.decode("gb18030", errors="replace")
    return dict(re.findall(r'var hq_str_(\w+)="(.*?)";', text))


def check_sina_etf(body: bytes) -> tuple[bool, str]:
    rows = _sina_vars(body)
    notes, ok = [], True
    for code in ETF_CODES:
        f = rows.get(code, "").split(",")
        good = (len(f) >= 32 and f[0] != ""
                and re.fullmatch(r"\d{4}-\d{2}-\d{2}", f[30] or "") is not None
                and re.fullmatch(r"\d{2}:\d{2}:\d{2}", f[31] or "") is not None)
        ok &= good
        notes.append(f"{code}:n={len(f)},last={f[3] if len(f) > 3 else '?'},"
                     f"t={f[30] if len(f) > 31 else '?'} {f[31] if len(f) > 31 else ''}")
    return ok, "; ".join(notes)


def check_tencent_etf(body: bytes) -> tuple[bool, str]:
    text = body.decode("gbk", errors="replace")
    rows = dict(re.findall(r'v_(\w+)="(.*?)";', text))
    notes, ok = [], True
    for code in ETF_CODES:
        f = rows.get(code, "").split("~")
        good = len(f) > 30 and f[3] != ""
        ok &= good
        notes.append(f"{code}:n={len(f)},last={f[3] if len(f) > 3 else '?'},"
                     f"t={f[30] if len(f) > 30 else '?'}")
    return ok, "; ".join(notes)


def check_em_stock(body: bytes) -> tuple[bool, str]:
    try:
        d = json.loads(body).get("data") or {}
    except ValueError:
        return False, "非JSON"
    ok = all(k in d for k in ("f43", "f57", "f59", "f86"))
    return ok, f"f43={d.get('f43')},f59={d.get('f59')},f60={d.get('f60')},f86={d.get('f86')}"


def check_em_lsjz(body: bytes) -> tuple[bool, str]:
    try:
        rows = (json.loads(body).get("Data") or {}).get("LSJZList") or []
    except ValueError:
        return False, "非JSON"
    if not rows:
        return False, "LSJZList为空或缺失"
    r = rows[0]
    ok = all(r.get(k) not in (None, "") for k in ("FSRQ", "DWJZ", "LJJZ"))
    return ok, f"rows={len(rows)},FSRQ={r.get('FSRQ')},DWJZ={r.get('DWJZ')},LJJZ={r.get('LJJZ')}"


def check_sina_hf(body: bytes) -> tuple[bool, str]:
    f = _sina_vars(body).get("hf_NQ", "").split(",")
    ok = len(f) >= 14 and f[0] != ""
    if not ok:
        return False, f"n={len(f)}"
    return True, f"n={len(f)},price={f[0]},bid={f[2]},ask={f[3]},time={f[6]},date={f[12]},name={f[13]}"


def check_sina_fx(body: bytes) -> tuple[bool, str]:
    rows = _sina_vars(body)
    notes, ok = [], True
    for code in ("fx_susdcny", "fx_susdcnh"):
        f = rows.get(code, "").split(",")
        good = len(f) >= 10 and f[0] != ""
        ok &= good
        notes.append(f"{code}:n={len(f)},head={','.join(f[:4])}")
    return ok, "; ".join(notes)


def check_cfets(body: bytes) -> tuple[bool, str]:
    try:
        d = json.loads(body)
    except ValueError:
        return False, "非JSON"
    recs = d.get("records") or []
    usd = [r for r in recs if r.get("ccyPair") == "USD/CNY"]
    if not usd:
        return False, f"records={len(recs)},无USD/CNY"
    r = usd[0]
    ok = r.get("bidPrc") not in (None, "", "---") and r.get("askPrc") not in (None, "", "---")
    upd = (d.get("data") or {}).get("lastDate")
    return ok, f"USD/CNY bid={r.get('bidPrc')},ask={r.get('askPrc')},time={r.get('time')},lastDate={upd}"


def check_sina_ndx_static(body: bytes) -> tuple[bool, str]:
    text = body.decode("utf-8", errors="replace")
    m = re.search(r'=\s*"([^"]*)"', text)
    if not m:
        return False, f"len={len(body)},未见赋值字符串"
    return True, f"len={len(body)},编码负载{len(m.group(1))}字符（需解码器，未执行）"


def check_html(keyword: str) -> Check:
    def _check(body: bytes) -> tuple[bool, str]:
        for enc in ("utf-8", "gb18030"):
            text = body.decode(enc, errors="ignore")
            if keyword in text:
                return True, f"len={len(body)},含“{keyword}”({enc})"
        return False, f"len={len(body)},未见“{keyword}”"
    return _check


SINA_REFERER = {"Referer": "https://finance.sina.com.cn/"}
ETF_LIST = ",".join(ETF_CODES)
CFETS_URL = "https://www.chinamoney.com.cn/r/cms/www/chinamoney/data/fx/rfx-sp-quot.json"

ENDPOINTS = [
    Endpoint("sina", "etf_batch", "no_referer", f"https://hq.sinajs.cn/list={ETF_LIST}", check_sina_etf),
    Endpoint("sina", "etf_batch", "referer", f"https://hq.sinajs.cn/list={ETF_LIST}", check_sina_etf,
             headers=SINA_REFERER),
    Endpoint("tencent", "etf_batch", "default", f"https://qt.gtimg.cn/q={ETF_LIST}", check_tencent_etf),
    Endpoint("eastmoney", "stock_get", "default",
             "https://push2.eastmoney.com/api/qt/stock/get?secid=1.513100&fields=f43,f57,f58,f59,f60,f86",
             check_em_stock),
    Endpoint("eastmoney", "fund_lsjz", "referer",
             "https://api.fund.eastmoney.com/f10/lsjz?fundCode=513100&pageIndex=1&pageSize=30",
             check_em_lsjz, headers={"Referer": "https://fundf10.eastmoney.com/"}),
    Endpoint("sina", "hf_NQ", "no_referer", "https://hq.sinajs.cn/list=hf_NQ", check_sina_hf),
    Endpoint("sina", "hf_NQ", "referer", "https://hq.sinajs.cn/list=hf_NQ", check_sina_hf, headers=SINA_REFERER),
    Endpoint("sina", "fx_spot", "no_referer", "https://hq.sinajs.cn/list=fx_susdcny,fx_susdcnh", check_sina_fx),
    Endpoint("sina", "fx_spot", "referer", "https://hq.sinajs.cn/list=fx_susdcny,fx_susdcnh", check_sina_fx,
             headers=SINA_REFERER),
    Endpoint("cfets", "fx_spot_quot", "GET", CFETS_URL, check_cfets),
    Endpoint("cfets", "fx_spot_quot", "POST", CFETS_URL, check_cfets, method="POST", data=b""),
    Endpoint("sina", "ndx_history", "default", "https://finance.sina.com.cn/staticdata/us/.NDX",
             check_sina_ndx_static),
    Endpoint("safe", "rmb_midrate_page", "default", "https://www.safe.gov.cn/safe/rmbhlzjj/index.html",
             check_html("中间价")),
    Endpoint("efunds", "fund_page_159696", "default", "https://www.efunds.com.cn/Mobile/fund/159696.shtml",
             check_html("159696")),
]


def fetch(ep: Endpoint) -> dict:
    headers = {"User-Agent": USER_AGENT, **ep.headers}
    req = urllib.request.Request(ep.url, data=ep.data, headers=headers, method=ep.method)
    t0 = time.monotonic_ns()
    status, error, body, resp_headers = None, None, b"", {}
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            status, body = resp.status, resp.read()
            resp_headers = dict(resp.headers.items())
    except urllib.error.HTTPError as e:
        status, body, resp_headers = e.code, e.read() or b"", dict(e.headers.items())
    except Exception as e:  # 网络层失败同样是证据
        error = f"{type(e).__name__}: {e}"
    t1 = time.monotonic_ns()
    received = time.time_ns()
    keep = {k: v for k, v in resp_headers.items()
            if k.lower() in ("content-type", "content-length", "date", "server", "last-modified")}
    return {
        "request": {"method": ep.method, "url": ep.url, "headers": headers},
        "status": status, "error": error, "resp_headers": keep,
        "body_b64": base64.b64encode(body).decode(), "body_sha256": hashlib.sha256(body).hexdigest(),
        "body_len": len(body), "received_utc_ns": received, "monotonic_ns": t1,
        "rtt_ms": round((t1 - t0) / 1e6, 1), "_body": body,
    }


def host_facts() -> dict:
    def run(cmd: list[str]) -> str:
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception as e:
            return f"ERR {e}"
    return {
        "utc_now": datetime.now(UTC).isoformat(),
        "local_tz": time.tzname, "python": sys.version.split()[0],
        "macos": platform.mac_ver()[0],
        "pmset": run(["pmset", "-g"]),
        "hardware_model": run(["sysctl", "-n", "hw.model"]),
        "sntp_offset": run(["sntp", "-t", "5", "time.apple.com"]),
    }


def main() -> None:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"PROBE-{stamp}-home-mac"
    out = Path(__file__).resolve().parents[1] / "reports" / "phase0" / "reach" / stamp
    out.mkdir(parents=True, exist_ok=True)
    # 第三方原始响应与主机信息不入库（公开仓库；DS-08 录制许可）：写到仓库外，报告里只留哈希
    evidence = Path.home() / "qdii-data" / "evidence" / "phase0" / "reach" / stamp
    evidence.mkdir(parents=True, exist_ok=True)

    lines = ["| 来源 | 端点 | 变体 | HTTP | 字节 | RTT ms | 字段完整 | 摘要 |", "|---|---|---|---|---|---|---|---|"]
    with (evidence / "raw.jsonl").open("w", encoding="utf-8") as raw:
        for seq, ep in enumerate(ENDPOINTS):
            rec = fetch(ep)
            body = rec.pop("_body")
            if rec["status"] == 200 and body:
                ok, note = ep.check(body)
            else:
                ok, note = False, rec["error"] or body[:120].decode("utf-8", errors="replace").replace("\n", " ")
            rec.update({
                "msg_id": f"{ep.source_id}:{rec['received_utc_ns']}:{seq}", "run_id": run_id,
                "source_id": ep.source_id, "endpoint_id": ep.endpoint_id, "variant": ep.variant,
                "fields_ok": ok, "check_note": note,
            })
            raw.write(json.dumps(rec, ensure_ascii=False) + "\n")
            raw.flush()
            note_md = note.replace("|", "/")
            lines.append(f"| {ep.source_id} | {ep.endpoint_id} | {ep.variant} | {rec['status'] or '—'} | "
                         f"{rec['body_len']} | {rec['rtt_ms']} | {'✅' if ok else '❌'} | {note_md} |")
            print(lines[-1], flush=True)
            time.sleep(PAUSE_S)

    facts = host_facts()
    (evidence / "host.json").write_text(json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8")
    digests = {name: hashlib.sha256((evidence / name).read_bytes()).hexdigest() for name in ("raw.jsonl", "host.json")}
    summary = [f"# 可达性探测 {run_id}", "", f"UTC {facts['utc_now']}", "", *lines, "",
               f"原始响应与主机信息保存在仓库外 `~/qdii-data/evidence/phase0/reach/{stamp}/`，"
               f"SHA-256：raw.jsonl `{digests['raw.jsonl']}`，host.json `{digests['host.json']}`。", "",
               "字段完整只表示结构可解析，不代表数据新鲜、实时或语义已核验。", ""]
    (out / "summary.md").write_text("\n".join(summary), encoding="utf-8")
    print(f"\n输出目录: {out}")


if __name__ == "__main__":
    main()
