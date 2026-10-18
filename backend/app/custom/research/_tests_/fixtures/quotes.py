"""Mock 行情 fixtures — P1.1 短路版测试。

覆盖场景:
- 主板涨停 (10%)
- 创业板涨停 (20%)
- 跌停
- 涨停炸板
- 未涨停（普通）
- 停牌 / prev_close=0
- 连板梯队样本: 1板 ×3、2板 ×2、3板 ×1、5+板 ×1

净室重写: 不从 easy-stock 复制 Go 字符串, 这里只构造算法所需的 Python 数据。
"""
from __future__ import annotations

from datetime import date, timedelta

from app.custom.research.short_term import Quote


def _today() -> str:
    return date(2026, 10, 16).isoformat()


def _prev() -> str:
    return (date(2026, 10, 16) - timedelta(days=1)).isoformat()


# === 模拟 A 股主板 / 创业板 涨停 / 跌停 行情 ===
def sample_quotes_basic() -> list[Quote]:
    """基础场景: 1板 3 只、2板 2 只、3板 1 只、5+板 1 只 + 跌停 + 炸板 + 普通。"""
    today = _today()
    quotes = [
        # 1板 ×3
        Quote(symbol="600001", name="A主板1板-甲", trade_date=today,
              prev_close=10.00, last_close=10.97, high=10.97, low=10.50,
              volume=12000, amount=13_164_000,
              industry="电子", theme_tags=["半导体"],
              limit_time="10:30:00"),
        Quote(symbol="600002", name="A主板1板-乙", trade_date=today,
              prev_close=20.00, last_close=21.93, high=21.93, low=21.20,
              volume=5000, amount=10_965_000,
              industry="通信", theme_tags=["5G"],
              limit_time="13:05:00"),
        Quote(symbol="600003", name="A主板1板-丙", trade_date=today,
              prev_close=8.00, last_close=8.78, high=8.78, low=8.50,
              volume=8000, amount=7_024_000,
              industry="医药", theme_tags=["创新药"],
              limit_time="14:00:00"),

        # 2板 ×2 (相对昨日 1 板 +1)
        Quote(symbol="600010", name="B主板2板-甲", trade_date=today,
              prev_close=12.00, last_close=13.16, high=13.16, low=12.80,
              volume=10000, amount=13_160_000,
              industry="汽车", theme_tags=["新能源汽车"],
              limit_time="09:35:00"),
        Quote(symbol="600011", name="B主板2板-乙", trade_date=today,
              prev_close=15.00, last_close=16.45, high=16.45, low=15.90,
              volume=7000, amount=11_515_000,
              industry="化工", theme_tags=["锂电"],
              limit_time="10:10:00"),

        # 3板 ×1
        Quote(symbol="600020", name="C主板3板-甲", trade_date=today,
              prev_close=18.00, last_close=19.74, high=19.74, low=19.20,
              volume=6000, amount=11_844_000,
              industry="医药", theme_tags=["创新药", "减肥药"],
              limit_time="09:25:00"),

        # 5+板 ×1 (7 板)
        Quote(symbol="600030", name="D主板7板", trade_date=today,
              prev_close=30.00, last_close=32.90, high=32.90, low=31.50,
              volume=9000, amount=29_610_000,
              industry="传媒", theme_tags=["短剧", "AI"],
              limit_time="09:30:00"),

        # 跌停
        Quote(symbol="600099", name="E主板跌停", trade_date=today,
              prev_close=10.00, last_close=9.00, high=9.10, low=9.00,
              volume=20000, amount=18_000_000,
              industry="地产", theme_tags=[],
              limit_time=None, is_limit_down=True),

        # 涨停炸板（封板后开板，change_pct 跌到 8% 但 limit_time 标记为已封板）
        Quote(symbol="600088", name="F主板炸板", trade_date=today,
              prev_close=10.00, last_close=10.80, high=11.00, low=10.50,
              volume=15000, amount=16_200_000,
              industry="金融", theme_tags=["券商"],
              limit_time="10:00:00", is_exploded=True),

        # 普通上涨
        Quote(symbol="600077", name="G主板+3%", trade_date=today,
              prev_close=10.00, last_close=10.30, high=10.40, low=10.10,
              volume=8000, amount=8_240_000,
              industry="食品", theme_tags=["消费"],),

        # 停牌 / prev_close=0
        Quote(symbol="600066", name="H主板停牌", trade_date=today,
              prev_close=0.0, last_close=10.50, high=10.50, low=10.50,
              volume=0, amount=0,
              industry="", theme_tags=[],
              limit_time=None),

        # 创业板涨停 20%（净室算法只判 10% 主板，20% 会判为"未涨停"，
        # 但 0.2 也 > 0.095 所以会被判为涨停——是预期的边界行为，详见测试）
        Quote(symbol="300001", name="I创业板20%", trade_date=today,
              prev_close=10.00, last_close=12.00, high=12.00, low=11.50,
              volume=9000, amount=10_800_000,
              industry="新能源", theme_tags=["光伏"],
              limit_time="11:00:00"),
    ]
    return quotes


def prev_pool_basic() -> list[dict]:
    """昨日涨停池（用于算连板数）。"""
    return [
        {"symbol": "600010", "board_count": 1},  # 今日 2 板
        {"symbol": "600011", "board_count": 1},  # 今日 2 板
        {"symbol": "600020", "board_count": 2},  # 今日 3 板
        {"symbol": "600030", "board_count": 6},  # 今日 7 板
    ]


def empty_quotes() -> list[Quote]:
    """空输入 — 边界。"""
    return []


def all_unrelated_quotes() -> list[Quote]:
    """全部未涨停 — 验证空涨停池。"""
    today = _today()
    return [
        Quote(symbol=f"6000{i:02d}", name=f"未涨停{i}", trade_date=today,
              prev_close=10.0 + i, last_close=10.0 + i + 0.3, high=10.0 + i + 0.5,
              low=10.0 + i - 0.1, volume=1000, amount=10_000_000,
              industry="杂", theme_tags=[])
        for i in range(5)
    ]