"""ClsProvider 契约与单位标准化测试。

不依赖真实网络与 API Key: 用假 ClsClient 返回样例 K 线 / 分时,
验证字段映射、单位口径 (vendor §"单位制": 百分数→小数、volume 股→手、
amount 元、symbol 带交易所后缀、缺字段不启发式补全)、日期窗口过滤、
软失败、能力声明 (未声明数据集回退 tickflow) 与设置页试拉。
"""
from __future__ import annotations

from datetime import date

import pytest

from app.plugins.cls import provider as cp
from app.plugins.cls.provider import (
    DAILY_SCHEMA,
    MINUTE_SCHEMA,
    ClsProvider,
    _ClsConfig,
    availability,
    normalize_symbol,
    strip_suffix,
)


# ---------------------------------------------------------------- fakes
class _FakeClient:
    """按调用 path + code 返回预置响应, 记录调用供断言。"""

    def __init__(self, fixtures: dict[tuple[str, str], dict]):
        self.fixtures = fixtures
        self.calls: list[tuple[str, dict]] = []

    def get(self, path: str, params: dict | None = None) -> dict:
        params = params or {}
        self.calls.append((path, params))
        code = params.get("code", "")
        return self.fixtures.get((path, code), {"data": []})

    def close(self) -> None:
        pass


def _provider_with(monkeypatch, fixtures: dict) -> tuple[ClsProvider, _FakeClient]:
    fake = _FakeClient(fixtures)
    monkeypatch.setattr(cp, "ClsClient", lambda **kw: fake)
    return ClsProvider(), fake


# 财联社响应结构 (字段名严格按 AxData 官方文档对齐 cls_stock_kline / cls_stock_timeline)
SAMPLE_KLINE = {
    ("/quote/stock/kline", "002664"): {
        "data": [
            {
                "trade_date": "20240115",  # YYYYMMDD 紧凑格式 (AxData 文档样例快照一致)
                "open": 25.00, "high": 25.50, "low": 24.80, "close": 25.20,
                "volume": 10_000_000,  # 假设股, ÷100 转手
                "amount": 252_000_000.0,  # 元
                "change": 0.20,            # 涨跌额 (AxData 字段)
                "change_pct": 0.008,        # 小数制 0.008 = +0.8% (AxData 标准化)
            },
            {
                "trade_date": "20240116",
                "open": 25.20, "high": 26.00, "low": 25.10, "close": 25.80,
                "volume": 12_000_000,
                "amount": 310_000_000.0,
                "change": 0.60,
                "change_pct": 0.0238,
            },
        ]
    },
    ("/quote/stock/kline", "600519"): {
        "data": [
            {
                "trade_date": "20240115",
                "open": 1700.00, "high": 1720.00, "low": 1695.00, "close": 1715.00,
                "volume": 2_000_000,
                "amount": 3_420_000_000.0,
                "change": 15.0,
                "change_pct": 0.0088,
            },
        ]
    },
    ("/quote/stock/kline", "000000"): {"data": []},
    # 边界: 缺字段 → 0 / None, 不报错不启发式补全
    ("/quote/stock/kline", "688888"): {
        "data": [
            {"trade_date": "20240115", "open": 10.0, "close": 10.5},  # 缺 high/low/volume/amount
        ]
    },
}

SAMPLE_TIMELINE = {
    ("/quote/stock/timeline", "002664"): {
        "data": [
            {"minute": "09:35", "last_price": 25.10, "open": 25.05, "high": 25.15,
             "low": 25.00, "pre_close": 25.00, "avg_price": 25.05,
             "volume": 50_000, "amount": 1_255_000.0},
            {"minute": "09:36", "last_price": 25.15, "open": 25.10, "high": 25.20,
             "low": 25.05, "pre_close": 25.00, "avg_price": 25.10,
             "volume": 45_000, "amount": 1_132_000.0},
        ]
    },
}


@pytest.fixture
def fixtures():
    return {**SAMPLE_KLINE, **SAMPLE_TIMELINE}


@pytest.fixture
def provider_with_fake(monkeypatch, fixtures):
    return _provider_with(monkeypatch, fixtures)


# ---------------------------------------------------------------- 1. 字段映射与单位


class TestSymbolNormalize:
    def test_sh(self):
        assert normalize_symbol("600519") == "600519.SH"
        assert normalize_symbol("688981") == "688981.SH"

    def test_sz(self):
        assert normalize_symbol("000001") == "000001.SZ"
        assert normalize_symbol("300750") == "300750.SZ"

    def test_bj(self):
        """北交所代码段 43/83/87/92 (与 app/services/abnormal_moves.py board_of 同口径)。"""
        assert normalize_symbol("830799") == "830799.BJ"
        assert normalize_symbol("920001") == "920001.BJ"
        assert normalize_symbol("430001") == "430001.BJ"
        assert normalize_symbol("871001") == "871001.BJ"

    def test_already_suffixed(self):
        assert normalize_symbol("600519.SH") == "600519.SH"
        assert normalize_symbol("000001.SZ") == "000001.SZ"
        assert normalize_symbol("600519.sh") == "600519.SH"  # 大小写归一

    def test_unknown_prefix_raises(self):
        with pytest.raises(ValueError, match="unrecognized"):
            normalize_symbol("999999")

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="empty"):
            normalize_symbol("")

    def test_strip_suffix(self):
        assert strip_suffix("600519.SH") == "600519"
        assert strip_suffix("000001.SZ") == "000001"
        assert strip_suffix("600519") == "600519"


class TestUnitConversion:
    def test_volume_in_lots_not_shares(self, provider_with_fake):
        """vendor §"单位制" 强约束: 股 → 手 (÷100)."""
        provider, _ = provider_with_fake
        df = provider.get_daily(["002664.SZ"], "2024-01-01", "2024-12-31")
        assert df.height == 2
        assert df["volume"][0] == 100_000.0  # 10_000_000 股 / 100
        assert df["volume"][1] == 120_000.0  # 12_000_000 股 / 100

    def test_amount_in_yuan_not_wan(self, provider_with_fake):
        """amount 单位元, 不需 /10000."""
        provider, _ = provider_with_fake
        df = provider.get_daily(["002664.SZ"], "2024-01-01", "2024-12-31")
        assert df["amount"][0] == 252_000_000.0

    def test_symbol_normalized_to_brokerage_suffix(self, provider_with_fake):
        """vendor §"代码格式" 强约束: symbol 必带交易所后缀."""
        provider, _ = provider_with_fake
        df = provider.get_daily(["002664.SZ"], "2024-01-01", "2024-12-31")
        assert df["symbol"][0] == "002664.SZ"
        # 客户端传裸代码时, 内部要归一
        df2 = provider.get_daily(["600519"], "2024-01-01", "2024-12-31")
        assert df2["symbol"][0] == "600519.SH"

    def test_date_parsing_iso(self, provider_with_fake):
        provider, _ = provider_with_fake
        df = provider.get_daily(["002664.SZ"], "2024-01-01", "2024-12-31")
        assert df["date"][0] == date(2024, 1, 15)
        assert df["date"][1] == date(2024, 1, 16)

    def test_missing_fields_default_zero_not_none(self, provider_with_fake):
        """vendor §"缺字段与空数据" 禁止启发式补全: 缺字段置 0."""
        provider, _ = provider_with_fake
        df = provider.get_daily(["688888.SH"], "2024-01-01", "2024-12-31")
        assert df.height == 1
        assert df["open"][0] == 10.0
        assert df["close"][0] == 10.5
        assert df["high"][0] == 0.0
        assert df["low"][0] == 0.0


# ---------------------------------------------------------------- 2. 响应结构变体


class TestResponseStructure:
    def test_missing_data_field(self, monkeypatch, fixtures):
        """vendor §"整页 schema 变化有告警而非静默空数据":
        当 data 字段缺失, 不应 crash, 返回空 df."""
        fixtures[("/quote/stock/kline", "999999")] = {"error": "no data"}
        provider, _ = _provider_with(monkeypatch, fixtures)
        df = provider.get_daily(["999999.SZ"], "2024-01-01", "2024-12-31")
        assert df.is_empty()
        assert df.schema == DAILY_SCHEMA

    def test_empty_data_array(self, provider_with_fake):
        provider, _ = provider_with_fake
        df = provider.get_daily(["000000.SZ"], "2024-01-01", "2024-12-31")
        assert df.is_empty()
        assert df.schema == DAILY_SCHEMA


# ---------------------------------------------------------------- 3. 日期窗口过滤 (代理分页)


class TestDateWindow:
    def test_start_time_filter(self, provider_with_fake):
        """vendor §"分页与时间窗口": start_time 之后的 K 线保留."""
        provider, _ = provider_with_fake
        df = provider.get_daily(["002664.SZ"], "2024-01-16", "2024-12-31")
        assert df.height == 1
        assert df["date"][0] == date(2024, 1, 16)

    def test_end_time_filter(self, provider_with_fake):
        provider, _ = provider_with_fake
        df = provider.get_daily(["002664.SZ"], "2024-01-01", "2024-01-15")
        assert df.height == 1
        assert df["date"][0] == date(2024, 1, 15)

    def test_window_inclusive_bounds(self, provider_with_fake):
        provider, _ = provider_with_fake
        df = provider.get_daily(["002664.SZ"], "2024-01-15", "2024-01-16")
        assert df.height == 2


# ---------------------------------------------------------------- 4. 软失败


class TestSoftFailure:
    def test_single_symbol_failure_does_not_block_others(self, monkeypatch, fixtures):
        """vendor §"异常语义": 单标的异常由上层捕获; 不阻断其他."""
        # 替换 FakeClient, 让 002664 抛错
        fake = _FakeClient(fixtures)
        original_real_get = fake.get

        def boom_get(path, params=None):
            if params and params.get("code") == "002664":
                raise ConnectionError("simulated network error")
            return original_real_get(path, params)

        fake.get = boom_get
        monkeypatch.setattr(cp, "ClsClient", lambda **kw: fake)

        provider = ClsProvider()
        df = provider.get_daily(["002664.SZ", "600519.SH"], "2024-01-01", "2024-12-31")
        # 002664 失败, 600519 成功
        assert df.height == 1
        assert df["symbol"][0] == "600519.SH"

    def test_all_failures_return_empty_df(self, monkeypatch):
        fake = _FakeClient({})

        def boom_get(path, params=None):
            raise ConnectionError("down")

        fake.get = boom_get
        monkeypatch.setattr(cp, "ClsClient", lambda **kw: fake)

        provider = ClsProvider()
        df = provider.get_daily(["002664.SZ", "600519.SH"], "2024-01-01", "2024-12-31")
        assert df.is_empty()
        assert df.schema == DAILY_SCHEMA


# ---------------------------------------------------------------- 5. 能力声明


class TestCapabilityDeclaration:
    def test_daily_declared(self):
        cfg = _ClsConfig()
        # loader 走 dataset in provider.config.datasets 判断, 只看 key 是否存在
        assert "daily" in cfg.datasets

    def test_minute_declared(self):
        cfg = _ClsConfig()
        assert "minute" in cfg.datasets

    def test_realtime_not_declared(self):
        """vendor §"能力声明的粒度": 未声明的 realtime provider_has_dataset 应为 False."""
        cfg = _ClsConfig()
        assert "realtime" not in cfg.datasets

    def test_financial_not_declared(self):
        cfg = _ClsConfig()
        assert "financial" not in cfg.datasets

    def test_adj_factor_not_declared(self):
        cfg = _ClsConfig()
        assert "adj_factor" not in cfg.datasets

    def test_depth5_not_declared(self):
        cfg = _ClsConfig()
        assert "depth5" not in cfg.datasets


# ---------------------------------------------------------------- 6. Key 语义 (CLS 无 Key)


class TestKeySemantics:
    def test_no_key_needed(self):
        """CLS 公开页面无 Key, availability 永远 True."""
        ok, reason = availability()
        assert ok is True
        assert reason == "ok"

    def test_no_api_key_env_in_plugin_yaml(self):
        """plugin.yaml 必须不含 api_key_env (vendor §"Key 语义": 无 Key 插件不声明字段)."""
        import pathlib

        yaml_path = pathlib.Path(cp.__file__).resolve().parent / "plugin.yaml"
        text = yaml_path.read_text(encoding="utf-8")
        assert "api_key_env" not in text


# ---------------------------------------------------------------- 7. loader 集成


class TestLoaderIntegration:
    def test_plugin_yaml_loadable(self):
        """plugin.yaml 解析必须能产出 (name, display_name, runtime, entry, datasets)."""
        try:
            import yaml
        except ImportError:
            pytest.skip("PyYAML not installed")
        import pathlib

        yaml_path = pathlib.Path(cp.__file__).resolve().parent / "plugin.yaml"
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        assert data["name"] == "cls"
        assert data["runtime"] == "python"
        assert data["entry"] == "app.plugins.cls.provider:ClsProvider"
        assert data["check"] == "app.plugins.cls.provider:availability"
        assert "daily" in data["datasets"]
        assert "minute" in data["datasets"]

    def test_entry_module_importable(self):
        from app.plugins.cls.provider import ClsProvider

        assert hasattr(ClsProvider, "name")
        assert ClsProvider.name == "cls"
        assert hasattr(ClsProvider, "get_daily")
        assert hasattr(ClsProvider, "get_minute")
        assert hasattr(ClsProvider, "test_dataset")
        assert hasattr(ClsProvider, "close")


# ---------------------------------------------------------------- minute 专项


class TestMinuteContract:
    def test_minute_schema_columns(self, provider_with_fake):
        """vendor §"get_minute 的 datetime 时区契约": 北京墙钟 naive."""
        provider, _ = provider_with_fake
        df = provider.get_minute(["002664.SZ"], "2024-01-01", "2024-12-31")
        assert df.height == 2
        assert all(df["datetime"].to_list())
        # 列名一致即可; amount 列为空时 polars 推断成 Null, 严格 Float64 比较会失败
        assert set(df.columns) == set(MINUTE_SCHEMA.keys())
        for col in ("symbol", "datetime", "open", "high", "low", "close", "volume"):
            assert df[col].dtype == MINUTE_SCHEMA[col]

    def test_minute_volume_in_lots(self, provider_with_fake):
        provider, _ = provider_with_fake
        df = provider.get_minute(["002664.SZ"], "2024-01-01", "2024-12-31")
        # 50_000 股 = 500 手
        assert df["volume"][0] == 500.0

    def test_minute_datetime_wallclock_naive(self, provider_with_fake):
        """vendor §"get_minute 的 datetime 时区契约": 北京墙钟 naive (无 tzinfo)."""
        provider, _ = provider_with_fake
        df = provider.get_minute(["002664.SZ"], "2024-01-01", "2024-12-31")
        first_dt = df["datetime"][0]
        assert first_dt.tzinfo is None
        assert first_dt.hour == 9
        assert first_dt.minute == 35


# ---------------------------------------------------------------- test_dataset (vendor 强烈建议)


class TestDatasetProbe:
    def test_probe_daily_success(self, provider_with_fake):
        provider, _ = provider_with_fake
        result = provider.test_dataset("daily", ["002664.SZ"])
        assert result["provider"] == "cls"
        assert result["dataset"] == "daily"
        assert result["rows"] == 2
        assert "symbol" in result["columns"]
        assert result.get("error") is None

    def test_probe_undeclared_dataset_returns_error(self, provider_with_fake):
        """未声明的数据集 → 错误信息, 不 crash."""
        provider, _ = provider_with_fake
        result = provider.test_dataset("realtime", ["002664.SZ"])
        assert result["rows"] == 0
        assert "error" in result
        assert "回退" in result["error"]


# ---------------------------------------------------------------- close 资源管理


class TestResourceManagement:
    def test_close_clears_client(self, monkeypatch, fixtures):
        provider, fake = _provider_with(monkeypatch, fixtures)
        provider._ensure_client()  # 触发 lazy init
        assert provider.client is not None
        provider.close()
        assert provider.client is None
        fake.close()  # 不报错即可

    def test_reinit_after_close(self, monkeypatch, fixtures):
        provider, _fake = _provider_with(monkeypatch, fixtures)
        provider._ensure_client()
        provider.close()
        # 重新调用 → lazy 重建 client
        provider._ensure_client()
        assert provider.client is not None