"""FundProfile 与 PH0-06 规则证据的一致性；研究提取的分类与公开仓库过滤。"""

import tomllib
from pathlib import Path

from qdii.apps.research_rules import Excerpt, classify, keep_in_repo

REPO = Path(__file__).resolve().parents[2]


def test_every_configured_fund_has_matching_rule_evidence():
    funds = tomllib.loads((REPO / "config" / "funds.toml").read_text(encoding="utf-8"))["fund"]
    assert {f["code"] for f in funds} == {"513100", "159696", "159501", "159660", "513390"}
    for f in funds:
        path = REPO / f["nav_fx_rule_evidence"]
        rule = tomllib.loads(path.read_text(encoding="utf-8"))
        assert rule["code"] == f["code"] and rule["exchange"] == f["exchange"]
        assert rule["nav_fx_rule"]["rule"] == f["nav_fx_rule"]
        assert rule["nav_fx_rule"]["status"] == f["nav_fx_rule_status"]
        if f["nav_fx_rule_status"] == "VERIFIED":  # VERIFIED 必须有可复核的条款页码、摘录与文件哈希
            fx = rule["clauses"]["valuation_fx"]
            assert fx["page"] > 0 and "汇率" in fx["excerpt"]
            assert len(rule["document"]["sha256"]) == 64
            for ev in rule["nav_fx_rule"]["evidence"]:
                assert (REPO / ev).exists(), ev


def test_classify_prefers_cash_substitute_and_iopv_over_valuation_fx():
    assert classify("替代金额按照T-2日估值汇率换算") == "PCF_CASH_SUBSTITUTE"
    assert classify("基金份额参考净值根据申购赎回清单和汇率计算") == "IOPV"
    assert classify("估值计算中涉及主要货币对人民币汇率的，以当日中间价为基准") == "VALUATION_FX"
    assert classify("与本项目无关的句子") is None


def test_public_repo_keeps_only_decision_excerpts():
    assert keep_in_repo(Excerpt(74, "VALUATION_FX", "…"))
    assert keep_in_repo(Excerpt(73, "DAY_DEFINITION", "本基金的估值日为…"))
    assert not keep_in_repo(Excerpt(60, "PCF_CASH_SUBSTITUTE", "…"))
    assert not keep_in_repo(Excerpt(9, "DAY_DEFINITION", "工作日：指…"))
