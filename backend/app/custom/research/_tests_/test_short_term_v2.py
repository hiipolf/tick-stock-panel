"""P2 v2.0b 单测 — 6 个新算法覆盖."""
from __future__ import annotations

from app.custom.research.short_term import LimitUpItem
from app.custom.research.short_term_v2 import (
    PROMOTION_SEGS,
    compute_deep_metrics,
    compute_emotion_score,
    compute_promotion_by_board,
    compute_secondary_indicators,
    compute_theme_heat,
    judge_emotion_phase,
)


def _item(symbol: str, board: int, amount: float = 1_000_000, industry: str = '', limit_time: str | None = '10:00:00') -> LimitUpItem:
    return LimitUpItem(
        symbol=symbol, name=symbol, trade_date='2026-10-16',
        limit_time=limit_time, first_limit_price=10.0 + board * 0.1,
        last_close=10.0 + board * 0.1, turnover=0.0, amount=amount,
        industry=industry, theme_tags=[], board_count=board,
    )


# ============================================================
# compute_emotion_score
# ============================================================

def test_emotion_score_empty() -> None:
    """空涨停池 → 0 涨停 → 广度 0, 但封板率 0, 高度 0, 默认 0.5 + 0 集中度 → 7.5."""
    score = compute_emotion_score([])
    assert 0 <= score <= 100
    assert score == 7.5  # 0.15 * 0.5 * 100 = 7.5


def test_emotion_score_full_mark() -> None:
    """12 只涨停, 全封板, 最高 11 板, 主线集中 1.0 → 接近 100."""
    today = [_item(f's{i}', 11 if i == 0 else 1, amount=10_000_000) for i in range(12)]
    industry_map = {'传媒': 12}
    score = compute_emotion_score(today, industry_map=industry_map)
    # breadth 12/40 = 0.3; seal 12/12 = 1; height 11/11 = 1; promotion 0.5; main 1.0
    # 0.30*30 + 0.25*100 + 0.20*100 + 0.15*50 + 0.10*100 = 9+25+20+7.5+10 = 71.5
    assert 70 <= score <= 72


def test_emotion_score_clamp() -> None:
    """分数 clamp [0, 100]."""
    today = [_item(f's{i}', 11, amount=10_000_000) for i in range(100)]
    score = compute_emotion_score(today, industry_map={f'行业{i}': 1 for i in range(100)})
    assert 0 <= score <= 100


def test_emotion_score_height_full() -> None:
    """11 板满分归一化 → 1.0."""
    today = [_item('s1', 11), _item('s2', 5)]
    score_11 = compute_emotion_score(today, industry_map={})
    today_5 = [_item('s1', 5), _item('s2', 5)]
    score_5 = compute_emotion_score(today_5, industry_map={})
    # 11 板 height_norm=1.0; 5 板 height_norm=5/11≈0.45
    assert score_11 > score_5


# ============================================================
# judge_emotion_phase
# ============================================================

def test_judge_phase_high() -> None:
    assert judge_emotion_phase(85) == '\u9ad8\u6f6e'  # 高潮
    assert judge_emotion_phase(80) == '\u9ad8\u6f6e'


def test_judge_phase_active() -> None:
    assert judge_emotion_phase(70) == '\u6d3b\u8dc3'  # 活跃
    assert judge_emotion_phase(60) == '\u6d3b\u8dc3'


def test_judge_phase_neutral() -> None:
    assert judge_emotion_phase(50) == '\u5206\u6b67'  # 分歧
    assert judge_emotion_phase(40) == '\u5206\u6b67'


def test_judge_phase_recession() -> None:
    assert judge_emotion_phase(30) == '\u9000\u6f6e'  # 退潮
    assert judge_emotion_phase(20) == '\u9000\u6f6e'


def test_judge_phase_ice() -> None:
    assert judge_emotion_phase(15) == '\u51b0\u70b9'  # 冰点
    assert judge_emotion_phase(0) == '\u51b0\u70b9'


# ============================================================
# compute_secondary_indicators
# ============================================================

def test_secondary_high_pos_avg() -> None:
    """5+ 板涨停股的隐含涨幅均值."""
    today = [_item('s1', 7), _item('s2', 5)]
    res = compute_secondary_indicators(today)
    # (7*0.10 + 5*0.10) / 2 = 0.60
    assert abs(res.high_pos_avg - 0.60) < 1e-4


def test_secondary_high_pos_avg_empty() -> None:
    """无 5+ 板 → 0."""
    today = [_item('s1', 1), _item('s2', 2)]
    res = compute_secondary_indicators(today)
    assert res.high_pos_avg == 0.0


def test_secondary_high_collapse() -> None:
    """昨 5+ 板今日不在 today_pool → 高度坍缩."""
    today = [_item('s1', 1)]
    prev = [{'symbol': 's1', 'board_count': 6}, {'symbol': 's2', 'board_count': 7}]
    res = compute_secondary_indicators(today, prev_pool=prev)
    # s1 今日 1 板 (不是 7+), s2 不在 today → 2 个坍缩
    assert res.high_collapse == 2


def test_secondary_high_promotion() -> None:
    """昨 5+ 板今日又涨停 → 高度晋级 1.0."""
    today = [_item('s1', 7), _item('s2', 6)]
    prev = [{'symbol': 's1', 'board_count': 6}, {'symbol': 's2', 'board_count': 5}]
    res = compute_secondary_indicators(today, prev_pool=prev)
    assert res.high_promotion == 1.0


def test_secondary_high_promotion_no_history() -> None:
    """无昨 5+ 板 → 0."""
    today = [_item('s1', 1)]
    res = compute_secondary_indicators(today, prev_pool=None)
    assert res.high_promotion == 0.0


# ============================================================
# compute_deep_metrics
# ============================================================

def test_deep_explode_rate() -> None:
    """3 涨停 + 3 炸板 = 50%."""
    today = [_item(f's{i}', 1) for i in range(3)]
    res = compute_deep_metrics(today, exploded_count=3)
    assert abs(res.final_explode_rate - 0.5) < 1e-4


def test_deep_explode_rate_zero() -> None:
    """无炸板 → 0."""
    today = [_item('s1', 1)]
    res = compute_deep_metrics(today, exploded_count=0)
    assert res.final_explode_rate == 0.0


def test_deep_promotion_overall() -> None:
    """今 6 只连板 (board>=2) 中 3 只在 prev_pool (共 6 只) → 0.5."""
    today = [_item('s1', 2), _item('s2', 2), _item('s3', 2), _item('s4', 1), _item('s5', 1), _item('s6', 1)]
    prev = [{'symbol': f's{i}', 'board_count': 1} for i in range(1, 7)]
    res = compute_deep_metrics(today, prev_pool=prev)
    assert abs(res.promotion_by_board_overall - 0.5) < 1e-4


def test_deep_main_line_concentration() -> None:
    """max(industry) / total."""
    today = [_item('s1', 1, industry='电子'), _item('s2', 1, industry='电子'),
             _item('s3', 1, industry='医药'), _item('s4', 1, industry='医药')]
    res = compute_deep_metrics(today, industry_map={'电子': 2, '医药': 2})
    assert abs(res.main_line_concentration - 0.5) < 1e-4


def test_deep_main_line_full() -> None:
    """单行业 = 100%."""
    today = [_item('s1', 1, industry='电子'), _item('s2', 1, industry='电子')]
    res = compute_deep_metrics(today, industry_map={'电子': 2})
    assert res.main_line_concentration == 1.0


def test_deep_high_risk_zero() -> None:
    """无 5+ 板 + 无炸板 → 高风险 0."""
    today = [_item('s1', 1), _item('s2', 2)]
    res = compute_deep_metrics(today, exploded_count=0)
    assert res.high_risk_score == 0


def test_deep_high_risk_with_5plus() -> None:
    """3 只 5+ 板 + 5 炸板 → 风险累加."""
    today = [_item('s1', 7), _item('s2', 6), _item('s3', 5), _item('s4', 1)]
    res = compute_deep_metrics(today, exploded_count=5)
    # 3 * 10 + 3 * 5 + int(0.5 * 30) = 30 + 15 + 15 = 60
    assert 50 <= res.high_risk_score <= 70


def test_deep_prev_feedback() -> None:
    """昨 1 板今 2 板 → 反馈 = 0.20 - 0.10 = 0.10."""
    today = [_item('s1', 2)]
    prev = [{'symbol': 's1', 'board_count': 1}]
    res = compute_deep_metrics(today, prev_pool=prev)
    assert abs(res.prev_limit_up_feedback - 0.10) < 1e-4


# ============================================================
# compute_promotion_by_board
# ============================================================

def test_promotion_4_segs() -> None:
    """返回 4 段."""
    today = []
    prev = []
    segs = compute_promotion_by_board(today, prev_pool=prev)
    assert len(segs) == 4
    assert all(s.to_label in {'1\u8fdb2', '2\u8fdb3', '3\u8fdb4', '5\u8fdb6'} for s in segs)


def test_promotion_full_match() -> None:
    """全晋升 → ratio 1.0."""
    today = [
        _item('a', 2), _item('b', 2),  # 1->2 全升
        _item('c', 3),                  # 2->3 全升
    ]
    prev = [
        {'symbol': 'a', 'board_count': 1},
        {'symbol': 'b', 'board_count': 1},
        {'symbol': 'c', 'board_count': 2},
    ]
    segs = compute_promotion_by_board(today, prev_pool=prev)
    seg_map = {s.from_board: s for s in segs}
    assert seg_map[1].ratio == 1.0
    assert seg_map[2].ratio == 1.0


def test_promotion_zero() -> None:
    """全未晋升 → ratio 0."""
    today = [_item('a', 1), _item('b', 1)]  # 没升级
    prev = [{'symbol': 'a', 'board_count': 1}, {'symbol': 'b', 'board_count': 1}]
    segs = compute_promotion_by_board(today, prev_pool=prev)
    seg_map = {s.from_board: s for s in segs}
    assert seg_map[1].ratio == 0.0
    assert seg_map[1].total == 2
    assert seg_map[1].promoted == 0


def test_promotion_partial() -> None:
    """半晋升 → 0.5."""
    today = [_item('a', 2), _item('b', 1)]
    prev = [{'symbol': 'a', 'board_count': 1}, {'symbol': 'b', 'board_count': 1}]
    segs = compute_promotion_by_board(today, prev_pool=prev)
    seg_map = {s.from_board: s for s in segs}
    assert seg_map[1].ratio == 0.5


def test_promotion_empty() -> None:
    """无 prev_pool → 全 0/0."""
    today = [_item('a', 2)]
    segs = compute_promotion_by_board(today, prev_pool=None)
    for s in segs:
        assert s.total == 0
        assert s.promoted == 0
        assert s.ratio == 0.0


# ============================================================
# compute_theme_heat
# ============================================================

def _mock_chain():
    """mock ThemeChain for testing."""
    from app.custom.research.chain import ChainNode, ThemeChain
    return {
        'ai': ThemeChain(
            theme_id='ai',
            theme_name='AI',
            chain=[
                ChainNode(node='光模块', stocks=['s1', 's2'], weight=1.0),
                ChainNode(node='服务器', stocks=['s3'], weight=1.0),
            ],
            industries=['电子', '通信'],
        ),
    }


def test_theme_heat_basic() -> None:
    """3 只涨停, 1 只 2 板, 全 1 行业 → 中等分 (>20)."""
    today = [
        _item('s1', 2, industry='电子'),
        _item('s2', 1, industry='电子'),
        _item('s3', 1, industry='电子'),
    ]
    chains = _mock_chain()
    res = compute_theme_heat(chains, today)
    assert len(res) == 1
    assert res[0].theme_id == 'ai'
    # 3 涨停 + 1 行业 (高 specificity) → 中等分
    assert res[0].heat_score > 20


def test_theme_heat_no_match() -> None:
    """无涨停在题材内 → 不返回."""
    today = [_item('s99', 1, industry='医药')]
    chains = _mock_chain()
    res = compute_theme_heat(chains, today)
    assert res == []


def test_theme_heat_sorted_desc() -> None:
    """多题材按分数倒序."""
    from app.custom.research.chain import ChainNode, ThemeChain
    today = [
        _item('a1', 1, industry='电子'), _item('a2', 1, industry='电子'),
        _item('b1', 5, industry='金融'),
    ]
    chains = {
        'a': ThemeChain(theme_id='a', theme_name='A', chain=[ChainNode('光', ['a1', 'a2'], 1.0)], industries=['电子']),
        'b': ThemeChain(theme_id='b', theme_name='B', chain=[ChainNode('券', ['b1'], 1.0)], industries=['金融']),
    }
    res = compute_theme_heat(chains, today)
    assert len(res) == 2
    # B 有 5 板 (height 5/11=0.45), A 有 2 只 (breadth 0.2)
    # B 分数应更高
    assert res[0].theme_id == 'b'


def test_theme_heat_with_prev() -> None:
    """昨日延续性影响分数."""
    today = [_item('s1', 1, industry='电子')]
    prev = [{'symbol': 's1', 'board_count': 1}]  # s1 在 _mock_chain 的 theme_stocks 里
    chains = _mock_chain()
    res_with = compute_theme_heat(chains, today, prev_pool=prev)
    res_no = compute_theme_heat(chains, today, prev_pool=None)
    assert len(res_with) == 1
    assert len(res_no) == 1
    # 有 prev 延续 = 1.0, 无 = 0 → 分数更高
    assert res_with[0].heat_score > res_no[0].heat_score
