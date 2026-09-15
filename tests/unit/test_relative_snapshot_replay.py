"""回放层集成测试：合成原始日志 + 真实日历 → 相对比较快照（勘误 E1 快照选择、as-of 截止）。"""

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from qdii.apps.relative_snapshot import build
from qdii.core.relative import RelativeMode, RelativeStatus
from qdii.io import rawlog
from tests.helpers import make_msg

REPO = Path(__file__).resolve().parents[2]
SH = ZoneInfo("Asia/Shanghai")
CODES = {"sh513100": "1.9831", "sz159696": "1.8461", "sz159501": "1.8627", "sz159660": "2.1668", "sh513390": "2.2262"}


def bj(h, m, s=0, day=15):
    return int(datetime(2026, 9, day, h, m, s, tzinfo=SH).timestamp() * 1e9)


def sina_body(t_str, prices):
    rows = []
    for code, px in prices.items():
        f = ["名"] + ["2.0"] * 5 + [px, px, "100", "200"] + ["1000", px] * 5 + ["1000", px] * 5
        f += ["2026-09-15", t_str, "00", ""]
        f[3], f[21] = px, px  # 最新价、卖一价
        rows.append(f'var hq_str_{code}="{",".join(f)}";')
    return "\n".join(rows).encode("gb18030")


def write(root, received_ns, t_str, prices, seq):
    msg = make_msg(sina_body(t_str, prices), received_utc_ns=received_ns, seq=seq, run_id="LIVE-test",
                   endpoint_id="sina.etf_batch")
    rawlog.RawLogWriter(root).append(msg)


def write_nav(root, received_ns, seq):
    for i, (vendor, nav) in enumerate(CODES.items()):
        code = vendor[2:]
        body = json.dumps({"ErrCode": 0, "TotalCount": 1, "Data": {"LSJZList": [
            {"FSRQ": "2026-09-11", "DWJZ": nav, "LJJZ": nav, "JZZZL": "0.87"}]}}).encode()
        from dataclasses import replace

        from qdii.core.types import RequestTemplate
        m = make_msg(body, received_utc_ns=received_ns + i, seq=seq + i, source_id="eastmoney", run_id="LIVE-test",
                     endpoint_id=f"eastmoney.lsjz.{code}")
        m = replace(m, request=RequestTemplate("GET", f"https://api.fund.eastmoney.com/f10/lsjz?fundCode={code}"))
        rawlog.RawLogWriter(root).append(m)


def setup(tmp_path):
    write_nav(tmp_path, bj(9, 0), 100)
    write(tmp_path, bj(11, 29, 57), "11:29:55", {c: "2.200" for c in CODES}, 1)
    write(tmp_path, bj(11, 33, 1), "11:30:00", {c: "2.210" for c in CODES}, 2)  # 午休起点冻结快照
    write(tmp_path, bj(14, 49, 58), "14:49:55", {**{c: "2.300" for c in CODES}, "sz159660": "2.400"}, 3)
    write(tmp_path, bj(15, 0, 6), "15:00:04", {c: "2.320" for c in CODES}, 4)
    write(tmp_path, bj(15, 35, 50), "15:35:45", {c: "9.999" for c in CODES}, 5)  # 收盘后过久，不作参考


def test_break_uses_frozen_1130_snapshot(tmp_path):
    setup(tmp_path)
    g, _ = build(tmp_path, REPO, bj(12, 0))
    assert g.mode is RelativeMode.CLOSING_REFERENCE and g.status is RelativeStatus.MODEL_REFERENCE
    assert {str(m.price) for m in g.members} == {"2.210"}


def test_current_uses_latest_ask_and_as_of_cutoff(tmp_path):
    setup(tmp_path)
    g, _ = build(tmp_path, REPO, bj(14, 50))
    assert g.mode is RelativeMode.CURRENT and g.price_basis == "ASK"
    assert next(m for m in g.members if m.code == "159660").price is not None
    assert all(str(m.price) in {"2.300", "2.400"} for m in g.members)  # 15:00 之后的快照不可见


def test_after_close_uses_close_snapshot_not_late_one(tmp_path):
    setup(tmp_path)
    g, _ = build(tmp_path, REPO, bj(16, 0))
    assert {str(m.price) for m in g.members} == {"2.320"}


def test_nav_not_yet_received_makes_group_ineligible(tmp_path):
    write(tmp_path, bj(14, 49, 58), "14:49:55", {c: "2.300" for c in CODES}, 3)
    write_nav(tmp_path, bj(20, 0), 100)  # 净值晚于截止时刻才收到
    g, _ = build(tmp_path, REPO, bj(14, 50))
    assert g.status is RelativeStatus.INELIGIBLE
