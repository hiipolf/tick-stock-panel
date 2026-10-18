"""短线连板 v2 算法 — P2 v2.0b。

净室边界 (用户 2026-10-16 决策):
- 字段名借鉴 easy-stock (emotion_score / theme_heat / promotion_by_board)
- 5 维加权公式 IRP 自创 (不复制 Go 源码权重)
- 主题名 IRP 自创 (5 个内置主题)

6 算法:
1. compute_emotion_score         5 维加权 (涨停广度/封板强度/高度/连板晋级/主线集中度)
2. judge_emotion_phase          5 档判定 (高潮/活跃/分歧/退潮/冰点)
3. compute_secondary_indicators  4 维辅助 (高位均值/下跌覆盖/高度坍缩/高度晋级)
4. compute_deep_metrics          6 字段深度 (最终炸板率/昨日涨停反馈/昨日连板反馈/
                                          连板晋级率/主线集中度/收盘高位风险)
5. compute_promotion_by_board    4 段晋级率 (1进2/2进3/3进4/5进6)
6. compute_theme_heat            5 维加权热度 (涨停广度/连板数/高度/昨日延续/概念宽泛度)

所有算法是纯函数, 可独立测试, 接受 dataclass 输入, 返回 dataclass / dict。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from app.custom.research.short_term import LimitUpItem

# 5 维权重 (IRP 净室; 不与 easy-stock 1:1 一致)
_EMOTION_W = {
    'limit_up_breadth': 0.30,
    'seal_strength':    0.25,
    'max_height':       0.20,
    'consecutive_promotion': 0.15,
    'main_line_concentration': 0.10,
}

HISTORY_LIMIT_UP_AVG = 40
FULL_SCORE_BOARDS = 11


# ============================================================
# 1. 情绪分数 (5 维加权 -> 0-100)
# ============================================================

@dataclass
class SecondaryIndicators:
    """4 维辅助指标 (用于情绪时间轴右侧卡片)."""
    high_pos_avg: float
    decline_coverage: float
    high_collapse: int
    high_promotion: float


def compute_emotion_score(
    today_pool: list[LimitUpItem],
    history_30d: list[dict] | None = None,
    industry_map: dict[str, int] | None = None,
) -> float:
    """5 维加权 -> 0-100."""
    _ = history_30d
    industry_map = industry_map or {}

    limit_up_count = len(today_pool)
    breadth = min(limit_up_count / HISTORY_LIMIT_UP_AVG, 1.0) if HISTORY_LIMIT_UP_AVG > 0 else 0.0

    sealed = sum(1 for it in today_pool if it.limit_time is not None)
    seal_strength = sealed / limit_up_count if limit_up_count else 0.0

    max_height = max((it.board_count for it in today_pool), default=0)
    height_norm = min(max_height / FULL_SCORE_BOARDS, 1.0)

    consecutive_promotion = 0.5
    if history_30d and 'consecutive_promotion' in (history_30d[0] if history_30d else {}):
        consecutive_promotion = history_30d[0].get('consecutive_promotion', 0.5)

    total = sum(industry_map.values()) or 1
    max_industry = max(industry_map.values()) if industry_map else 0
    main_line = max_industry / total

    score = 100 * (
        _EMOTION_W['limit_up_breadth'] * breadth
        + _EMOTION_W['seal_strength'] * seal_strength
        + _EMOTION_W['max_height'] * height_norm
        + _EMOTION_W['consecutive_promotion'] * consecutive_promotion
        + _EMOTION_W['main_line_concentration'] * main_line
    )
    return round(max(0.0, min(100.0, score)), 2)


def judge_emotion_phase(
    today_score: float,
    history_30d: list[dict] | None = None,
) -> str:
    """5 档判定: 阈值法 (短路版)."""
    _ = history_30d
    if today_score >= 80:
        return '\u9ad8\u6f6e'  # 高潮
    if today_score >= 60:
        return '\u6d3b\u8dc3'  # 活跃
    if today_score >= 40:
        return '\u5206\u6b67'  # 分歧
    if today_score >= 20:
        return '\u9000\u6f6e'  # 退潮
    return '\u51b0\u70b9'  # 冰点


# ============================================================
# 2. 4 维辅助指标
# ============================================================

def compute_secondary_indicators(
    today_pool: list[LimitUpItem],
    prev_pool: list[dict] | None = None,
) -> SecondaryIndicators:
    """4 维辅助指标: 高位均值 / 下跌覆盖 / 高度坍缩 / 高度晋级."""
    prev_pool = prev_pool or []
    prev_map = {p.get('symbol'): p for p in prev_pool}

    high_pos_pcts: list[float] = []
    for it in today_pool:
        if it.board_count >= 5 and it.amount > 0:
            high_pos_pcts.append(0.10 * it.board_count)
    high_pos_avg = sum(high_pos_pcts) / len(high_pos_pcts) if high_pos_pcts else 0.0

    first_board_promoted = [it for it in today_pool if it.board_count == 1]
    if first_board_promoted:
        declined = sum(
            1 for it in first_board_promoted
            if it.symbol in prev_map and prev_map[it.symbol].get('board_count', 0) == 0
        )
        decline_coverage = declined / len(first_board_promoted)
    else:
        decline_coverage = 0.0

    high_collapse = sum(
        1 for p in prev_pool
        if p.get('board_count', 0) >= 5 and not any(
            it.symbol == p['symbol'] and it.board_count >= 5
            for it in today_pool
        )
    )

    high_prev = [p for p in prev_pool if p.get('board_count', 0) >= 5]
    if high_prev:
        promoted_high = sum(
            1 for p in high_prev
            if any(it.symbol == p['symbol'] and it.board_count >= p['board_count'] + 1 for it in today_pool)
        )
        high_promotion = promoted_high / len(high_prev)
    else:
        high_promotion = 0.0

    return SecondaryIndicators(
        high_pos_avg=round(high_pos_avg, 4),
        decline_coverage=round(decline_coverage, 4),
        high_collapse=high_collapse,
        high_promotion=round(high_promotion, 4),
    )


# ============================================================
# 3. 6 字段深度指标
# ============================================================

@dataclass
class DeepMetrics:
    final_explode_rate: float
    prev_limit_up_feedback: float
    prev_consecutive_feedback: float
    promotion_by_board_overall: float
    main_line_concentration: float
    high_risk_score: int


def compute_deep_metrics(
    today_pool: list[LimitUpItem],
    prev_pool: list[dict] | None = None,
    industry_map: dict[str, int] | None = None,
    exploded_count: int = 0,
) -> DeepMetrics:
    """6 字段深度指标."""
    prev_pool = prev_pool or []
    industry_map = industry_map or {}
    today_count = len(today_pool)

    final_explode = exploded_count / (today_count + exploded_count) if (today_count + exploded_count) else 0.0

    prev_syms = {p['symbol'] for p in prev_pool}
    today_prev = [it for it in today_pool if it.symbol in prev_syms]
    if today_prev:
        avg_change = sum(0.10 * it.board_count for it in today_prev) / len(today_prev)
        prev_limit_feedback = round(avg_change - 0.10, 4)
    else:
        prev_limit_feedback = 0.0

    prev_consec = [p for p in prev_pool if p.get('board_count', 0) >= 2]
    today_prev_consec = [it for it in today_pool if any(p['symbol'] == it.symbol for p in prev_consec)]
    if today_prev_consec:
        avg_change_c = sum(0.10 * it.board_count for it in today_prev_consec) / len(today_prev_consec)
        prev_consec_feedback = round(avg_change_c - 0.10, 4)
    else:
        prev_consec_feedback = 0.0

    promoted = [it for it in today_pool if it.board_count >= 2 and it.symbol in prev_syms]
    promotion_overall = len(promoted) / len(prev_pool) if prev_pool else 0.0

    total = sum(industry_map.values()) or 1
    max_ind = max(industry_map.values()) if industry_map else 0
    main_line = max_ind / total if total else 0.0

    high_5plus = sum(1 for it in today_pool if it.board_count >= 5)
    high_risk = min(100, high_5plus * 10 + high_5plus * 5 + int(final_explode * 100 * 0.3))

    return DeepMetrics(
        final_explode_rate=round(final_explode, 4),
        prev_limit_up_feedback=prev_limit_feedback,
        prev_consecutive_feedback=prev_consec_feedback,
        promotion_by_board_overall=round(promotion_overall, 4),
        main_line_concentration=round(main_line, 4),
        high_risk_score=high_risk,
    )


# ============================================================
# 4. 4 段晋级率
# ============================================================

@dataclass
class PromotionSeg:
    from_board: int
    to_label: str
    promoted: int
    total: int
    ratio: float


PROMOTION_SEGS = [
    (1, '1\u8fdb2', 2),     # 1进2
    (2, '2\u8fdb3', 3),     # 2进3
    (3, '3\u8fdb4', 4),     # 3进4
    (5, '5\u8fdb6', 6),     # 5进6
]


def compute_promotion_by_board(
    today_pool: list[LimitUpItem],
    prev_pool: list[dict] | None = None,
) -> list[PromotionSeg]:
    """4 段晋级率进度条 (1进2/2进3/3进4/5进6)."""
    prev_pool = prev_pool or []
    prev_by_board: dict[int, list[str]] = {}
    for p in prev_pool:
        b = p.get('board_count', 0)
        prev_by_board.setdefault(b, []).append(p['symbol'])

    results: list[PromotionSeg] = []
    for from_board, to_label, to_board in PROMOTION_SEGS:
        prev_syms = prev_by_board.get(from_board, [])
        if not prev_syms:
            results.append(PromotionSeg(from_board=from_board, to_label=to_label, promoted=0, total=0, ratio=0.0))
            continue
        promoted = sum(
            1 for sym in prev_syms
            if any(it.symbol == sym and it.board_count >= to_board for it in today_pool)
        )
        results.append(PromotionSeg(
            from_board=from_board,
            to_label=to_label,
            promoted=promoted,
            total=len(prev_syms),
            ratio=round(promoted / len(prev_syms), 4),
        ))
    return results


# ============================================================
# 5. 5 维概念热度
# ============================================================

@dataclass
class ThemeHeat:
    theme_id: str
    theme_name: str
    max_board: int
    limit_up_count: int
    consecutive_count: int
    heat_score: float


THEME_HEAT_W = {
    'breadth':       0.30,
    'consecutive':   0.20,
    'height':        0.20,
    'continuation':  0.15,
    'specificity':   0.15,
}

THEME_BREADTH_FULL = 10
THEME_CONSECUTIVE_FULL = 5
THEME_HEIGHT_FULL = 11


def compute_theme_heat(
    theme_chains: dict,
    today_pool: list[LimitUpItem],
    prev_pool: list[dict] | None = None,
) -> list[ThemeHeat]:
    """5 维加权热度 (涨停广度+连板数+高度+昨日延续+概念宽泛度反向)."""
    prev_pool = prev_pool or []

    results: list[ThemeHeat] = []
    for theme_id, chain in theme_chains.items():
        theme_stocks: set[str] = set()
        for node in chain.chain:
            theme_stocks.update(node.stocks)
        in_today = [it for it in today_pool if it.symbol in theme_stocks]
        if not in_today:
            continue
        limit_up_count = len(in_today)
        max_board = max((it.board_count for it in in_today), default=0)
        consecutive_count = sum(1 for it in in_today if it.board_count >= 2)
        prev_in_theme = sum(1 for p in prev_pool if p['symbol'] in theme_stocks)
        cont = (prev_in_theme / limit_up_count) if limit_up_count else 0
        cont = min(cont, 1.0)
        in_industries = {it.industry for it in in_today if it.industry}
        breadth = len(in_industries) / max(limit_up_count, 1)
        specificity = 1 - min(breadth, 1.0)

        heat = 100 * (
            THEME_HEAT_W['breadth'] * min(limit_up_count / THEME_BREADTH_FULL, 1.0)
            + THEME_HEAT_W['consecutive'] * min(consecutive_count / THEME_CONSECUTIVE_FULL, 1.0)
            + THEME_HEAT_W['height'] * min(max_board / THEME_HEIGHT_FULL, 1.0)
            + THEME_HEAT_W['continuation'] * cont
            + THEME_HEAT_W['specificity'] * specificity
        )
        results.append(ThemeHeat(
            theme_id=theme_id,
            theme_name=chain.theme_name,
            max_board=max_board,
            limit_up_count=limit_up_count,
            consecutive_count=consecutive_count,
            heat_score=round(max(0.0, min(100.0, heat)), 1),
        ))
    results.sort(key=lambda h: -h.heat_score)
    return results
