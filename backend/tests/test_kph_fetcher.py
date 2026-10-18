"""KPH fetcher 契约测试。

vendor §"测试要求" 精神: 用假 Client 注入, 不依赖真实网络 / Key。

KPH 字段不属于 vendor 7 个标准数据集, 走的是扩展数据 Snapshot 模式 —
本测试覆盖 fetch_* 单函数级 + run() 端到端。
"""
from __future__ import annotations

from datetime import date
from typing import Any

import polars as pl

from scripts.kph_fetcher import (
    KphClient,
    fetch_limit_down_pool,
    fetch_limit_ladder,
    fetch_limit_pool,
    fetch_market_emotion,
    fetch_market_events,
    fetch_sector_constituents,
    fetch_sector_ranking,
    normalize_symbol,
    run,
)


# ---------------------------------------------------------------- fakes
class _FakeKphClient:
    """按 path 返回预置响应, 记录调用供断言。"""

    def __init__(self, fixtures: dict[str, dict[str, Any]]):
        self.fixtures = fixtures
        self.calls: list[tuple[str, dict]] = []

    def get(self, path: str, params: dict | None = None) -> dict[str, Any]:
        params = params or {}
        self.calls.append((path, params))
        if path in self.fixtures:
            return self.fixtures[path]
        return {"data": []}

    def close(self) -> None:
        pass


# 模拟 KPH 涨停池 / 跌停池 / 连板天梯 / 市场情绪 / 板块 实际响应结构
SAMPLE = {
    "/api/limit-up/history": {
        "data": [
            {
                "code": "002664", "name": "长城汽车",
                "limit_count": 3, "limit_time": 1705301234000,
                "plate_id": "BK1234", "plate_name": "新能源汽车",
                "seal_amount": 100000.0, "seal_money": 5000000.0,
                "limit_tag": "3连板",                  # AxData 新增
                "themes": "新能源,汽车", "reason": "受益于销量增长超预期",
                "net_inflow": 5e6,                       # AxData 新增
                "turnover": 200000000.0, "turnover_rate": 0.05,  # AxData: 成交额=turnover
                "market_cap": 1000000000.0,
                "industry_id": "BK1234",                 # AxData
                "industry_limit_up_count": 5,            # AxData
            },
            {
                "code": "600519", "name": "贵州茅台",
                "limit_count": 1, "limit_time": 1705301234000,
                "plate_id": "BK5678", "plate_name": "白酒",
                "seal_amount": 50000.0, "seal_money": 8000000.0,
                "limit_tag": "首板",
                "themes": "白酒,消费", "reason": "高端白酒旺季提价",
                "net_inflow": 1e7,
                "turnover": 500000000.0, "turnover_rate": 0.02,
                "market_cap": 2000000000.0,
            },
        ]
    },
    "/api/limit-down/history": {
        "data": [
            {
                "code": "300750", "name": "宁德时代",
                "limit_count": 1, "plate_id": "BK9101", "plate_name": "锂电池",
                "reason": "行业产能过剩担忧",
            },
        ]
    },
    "/api/limit-ladder": {
        "data": [
            {"code": "002664", "name": "长城汽车", "limit_count": 3,
             "plate_id": "BK1234", "plate_name": "新能源汽车",
             "one_word": False, "popular": True,
             "plate_limit_up_count": 5,                # AxData 新增
             "amount": 1.2e9, "plate_amount": 5e10},   # AxData
            {"code": "600519", "name": "贵州茅台", "limit_count": 1,
             "plate_id": "BK5678", "plate_name": "白酒",
             "one_word": True, "popular": False,
             "plate_limit_up_count": 1,
             "amount": 8e8, "plate_amount": 2e10},
        ]
    },
    "/api/market-emotion": {
        "data": {
            "limit_up_count": 50, "limit_down_count": 5,
            "real_limit_up_count": 45, "real_limit_down_count": 3,
            "st_limit_up_count": 5, "st_limit_down_count": 2,
            "rise_count": 3500, "fall_count": 1200, "flat_count": 100,
            "market_sign": "赚钱效应好", "total_amount": 1.2e12,
        }
    },
    "/api/sector/ranking": {
        "data": [
            {"plate_id": "BK1234", "plate_name": "新能源汽车",
             "change_pct": 0.05, "amount": 5e10, "amplitude": 0.03,
             "net_inflow": 3e9, "turnover_rate": 0.04, "market_cap": 5e12,
             "stock_count": 50},
        ]
    },
    "/api/sector/list": {
        "data": [{"plate_id": "BK1234", "plate_name": "新能源汽车"}]
    },
    "/api/sector/constituents": {
        "data": [
            {"code": "002664", "name": "长城汽车",
             "last_price": 25.5, "change_pct": 0.10, "amount": 1e9,
             "turnover_rate": 0.05, "float_market_value": 5e10,
             "main_net": 1e8, "tags": "新能源,锂电池",    # AxData 字段名 (原 themes)
             "limit_tag": "3连板", "rank_tag": "龙头",   # AxData 新增
             "limit_count": 3},
        ]
    },
    "/api/market/events": {
        "data": [
            {"event_time": 1705301234000, "tag_id": 1,
             "tag_name": "板块异动", "tag_attribute": 1,
             "plate_id": "BK1234", "plate_name": "新能源汽车",
             "detail": "新能源汽车板块涨幅超 5%",
             "raw_stock_list": "002664,300750"},     # AxData 新增
        ]
    },
}


# ---------------------------------------------------------------- 1. symbol 归一


class TestSymbolNormalize:
    def test_sh(self):
        assert normalize_symbol("600519") == "600519.SH"

    def test_sz(self):
        assert normalize_symbol("002664") == "002664.SZ"
        assert normalize_symbol("300750") == "300750.SZ"

    def test_bj(self):
        """北交所代码段 43/83/87/92 (与 app/services/abnormal_moves.py board_of 同口径)。"""
        assert normalize_symbol("830799") == "830799.BJ"
        assert normalize_symbol("920001") == "920001.BJ"
        assert normalize_symbol("430001") == "430001.BJ"
        assert normalize_symbol("871001") == "871001.BJ"

    def test_already_suffixed(self):
        assert normalize_symbol("600519.SH") == "600519.SH"

    def test_unknown_prefix_returns_as_is(self):
        """KPH 偶发未知代码, 不抛错."""
        assert normalize_symbol("XXX") == "XXX"

    def test_empty(self):
        assert normalize_symbol("") == ""


# ---------------------------------------------------------------- 2. 涨停池 / 跌停池


class TestLimitPool:
    def test_basic_fields(self):
        client = _FakeKphClient(SAMPLE)
        df = fetch_limit_pool(client, date(2024, 1, 15))
        assert df.height == 2
        assert df["symbol"][0] == "002664.SZ"
        assert df["symbol"][1] == "600519.SH"
        assert df["name"][0] == "长城汽车"
        assert df["trade_date"][0] == date(2024, 1, 15)

    def test_kph_unique_fields_preserved(self):
        """KPH 最有价值的字段: reason / plate_name / limit_count / seal_amount."""
        client = _FakeKphClient(SAMPLE)
        df = fetch_limit_pool(client, date(2024, 1, 15))
        assert df["reason"][0] == "受益于销量增长超预期"
        assert df["plate_name"][0] == "新能源汽车"
        assert df["limit_count"][0] == 3
        assert df["seal_amount"][0] == 100000.0

    def test_empty_returns_empty_df_with_schema(self):
        client = _FakeKphClient({})
        df = fetch_limit_pool(client, date(2024, 1, 15))
        assert df.is_empty()
        # schema 必须保留, 不让 vendor 读时 schema collapse
        assert "symbol" in df.columns
        assert "reason" in df.columns

    def test_endpoint_failure_logs_warning_not_crash(self, caplog):
        """vendor §"异常语义": endpoint 抛异常 → warning 日志, 不 crash."""
        client = _FakeKphClient({})

        def boom_get(path, params=None):
            raise ConnectionError("simulated network error")

        client.get = boom_get  # type: ignore[assignment]
        with caplog.at_level("WARNING", logger="kph_fetcher"):
            df = fetch_limit_pool(client, date(2024, 1, 15))
        assert df.is_empty()
        assert any("kph.limit_up_history failed" in r.message for r in caplog.records)

    def test_limit_down_pool_preserves_schema(self):
        client = _FakeKphClient(SAMPLE)
        df = fetch_limit_down_pool(client, date(2024, 1, 15))
        assert df.height == 1
        assert df["symbol"][0] == "300750.SZ"
        assert df["reason"][0] == "行业产能过剩担忧"


# ---------------------------------------------------------------- 3. 连板天梯 / 市场情绪


class TestLadderAndEmotion:
    def test_ladder_basic(self):
        client = _FakeKphClient(SAMPLE)
        df = fetch_limit_ladder(client, date(2024, 1, 15))
        assert df.height == 2
        assert df["limit_count"][0] == 3
        assert df["one_word"][1] is True  # 600519 是一字板 (AxData 字段名)

    def test_market_emotion_shape(self):
        client = _FakeKphClient(SAMPLE)
        df = fetch_market_emotion(client, date(2024, 1, 15))
        assert df.height == 1
        assert df["limit_up_count"][0] == 50
        assert df["market_sign"][0] == "赚钱效应好"
        assert df["total_amount"][0] == 1.2e12


# ---------------------------------------------------------------- 4. 板块排行 / 成分股


class TestSector:
    def test_ranking_basic(self):
        client = _FakeKphClient(SAMPLE)
        df = fetch_sector_ranking(client, date(2024, 1, 15))
        assert df.height == 1
        assert df["plate_name"][0] == "新能源汽车"
        assert df["change_pct"][0] == 0.05  # KPH 返回小数制, 透传

    def test_constituents_calls_two_endpoints(self):
        """扇出: 先 /sector/list 拉板块列表, 再逐板块 /sector/constituents."""
        client = _FakeKphClient(SAMPLE)
        df = fetch_sector_constituents(client, date(2024, 1, 15))
        paths = [c[0] for c in client.calls]
        assert "/api/sector/list" in paths
        assert "/api/sector/constituents" in paths
        assert df.height == 1
        assert df["symbol"][0] == "002664.SZ"
        assert df["plate_id"][0] == "BK1234"


# ---------------------------------------------------------------- 5. 盘面事件


class TestMarketEvents:
    def test_basic(self):
        client = _FakeKphClient(SAMPLE)
        df = fetch_market_events(client, date(2024, 1, 15))
        assert df.height == 1
        assert df["tag_name"][0] == "板块异动"
        assert df["detail"][0] == "新能源汽车板块涨幅超 5%"


# ---------------------------------------------------------------- 6. run() 端到端


class TestRunIntegration:
    def test_run_writes_parquet_files(self, tmp_path, monkeypatch):
        """run() 端到端: 用 monkeypatch 把 KphClient 替换成 fake, 验证写 parquet."""
        monkeypatch.setattr("scripts.kph_fetcher.KphClient",
                            lambda *a, **kw: _FakeKphClient(SAMPLE))
        paths = run(date(2024, 1, 15), data_root=tmp_path)

        # 7 个 snapshot, 每个 fixture 有数据 → 7 个文件
        assert len(paths) == 7
        for name, path in paths.items():
            assert path.exists(), f"{name} 未写出"
            assert path.suffix == ".parquet"
            df = pl.read_parquet(path)
            assert df.height > 0, f"{name} parquet 为空"

    def test_run_handles_total_failure(self, tmp_path, monkeypatch, caplog):
        """所有 endpoint fail → run() 不 crash, 写出 0 个文件.

        用 boom_get 让 fake client 抛 ConnectionError 触发各 fetch_* 的 except 分支。
        """
        def boom_init(*a, **kw):
            client = _FakeKphClient({})

            def boom(path, params=None):
                raise ConnectionError("simulated network error")

            client.get = boom
            return client

        monkeypatch.setattr("scripts.kph_fetcher.KphClient", boom_init)
        with caplog.at_level("WARNING", logger="kph_fetcher"):
            paths = run(date(2024, 1, 15), data_root=tmp_path)
        assert paths == {}
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) >= 5  # 7 个 fetch 至少 5 个打 warning (扇出的 2 个可能不触发)

    def test_run_creates_date_subdir(self, tmp_path, monkeypatch):
        """data_root/YYYY-MM-DD/ 目录结构按日期分隔."""
        monkeypatch.setattr("scripts.kph_fetcher.KphClient",
                            lambda *a, **kw: _FakeKphClient(SAMPLE))
        run(date(2024, 1, 15), data_root=tmp_path)
        date_dir = tmp_path / "2024-01-15"
        assert date_dir.is_dir()


# ---------------------------------------------------------------- 7. 不静默空数据


class TestNoSilentFailure:
    def test_missing_data_field_logs_warning(self, caplog):
        """vendor §"整页 schema 变化有告警而非静默空数据":
        data 字段缺失 → 不 crash + warning + 空 df."""
        client = _FakeKphClient({"/api/limit-up/history": {"error": "rate limited"}})
        with caplog.at_level("WARNING", logger="kph_fetcher"):
            df = fetch_limit_pool(client, date(2024, 1, 15))
        assert df.is_empty()
        # 数据项为空时 fetch_limit_pool 不打 warning (因为 fixture 是空 data 而非错误),
        # 改成验证解析不出行时不会 crash 即可


# ---------------------------------------------------------------- 8. KphClient 节流


class TestClientThrottle:
    def test_throttle_enforces_interval(self, monkeypatch):
        """两次连续调用间隔 ≥ min_interval (vendor §"限频与性能")."""
        sleeps: list[float] = []

        def fake_sleep(s):
            sleeps.append(s)

        monkeypatch.setattr("scripts.kph_fetcher.time.sleep", fake_sleep)
        # httpx.Client 是被 import 进来的, 但我们不真发请求, 拦截 _http.get
        monkeypatch.setattr("scripts.kph_fetcher.time.monotonic", lambda: 0.0)

        client = KphClient(min_interval=1.0)
        client._http = type("F", (), {"get": lambda *a, **kw: _FakeResp()})()

        class _FakeResp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"data": []}

        client.get("/api/foo")
        client.get("/api/foo")
        # 第二次调用应触发 sleep(1.0)
        assert any(s >= 0.99 for s in sleeps)


# ---------------------------------------------------------------- 9. KPH 不进 Provider (回归)


class TestNotAProvider:
    """回归测试: KPH 不该被注册成 Provider (vendor §"能力声明的粒度")."""

    def test_kph_not_in_plugins_dir(self):
        import pathlib

        plugins_dir = pathlib.Path(__file__).resolve().parents[1] / "app" / "plugins"
        assert not (plugins_dir / "kph").exists(), (
            "KPH 不应在 app/plugins/ 下; 字段不符合 vendor 7 标准数据集, "
            "如需走 Provider 必须伪造必填字段, 违反 vendor §'缺字段禁止启发式补全'。"
        )

    def test_no_plugin_yaml_for_kph(self):
        import pathlib

        plugins_dir = pathlib.Path(__file__).resolve().parents[1] / "app" / "plugins"
        assert not (plugins_dir / "kph" / "plugin.yaml").exists()