"""产品披露（2026-09-18 期货基差复审）："有估算"与"通过绝对决策验证"分开，未知界限用 null。

第一阶段不存在 absolute_decision_eligible = true 的路径：亚洲决策时点的期货代理误差未识别。
页面、命令行、机器 JSON 与下载材料显示同一状态；原快照与输入包内容、哈希不变。
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from qdii.apps.relative_snapshot import (
    DECISION_NOTICE,
    decision_disclosure,
    full_bundle,
    render,
    to_json,
    view,
)
from qdii.apps.status import render_html
from qdii.core.relative_bundle import (
    bundle_from_dict,
    bundle_id,
    canonical_json,
    evaluate_bundle,
    snapshot_to_dict,
)
from tests.unit.test_enav import enav_state, fx, hf
from tests.unit.test_relative_bundle_state import bj, factor_msgs, fed_state

FIELDS = ("estimate_available", "absolute_decision_eligible", "absolute_decision_status", "absolute_error_bound_bp",
          "decision_limitation_codes")


def page(rel: dict) -> str:
    return render_html({"run_id": "t", "started_utc_ns": 0, "last_heartbeat_utc_ns": None, "warnings": [],
                        "window": {"active": True}, "host": None, "endpoints": [], "etf": {}, "parse_issues": {},
                        "events_recent": [], "relative": rel})


def current():
    st = enav_state()
    b = st.bundle(bj(14, 50))
    return evaluate_bundle(b), b, {f.code: f.name for f in st.funds}


def test_proxy_estimate_is_available_but_not_decision_eligible():
    snap, b, names = current()
    v = view(snap, b, names)
    for r in v["rows"]:
        assert {k: r[k] for k in FIELDS} == {
            "estimate_available": True, "absolute_decision_eligible": False,
            "absolute_decision_status": "UNVALIDATED_ERROR", "absolute_error_bound_bp": None,
            "decision_limitation_codes": ["ASIA_FUTURES_ERROR_UNIDENTIFIED"]}
    assert v["estimate_available"] is True and v["estimate_available_count"] == 5 and v["member_count"] == 5
    assert v["absolute_decision_eligible"] is False and v["absolute_decision_status"] == "UNVALIDATED_ERROR"
    assert v["absolute_error_bound_bp"] is None
    assert v["absolute_premium_available"] is v["estimate_available"]  # 兼容别名，只表示存在代理估算


def test_one_member_with_an_estimate_does_not_make_the_group_decidable():
    from qdii.apps.relative_snapshot import new_state
    from tests.unit.test_relative_bundle_state import REPO, etf_msg, nav_msg
    from tests.unit.test_relative_snapshot_replay import CODES

    st = new_state(REPO)
    for i, (vendor, nav) in enumerate(CODES.items()):
        st.ingest(nav_msg(vendor[2:], [{"FSRQ": "2026-09-11", "DWJZ": nav, "LJJZ": nav, "JZZZL": ""}], bj(9, 0, i), i))
    only = next(iter(CODES))
    st.ingest(etf_msg("14:49:55", {only: "2.300"}, bj(14, 49, 58), 10))
    for msg in factor_msgs([("2026-09-11", "29,368.44"), ("2026-09-14", "29,127.16")], [("2026-09-11", "6.7743")]):
        st.ingest(msg)
    st.ingest(hf(bj(14, 49, 50), "29058.25", "29058.50", "29152.25", bj(14, 49, 51), 90))
    st.ingest(fx(bj(14, 49, 47), "6.7131", "6.7132", bj(14, 49, 49), 91))
    b = st.bundle(bj(14, 50))
    v = view(evaluate_bundle(b), b, {})
    assert v["estimate_available_count"] == 1 and v["member_count"] == 5
    assert v["estimate_available"] is True and v["absolute_decision_eligible"] is False


def test_reference_snapshot_is_reference_only():
    st = enav_state()
    b = st.bundle(bj(5, 0, day=16))
    v = view(evaluate_bundle(b), b, {})
    assert {r["absolute_decision_status"] for r in v["rows"]} == {"REFERENCE_ONLY"}
    assert all(not r["estimate_available"] and "REFERENCE_NOT_CURRENT" in r["decision_limitation_codes"]
               for r in v["rows"])
    assert v["absolute_decision_status"] == "REFERENCE_ONLY" and v["absolute_decision_eligible"] is False


@pytest.mark.parametrize("state", ["stale", "cold_start"])
def test_no_estimate_keeps_its_original_reasons(state):
    if state == "stale":
        st = enav_state()
        b = st.bundle(bj(14, 55))  # 行情停更（四审 D1）
    else:
        st = fed_state()  # 冷启动：指数、汇率、期货都还没到
        b = st.bundle(bj(14, 50))
    snap = evaluate_bundle(b)
    v = view(snap, b, {})
    assert {r["absolute_decision_status"] for r in v["rows"]} == {"NO_ESTIMATE"}
    assert v["estimate_available"] is False and v["absolute_premium_available"] is False
    assert all(r["enav"]["status"] == "UNAVAILABLE" and r["enav"]["reasons"] for r in v["rows"])


@pytest.mark.parametrize("premium", [-0.05, 0.0, 0.0999, 0.1, 0.1001, 0.5])
def test_premium_near_or_far_from_any_threshold_does_not_unlock_a_decision(premium):
    snap, b, names = current()
    snap = replace(snap, enav=tuple(replace(x, premium=premium) for x in snap.enav))
    v = view(snap, b, names)
    assert all(r["absolute_decision_eligible"] is False and r["absolute_error_bound_bp"] is None for r in v["rows"])
    assert v["absolute_decision_status"] == "UNVALIDATED_ERROR" and v["absolute_decision_eligible"] is False


@pytest.mark.parametrize("status", ["PROXY_ANCHOR", "REFERENCE", "UNAVAILABLE", None, "SOMETHING_NEW"])
def test_there_is_no_eligible_path_in_phase_one(status):
    d = decision_disclosure(status)
    assert d["absolute_decision_eligible"] is False and d["absolute_error_bound_bp"] is None
    assert d["decision_limitation_codes"]


def test_page_cli_json_and_download_show_the_same_state():
    snap, b, names = current()
    v = view(snap, b, names)
    env = full_bundle(snap, b)
    exported = json.loads(to_json(snap, b, names))
    group = {k: v[k] for k in FIELDS}
    assert {k: env["decision_disclosure"]["group"][k] for k in FIELDS} == group
    assert {k: exported["view"][k] for k in FIELDS} == group
    for r in v["rows"]:
        assert {k: env["decision_disclosure"]["members"][r["code"]][k] for k in FIELDS} == {k: r[k] for k in FIELDS}
    text, html = render(snap, b, names), page(v)
    assert DECISION_NOTICE in text and DECISION_NOTICE in html
    assert "不能替代绝对估值验证" in text and "不能替代绝对估值验证" in html
    assert "可买入" not in text and "可买入" not in html


def test_download_envelope_leaves_bundle_and_snapshot_untouched():
    snap, b, _ = current()
    env = full_bundle(snap, b)
    assert env["bundle"] == json.loads(canonical_json(b))
    assert bundle_id(bundle_from_dict(env["bundle"])) == env["bundle_id"] == snap.bundle_id
    assert env["snapshot"] == snapshot_to_dict(snap)
    assert "decision_disclosure" not in env["snapshot"] and "decision_disclosure" not in env["bundle"]
