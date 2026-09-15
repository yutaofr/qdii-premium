"""PH0-07 历史经济验证（VM-12，纯函数）。

对每只基金相邻两个净值日 (d0, d1)，比较实际净值比与 M0 预测比
   pred = I(idx_date(d1)) / I(idx_date(d0)) × X(fx_date(d1)) / X(fx_date(d0))
在若干“日期对齐 × 汇率规则”假设下的残差。前 60 个干净区间选假设，后 60 个封存区间报告。

结论只是“历史经济拟合”（HISTORICAL_RESEARCH_ONLY）：没有历史获知时间，不证明披露及时性，
误差最低也不证明基金合同采用该规则（VM-12）。
"""

from __future__ import annotations

import bisect
import itertools
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

from qdii.core.model_m0 import nav_ratio
from qdii.core.types import NavObservation

GROWTH_TOLERANCE = 0.0002  # 来源日增长率(2 位小数 %)与净值比不一致超过 2bp → 视为事件断点


@dataclass(frozen=True, slots=True)
class Hypothesis:
    name: str
    index_lag: int  # 0 = 净值日期即美股交易日；1 = 对应前一个美股交易日
    fx_rule: str  # "T" 最近一个 ≤ d 的中间价；"T+1" 第一个 > d 的中间价；"NONE" 不计汇率


HYPOTHESES: tuple[Hypothesis, ...] = (
    Hypothesis("L0_FX_T", 0, "T"),
    Hypothesis("L0_FX_T+1", 0, "T+1"),
    Hypothesis("L1_FX_T", 1, "T"),
    Hypothesis("L1_FX_T+1", 1, "T+1"),
    Hypothesis("L0_NOFX", 0, "NONE"),
)
US_HOLIDAY_NOTE = "d 不是美股交易日时，L0 取之前最近的美股收盘"


@dataclass(frozen=True, slots=True)
class Interval:
    code: str
    d0: date
    d1: date
    nav0: float
    nav1: float
    actual_ratio: float
    excluded: str | None
    residuals: Mapping[str, float | None] = field(default_factory=dict)  # actual/pred − 1


@dataclass(frozen=True, slots=True)
class Stats:
    n: int
    mean_bp: float | None
    mae_bp: float | None
    p95_bp: float | None
    max_bp: float | None


class _Series:
    def __init__(self, points: Mapping[date, float]) -> None:
        self.dates = sorted(points)
        self.values = points

    def on(self, d: date) -> float | None:
        return self.values.get(d)

    def shift_back(self, d: date, n: int) -> date | None:
        """不晚于 d 的最近序列日，再往前退 n 个（与 M0 中 c = 最近已完成收盘一致）。

        实测：基金在美股休市、A 股开市的日期照常公布净值（如 2025-01-09、耶稣受难日），
        此时对应的是之前最近一个美股收盘。
        """
        i = bisect.bisect_right(self.dates, d) - 1 - n
        return self.dates[i] if i >= 0 else None

    def latest_le(self, d: date) -> date | None:
        i = bisect.bisect_right(self.dates, d)
        return self.dates[i - 1] if i else None

    def first_gt(self, d: date) -> date | None:
        i = bisect.bisect_right(self.dates, d)
        return self.dates[i] if i < len(self.dates) else None


def _fx_date(series: _Series, d: date, rule: str) -> date | None:
    if rule == "T":
        return series.latest_le(d)
    if rule == "T+1":
        return series.first_gt(d)
    raise ValueError(rule)


def _predict(h: Hypothesis, d0: date, d1: date, idx: _Series, fx: _Series) -> float | None:
    i0d, i1d = idx.shift_back(d0, h.index_lag), idx.shift_back(d1, h.index_lag)
    if i0d is None or i1d is None:
        return None
    # i0d == i1d（期间无新的美股收盘）时指数因子为 1，净值变化只来自汇率与费用，照常计算
    if h.fx_rule == "NONE":
        return nav_ratio(idx.on(i0d), idx.on(i1d))
    x0d, x1d = _fx_date(fx, d0, h.fx_rule), _fx_date(fx, d1, h.fx_rule)
    if x0d is None or x1d is None:
        return None
    return nav_ratio(idx.on(i0d), idx.on(i1d), fx.on(x0d), fx.on(x1d))


def build_intervals(
    navs: Sequence[NavObservation],
    index_closes: Mapping[date, float],
    fixings: Mapping[date, float],
    hypotheses: Sequence[Hypothesis] = HYPOTHESES,
) -> list[Interval]:
    idx, fx = _Series(index_closes), _Series(fixings)
    rows = sorted((n for n in navs if n.unit_nav is not None), key=lambda n: n.nav_date)
    out: list[Interval] = []
    for prev, cur in itertools.pairwise(rows):
        nav0, nav1 = float(prev.unit_nav), float(cur.unit_nav)  # type: ignore[arg-type]
        actual = nav1 / nav0
        excluded = None
        if cur.event_fields:
            excluded = "EVENT_FIELDS"
        elif cur.growth_pct is not None and abs(float(cur.growth_pct) / 100 - (actual - 1)) > GROWTH_TOLERANCE:
            excluded = "GROWTH_MISMATCH"
        residuals: dict[str, float | None] = {}
        for h in hypotheses:
            pred = _predict(h, prev.nav_date, cur.nav_date, idx, fx)
            residuals[h.name] = None if pred is None else actual / pred - 1
        out.append(Interval(cur.code, prev.nav_date, cur.nav_date, nav0, nav1, actual, excluded, residuals))
    return out


def clean(intervals: Sequence[Interval], hypotheses: Sequence[Hypothesis] = HYPOTHESES) -> list[Interval]:
    """排除事件断点，并要求所有假设都可计算，保证各假设在同一样本上比较。"""
    return [
        iv for iv in intervals
        if iv.excluded is None and all(iv.residuals.get(h.name) is not None for h in hypotheses)
    ]


def stats(values: Sequence[float]) -> Stats:
    if not values:
        return Stats(0, None, None, None, None)
    bp = [v * 1e4 for v in values]
    absv = sorted(abs(v) for v in bp)
    rank = max(0, math.ceil(0.95 * len(absv)) - 1)
    return Stats(len(bp), sum(bp) / len(bp), sum(absv) / len(absv), absv[rank], absv[-1])


@dataclass(frozen=True, slots=True)
class RuleSelection:
    n_clean: int
    shortfall: int  # 距 dev+holdout 目标还差多少区间
    dev: Mapping[str, Stats]
    holdout: Mapping[str, Stats]
    selected: str | None  # 仅用 dev 选出


def select_rule(
    clean_intervals: Sequence[Interval],
    hypotheses: Sequence[Hypothesis] = HYPOTHESES,
    n_dev: int = 60,
    n_holdout: int = 60,
) -> RuleSelection:
    ordered = sorted(clean_intervals, key=lambda iv: iv.d1)
    need = n_dev + n_holdout
    window = ordered[-need:]
    shortfall = max(0, need - len(ordered))
    split = max(0, len(window) - n_holdout) if shortfall else n_dev
    dev, hold = window[:split], window[split:]

    def per(ivs: Sequence[Interval]) -> dict[str, Stats]:
        return {h.name: stats([iv.residuals[h.name] for iv in ivs]) for h in hypotheses}  # type: ignore[misc]

    dev_stats = per(dev)
    candidates = [(s.mae_bp, name) for name, s in dev_stats.items() if s.mae_bp is not None]
    selected = min(candidates)[1] if candidates else None
    return RuleSelection(len(ordered), shortfall, dev_stats, per(hold), selected)
