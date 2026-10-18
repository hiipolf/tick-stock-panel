"""短线连板分析 — P1.1 核心算法。

净室重写自 easy-stock `internal/stockanalysis/short_term_quantitative.go`：
- 涨停判定（change_pct >= 0.095）
- 连板数累计算法
- 梯队分组（1/2/3/4/5+）
- 晋级率（昨日 N 板今日又涨停）
- 封板率、炸板率统计

业务函数是纯函数，不依赖 TSP app state，可独立单元测试。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable


# === 常量（A 股 10% 涨停线，留 0.5% 容差，对应主板 10%） ===
LIMIT_UP_THRESHOLD = 0.095  # 主板涨停线（含容差）
LIMIT_UP_BOARDS = (1, 2, 3, 4)  # 梯队分桶（不含 5+）
TOP_BOARD_THRESHOLD = 5  # 5 板及以上统一为 "5+" 梯队


# === 数据模型 ===
@dataclass
class Quote:
    """单只股票某一交易日的行情快照。"""
    symbol: str
    name: str
    trade_date: str  # YYYY-MM-DD
    prev_close: float
    last_close: float
    high: float
    low: float
    volume: float  # 手
    amount: float  # 元
    industry: str = ""
    theme_tags: list[str] = field(default_factory=list)
    limit_time: str | None = None  # HH:MM:SS，None 表示未涨停
    is_exploded: bool = False  # 是否曾封板后开板
    is_limit_down: bool = False  # 是否跌停

    @property
    def change_pct(self) -> float:
        """涨跌幅（小数制，0.0366 = 3.66%）。"""
        if self.prev_close <= 0:
            return 0.0
        return (self.last_close - self.prev_close) / self.prev_close

    @property
    def is_limit_up(self) -> bool:
        """是否涨停（A 股主板 10% 涨停线，留 0.5% 容差）。"""
        return self.change_pct >= LIMIT_UP_THRESHOLD


@dataclass
class LimitUpItem:
    """涨停池中的一条记录。"""
    symbol: str
    name: str
    trade_date: str
    limit_time: str | None
    first_limit_price: float
    last_close: float
    turnover: float  # 换手率（百分数，5.2 = 5.2%）
    amount: float  # 成交额（元）
    industry: str
    theme_tags: list[str]
    board_count: int  # 连续涨停天数

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "trade_date": self.trade_date,
            "limit_time": self.limit_time,
            "first_limit_price": self.first_limit_price,
            "last_close": self.last_close,
            "turnover": self.turnover,
            "amount": self.amount,
            "industry": self.industry,
            "theme_tags": self.theme_tags,
            "board_count": self.board_count,
        }


@dataclass
class Ladder:
    """连板梯队中的一条记录。"""
    board: int  # 1/2/3/4/5+
    count: int
    symbols: list[str]
    first_limit_time: str | None
    last_limit_time: str | None

    def to_dict(self) -> dict:
        return {
            "board": self.board,
            "count": self.count,
            "symbols": self.symbols,
            "first_limit_time": self.first_limit_time,
            "last_limit_time": self.last_limit_time,
        }


# === 核心算法 ===
def aggregate_limit_up_pool(
    quotes: Iterable[Quote],
    prev_pool: list[dict] | None = None,
) -> tuple[list[LimitUpItem], dict]:
    """聚合涨停池。

    Args:
        quotes: 今日所有股票行情
        prev_pool: 昨日涨停池（用于算连板数），可选

    Returns:
        (涨停池列表, 统计信息)
    """
    prev_board_map: dict[str, int] = {}
    if prev_pool:
        for item in prev_pool:
            prev_board_map[item["symbol"]] = item.get("board_count", 0)

    items: list[LimitUpItem] = []
    limit_up_count = 0
    limit_down_count = 0
    exploded_count = 0

    for q in quotes:
        if q.is_limit_down:
            limit_down_count += 1
        if q.is_exploded:
            exploded_count += 1
        if not q.is_limit_up:
            continue
        limit_up_count += 1

        # 连板数：昨日本涨停 → 1 板；昨日 N 板 → N+1 板
        prev = prev_board_map.get(q.symbol, 0)
        board = 1 if prev == 0 else prev + 1

        items.append(LimitUpItem(
            symbol=q.symbol,
            name=q.name,
            trade_date=q.trade_date,
            limit_time=q.limit_time,
            first_limit_price=q.high,
            last_close=q.last_close,
            turnover=0.0,  # 行情数据不含换手率，调用方补
            amount=q.amount,
            industry=q.industry,
            theme_tags=list(q.theme_tags),
            board_count=board,
        ))

    # 按连板数倒序、时间正序排序
    items.sort(key=lambda it: (-it.board_count, it.limit_time or "99:99:99"))

    stats = {
        "limit_up_total": limit_up_count,
        "limit_down_total": limit_down_count,
        "exploded_total": exploded_count,
    }
    return items, stats


def build_ladder(items: list[LimitUpItem]) -> list[Ladder]:
    """构建连板梯队。"""
    bucket: dict[int, list[LimitUpItem]] = {b: [] for b in LIMIT_UP_BOARDS}
    top_bucket: list[LimitUpItem] = []  # 5+ 板

    for it in items:
        if it.board_count in bucket:
            bucket[it.board_count].append(it)
        elif it.board_count >= TOP_BOARD_THRESHOLD:
            top_bucket.append(it)

    ladders: list[Ladder] = []
    for b in LIMIT_UP_BOARDS:
        lst = bucket[b]
        if not lst:
            continue
        times = [it.limit_time for it in lst if it.limit_time]
        ladders.append(Ladder(
            board=b,
            count=len(lst),
            symbols=[it.symbol for it in lst],
            first_limit_time=min(times) if times else None,
            last_limit_time=max(times) if times else None,
        ))

    if top_bucket:
        times = [it.limit_time for it in top_bucket if it.limit_time]
        # 5+ 梯队用 5 表示（>=5 板）
        ladders.append(Ladder(
            board=TOP_BOARD_THRESHOLD,
            count=len(top_bucket),
            symbols=[it.symbol for it in top_bucket],
            first_limit_time=min(times) if times else None,
            last_limit_time=max(times) if times else None,
        ))

    # 按板数从高到低
    ladders.sort(key=lambda l: -l.board)
    return ladders


def compute_promotion_rate(
    today_items: list[LimitUpItem],
    prev_pool: list[dict] | None,
) -> dict:
    """晋级率（昨日 N 板今日又涨停的比例）。"""
    if not prev_pool:
        return {"by_board": {}, "overall": 0.0}

    prev_by_board: dict[int, list[str]] = {}
    for it in prev_pool:
        b = it.get("board_count", 0)
        prev_by_board.setdefault(b, []).append(it["symbol"])

    today_symbols = {it.symbol for it in today_items}
    today_by_board_from_prev: dict[int, set[str]] = {b: set() for b in prev_by_board}
    for it in today_items:
        for b, syms in prev_by_board.items():
            if it.symbol in syms:
                today_by_board_from_prev[b].add(it.symbol)

    by_board = {}
    for b, syms in prev_by_board.items():
        if not syms:
            continue
        by_board[str(b)] = round(len(today_by_board_from_prev[b]) / len(syms), 4)

    total_prev = sum(len(s) for s in prev_by_board.values())
    total_promoted = sum(len(s) for s in today_by_board_from_prev.values())
    overall = round(total_promoted / total_prev, 4) if total_prev else 0.0

    return {"by_board": by_board, "overall": overall}


def compute_stats(
    today_items: list[LimitUpItem],
    ladder: list[Ladder],
    total_quotes: int,
) -> dict:
    """计算综合统计（封板率、最高板、炸板率等）。"""
    seal_count = sum(1 for it in today_items if it.limit_time is not None)
    limit_up_total = len(today_items)
    max_board = max((it.board_count for it in today_items), default=0)
    seal_rate = round(seal_count / limit_up_total, 4) if limit_up_total else 0.0

    return {
        "limit_up_total": limit_up_total,
        "max_board": max_board,
        "seal_rate": seal_rate,
        "exploded_rate": 0.0,  # 由调用方从 quote pool 计算后传入
        "ladder_count": len(ladder),
    }
