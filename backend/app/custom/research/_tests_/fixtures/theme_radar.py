"""趋势题材雷达快照 fixtures — P1.1。"""
from __future__ import annotations

from app.custom.research.theme_radar import ThemeSnapshot


def history_30d_dense() -> list[ThemeSnapshot]:
    """30 日历史 + 今日 — 5 阶段判定用。"""
    snapshots = []
    # 30 日背景: 中等强度震荡
    for i in range(30):
        snapshots.append(ThemeSnapshot(
            theme_id="ai_chain",
            theme_name="AI 算力链",
            trade_date=f"2026-09-{16 + (i % 15):02d}",
            change_pct=0.02 + (i % 5) * 0.005,
            width=0.5 + (i % 7) * 0.05,
            consecutive_days=i + 1,
            rank=i % 5 + 1,
        ))
    return snapshots


def today_high_phase() -> ThemeSnapshot:
    """今日: 高潮（trend_score 应当进入顶部 10%）。"""
    return ThemeSnapshot(
        theme_id="ai_chain",
        theme_name="AI 算力链",
        trade_date="2026-10-16",
        change_pct=0.099,  # 接近 10%
        width=0.95,
        consecutive_days=30,
        rank=1,
    )


def today_burst_phase() -> ThemeSnapshot:
    """今日: 启动（底部反弹 +20）。"""
    return ThemeSnapshot(
        theme_id="ai_chain",
        theme_name="AI 算力链",
        trade_date="2026-10-16",
        change_pct=0.06,
        width=0.70,
        consecutive_days=15,
        rank=3,
    )


def history_30d_with_bottom() -> list[ThemeSnapshot]:
    """历史: 中部 20 天 + 底部 10 天。"""
    snapshots = []
    for i in range(20):
        snapshots.append(ThemeSnapshot(
            theme_id="ai_chain", theme_name="AI 算力链",
            trade_date=f"2026-09-{20 + (i % 10):02d}",
            change_pct=0.04 + (i % 4) * 0.005,
            width=0.6, consecutive_days=i + 1, rank=2,
        ))
    # 底部 10 天
    for i in range(10):
        snapshots.append(ThemeSnapshot(
            theme_id="ai_chain", theme_name="AI 算力链",
            trade_date=f"2026-10-{6 + i:02d}",
            change_pct=0.01, width=0.2, consecutive_days=i + 1, rank=15,
        ))
    return snapshots


def today_quiet_phase() -> ThemeSnapshot:
    """今日: 冰点（底部 10%）。"""
    return ThemeSnapshot(
        theme_id="ai_chain", theme_name="AI 算力链",
        trade_date="2026-10-16",
        change_pct=-0.05, width=0.1, consecutive_days=1, rank=20,
    )


def short_history() -> list[ThemeSnapshot]:
    """不足 5 日历史 — 应当返回 '未知'。"""
    return [
        ThemeSnapshot(
            theme_id="t", theme_name="t", trade_date="2026-10-15",
            change_pct=0.03, width=0.5, consecutive_days=1, rank=3,
        ),
    ]