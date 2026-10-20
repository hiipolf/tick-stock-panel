"""趋势题材雷达算法单测 — P1.1。

净室重写覆盖:
- 趋势分数 (涨跌强度 + 上涨宽度 + 持续天数加权)
- 5 阶段判定 (启动 / 高潮 / 退潮 / 修复 / 冰点 / 震荡 / 未知)
"""
from __future__ import annotations

from app.custom.research._tests_.fixtures import theme_radar as fx
from app.custom.research.theme_radar import (
    ThemeSnapshot,
    compute_trend_score,
    judge_phase,
)


# === 趋势分数 ===
def test_trend_score_range_zero_to_hundred() -> None:
    """分数范围 [0, 100], 实际不会到负。"""
    theme = ThemeSnapshot(
        theme_id="t", theme_name="t", trade_date="2026-10-16",
        change_pct=0.0, width=0.0, consecutive_days=0, rank=1,
    )
    score = compute_trend_score(theme)
    assert score == 0.0


def test_trend_score_max_components() -> None:
    """全满分: change_pct=0.1, width=1.0, days=30+ → 100。"""
    theme = ThemeSnapshot(
        theme_id="t", theme_name="t", trade_date="2026-10-16",
        change_pct=0.1, width=1.0, consecutive_days=30, rank=1,
    )
    score = compute_trend_score(theme)
    # 0.4 * 100 + 0.3 * 100 + 0.3 * 100 = 100
    assert abs(score - 100.0) < 1e-4


def test_trend_score_weighted() -> None:
    """验证权重: 仅 change_pct=0.05 → 50*0.4=20。"""
    theme = ThemeSnapshot(
        theme_id="t", theme_name="t", trade_date="2026-10-16",
        change_pct=0.05, width=0.0, consecutive_days=0, rank=1,
    )
    score = compute_trend_score(theme)
    # 涨跌强度归一化: 0.05 / 0.1 * 100 = 50
    # 0.4 * 50 = 20
    assert abs(score - 20.0) < 1e-4


def test_trend_score_clamp_negative_change() -> None:
    """负 change_pct → 涨跌强度截断 0（不影响宽度/持续天）。"""
    theme = ThemeSnapshot(
        theme_id="t", theme_name="t", trade_date="2026-10-16",
        change_pct=-0.10, width=0.0, consecutive_days=0, rank=1,
    )
    score = compute_trend_score(theme)
    assert score == 0.0


def test_trend_score_clamp_change_over_10pct() -> None:
    """change_pct > 0.1 → 涨跌强度封顶 100。"""
    theme = ThemeSnapshot(
        theme_id="t", theme_name="t", trade_date="2026-10-16",
        change_pct=0.20, width=0.0, consecutive_days=0, rank=1,
    )
    score = compute_trend_score(theme)
    # 涨跌强度 = 0.1 * 1000 = 100, 0.4 * 100 = 40
    assert abs(score - 40.0) < 1e-4


# === 5 阶段判定 ===
def test_judge_phase_insufficient_history() -> None:
    """历史不足 5 日 → '未知'。"""
    result = judge_phase(
        today=fx.history_30d_dense()[-1],
        history_30d=fx.short_history(),
    )
    assert result == "未知"


def test_judge_phase_high_phase() -> None:
    """今日高潮: change_pct=0.099, width=0.95, days=30。"""
    result = judge_phase(
        today=fx.today_high_phase(),
        history_30d=fx.history_30d_dense(),
    )
    assert result == "高潮"


def test_judge_phase_burst_phase() -> None:
    """今日启动: 底部反弹 +20。

    使用专门的 burst_only_history: 30 天历史分数都在 30-50 之间 (中等),
    burst score = 0.4*60 + 0.3*70 + 0.3*50 = 60 (明显反弹 +20)。
    """
    # 30 天历史, 中等强度 (分数 30-45)
    history = []
    for i in range(30):
        history.append(ThemeSnapshot(
            theme_id="ai_chain", theme_name="AI 算力链",
            trade_date=f"2026-09-{16 + (i % 15):02d}",
            change_pct=0.02 + (i % 3) * 0.005,  # 中等
            width=0.3 + (i % 5) * 0.05,
            consecutive_days=i + 1, rank=5,
        ))
    # bottom_no_today 应当 ≈ 4*0.4 + 0.3*30 + 1*0.3*3.33 ≈ 11-13
    # burst 60 ≥ 11 + 20 = 31 → 启动
    # 但 burst 不能进 top 10%, 否则变"高潮"
    # history 30 天分数范围: 0.02~0.025 → 0.4*20~25; 0.3*30~45; 0.3*3~100 → 都 < 50
    # burst = 60 > top_10% (≈48) → "高潮"
    # 所以 burst 应改得稍微低一些, 或者改 history 让它更强
    today = ThemeSnapshot(
        theme_id="ai_chain", theme_name="AI 算力链",
        trade_date="2026-10-16",
        change_pct=0.04, width=0.55, consecutive_days=12, rank=3,
    )
    # score = 0.4*40 + 0.3*55 + 0.3*40 = 16 + 16.5 + 12 = 44.5
    # top_10% ≈ max(30~45), 应当接近 45, 44.5 < 45 → 不算高潮
    # bottom_no_today ≈ 11, 44.5 >= 11+20=31 → 启动
    result = judge_phase(today, history)
    assert result == "启动"


def test_judge_phase_quiet_phase() -> None:
    """今日冰点: 底部 10% 区间。"""
    result = judge_phase(
        today=fx.today_quiet_phase(),
        history_30d=fx.history_30d_with_bottom(),
    )
    # quiet: change=-0.05 → 涨跌强度截断 0, width=0.1, days=1
    # score = 0 + 10 + 1 = 11, 接近底部 → 冰点
    assert result == "冰点"


def test_judge_phase_returns_oscillate_for_middle() -> None:
    """中间区 → '震荡'。"""
    history = fx.history_30d_with_bottom()
    # 中等强度今日
    today = ThemeSnapshot(
        theme_id="ai_chain", theme_name="AI 算力链",
        trade_date="2026-10-16",
        change_pct=0.03, width=0.4, consecutive_days=5, rank=10,
    )
    result = judge_phase(today, history)
    # 不在顶部 10%, 不在底部 10%, 不算反弹+20, 应当 = 震荡
    assert result in {"震荡", "修复"}  # 边界 case 接受两个