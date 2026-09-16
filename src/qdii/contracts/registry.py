"""解析器注册表：contract_version → parse 函数（ADR-009）。

原始日志永久可用新版本解析器重解析；旧版本不删除。
"""

from __future__ import annotations

from collections.abc import Callable

from qdii.contracts import (
    cfets_fx_spot_v1,
    chinamoney_ccpr_his_v1,
    eastmoney_lsjz_v1,
    index_history_v1,
    sina_a_share_v1,
    sina_hf_quarterly_v1,
    sina_hf_v1,
    sina_hf_v2,
)
from qdii.core.types import ParseResult, RawMessage

Parser = Callable[[RawMessage], ParseResult]

PARSERS: dict[str, Parser] = {
    sina_a_share_v1.CONTRACT_VERSION: sina_a_share_v1.parse,
    sina_hf_v1.CONTRACT_VERSION: sina_hf_v1.parse,
    sina_hf_v2.CONTRACT_VERSION: sina_hf_v2.parse,
    sina_hf_quarterly_v1.CONTRACT_VERSION: sina_hf_quarterly_v1.parse,
    cfets_fx_spot_v1.CONTRACT_VERSION: cfets_fx_spot_v1.parse,
    eastmoney_lsjz_v1.CONTRACT_VERSION: eastmoney_lsjz_v1.parse,
    index_history_v1.FRED_VERSION: index_history_v1.parse_fred,
    index_history_v1.NASDAQ_VERSION: index_history_v1.parse_nasdaq,
    chinamoney_ccpr_his_v1.CONTRACT_VERSION: chinamoney_ccpr_his_v1.parse,
}


def get_parser(contract_version: str) -> Parser | None:
    return PARSERS.get(contract_version)
