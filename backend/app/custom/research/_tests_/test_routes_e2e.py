"""HTTP 端点 e2e 测试 — P1.1。

净室重写: 用 FastAPI TestClient 跑 build_router() 暴露的所有端点。
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.custom.research._tests_.fixtures import quotes as qfx
from app.custom.research._tests_.fixtures import theme_index as tfx
from app.custom.research.routes import build_router


def _make_client() -> TestClient:
    """构造独立 FastAPI app + TestClient。"""
    app = FastAPI()
    app.include_router(build_router())
    return TestClient(app)


# === /status ===
def test_status_endpoint() -> None:
    """status 端点返回 phase + 工具列表。"""
    client = _make_client()
    r = client.get("/api/custom/research/status")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "research.studio"
    assert body["phase"].startswith("P1.")
    assert "get_limit_up_pool" in body["tools"]
    assert "get_theme_attribution" in body["tools"]


def test_hello_endpoint() -> None:
    """hello 端点保留 P0 行为。"""
    client = _make_client()
    r = client.get("/api/custom/research/hello?name=IRP")
    assert r.json() == {"message": "hello, IRP!", "from": "research.studio"}


# === /limit-up-pool ===
def test_limit_up_pool_endpoint() -> None:
    """涨停池端点: 走 fixture, 应返 8 只涨停 + 4 梯队。"""
    client = _make_client()
    payload = {
        "trade_date": "2026-10-16",
        "quotes": [
            {
                "symbol": q.symbol, "name": q.name, "trade_date": q.trade_date,
                "prev_close": q.prev_close, "last_close": q.last_close,
                "high": q.high, "low": q.low, "volume": q.volume, "amount": q.amount,
                "industry": q.industry, "theme_tags": q.theme_tags,
                "limit_time": q.limit_time,
                "is_exploded": q.is_exploded, "is_limit_down": q.is_limit_down,
            }
            for q in qfx.sample_quotes_basic()
        ],
        "prev_pool": qfx.prev_pool_basic(),
    }
    r = client.post("/api/custom/research/limit-up-pool", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert len(body["result"]["items"]) == 8
    assert body["result"]["stats"]["limit_up_total"] == 8
    assert len(body["result"]["ladder"]) == 4
    assert body["result"]["stats"]["promotion_rate"]["overall"] == 1.0


# === /limit-up-ladder ===
def test_limit_up_ladder_endpoint() -> None:
    """连板梯队端点: 拿 pre-aggregated items 重算梯队。"""
    client = _make_client()
    # 用上游算法的输出做输入
    payload = {
        "items": [
            {"symbol": "600001", "board_count": 1, "limit_time": "10:30:00"},
            {"symbol": "600010", "board_count": 2, "limit_time": "09:35:00"},
            {"symbol": "600020", "board_count": 3, "limit_time": "09:25:00"},
        ]
    }
    r = client.post("/api/custom/research/limit-up-ladder", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    boards = [l["board"] for l in body["result"]["ladder"]]
    assert boards == [3, 2, 1]


# === /theme-index-kline ===
def test_theme_index_kline_endpoint() -> None:
    """题材指数 K 线端点。"""
    client = _make_client()
    bars = tfx.theme_stocks_bars(days=10)
    payload = {
        "theme_id": "ai_chain",
        "freq": "1d",
        "stocks": {
            sym: [
                {"date": b.date, "close": b.close, "volume": b.volume, "amount": b.amount}
                for b in stock_bars
            ]
            for sym, stock_bars in bars.items()
        },
    }
    r = client.post("/api/custom/research/theme-index-kline", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["result"]["theme_id"] == "ai_chain"
    assert body["result"]["freq"] == "1d"
    assert len(body["result"]["bars"]) == 10


# === /theme-attribution ===
def test_theme_attribution_endpoint() -> None:
    """产业链归因端点: 读 backend_pack/data/theme_chain.json。"""
    client = _make_client()
    payload = {
        "theme_id": "ai_chain",
        "change_pct_map": {
            "300750": 0.10,
            "002594": 0.05,
            "000858": 0.03,
            "601318": 0.02,
            "600519": 0.01,
        },
    }
    r = client.post("/api/custom/research/theme-attribution", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["result"]["theme_name"] == "AI 算力链"
    assert len(body["result"]["attribution"]) == 3


def test_theme_attribution_missing_theme() -> None:
    """未知 theme_id → 400。"""
    client = _make_client()
    payload = {"theme_id": "nonexistent", "change_pct_map": {}}
    r = client.post("/api/custom/research/theme-attribution", json=payload)
    assert r.status_code == 400


# === /theme-phase ===
def test_theme_phase_endpoint() -> None:
    """5 阶段判定端点: 高潮。"""
    client = _make_client()
    payload = {
        "today": {
            "theme_id": "ai_chain", "theme_name": "AI 算力链",
            "trade_date": "2026-10-16",
            "change_pct": 0.099, "width": 0.95, "consecutive_days": 30, "rank": 1,
        },
        "history_30d": [
            {
                "theme_id": "ai_chain", "theme_name": "AI 算力链",
                "trade_date": f"2026-09-{16 + (i % 15):02d}",
                "change_pct": 0.02, "width": 0.5, "consecutive_days": i + 1, "rank": 5,
            }
            for i in range(30)
        ],
    }
    r = client.post("/api/custom/research/theme-phase", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["phase"] == "高潮"


# === /tools ===
def test_tools_endpoint() -> None:
    """工具发现端点: 返回 4 工具 JSON Schema。"""
    client = _make_client()
    r = client.get("/api/custom/research/tools")
    assert r.status_code == 200
    body = r.json()
    names = [t["function"]["name"] for t in body["tools"]]
    assert names == [
        "get_limit_up_pool",
        "get_limit_up_ladder",
        "get_theme_index_kline",
        "get_theme_attribution",
    ]
    # 每个 schema 必须有 parameters
    for tool in body["tools"]:
        assert "parameters" in tool["function"]
        assert tool["function"]["parameters"]["type"] == "object"


# === 异常路径 ===
def test_limit_up_pool_invalid_input() -> None:
    """缺 quotes 字段 → 422 (pydantic 校验)。"""
    client = _make_client()
    r = client.post("/api/custom/research/limit-up-pool", json={"trade_date": "2026-10-16"})
    assert r.status_code == 422


def test_theme_index_kline_invalid_freq() -> None:
    """非法 freq → 500-ish (算法内部 ValueError)。

    TestClient 把异常抛成 500; 用户应在 schema 阶段拦截。
    """
    client = _make_client()
    payload = {
        "theme_id": "ai_chain",
        "freq": "1h",  # 非法
        "stocks": {"a": [{"date": "2026-10-01", "close": 50.0}]},
    }
    r = client.post("/api/custom/research/theme-index-kline", json=payload)
    # 算法 ValueError 抛到 TestClient, 默认 500
    assert r.status_code in (400, 500)


# === P2 v2.0b 聚合端点 ===
def test_short_term_overview_endpoint() -> None:
    """聚合端点: 6 算法 + 涨停池 + 梯队 + stats 一次返回."""
    client = _make_client()
    payload = {
        "trade_date": "2026-10-16",
        "quotes": [
            {
                "symbol": q.symbol, "name": q.name, "trade_date": q.trade_date,
                "prev_close": q.prev_close, "last_close": q.last_close,
                "high": q.high, "low": q.low, "volume": q.volume, "amount": q.amount,
                "industry": q.industry, "theme_tags": q.theme_tags,
                "limit_time": q.limit_time,
                "is_exploded": q.is_exploded, "is_limit_down": q.is_limit_down,
            }
            for q in qfx.sample_quotes_basic()
        ],
        "prev_pool": qfx.prev_pool_basic(),
        "exploded_count": 0,
        "history_30d": [],
        "industry_map": {"传媒": 2, "医药": 2, "电子": 1},
    }
    r = client.post("/api/custom/research/short-term-overview", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    result = body["result"]
    # 6 算法字段
    assert "emotion_score" in result
    assert "phase" in result
    assert result["phase"] in {"\u9ad8\u6f6e", "\u6d3b\u8dc3", "\u5206\u6b67", "\u9000\u6f6e", "\u51b0\u70b9"}  # 高潮/活跃/分歧/退潮/冰点
    secondary = result["secondary_indicators"]
    assert all(k in secondary for k in ("high_pos_avg", "decline_coverage", "high_collapse", "high_promotion"))
    deep = result["deep_metrics"]
    assert all(k in deep for k in ("final_explode_rate", "prev_limit_up_feedback", "prev_consecutive_feedback",
                                   "promotion_by_board_overall", "main_line_concentration", "high_risk_score"))
    assert len(result["promotion_by_board"]) == 4
    # theme_heat: 短路版 fixture symbol 不在 chain 内, 允许为 0
    # (端到端数据接入 v2.0c 后, 涨停股匹配 chain 才能产出)
    assert isinstance(result["theme_heat"], list)
    # 涨停池 + 梯队
    assert len(result["items"]) >= 7
    assert len(result["ladder"]) >= 4
    # stats
    assert result["stats"]["limit_up_total"] >= 7
    assert result["stats"]["max_board"] >= 5


def test_short_term_overview_empty_input() -> None:
    """空输入 → 仍 200, 字段为空."""
    client = _make_client()
    payload = {
        "trade_date": "2026-10-16",
        "quotes": [],
        "prev_pool": None,
    }
    r = client.post("/api/custom/research/short-term-overview", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["result"]["items"] == []
    assert body["result"]["ladder"] == []
    assert body["result"]["emotion_score"] >= 0


# === P2 v2.0j main_line_concentration 回归测试 ===
def test_build_quotes_from_ladder_pulls_ext_hy_ths_industry() -> None:
    """v2.0j: _build_quotes_from_ladder 从 st.ext_hy_ths__所属同花顺行业
    抽 industry, 同时构建 industry_map; main_line_concentration 不再永远 0%。

    之前 P2 v2.0c / v2.0i 时 industry_map 恒为空 (snapshot 不含 industry),
    导致 deep_metrics.main_line_concentration 总是 0%. 这个测试是回归保护.
    """
    from app.custom.research.routes import (
        _aggregate_overview,
        _build_quotes_from_ladder,
    )

    # 构造一组真实形态的 ladder (每个 stock 带 ext_hy_ths__所属同花顺行业)
    ladder = {
        "as_of": "2026-10-16",
        "tiers": [
            {"stocks": [
                {"symbol": "600825.SH", "name": "新华传媒", "close": 11.0,
                 "change_pct": 0.10, "consecutive_limit_ups": 5, "status": "limit_up",
                 "ext_hy_ths__所属同花顺行业": "文化传媒-出版",
                 "ext_gn_ths__所属概念": "创投;文化传媒;融资融券"},
                {"symbol": "600977.SH", "name": "中国电影", "close": 12.5,
                 "change_pct": 0.10, "consecutive_limit_ups": 3, "status": "limit_up",
                 "ext_hy_ths__所属同花顺行业": "文化传媒-出版",
                 "ext_gn_ths__所属概念": "文化传媒;AI语料"},
            ]},
            {"stocks": [
                {"symbol": "300750.SZ", "name": "宁德时代", "close": 230.0,
                 "change_pct": 0.10, "consecutive_limit_ups": 1, "status": "limit_up",
                 "ext_hy_ths__所属同花顺行业": "电池-动力电池",
                 "ext_gn_ths__所属概念": "新能源汽车;锂电池"},
                {"symbol": "002594.SZ", "name": "比亚迪", "close": 95.0,
                 "change_pct": 0.10, "consecutive_limit_ups": 1, "status": "limit_up",
                 "ext_hy_ths__所属同花顺行业": "汽车整车-乘用车",
                 "ext_gn_ths__所属概念": "新能源汽车;整车"},
            ]},
            {"stocks": [
                # 一只 stock 没带 ext_hy_ths (fallback → 空 industry, 不计入 map)
                {"symbol": "000001.SZ", "name": "平安银行", "close": 12.0,
                 "change_pct": 0.10, "consecutive_limit_ups": 1, "status": "limit_up",
                 "ext_gn_ths__所属概念": "金融"},
            ]},
        ],
    }
    # snapshot 我们故意不传 (模拟真实场景: snapshot 不含 industry)
    snapshot: dict = {}

    quotes, prev_pool, industry_map = _build_quotes_from_ladder(ladder, snapshot)

    # 1. Quote.industry 来自 st.ext_hy_ths__所属同花顺行业, 而非 snapshot
    industry_by_sym = {q.symbol: q.industry for q in quotes}
    assert industry_by_sym["600825.SH"] == "文化传媒-出版"
    assert industry_by_sym["300750.SZ"] == "电池-动力电池"
    assert industry_by_sym["000001.SZ"] == ""  # fallback 空

    # 2. theme_tags 从 ext_gn_ths__所属概念 拆分
    tags_825 = next(q.theme_tags for q in quotes if q.symbol == "600825.SH")
    assert "创投" in tags_825
    assert "文化传媒" in tags_825
    assert "融资融券" in tags_825

    # 3. industry_map 聚合: 4 只带 industry, 分布见下
    assert industry_map == {"文化传媒-出版": 2, "电池-动力电池": 1, "汽车整车-乘用车": 1}

    # 4. _aggregate_overview 拿 industry_map 后, main_line_concentration > 0%
    result = _aggregate_overview(
        quotes, ladder["as_of"], prev_pool=prev_pool, industry_map=industry_map
    )
    deep = result["deep_metrics"]
    # 2/4 = 0.5 (传媒是最强行业, 2 只)
    assert deep["main_line_concentration"] > 0.0
    assert abs(deep["main_line_concentration"] - 0.5) < 0.01


def test_aggregate_overview_fallback_industry_from_quotes() -> None:
    """v2.0j: industry_map 不传时, _aggregate_overview 兜底从 quotes[i].industry 派生."""
    from app.custom.research.routes import _aggregate_overview

    # 4 只 Quote, 全带 industry=电子; 不传 industry_map
    quotes = [
        qfx.sample_quotes_basic()[0].__class__(  # 用现有 fixture class 构造
            symbol="x1", name="x1", trade_date="2026-10-16",
            prev_close=10.0, last_close=11.0, high=11.0, low=10.9,
            volume=1_000_000, amount=11_000_000,
            industry="电子-半导体", theme_tags=[], limit_time="10:00:00",
            is_exploded=False, is_limit_down=False,
        ),
    ] * 4
    # 只看主 line: 全是 "电子-半导体", 应 main_line_concentration = 1.0
    result = _aggregate_overview(quotes, "2026-10-16", prev_pool=[])
    deep = result["deep_metrics"]
    assert abs(deep["main_line_concentration"] - 1.0) < 0.01


def test_aggregate_overview_industry_empty_still_zero() -> None:
    """v2.0j: 所有 Quote.industry 都空 → main_line_concentration 仍 0% (兜底派生空 map).

    fixture sample_quotes_basic 默认 industry 非空 ('电子'/'通信'/...), 这里手动
    把所有 Quote.industry 置空, 模拟真实场景 'snapshot 不带 industry 且 stock 也无
    ext_hy_ths'; 主线集中度应回退到 0% (符合常识).
    """
    from dataclasses import replace

    from app.custom.research.routes import _aggregate_overview

    # 强制清空 industry (fixture 默认 '电子' 等)
    quotes = [replace(q, industry="") for q in qfx.sample_quotes_basic()]
    result = _aggregate_overview(quotes, "2026-10-16", prev_pool=qfx.prev_pool_basic())
    deep = result["deep_metrics"]
    assert deep["main_line_concentration"] == 0.0


def test_aggregate_overview_industry_from_fixture_nonempty() -> None:
    """v2.0j: sample_quotes_basic 默认带 industry (电子/通信/医药/...) → 非 0%."""
    from app.custom.research.routes import _aggregate_overview

    quotes = qfx.sample_quotes_basic()  # 默认带 industry
    result = _aggregate_overview(quotes, "2026-10-16", prev_pool=qfx.prev_pool_basic())
    deep = result["deep_metrics"]
    # 之前的修复前永远是 0%; 现在兜底派生让非零
    assert deep["main_line_concentration"] > 0.0