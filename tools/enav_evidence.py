"""四审 D3：真实来源贯通的估算净值成功样本，并用独立代码从原始报文复算。

性质（必须如实标注）：
- 行情（新浪 ETF、hf_NQ）、净值（东方财富）、即期汇率（CFETS）来自生产原始日志；
- 指数收盘（Nasdaq）与中间价（CCPR）来自 2026-09-15 研究抓取（RESEARCH-20260915T072333Z，北京 15:23 收到），
  端点 ID 由 research.* 映射到生产 ID；**不是生产采集器当时写入的快照**。
样本：
- AS_OF_REFERENCE：截止北京 15:30。所有输入的 received_at 均不晚于截止时刻，as-of 真实；收盘参考模式，状态应为 REFERENCE。
- BACKFILLED_CURRENT：截止北京 14:50（连续交易）。指数与中间价实际在截止之后才收到，此处把接收时间改写为 09:00，
  **仅用于验证当前估算链的计算，不代表当时已知信息**，状态应为 PROXY_ANCHOR。

独立复算不调用任何 qdii 解析器或估值函数：直接按字段位置/JSON 键读取原始报文，用 Decimal 计算。

用法：uv run python tools/enav_evidence.py
输出：~/qdii-data/evidence/enav-2026-09-15/（快照，仓库外）与 reports/mvp/evidence/enav-2026-09-15.json（摘要）。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from qdii.apps.relative_snapshot import new_state, relevant
from qdii.core.relative_bundle import canonical_json, evaluate_bundle, snapshot_to_dict
from qdii.io import rawlog
from qdii.io.snapshot_store import SnapshotStore

REPO = Path(__file__).resolve().parents[1]
DATA = Path.home() / "qdii-data"
SH = ZoneInfo("Asia/Shanghai")
DAY = "2026-09-15"
REMAP = {"research.nasdaq.NDX": "nasdaq.ndx_history", "research.chinamoney.ccpr": "chinamoney.ccpr"}
BACKFILL_RECEIVED = int(datetime(2026, 9, 15, 9, 0, tzinfo=SH).timestamp() * 1e9)
SAMPLES = {
    "AS_OF_REFERENCE": (int(datetime(2026, 9, 15, 15, 30, tzinfo=SH).timestamp() * 1e9), False),
    "BACKFILLED_CURRENT": (int(datetime(2026, 9, 15, 14, 50, tzinfo=SH).timestamp() * 1e9), True),
}


def messages(cutoff: int, backfill: bool) -> list:
    out = []
    for m in rawlog.iter_messages(DATA / "research", dates=[DAY]):
        if m.endpoint_id in REMAP:
            m = replace(m, endpoint_id=REMAP[m.endpoint_id])
            if backfill:
                m = replace(m, received_utc_ns=BACKFILL_RECEIVED)
            if m.received_utc_ns <= cutoff:
                out.append(m)
    out += [m for m in rawlog.iter_messages(DATA, dates=["2026-09-14", DAY], end_utc_ns=cutoff + 1)
            if relevant(m.endpoint_id)]
    return sorted(out, key=lambda m: (m.received_utc_ns, m.msg_id))


# ---------- 独立复算：直接读原始报文 ----------

def _sina_line(body: bytes, prefix: str) -> list[str]:
    m = re.search(rf'var hq_str_{re.escape(prefix)}="(.*?)";', body.decode("gb18030"))
    assert m, prefix
    return m.group(1).split(",")


def recompute(member: dict, bundle: dict, by_id: dict) -> dict:
    code = member["code"]
    exch = "sh" if code.startswith("5") else "sz"
    etf = _sina_line(by_id[member["snapshot_msg_id"]].body, f"{exch}{code}")
    price = Decimal(etf[21] if bundle["price_basis"] == "ASK" else etf[3])

    nav_rows = json.loads(by_id[member["nav_msg_id"]].body)["Data"]["LSJZList"]
    n0 = Decimal(next(r["DWJZ"] for r in nav_rows if r["FSRQ"] == member["nav_date"]))

    idx_msg_a, fix_msg = (by_id[i] for i in member["anchor_factor_msg_ids"])

    def ndx(msg, iso: str) -> Decimal:
        mdy = f"{iso[5:7]}/{iso[8:10]}/{iso[:4]}"
        rows = json.loads(msg.body)["data"]["tradesTable"]["rows"]
        return Decimal(next(r["close"] for r in rows if r["date"] == mdy).replace(",", ""))

    ia = ndx(idx_msg_a, member["index_date"])
    ic = ndx(by_id[bundle["us_close_msg_id"]], bundle["us_close_date"])
    fix_doc = json.loads(fix_msg.body)
    x0 = Decimal(next(r["values"][0] for r in fix_doc["records"] if r["date"] == member["fx_date"]))

    hf = _sina_line(by_id[member["futures_msg_id"]].body, "hf_NQ")
    ft, fc = (Decimal(hf[2]) + Decimal(hf[3])) / 2, Decimal(hf[7])
    cf = json.loads(by_id[member["fx_msg_id"]].body)
    usd = next(r for r in cf["records"] if r["ccyPair"] == "USD/CNY")
    xt = (Decimal(usd["bidPrc"]) + Decimal(usd["askPrc"])) / 2

    enav = n0 * ic / ia * ft / fc * xt / x0
    return {"price": str(price), "N0": str(n0), "I_a": str(ia), "a": member["index_date"], "I_c": str(ic),
            "c": bundle["us_close_date"], "X0": str(x0), "X0_date": member["fx_date"], "F_t": str(ft),
            "F_c_prev_settle": str(fc), "hf_quote_time_bj": f"{hf[12]} {hf[6]}", "X_t": str(xt),
            "cfets_time_bj": cf["data"]["showDateCN"], "enav": f"{enav:.10f}", "premium": f"{price / enav - 1:.10f}",
            "basis": f"{fc / ic - 1:.10f}"}


def main() -> None:
    out = DATA / "evidence" / f"enav-{DAY}"
    if out.exists():
        shutil.rmtree(out)
    store = SnapshotStore(out)
    summary: dict = {"nature": __doc__.split("用法")[0].strip(), "generated_utc": datetime.now(ZoneInfo("UTC")).isoformat(
        timespec="seconds"), "evidence_dir": str(out), "samples": {}}
    for name, (cutoff, backfill) in SAMPLES.items():
        msgs = messages(cutoff, backfill)
        state = new_state(REPO)
        for m in msgs:
            state.ingest(m)
        bundle = state.bundle(cutoff)
        snap = evaluate_bundle(bundle)
        store.append(canonical_json(bundle), snapshot_to_dict(snap), 0)
        plain = json.loads(canonical_json(bundle))
        by_id = {m.msg_id: m for m in msgs}
        members = []
        for spec, res in zip(plain["members"], snap.enav, strict=True):
            row = {"code": spec["code"], "status": res.status, "reasons": [r.value for r in res.reasons],
                   "enav": res.enav, "premium": res.premium}
            if res.enav is not None:
                indep = recompute(spec, plain, by_id)
                row["independent"] = indep
                row["match"] = (abs(float(indep["enav"]) / res.enav - 1) < 1e-9
                                and abs(float(indep["premium"]) - res.premium) < 1e-9
                                and indep["a"] == spec["index_date"] and indep["c"] == plain["us_close_date"])
            members.append(row)
        summary["samples"][name] = {
            "cutoff_bj": datetime.fromtimestamp(cutoff / 1e9, tz=SH).isoformat(), "mode": bundle.mode,
            "backfilled_factor_receive_time": backfill, "bundle_id": snap.bundle_id,
            "bundle_sha256": hashlib.sha256(canonical_json(bundle).encode()).hexdigest(), "members": members,
        }
        print(name, bundle.mode, [(r["code"], r["status"], r.get("match")) for r in members])
    path = REPO / "reports" / "mvp" / "evidence" / f"enav-{DAY}.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(path)


if __name__ == "__main__":
    main()
