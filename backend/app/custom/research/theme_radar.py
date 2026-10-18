"""趋势题材雷达 — P1.1 核心算法。

净室重写自 easy-stock `internal/sector/radar.go` + `radar_fusion.go`：
- 趋势分数（涨跌强度 + 上涨宽度 + 持续天数加权）
- 5 阶段判定（启动 / 高潮 / 退潮 / 修复 / 冰点）

业务函数是纯函数，可独立测试。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ThemeSnapshot:
    """单个题材某日快照（来自 TSP 65 端点 get_sector_rotation）。"""
    theme_id: str
    theme_name: str
    trade_date: str
    change_pct: float  # 板块涨跌（小数制）
    width: float  # 上涨宽度（0-1）
    consecutive_days: int  # 连续出现在榜的天数
    rank: int  # 当前排名


def compute_trend_score(theme: ThemeSnapshot) -> float:
    """趋势分数 = 0.4 * 涨跌强度 + 0.3 * 上涨宽度 + 0.3 * 持续天数（归一化到 0-100）。

    涨跌强度归一化：change_pct ∈ [-0.1, 0.1] → [0, 100]
    上涨宽度：width ∈ [0, 1] → [0, 100]
    持续天数：consecutive_days ∈ [0, 30] → [0, 100]（超过 30 算 100）
    """
    intensity = max(0.0, min(theme.change_pct, 0.1)) * 1000  # [-0.1, 0.1] → [0, 100]
    width_score = theme.width * 100
    duration = min(theme.consecutive_days / 30.0, 1.0) * 100
    return round(0.4 * intensity + 0.3 * width_score + 0.3 * duration, 4)


def judge_phase(
    today: ThemeSnapshot,
    history_30d: list[ThemeSnapshot],
) -> str:
    """5 阶段判定：启动 / 高潮 / 退潮 / 修复 / 冰点。

    简化规则（v1.0）：
    - 启动：trend_score 比 30 日底部反弹 +20
    - 高潮：trend_score 在 30 日前 10% 区间
    - 退潮：trend_score 从 5 日高点回落 -15
    - 修复：trend_score 从底部反弹 +10（未到高潮）
    - 冰点：trend_score 在 30 日底部 10%
    """
    if len(history_30d) < 5:
        return "未知"

    today_score = compute_trend_score(today)
    sorted_scores = sorted(compute_trend_score(h) for h in history_30d)
    bottom_10_pct = sorted_scores[max(0, int(len(sorted_scores) * 0.1))]
    top_10_pct = sorted_scores[min(len(sorted_scores) - 1, int(len(sorted_scores) * 0.9))]

    recent_5_scores = [compute_trend_score(h) for h in history_30d[-5:]]
    recent_5_high = max(recent_5_scores) if recent_5_scores else today_score

    if today_score >= top_10_pct:
        return "高潮"
    if today_score <= bottom_10_pct:
        return "冰点"
    if today_score < recent_5_high - 15:
        return "退潮"
    # 反弹检测：与 30 日底部比较
    sorted_scores_no_today = sorted(compute_trend_score(h) for h in history_30d)
    bottom_no_today = sorted_scores_no_today[0]
    if today_score >= bottom_no_today + 20:
        return "启动"
    if today_score >= bottom_no_today + 10:
        return "修复"
    return "震荡"
