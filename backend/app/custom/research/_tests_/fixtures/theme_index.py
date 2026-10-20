"""Mock 题材 K 线 + 产业链映射 fixtures — P1.1。

净室重写: 5 只成分股各 10 个交易日, 用于等权指数算法测试。
"""
from __future__ import annotations

from datetime import date, timedelta

from app.custom.research.theme_index import StockBar


def theme_chinese_aid() -> str:
    """测试用主题 id。"""
    return "ai_chain"


def theme_stocks_bars(days: int = 10) -> dict[str, list[StockBar]]:
    """5 只成分股, 每天涨跌不等。"""
    base = date(2026, 10, 1)
    # 简单合成: 5 只股按日做小幅波动
    patterns = [
        # sym, [每日收益率]
        ("601318",  [0.01, 0.02, -0.01, 0.03, 0.01, -0.02, 0.04, 0.02, -0.01, 0.03]),
        ("600519",  [0.02, 0.01, 0.03, -0.01, 0.02, 0.01, -0.02, 0.03, 0.01, 0.02]),
        ("000858",  [0.03, -0.02, 0.02, 0.01, -0.01, 0.03, 0.01, 0.02, -0.01, 0.02]),
        ("002594",  [0.01, 0.03, -0.02, 0.02, 0.01, -0.01, 0.03, -0.01, 0.02, 0.01]),
        ("300750",  [0.02, -0.01, 0.03, 0.01, -0.02, 0.02, 0.01, -0.01, 0.03, 0.01]),
    ]
    bars: dict[str, list[StockBar]] = {}
    for sym, returns in patterns:
        sym_bars = []
        price = 50.0
        for i, r in enumerate(returns[:days]):
            d = (base + timedelta(days=i)).isoformat()
            prev = prev_close = price
            close = price * (1 + r)
            sym_bars.append(StockBar(
                date=d, close=round(close, 4),
                volume=1000.0 + i * 100, amount=50_000_000 + i * 5_000_000,
            ))
            price = close
        bars[sym] = sym_bars
    return bars


def theme_chain_minimal_json() -> str:
    """最小可用产业链 JSON — 单元测试用。"""
    return """{
  "AI 算力链": {
    "theme_id": "ai_chain",
    "industries": ["电子", "通信"],
    "chain": [
      {"node": "上游-光模块", "stocks": ["300750", "002594"], "weight": 1.5},
      {"node": "中游-服务器", "stocks": ["000858"], "weight": 1.2},
      {"node": "下游-应用", "stocks": ["601318", "600519"], "weight": 0.8}
    ]
  }
}"""


def change_pct_for_chain() -> dict[str, float]:
    """测试用: 每只成分股的当日涨跌幅。"""
    return {
        "300750": 0.10,   # 光模块涨停
        "002594": 0.05,
        "000858": 0.03,
        "601318": 0.02,
        "600519": 0.01,
    }