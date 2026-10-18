"""短线连板算法单测 — P1.1。

净室重写覆盖:
- 涨停判定 (10% 阈值 + 0.5% 容差)
- 连板数累加（昨日 N 板 → 今日 N+1）
- 5+板合并
- 封板率
- 晋级率 (by_board + overall)
- 边界: 空输入、全未涨停、prev_close=0
"""
from __future__ import annotations

from app.custom.research._tests_.fixtures import quotes as fx
from app.custom.research.short_term import (
    Quote,
    aggregate_limit_up_pool,
    build_ladder,
    compute_promotion_rate,
    compute_stats,
)


# === 涨停判定 ===
def test_quote_is_limit_up_main_board() -> None:
    """主板 10% 涨停 → True。"""
    q = Quote(symbol="x", name="x", trade_date="2026-10-16",
              prev_close=10.0, last_close=11.0, high=11.0, low=10.5,
              volume=1, amount=1)
    assert q.change_pct == 0.1
    assert q.is_limit_up is True


def test_quote_is_limit_up_threshold_within_tolerance() -> None:
    """9.5% 容差内仍判为涨停。"""
    q = Quote(symbol="x", name="x", trade_date="2026-10-16",
              prev_close=10.0, last_close=10.96, high=10.96, low=10.5,
              volume=1, amount=1)
    # 0.096 >= 0.095
    assert q.is_limit_up is True


def test_quote_is_limit_up_below_threshold() -> None:
    """9.4% (低于 0.095) → False。"""
    q = Quote(symbol="x", name="x", trade_date="2026-10-16",
              prev_close=10.0, last_close=10.94, high=10.94, low=10.5,
              volume=1, amount=1)
    assert q.is_limit_up is False


def test_quote_change_pct_zero_prev() -> None:
    """prev_close=0 → change_pct=0, 不涨停。"""
    q = Quote(symbol="x", name="x", trade_date="2026-10-16",
              prev_close=0.0, last_close=10.0, high=10.0, low=10.0,
              volume=0, amount=0)
    assert q.change_pct == 0.0
    assert q.is_limit_up is False


# === 聚合涨停池 ===
def test_aggregate_limit_up_pool_basic() -> None:
    """基础样本: 11 只股票里应有 8 只涨停 (主板 1板3 + 2板2 + 3板1 + 5+1 + 创业板20% 1板1)。

    创业板 20% 涨停也满足 change_pct >= 0.095 阈值, 故被判为涨停 (1 板)。
    炸板 600088 (change_pct=0.08 < 0.095) 不算涨停。
    """
    items, stats = aggregate_limit_up_pool(fx.sample_quotes_basic(), fx.prev_pool_basic())
    assert len(items) == 8, f"实际 {len(items)} 只涨停"
    assert stats["limit_up_total"] == 8
    assert stats["limit_down_total"] == 1
    assert stats["exploded_total"] == 1


def test_aggregate_board_count_correct() -> None:
    """连板数正确: 昨 1板 → 今 2板; 昨 2板 → 今 3板; 昨 6板 → 今 7板。"""
    items, _ = aggregate_limit_up_pool(fx.sample_quotes_basic(), fx.prev_pool_basic())
    by_sym = {it.symbol: it.board_count for it in items}
    assert by_sym["600010"] == 2  # 昨 1板
    assert by_sym["600011"] == 2  # 昨 1板
    assert by_sym["600020"] == 3  # 昨 2板
    assert by_sym["600030"] == 7  # 昨 6板
    # 昨未涨停的今日首次涨停 → 1板
    assert by_sym["600001"] == 1
    assert by_sym["600002"] == 1
    assert by_sym["600003"] == 1


def test_aggregate_sorted_by_board_desc_time_asc() -> None:
    """排序: 连板数倒序, 同板数按封板时间正序。"""
    items, _ = aggregate_limit_up_pool(fx.sample_quotes_basic(), fx.prev_pool_basic())
    boards = [it.board_count for it in items]
    # 第一个应是 7 板 (600030, 09:30:00)
    assert boards[0] == 7
    # 末尾应是 1 板
    assert boards[-1] == 1


def test_aggregate_empty_input() -> None:
    """空输入 → 空池, stats 0。"""
    items, stats = aggregate_limit_up_pool(fx.empty_quotes())
    assert items == []
    assert stats["limit_up_total"] == 0


def test_aggregate_no_limit_up() -> None:
    """全未涨停 → 空池。"""
    items, stats = aggregate_limit_up_pool(fx.all_unrelated_quotes())
    assert items == []
    assert stats["limit_up_total"] == 0


def test_aggregate_without_prev_pool() -> None:
    """不传 prev_pool → 全部今日 1 板。"""
    items, _ = aggregate_limit_up_pool(fx.sample_quotes_basic(), prev_pool=None)
    by_sym = {it.symbol: it.board_count for it in items}
    # 600020 / 600030 今日无 prev_board 也都判 1 板
    assert by_sym["600030"] == 1
    assert by_sym["600020"] == 1


# === 梯队分组 ===
def test_build_ladder_grouping() -> None:
    """梯队分组: 1/2/3 各一组, 5+板 单独一组 (含创业板 1 板)。"""
    items, _ = aggregate_limit_up_pool(fx.sample_quotes_basic(), fx.prev_pool_basic())
    ladders = build_ladder(items)
    by_board = {lad.board: lad for lad in ladders}
    assert 1 in by_board
    assert 2 in by_board
    assert 3 in by_board
    assert 5 in by_board  # 5+ 用 5 占位
    assert by_board[1].count == 4  # 主板 3 + 创业板 1
    assert by_board[2].count == 2
    assert by_board[3].count == 1
    assert by_board[5].count == 1


def test_build_ladder_limit_times() -> None:
    """封板时间: first=最早, last=最晚。"""
    items, _ = aggregate_limit_up_pool(fx.sample_quotes_basic(), fx.prev_pool_basic())
    ladders = build_ladder(items)
    one = next(l for l in ladders if l.board == 1)
    # 1板: 10:30 / 13:05 / 14:00
    assert one.first_limit_time == "10:30:00"
    assert one.last_limit_time == "14:00:00"


def test_build_ladder_sorted_desc() -> None:
    """梯队按板数从高到低。"""
    items, _ = aggregate_limit_up_pool(fx.sample_quotes_basic(), fx.prev_pool_basic())
    ladders = build_ladder(items)
    boards = [l.board for l in ladders]
    assert boards == sorted(boards, reverse=True)


# === 晋级率 ===
def test_promotion_rate_basic() -> None:
    """晋级率: 昨 1板 2 只, 今日 600010/600011 都涨停 → 1.0;
    昨 2板 1 只 (600020), 今日 600020 涨停 → 1.0;
    昨 6板 1 只 (600030), 今日 600030 涨停 → 1.0;
    overall = 4 / 4 = 1.0。"""
    items, _ = aggregate_limit_up_pool(fx.sample_quotes_basic(), fx.prev_pool_basic())
    rate = compute_promotion_rate(items, fx.prev_pool_basic())
    assert rate["by_board"]["1"] == 1.0
    assert rate["by_board"]["2"] == 1.0
    assert rate["by_board"]["6"] == 1.0
    assert rate["overall"] == 1.0


def test_promotion_rate_no_prev_pool() -> None:
    """无 prev_pool → 0.0, 空 by_board。"""
    items, _ = aggregate_limit_up_pool(fx.sample_quotes_basic(), prev_pool=None)
    rate = compute_promotion_rate(items, None)
    assert rate["by_board"] == {}
    assert rate["overall"] == 0.0


def test_promotion_rate_partial() -> None:
    """部分晋级: 昨 1板 2 只, 今日只 1 只涨停 → 0.5。"""
    # 自造: 昨 1板 600100, 今日 600100 涨停
    today = "2026-10-16"
    today_quotes = [
        Quote(symbol="600100", name="晋级", trade_date=today,
              prev_close=10.0, last_close=10.97, high=10.97, low=10.5,
              volume=1, amount=1, limit_time="10:00:00"),
        Quote(symbol="600200", name="未晋级", trade_date=today,
              prev_close=10.0, last_close=10.50, high=10.5, low=10.3,
              volume=1, amount=1),
    ]
    items, _ = aggregate_limit_up_pool(today_quotes)
    prev_pool = [
        {"symbol": "600100", "board_count": 1},
        {"symbol": "600200", "board_count": 1},
    ]
    rate = compute_promotion_rate(items, prev_pool)
    assert rate["by_board"]["1"] == 0.5
    assert rate["overall"] == 0.5


# === 综合统计 ===
def test_compute_stats_basic() -> None:
    """综合统计: limit_up_total / max_board / seal_rate / ladder_count。"""
    items, _ = aggregate_limit_up_pool(fx.sample_quotes_basic(), fx.prev_pool_basic())
    ladders = build_ladder(items)
    stats = compute_stats(items, ladders, total_quotes=11)
    assert stats["limit_up_total"] == 8
    assert stats["max_board"] == 7
    # 8 只涨停都有 limit_time → seal_rate = 1.0
    assert stats["seal_rate"] == 1.0
    assert stats["ladder_count"] == 4  # 1/2/3/5+


def test_compute_stats_no_limit_up() -> None:
    """无涨停 → max_board=0, seal_rate=0.0。"""
    items, _ = aggregate_limit_up_pool(fx.all_unrelated_quotes())
    ladders = build_ladder(items)
    stats = compute_stats(items, ladders, total_quotes=5)
    assert stats["limit_up_total"] == 0
    assert stats["max_board"] == 0
    assert stats["seal_rate"] == 0.0
    assert stats["ladder_count"] == 0


def test_limit_up_item_to_dict() -> None:
    """to_dict 字段完整。"""
    items, _ = aggregate_limit_up_pool(fx.sample_quotes_basic(), fx.prev_pool_basic())
    d = items[0].to_dict()
    expected_keys = {
        "symbol", "name", "trade_date", "limit_time", "first_limit_price",
        "last_close", "turnover", "amount", "industry", "theme_tags", "board_count",
    }
    assert set(d.keys()) == expected_keys