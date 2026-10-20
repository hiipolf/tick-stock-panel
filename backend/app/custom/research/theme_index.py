"""题材指数 K 线 — P1.1 核心算法。

净室重写自 easy-stock `internal/themeindex/`：
- 自维护"等权参考指数"（成分股等权重，不按市值加权）
- 支持日 K / 周 K / 月 K
- 缓存到 data/theme_index/<theme_id>/daily.json

业务函数是纯函数，可独立测试。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass
class StockBar:
    """单只股票一个交易日的 K 线。"""
    date: str  # YYYY-MM-DD
    close: float
    volume: float = 0.0
    amount: float = 0.0


@dataclass
class ThemeIndexBar:
    """题材指数一个交易日的 K 线。"""
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "open": round(self.open, 4),
            "high": round(self.high, 4),
            "low": round(self.low, 4),
            "close": round(self.close, 4),
            "volume": round(self.volume, 2),
            "amount": round(self.amount, 2),
        }


def compute_ew_index(
    stocks: dict[str, list[StockBar]],
    base_value: float = 1000.0,
) -> list[ThemeIndexBar]:
    """等权指数聚合。

    逻辑：
    - 对每个交易日，等权计算所有成分股的收益率
    - 指数 = 上一日指数 × (1 + 成分股等权平均收益率)
    - 初始指数 = base_value

    Args:
        stocks: {symbol: [Bar...]}，每只成分股按日期排序的 K 线
        base_value: 基准初始值（默认 1000）

    Returns:
        按日期排序的指数 K 线
    """
    if not stocks:
        return []

    # 1. 收集所有日期
    all_dates: set[str] = set()
    for bars in stocks.values():
        for b in bars:
            all_dates.add(b.date)
    sorted_dates = sorted(all_dates)
    if not sorted_dates:
        return []

    # 2. 建 {symbol: {date: bar}} 索引
    stock_by_date: dict[str, dict[str, StockBar]] = {}
    for sym, bars in stocks.items():
        stock_by_date[sym] = {b.date: b for b in bars}

    # 3. 每日聚合
    result: list[ThemeIndexBar] = []
    prev_value = base_value
    prev_closes: dict[str, float] = {}

    for date in sorted_dates:
        # 当日有数据的成分股
        today_closes: dict[str, float] = {}
        today_volumes: dict[str, float] = {}
        today_amounts: dict[str, float] = {}
        for sym, by_date in stock_by_date.items():
            if date in by_date:
                today_closes[sym] = by_date[date].close
                today_volumes[sym] = by_date[date].volume
                today_amounts[sym] = by_date[date].amount

        if not today_closes:
            continue

        # 等权平均收益率
        if prev_closes:
            returns = []
            for sym, today_close in today_closes.items():
                if sym in prev_closes and prev_closes[sym] > 0:
                    returns.append(today_close / prev_closes[sym] - 1.0)
            avg_return = sum(returns) / len(returns) if returns else 0.0
        else:
            avg_return = 0.0

        new_value = prev_value * (1.0 + avg_return)

        # OHLC 简化：等权指数当日只有 close（用 avg_return 推算）
        # 严格应该按成分股 OHLC 加权，这里用 close 单价做近似
        result.append(ThemeIndexBar(
            date=date,
            open=round(prev_value, 4),
            high=round(max(prev_value, new_value), 4),
            low=round(min(prev_value, new_value), 4),
            close=round(new_value, 4),
            volume=sum(today_volumes.values()),
            amount=sum(today_amounts.values()),
        ))

        prev_value = new_value
        prev_closes = today_closes

    return result


def aggregate_freq(bar_list: list[ThemeIndexBar], freq: str) -> list[ThemeIndexBar]:
    """频率聚合（日 → 周/月）。"""
    if freq == "1d" or not bar_list:
        return bar_list
    if freq not in ("1w", "1m"):
        raise ValueError(f"unsupported freq: {freq}")

    grouped: dict[str, list[ThemeIndexBar]] = {}
    for bar in bar_list:
        if freq == "1w":
            # ISO 周：取周一日期
            from datetime import datetime
            d = datetime.strptime(bar.date, "%Y-%m-%d")
            week_start = d.toordinal() - d.weekday()  # 周一
            key = str(d.fromordinal(week_start).date())
        else:  # "1m"
            key = bar.date[:7]  # YYYY-MM
        grouped.setdefault(key, []).append(bar)

    result: list[ThemeIndexBar] = []
    for key in sorted(grouped):
        bars = grouped[key]
        if not bars:
            continue
        result.append(ThemeIndexBar(
            date=bars[-1].date,
            open=bars[0].open,
            high=max(b.high for b in bars),
            low=min(b.low for b in bars),
            close=bars[-1].close,
            volume=sum(b.volume for b in bars),
            amount=sum(b.amount for b in bars),
        ))
    return result
