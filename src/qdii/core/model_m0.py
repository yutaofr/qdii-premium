"""VM-01 基础全敞口模型 M0（纯函数）。

所有输入必须有限且为正，否则返回 None（不抛异常、不返回 0）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass


def _ok(*xs: float | None) -> bool:
    return all(x is not None and math.isfinite(x) and x > 0 for x in xs)


@dataclass(frozen=True, slots=True)
class M0Factors:
    spot: float  # I(c) / I(a)
    future: float  # F_k(t) / F_k(c)
    fx: float  # X(t) / X0


def m0_factors(i_a: float, i_c: float, f_c: float, f_t: float, x0: float, x_t: float) -> M0Factors | None:
    if not _ok(i_a, i_c, f_c, f_t, x0, x_t):
        return None
    return M0Factors(i_c / i_a, f_t / f_c, x_t / x0)


def m0_nav(n0: float, factors: M0Factors | None) -> float | None:
    if factors is None or not _ok(n0):
        return None
    return n0 * factors.spot * factors.future * factors.fx


def premium(price: float | None, nav: float | None) -> float | None:
    """P / N − 1；任一侧无效返回 None（QS-04：零价不得产生 −100%）。"""
    if not _ok(price, nav):
        return None
    return price / nav - 1  # type: ignore[operator]


def nav_ratio(i0: float, i1: float, x0: float | None = None, x1: float | None = None) -> float | None:
    """两个日终锚点之间的 M0 预测净值比：I1/I0 ×（可选）X1/X0。"""
    if not _ok(i0, i1):
        return None
    if x0 is None and x1 is None:
        return i1 / i0
    if not _ok(x0, x1):
        return None
    return (i1 / i0) * (x1 / x0)  # type: ignore[operator]
