"""题材指数算法单测 — P1.1。

净室重写覆盖:
- 等权指数聚合 (基础值 1000, 单调 → 终值可推算)
- OHLC 简化规则 (open=昨收, high/low 包络)
- 频率聚合 (1d / 1w / 1m)
- 产业链加载 + 归因
- 边界: 空输入、单只股
"""
from __future__ import annotations

import json
from pathlib import Path

from app.custom.research._tests_.fixtures import theme_index as fx
from app.custom.research.chain import (
    ThemeChain,
    attribute_chain,
    load_theme_chain,
)
from app.custom.research.theme_index import (
    aggregate_freq,
    compute_ew_index,
)


# === 等权指数 ===
def test_ew_index_basic_shape() -> None:
    """基础形态: 返回长度 = 日历日数; 第一日 open=1000, close=1000 (无前日均收益)。"""
    bars = fx.theme_stocks_bars(days=10)
    index = compute_ew_index(bars)
    # 5 只股都有 10 个交易日 → 10 个 index bar
    assert len(index) == 10
    # 第一日: 没 prev_closes, avg_return=0, close=base=1000
    first = index[0]
    assert first.open == 1000.0
    assert first.close == 1000.0


def test_ew_index_monotone_close() -> None:
    """5 只股全为正收益 → 指数单调上升。"""
    bars = {
        "a1": [_make_bar(d, 0.01) for d in _dates(5)],
        "a2": [_make_bar(d, 0.02) for d in _dates(5)],
    }
    index = compute_ew_index(bars)
    closes = [b.close for b in index]
    assert closes == sorted(closes)


def test_ew_index_open_equals_prev_close() -> None:
    """当日 open = 昨日 close（首日除外 = base）。"""
    bars = {
        "a1": [_make_bar(d, 0.01) for d in _dates(5)],
        "a2": [_make_bar(d, -0.01) for d in _dates(5)],
    }
    index = compute_ew_index(bars)
    for i in range(1, len(index)):
        # close[i-1] 是 prev_value, 但 index 的 open 是 prev_value 的快照
        # 因为等权指数本身就是个连续值, 故 round 误差不影响比较
        assert abs(index[i].open - index[i - 1].close) < 0.001


def test_ew_index_high_low_envelopes() -> None:
    """high >= max(open, close), low <= min(open, close)。"""
    bars = fx.theme_stocks_bars(days=10)
    index = compute_ew_index(bars)
    for bar in index:
        assert bar.high >= max(bar.open, bar.close)
        assert bar.low <= min(bar.open, bar.close)


def test_ew_index_empty() -> None:
    """空输入 → 空输出。"""
    assert compute_ew_index({}) == []


def test_ew_index_one_stock() -> None:
    """单只股 → 指数 = base * 累积 (1 + ri)。

    day0: 不计收益, index = base = 1000
    day1: ret = 0.10, index = 1100
    day2: 假设当日价 = 上日 * 1.10 (实现为 close=55, ret = 0.10 实际), index = 1210
    这里用精确价格 close=55 来推算 ret = (55-50)/50 = 0.10 → index = 1100 * 1.10 = 1210。
    """
    from app.custom.research.theme_index import StockBar

    dates = _dates(3)
    bars = {"only": [
        StockBar(date=dates[0], close=50.0, volume=1000.0, amount=50_000_000.0),
        StockBar(date=dates[1], close=55.0, volume=1000.0, amount=50_000_000.0),  # +10%
        StockBar(date=dates[2], close=60.5, volume=1000.0, amount=50_000_000.0),  # +10%
    ]}
    index = compute_ew_index(bars)
    assert len(index) == 3
    assert index[0].close == 1000.0
    assert abs(index[1].close - 1100.0) < 1e-4
    # day2: (60.5-55)/55 = 0.10, index = 1100 * 1.10 = 1210
    assert abs(index[2].close - 1210.0) < 1e-4


# === 频率聚合 ===
def test_aggregate_freq_1d_passthrough() -> None:
    """freq=1d → 原样返回。"""
    bars = fx.theme_stocks_bars(days=5)
    index = compute_ew_index(bars)
    assert aggregate_freq(index, "1d") == index


def test_aggregate_freq_1w_grouping() -> None:
    """freq=1w → 按 ISO 周聚合, 同周多日合一。"""
    # 用 14 天连续, 跨 2-3 周
    bars = {"a": [_make_bar(d, 0.01) for d in _dates(14)]}
    index = compute_ew_index(bars)
    weekly = aggregate_freq(index, "1w")
    # 14 天大约 2-3 周 (取决于起始日是周几)
    assert len(weekly) >= 2
    assert len(weekly) <= 3


def test_aggregate_freq_1m_grouping() -> None:
    """freq=1m → 按月聚合。"""
    # 跨 2 个月: 10月 5 天 + 11月 5 天
    bars = {
        "a": [_make_bar(f"2026-10-{i + 1:02d}", 0.01) for i in range(5)] +
             [_make_bar(f"2026-11-{i + 1:02d}", 0.02) for i in range(5)],
    }
    index = compute_ew_index(bars)
    monthly = aggregate_freq(index, "1m")
    assert len(monthly) == 2


def test_aggregate_freq_invalid() -> None:
    """非法 freq → ValueError。"""
    import pytest

    bars = fx.theme_stocks_bars(days=3)
    index = compute_ew_index(bars)
    with pytest.raises(ValueError, match="unsupported freq"):
        aggregate_freq(index, "1h")


def test_aggregate_freq_empty() -> None:
    """空 bars → 空输出。"""
    assert aggregate_freq([], "1w") == []


# === 产业链加载 + 归因 ===
def test_load_theme_chain(tmp_path: Path) -> None:
    """加载 JSON 产业链映射。"""
    path = tmp_path / "chain.json"
    path.write_text(fx.theme_chain_minimal_json(), encoding="utf-8")
    chains = load_theme_chain(path)
    assert "ai_chain" in chains
    chain = chains["ai_chain"]
    assert chain.theme_name == "AI 算力链"
    assert chain.industries == ["电子", "通信"]
    assert len(chain.chain) == 3
    # 上游-光模块权重 1.5
    assert chain.chain[0].weight == 1.5
    assert chain.chain[0].stocks == ["300750", "002594"]


def test_load_theme_chain_missing_file(tmp_path: Path) -> None:
    """文件不存在 → 空 dict。"""
    chains = load_theme_chain(tmp_path / "missing.json")
    assert chains == {}


def test_attribute_chain_basic(tmp_path: Path) -> None:
    """归因: 节点加权平均涨跌幅 + 贡献度。"""
    path = tmp_path / "chain.json"
    path.write_text(fx.theme_chain_minimal_json(), encoding="utf-8")
    chains = load_theme_chain(path)
    chain = chains["ai_chain"]
    cp = fx.change_pct_for_chain()

    attribution = attribute_chain(chain, cp)
    # 3 个节点
    assert len(attribution) == 3
    # 上游-光模块: (300750=0.10 + 002594=0.05) / 2 = 0.075
    upstream = attribution[0]
    assert upstream["node"] == "上游-光模块"
    assert abs(upstream["avg_change_pct"] - 0.075) < 1e-4
    # avg_change * node.weight = 0.075 * 1.5 = 0.1125
    assert abs(upstream["contribution"] - 0.1125) < 1e-4
    assert len(upstream["stocks"]) == 2


def test_attribute_chain_no_overlap(tmp_path: Path) -> None:
    """change_pct_map 不含任何节点股票 → avg_change_pct=0, contribution=0。"""
    path = tmp_path / "chain2.json"
    path.write_text(fx.theme_chain_minimal_json(), encoding="utf-8")
    chains = load_theme_chain(path)
    chain = chains["ai_chain"]
    attribution = attribute_chain(chain, {})
    for node in attribution:
        assert node["avg_change_pct"] == 0.0
        assert node["contribution"] == 0.0
        assert node["stocks"] == []


def test_chain_to_dict() -> None:
    """ThemeChain.to_dict 字段对齐。"""
    chain = ThemeChain(theme_id="x", theme_name="X")
    d = chain.to_dict()
    assert d["theme_id"] == "x"
    assert d["theme_name"] == "X"
    assert d["chain"] == []
    assert d["industries"] == []


# === helpers ===
def _make_bar(date: str, ret: float) -> "StockBar":
    """生成单只股的单日 bar: 50 起, 按 ret 收益。"""
    from app.custom.research.theme_index import StockBar
    close = 50.0 * (1 + ret)
    return StockBar(date=date, close=close, volume=1000.0, amount=50_000_000.0)


def _dates(n: int) -> list[str]:
    """生成 n 个连续日期。"""
    from datetime import date, timedelta
    base = date(2026, 10, 1)
    return [(base + timedelta(days=i)).isoformat() for i in range(n)]