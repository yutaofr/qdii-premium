from datetime import UTC, date, datetime, time
from pathlib import Path

import pytest

from qdii.apps.collect import load_collector_config
from qdii.pipeline.windows import HostWindow

REPO = Path(__file__).resolve().parents[2]
W = HostWindow("Europe/Paris", time(7, 30), "Asia/Shanghai", time(15, 5))


def ns(*args):
    return int(datetime(*args, tzinfo=UTC).timestamp()) * 10**9


def test_summer_window_is_beijing_1330_to_1505():
    # 2026-09-15 周二：巴黎 CEST(UTC+2) 07:30 = 05:30 UTC；北京 15:05 = 07:05 UTC
    s, e = W.bounds(date(2026, 9, 15))
    assert (s, e) == (ns(2026, 9, 15, 5, 30), ns(2026, 9, 15, 7, 5))
    assert W.active(ns(2026, 9, 15, 5, 30)) and not W.active(ns(2026, 9, 15, 5, 29, 59))
    assert W.active(ns(2026, 9, 15, 7, 4, 59)) and not W.active(ns(2026, 9, 15, 7, 5))


def test_winter_window_shrinks_to_35_minutes():
    # 2026-10-26 周一：欧洲已切冬令时（CET, UTC+1），07:30 巴黎 = 06:30 UTC = 北京 14:30
    s, e = W.bounds(date(2026, 10, 26))
    assert s == ns(2026, 10, 26, 6, 30) and e == ns(2026, 10, 26, 7, 5)
    assert (e - s) / 1e9 == 35 * 60


def test_weekend_inactive_and_next_bounds_skips_to_monday():
    assert not W.active(ns(2026, 9, 19, 6, 0))  # 周六
    s, _ = W.next_bounds(ns(2026, 9, 18, 8, 0))  # 周五窗口结束后
    assert s == ns(2026, 9, 21, 5, 30)


def test_overlap_counts_only_window_time():
    # 前一晚 22:00 UTC 睡到次日 06:00 UTC：只有 05:30—06:00 这 30 分钟在窗口内
    assert W.overlap_s(ns(2026, 9, 14, 22, 0), ns(2026, 9, 15, 6, 0)) == 30 * 60
    assert W.overlap_s(ns(2026, 9, 14, 22, 0), ns(2026, 9, 15, 5, 0)) == 0
    # 跨两个交易日的长缺口
    assert W.overlap_s(ns(2026, 9, 15, 0, 0), ns(2026, 9, 17, 0, 0)) == 2 * 95 * 60


def test_invalid_window_rejected():
    bad = HostWindow("Asia/Shanghai", time(15, 5), "Europe/Paris", time(7, 30))
    with pytest.raises(ValueError):
        bad.bounds(date(2026, 9, 15))


def test_repo_collector_config_has_paris_window():
    cfg = load_collector_config(REPO / "config" / "collector.toml")
    assert cfg.host_window.parts[0] == W
    assert cfg.anchor_window is cfg.host_window.parts[1] and cfg.anchor_window.before_s == 300
    assert cfg.status_lan_enabled is False
