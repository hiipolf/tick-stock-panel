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