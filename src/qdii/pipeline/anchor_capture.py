"""美股收盘期货锚点捕获（DS-09 / DS-10 / VM-03）。

在线：采集器把 hf_NQ 报文 ingest 进来；每个日历收盘 c 到达 c + 2 分钟后，只用 received_at ≤ c+2 分钟的样本
选取锚点，结果（含 MISSING）只追加写入锚点库。主机在窗口内睡眠时如实记为 MISSING / FAILED。
回放：按原始日志顺序 ingest，在同一截止时刻重算，结果应与锚点库一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from qdii.contracts import sina_hf_v1
from qdii.core.anchor import AnchorPolicy, AnchorResult, FuturesSample, select_anchor
from qdii.core.types import MarketQuote, PriceType, RawMessage, ReasonCode
from qdii.pipeline.host_windows import CloseWindow

HF_ENDPOINT = "sina.hf_NQ"
NEW_YORK = ZoneInfo("America/New_York")
CAPTURE_AFTER_S = 120  # DS-10：c + 2 分钟做缺口检查
LOOKBACK_S = 3 * 86400  # 启动后补评估最近 3 天内尚未记录的收盘


@dataclass
class AnchorTracker:
    closes: CloseWindow
    policy: AnchorPolicy = field(default_factory=AnchorPolicy)
    samples: list[FuturesSample] = field(default_factory=list)

    def ingest(self, msg: RawMessage) -> bool:
        if msg.endpoint_id != HF_ENDPOINT or msg.status != 200:
            return False
        bid = ask = None
        provider = None
        tick_ok = True
        for rec in sina_hf_v1.parse(msg).records:
            if not isinstance(rec, MarketQuote):
                continue
            provider = rec.provider_time.utc_ns
            if ReasonCode.TICK_MISMATCH in rec.reason_codes and rec.price_type in (PriceType.BID, PriceType.ASK):
                tick_ok = False
            if rec.price_type is PriceType.BID:
                bid = rec.value
            elif rec.price_type is PriceType.ASK:
                ask = rec.value
        self.samples.append(FuturesSample(msg.msg_id, provider, msg.received_utc_ns, bid, ask, tick_ok))
        return True

    def evaluate(self, c_utc_ns: int) -> AnchorResult:
        et_date = datetime.fromtimestamp(c_utc_ns / 1e9, tz=NEW_YORK).date()
        return select_anchor(self.samples, series_id=sina_hf_v1.SERIES_ID, c_utc_ns=c_utc_ns,
                             cutoff_utc_ns=c_utc_ns + CAPTURE_AFTER_S * 1_000_000_000, et_date=et_date,
                             policy=self.policy)

    def due(self, now_utc_ns: int, done: set[int]) -> list[AnchorResult]:
        """c + 2 分钟已过、且尚未记录的收盘；评估后裁剪不再需要的旧样本。"""
        latest_due = now_utc_ns - CAPTURE_AFTER_S * 1_000_000_000
        pending = [c for c in self.closes.closes_between(now_utc_ns - LOOKBACK_S * 1_000_000_000, latest_due)
                   if c not in done]
        results = [self.evaluate(c) for c in pending]
        keep_from = latest_due - int((self.closes.before_s + 3600) * 1e9)
        self.samples = [s for s in self.samples if s.received_utc_ns >= keep_from]
        return results
