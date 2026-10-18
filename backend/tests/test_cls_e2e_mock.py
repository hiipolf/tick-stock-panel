"""CLS Provider 端到端 smoke test — 用 in-process mock server 验证真实 httpx 流程。

不依赖真实 cls.cn / 公网. 走通: httpx 真实请求 → 节流 → JSON 解析 →
单位转换 → Polars DataFrame → 跨交易所 symbol 归一 → 软失败.

如果这个测试通过, 说明 cls 插件端到端流程正确, 真实 endpoint 接入只需要
替换 BASE_URL 和响应字段名 (vendor §"接口响应结构变体" 已覆盖).
"""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from app.plugins.cls.client import ClsClient
from app.plugins.cls.provider import ClsProvider
from scripts.mock_cls_server import start_server

# ----- fixtures -----

@pytest.fixture(scope="module")
def mock_server():
    """module 级起一次 mock server (port=0 让 OS 分配可用端口)."""
    server = start_server(host="127.0.0.1", port=0)
    yield server
    server.stop()


@pytest.fixture
def client(mock_server):
    """真 ClsClient 指向 mock server, 关掉节流加速测试."""
    return ClsClient(base_url=mock_server.base_url, min_interval=0.0)


@pytest.fixture
def provider(client):
    """把真 ClsClient (指向 mock server) 直接挂到 provider.client,
    让 _ensure_client 跳过重建 (vendor §"lazy init" 行为)."""
    p = ClsProvider()
    p.client = client
    return p


# ----- 1. 端到端 K 线 -----

class TestE2EKline:
    def test_daily_one_symbol(self, provider):
        df = provider.get_daily(["600519.SH"], "2024-01-01", "2030-12-31")
        assert df.height > 0
        assert df.schema["symbol"] == pl.Utf8
        assert df.schema["date"] == pl.Date
        # mock 给 200 根日 K (limit=200 default), 周末过滤后约 143 根
        assert 100 <= df.height <= 200
        # 全是同一只股票
        assert df["symbol"].unique().to_list() == ["600519.SH"]

    def test_daily_multi_symbol_cross_exchange(self, provider):
        """跨上 / 深 / 北交所三个市场, 验证 symbol 归一."""
        df = provider.get_daily(
            ["600519.SH", "000001.SZ", "830799.BJ", "920001.BJ"],
            "2024-01-01", "2030-12-31",
        )
        assert df.height > 0
        symbols = set(df["symbol"].unique().to_list())
        assert "600519.SH" in symbols
        assert "000001.SZ" in symbols
        assert "830799.BJ" in symbols
        assert "920001.BJ" in symbols
        assert len(symbols) == 4

    def test_daily_unit_conversion_end_to_end(self, provider):
        """端到端 unit 验证: volume 股 → 手 (÷100), changePercent 透传为小数制."""
        df = provider.get_daily(["600519.SH"], "2024-01-01", "2030-12-31")
        # 贵州茅台日成交量典型 1.2M 股 → 12K 手
        vol_lots = df["volume"].to_list()
        # 至少 1 行 volume 在 [3000, 30000] 范围 (手)
        assert any(3000 <= v <= 30_000 for v in vol_lots), f"volume (手) 范围异常: {vol_lots[:5]}"
        # amount (元) 应该非零且大于 1e8 (茅台日均成交额)
        amounts = df["amount"].to_list()
        assert any(a > 1e8 for a in amounts), f"amount 异常: {amounts[:5]}"

    def test_daily_date_filter(self, provider):
        """日期窗口过滤: vendor §"分页与时间窗口"."""
        df = provider.get_daily(["600519.SH"], "2026-01-01", "2030-12-31")
        # 起点 2026-01-01, mock 给 30 根日 K (包含 2025 范围)
        assert df.height > 0
        assert df["date"].min() >= date(2026, 1, 1)


# ----- 2. 端到端 分时 -----

class TestE2ETimeline:
    def test_minute_basic(self, provider):
        df = provider.get_minute(["600519.SH"], "2024-01-01", "2030-12-31")
        # 240 根 (120 上午 + 120 下午) 但 mock 对周内日才生成, 周末时也可能空
        # 不强制断言 row 数, 但 schema 必须正确
        assert set(df.columns) >= {"symbol", "datetime", "open", "high", "low", "close", "volume"}

    def test_minute_volume_in_lots(self, provider):
        df = provider.get_minute(["002664.SZ"], "2024-01-01", "2030-12-31")
        if df.height > 0:
            vol_lots = df["volume"].to_list()
            # 长城汽车日均 35M 股 / 240 ≈ 146K 股/分, 即 1458 手左右
            assert any(500 <= v <= 3000 for v in vol_lots), f"volume (手) 异常: {vol_lots[:5]}"


# ----- 3. 软失败 -----

class TestE2ESoftFailure:
    def test_error_code_returns_empty_df(self, provider):
        """ERRxxx code → mock 返回 HTTP 500 → get_daily 返回空 df 不 crash."""
        df = provider.get_daily(["ERR999.SH"], "2024-01-01", "2030-12-31")
        assert df.is_empty()
        assert df.schema == pl.Schema(
            {"symbol": pl.Utf8, "date": pl.Date, "open": pl.Float64, "high": pl.Float64,
             "low": pl.Float64, "close": pl.Float64, "volume": pl.Float64, "amount": pl.Float64}
        )

    def test_empty_code_returns_empty_df(self, provider):
        """000000 code → mock 返回空 data → 空 df."""
        df = provider.get_daily(["000000.SZ"], "2024-01-01", "2030-12-31")
        assert df.is_empty()

    def test_mixed_success_and_failure(self, provider):
        """部分标的 fail, 其他标的成功, 不互相影响."""
        df = provider.get_daily(
            ["ERR500.SH", "600519.SH", "000001.SZ"],
            "2024-01-01", "2030-12-31",
        )
        assert df.height > 0
        # 至少 600519 / 000001 成功
        symbols = set(df["symbol"].unique().to_list())
        assert "600519.SH" in symbols
        assert "000001.SZ" in symbols
        assert "ERR500.SH" not in symbols


# ----- 4. server 自身 sanity -----

class TestMockServerItself:
    """Mock server 自身的 sanity, 防止 mock 假绿."""

    def test_health_endpoint(self, mock_server):
        import httpx
        r = httpx.get(f"{mock_server.base_url}/health", timeout=2.0)
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_kline_endpoint_shape(self, mock_server):
        import httpx
        r = httpx.get(
            f"{mock_server.base_url}/quote/stock/kline",
            params={"code": "600519", "type": "day", "limit": 30},
            timeout=2.0,
        )
        assert r.status_code == 200
        data = r.json()["data"]
        assert len(data) > 0
        first = data[0]
        # 字段名跟 vendor cls plugin 的 _map_daily_bar 对齐
        assert {"time", "open", "high", "low", "close", "volume", "amount", "changePercent"} <= set(first.keys())

    def test_timeline_endpoint_shape(self, mock_server):
        import httpx
        r = httpx.get(
            f"{mock_server.base_url}/quote/stock/timeline",
            params={"code": "002664"},
            timeout=2.0,
        )
        assert r.status_code == 200
        data = r.json()["data"]
        assert len(data) > 0
        first = data[0]
        assert {"minute", "last_price", "open", "high", "low", "volume", "amount"} <= set(first.keys())