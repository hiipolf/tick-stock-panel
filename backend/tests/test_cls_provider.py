"""财联社 (CLS) Provider 契约测试.

不依赖真实网络: 用假 ClsClient 注入实测响应, 验证
1. 字段映射与单位转换 (business_amount 股→手 / change 已是小数制不再 /100 /
   business_balance 元)
2. 分钟 `minute` int HHMM → 北京墙钟 naive datetime (930 → 09:30:00)
3. 缺字段置 None 不伪造
4. symbol 归一 (北交所二位前缀 {43,83,87,92}; 900001 → .SH)
5. `secu_code` 双向转换 (600519.SH ↔ sh600519)
6. 软失败: 单标的失败不阻断整批
7. on_chunk_done 覆盖空批次且最终 cur == total
8. 能力声明: 只有 daily / minute
9. test_dataset 成功 / 失败两种形状
10. sector_rotation 遇到非 dict 元素要跳过 (不 AttributeError)
11. sign 计算正确性 (固定输入 → 确定值)

样例数据全部来自 2026-10-19 真连 CLS 的响应 (字段名 / 单位均已核对).

vendor 规范: docs/plugin-development.md §"测试要求"
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

try:  # 装进 TSP 仓库后走正式包路径
    from app.plugins.cls import bridge as cls_bridge
    from app.plugins.cls import client as cls_client
    from app.plugins.cls import provider as cls_provider
except ModuleNotFoundError:  # 暂存区: 插件还在 plugins/ 下
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from cls import bridge as cls_bridge
    from cls import client as cls_client
    from cls import provider as cls_provider
ClsError = cls_client.ClsError
ClsValidationError = cls_client.ClsValidationError
make_sign = cls_client.make_sign
signed_params = cls_client.signed_params
web_params = cls_client.web_params
BJ_PREFIXES = cls_provider.BJ_PREFIXES
DAILY_SCHEMA = cls_provider.DAILY_SCHEMA
MINUTE_SCHEMA = cls_provider.MINUTE_SCHEMA
ClsConfig = cls_provider.ClsConfig
ClsProvider = cls_provider.ClsProvider
from_secu_code = cls_provider.from_secu_code
normalize_symbol = cls_provider.normalize_symbol
strip_suffix = cls_provider.strip_suffix
to_secu_code = cls_provider.to_secu_code

# ────────────────── 假 client ──────────────────


class FakeClsClient:
    """按方法名返回预置响应, 记录调用供断言.可指定某些方法抛错."""

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, Any]] = []
        self.fail: set[str] = set()

    def _maybe_fail(self, kind: str) -> None:
        if kind in self.fail:
            raise ClsError(f"fake failure: {kind}")

    def _reply(self, kind: str, payload: Any = None) -> Any:
        self.calls.append((kind, payload))
        self._maybe_fail(kind)
        if kind in self.responses:
            return self.responses[kind]
        return []

    def kline(
        self,
        secu_code: str,
        *,
        kline_type: str = "fd1",
        limit: int = 500,
        offset: int = 0,
    ) -> Any:
        self.calls.append(("kline", (secu_code, kline_type, limit, offset)))
        self._maybe_fail("kline")
        if secu_code in self.fail:
            raise ClsError(f"fake failure: kline {secu_code}")
        return self.responses.get("kline", [])

    def timeline(self, secu_code: str, *, fields: str = "") -> Any:
        self.calls.append(("timeline", secu_code))
        self._maybe_fail("timeline")
        if secu_code in self.fail:
            raise ClsError(f"fake failure: timeline {secu_code}")
        return self.responses.get("timeline", {})

    def stock_emotion(self) -> Any:
        return self._reply("stock_emotion", self.responses.get("stock_emotion", {}))

    def limit_up_pool(self, pool_type: str = "up_pool") -> Any:
        return self._reply("limit_up_pool", self.responses.get("limit_up_pool", []))

    def plate_list(self, page: int = 1) -> Any:
        return self._reply("plate_list", self.responses.get("plate_list", {}))

    def plate_heat(self, plate_type: str = "concept", page: int = 1, page_size: int = 20) -> Any:
        return self._reply("plate_heat", self.responses.get("plate_heat", []))

    def plate_rotation(self, days: int = 4) -> Any:
        return self._reply("plate_rotation", self.responses.get("plate_rotation", []))

    def today_tuyere(self) -> Any:
        return self._reply("today_tuyere", self.responses.get("today_tuyere", {}))

    def market_mainline(self) -> Any:
        return self._reply("market_mainline", self.responses.get("market_mainline", {}))

    def tuyere_stocks(self, plate_code: str, page: int = 1, page_size: int = 20) -> Any:
        return self._reply("tuyere_stocks", self.responses.get("tuyere_stocks", []))

    def popular_stocks(self, plate_code: str, page: int = 1, page_size: int = 20) -> Any:
        return self._reply("popular_stocks", self.responses.get("popular_stocks", []))

    def telegraph(self, rn: int = 20, last_time: str | int = "") -> Any:
        return self._reply("telegraph", self.responses.get("telegraph", {}))

    def close(self) -> None:
        pass


# ────────────────── 实测样例 (2026-10-19 真连响应) ──────────────────

# x-quote.cls.cn/quote/stock/kline → data 是 list of dict
SAMPLE_KLINE = [
    {
        "date": 20261015,
        "secu_code": "sh600519",
        "close_px": 1312.97,
        "high_px": 1317.76,
        "low_px": 1282.0,
        "open_px": 1286.0,
        "preclose_px": 1286.0,
        "change": 0.021,
        "change_color": 1,
        "business_amount": 5767722,
        "business_balance": 7537706224,
        "tr": 0.0046,
        "amp": 0.0278,
        "ma5": 1284.97,
        "ma10": 1265.57,
        "ma20": 1265.32,
    },
    {
        "date": 20261016,
        "secu_code": "sh600519",
        "close_px": 1329.0,
        "high_px": 1329.88,
        "low_px": 1301.68,
        "open_px": 1311.57,
        "preclose_px": 1312.97,
        "change": 0.0122,
        "business_amount": 3850175,
        "business_balance": 5075528306,
        "tr": 0.0031,
        "amp": 0.0215,
    },
]

# x-quote.cls.cn/quote/stock/tline → data 是 dict {"date": [int], "line": [...]}
SAMPLE_TIMELINE = {
    "date": [20261019],
    "line": [
        {
            "date": 20261019,
            "minute": 930,
            "last_px": 1322.5,
            "av_px": 1322.5,
            "change": -0.0019,
            "preclose_px": 1325.0,
            "open_px": 1325.0,
            "business_amount": 13081,
            "business_balance": 17299623,
        },
        {
            "date": 20261019,
            "minute": 1130,
            "last_px": 1330.0,
            "av_px": 1326.0,
            "preclose_px": 1328.0,
            "open_px": 1329.0,
            "business_amount": 75200,
            "business_balance": 99661827,
        },
    ],
}

# x-quote.cls.cn/v2/quote/a/stock/emotion
SAMPLE_EMOTION = {
    "market_degree": "38",
    "shsz_balance": "1.44万亿",
    "up_ratio": "69.00%",
    "up_ratio_num": "46",
    "up_open_num": "21",
    "performance": "0.48%",
    "up_open_ratio": "51%",
    "profit_ratio": "54%",
    "up_down_dis": {
        "status": True,
        "suspend_num": 11,
        "up_num": 49,
        "down_num": 25,
        "rise_num": 2275,
        "fall_num": 3129,
        "flat_num": 160,
        "down_10": 63,
        "down_8": 67,
        "down_6": 176,
        "down_4": 882,
        "down_2": 1941,
        "up_2": 1528,
    },
    "limit_up_board": {
        "row1": ["一板", "二板", "三板", "高度板"],
        "row2": ["35", "9", "2", "0"],
        "row3": ["连板率", "17%", "50%", "0%"],
    },
}

# x-quote.cls.cn/quote/index/up_down_analysis (up_pool)
SAMPLE_LIMIT_UP = [
    {
        "secu_code": "sz301666",
        "secu_name": "大普微-UW",
        "change": 0.2,
        "last_px": 328.51,
        "time": "2026-10-19 10:25:00",
        "up_reason": "存储芯片|大普微是国内领先的本土企业级SSD供应商",
        "num_stocks": 49,
        "plate": [
            {"secu_code": "cls80098", "secu_name": "存储芯片", "change": 0.0071},
            {"secu_code": "cls80195", "secu_name": "先进封装", "change": -0.0017},
        ],
    }
]

# x-quote.cls.cn/v2/quote/a/plate/plate_heat_list
SAMPLE_HEAT = [
    {
        "plate_code": "cls80094",
        "rank": 1,
        "cur_heat": 4331.8042408835,
        "rank_change": 5,
        "is_new": 0,
        "plate_name": "特斯拉概念",
    }
]

# x-quote.cls.cn/web_quote/plate/plate_list
SAMPLE_PLATE_LIST = {
    "is_all": 1,
    "plate_data": [
        {
            "secu_name": "半导体",
            "secu_code": "cls81901",
            "limit_up": 17,
            "limit_down": 8,
            "main_fund_diff": 6964505,
            "trade_status": "ENDTR",
            "limit_up_num": 1,
            "limit_down_num": 0,
            "change": 0.0169,
            "first_stock": {
                "last_px": 67.22,
                "secu_code": "sz003021",
                "secu_name": "智微智能",
                "trade_status": "ENDTR",
                "change": 0.1,
                "tr": 0.0246,
            },
        }
    ],
}

# x-quote.cls.cn/v2/quote/a/plate/rotation
SAMPLE_ROTATION = [
    {
        "trade_date": "2026-10-19",
        "plates": [
            {"plate_code": "cls82604", "plate_name": "物理AI", "change": 5.63},
            {"plate_code": "cls80068", "plate_name": "染料涂料", "change": 1.46},
        ],
    }
]

# api3.cls.cn/v2/todayTuyere
SAMPLE_TUYERE = {
    "today_tuyere": [
        {
            "id": 14,
            "title": "物理AI",
            "plate_code": "cls82604",
            "subject_id": 12868,
            "interpret": "当地时间10月20日至22日,英伟达GTC大会将在德国柏林举行.",
        }
    ]
}

# api3.cls.cn/v2/dingPan/mainline (⚠️ chances/winds 是 list, styles 是 dict)
SAMPLE_MAINLINE = {
    "chance_desc": "判断主线机会,洞悉趋势强度",
    "chances": [
        {
            "continued": 4,
            "hot": 4,
            "id": 4,
            "mainLine_desc": "字节跳动旗下短剧应用PineDrama上线约9个月后累计下载量突破1亿.",
            "plates": [{"code": "cls80155", "name": "文化传媒", "rise_count": 2}],
            "title": "影视传媒",
        }
    ],
    "mainLine_desc": "精准把控主力动向",
    "style_desc": "...",
    "styles": {
        "id": 2499598,
        "index_1": {"code": "sh000001", "name": "上证指数"},
        "index_2": None,
        "interpret": "10月A股进入业绩验证期,市场风格再平衡仍是主线.",
        "title": "A股风格重构深化",
    },
    "wind_desc": "...",
    "winds": [
        {
            "faucet_stocks": [
                {"stock_code": "sz301313", "stock_name": "凡拓数创", "rise_range": 20},
            ],
            "hot": 4,
            "id": 14,
            "interpret": "英伟达GTC大会将在德国柏林举行.",
            "plate_change": 0.0563,
            "plate_code": "cls82604",
            "subject_id": 12868,
            "title": "物理AI",
        }
    ],
}

# api3.cls.cn/v1/roll/get_roll_list
SAMPLE_TELEGRAPH = {
    "roll_data": [
        {
            "id": 2508990,
            "title": "",
            "content": "财联社10月19日电,Anthropic准备推出推理能力更强的模型.",
            "brief": "财联社10月19日电,Anthropic准备推出推理能力更强的模型.",
            "ctime": 1792418853,
            "level": "C",
            "reading_num": 2844,
        },
        {
            "id": 2508954,
            "title": "【公告全知道】PCB+光模块",
            "content": "①PCB+光模块+卫星通信+华为!",
            "brief": "①PCB+光模块+卫星通信+华为!",
            "ctime": 1792418805,
            "level": "C",
            "reading_num": 0,
        },
        {
            "id": 2500000,
            "title": "普通快讯",
            "content": "非重要",
            "brief": "非重要",
            "ctime": 1792410000,
            "level": "B",
            "reading_num": 1,
        },
    ]
}

# x-quote.cls.cn/v2/quote/a/plate/tuyere/stocks (change 是**带百分号的字符串**)
SAMPLE_TUYERE_STOCKS = [
    {
        "secu_code": "sz301666",
        "secu_name": "大普微-UW",
        "ori_code": "301666",
        "change": "+20.00%",
        "change_px": 54.75,
        "last_px": "328.51",
        "trade_status": "ENDTR",
        "limit_up_days": 0,
    }
]


# ────────────────── fixtures ──────────────────


def _provider(responses: dict[str, Any] | None = None) -> ClsProvider:
    provider = ClsProvider(config=ClsConfig(min_interval=0.0))
    provider.client = FakeClsClient(responses or {})
    return provider


@pytest.fixture
def kline_provider() -> ClsProvider:
    return _provider({"kline": SAMPLE_KLINE})


@pytest.fixture
def timeline_provider() -> ClsProvider:
    return _provider({"timeline": SAMPLE_TIMELINE})


# ────────────────── 1. kline 字段映射与单位 ──────────────────


class TestKlineMapping:
    def test_volume_shares_to_lots(self, kline_provider):
        """business_amount 是**股** → 必须 /100 转手 (vendor §"单位制" 强约束)."""
        df = kline_provider.get_daily(["600519.SH"], "2026-10-01", "2026-10-31")
        assert df.height == 2
        # 5767722 股 = 57677.22 手
        assert df["volume"][0] == pytest.approx(57677.22)
        # 3850175 股 = 38501.75 手
        assert df["volume"][1] == pytest.approx(38501.75)

    def test_amount_in_yuan(self, kline_provider):
        """business_balance 单位是元 (不是万元)."""
        df = kline_provider.get_daily(["600519.SH"], "2026-10-01", "2026-10-31")
        assert df["amount"][0] == pytest.approx(7537706224.0)
        assert df["amount"][1] == pytest.approx(5075528306.0)

    def test_ohlc_from_px_fields(self, kline_provider):
        """open_px/high_px/low_px/close_px → open/high/low/close, 不复权原始价."""
        df = kline_provider.get_daily(["600519.SH"], "2026-10-01", "2026-10-31")
        assert df["open"][0] == pytest.approx(1286.0)
        assert df["high"][0] == pytest.approx(1317.76)
        assert df["low"][0] == pytest.approx(1282.0)
        assert df["close"][0] == pytest.approx(1312.97)

    def test_date_is_date_object(self, kline_provider):
        """date 字段是 int YYYYMMDD → canonical pl.Date."""
        df = kline_provider.get_daily(["600519.SH"], "2026-10-01", "2026-10-31")
        assert df["date"][0] == date(2026, 10, 15)
        assert df["date"][1] == date(2026, 10, 16)

    def test_change_already_decimal_not_rescaled(self, kline_provider):
        """change 已经是小数制 (0.021 = 2.1%) → **不能再 /100**."""
        bar = kline_provider.map_daily_bar("600519.SH", SAMPLE_KLINE[0])
        assert bar["change_pct"] == pytest.approx(0.021)
        assert bar["change_pct"] != pytest.approx(0.00021)

    def test_turnover_and_amplitude_decimal(self, kline_provider):
        """tr / amp 也是小数制."""
        bar = kline_provider.map_daily_bar("600519.SH", SAMPLE_KLINE[0])
        assert bar["turnover_rate"] == pytest.approx(0.0046)
        assert bar["amplitude"] == pytest.approx(0.0278)

    def test_daily_schema_is_8_columns(self, kline_provider):
        """vendor 契约: daily 固定 8 列 (change_pct 等 CLS 独有字段不进表)."""
        df = kline_provider.get_daily(["600519.SH"], "2026-10-01", "2026-10-31")
        assert df.columns == [
            "symbol",
            "date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "amount",
        ]
        assert df.schema == DAILY_SCHEMA
        assert "change_pct" not in df.columns

    def test_date_range_filter(self, kline_provider):
        """只返回 [start_time, end_time] 区间内的 K 线."""
        df = kline_provider.get_daily(["600519.SH"], "2026-10-16", "2026-10-31")
        assert df.height == 1
        assert df["date"][0] == date(2026, 10, 16)

    def test_secu_code_passed_to_client(self, kline_provider):
        """入参 600519.SH → 上游 secu_code sh600519."""
        kline_provider.get_daily(["600519.SH"], "2026-10-01", "2026-10-31")
        kinds = [c for c in kline_provider.client.calls if c[0] == "kline"]
        assert kinds[0][1][0] == "sh600519"


# ────────────────── 2. timeline minute → 北京墙钟 ──────────────────


class TestMinuteContract:
    def test_minute_int_to_beijing_wallclock(self, timeline_provider):
        """minute int HHMM → 北京墙钟 naive datetime (930 → 09:30:00)."""
        df = timeline_provider.get_minute(["600519.SH"], "2026-10-19", "2026-10-19")
        assert df.height == 2
        assert df["datetime"][0] == datetime(2026, 10, 19, 9, 30, 0)
        assert df["datetime"][1] == datetime(2026, 10, 19, 11, 30, 0)

    def test_datetime_is_naive(self, timeline_provider):
        """必须 naive (带时区会被 vendor 守卫二次转换, 源头就要写对)."""
        df = timeline_provider.get_minute(["600519.SH"], "2026-10-19", "2026-10-19")
        assert df.schema["datetime"].time_zone is None

    def test_datetime_uses_response_date_not_today(self, timeline_provider):
        """交易日取响应里的 date, **不是** date.today()."""
        df = timeline_provider.get_minute(["600519.SH"], "2026-10-19", "2026-10-19")
        assert df["datetime"][0].date() == date(2026, 10, 19)

    def test_minute_volume_shares_to_lots(self, timeline_provider):
        """business_amount (股) → /100 → 手."""
        df = timeline_provider.get_minute(["600519.SH"], "2026-10-19", "2026-10-19")
        assert df["volume"][0] == pytest.approx(130.81)
        assert df["volume"][1] == pytest.approx(752.0)

    def test_minute_amount_in_yuan(self, timeline_provider):
        """business_balance 单位元."""
        df = timeline_provider.get_minute(["600519.SH"], "2026-10-19", "2026-10-19")
        assert df["amount"][0] == pytest.approx(17299623.0)

    def test_minute_open_close(self, timeline_provider):
        """open_px → open, last_px → close."""
        df = timeline_provider.get_minute(["600519.SH"], "2026-10-19", "2026-10-19")
        assert df["open"][0] == pytest.approx(1325.0)
        assert df["close"][0] == pytest.approx(1322.5)

    def test_minute_schema_is_8_columns(self, timeline_provider):
        df = timeline_provider.get_minute(["600519.SH"], "2026-10-19", "2026-10-19")
        assert df.columns == [
            "symbol",
            "datetime",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "amount",
        ]
        assert df.schema == MINUTE_SCHEMA

    def test_bad_freq_raises_validation_error(self, timeline_provider):
        """校验错误 fail-fast (与运行时错误区分)."""
        with pytest.raises(ClsValidationError):
            timeline_provider.get_minute(["600519.SH"], "2026-10-19", "2026-10-19", freq="5m")

    def test_timeline_date_list_fallback(self):
        """data["date"] 是 list 时也要能取到交易日."""
        provider = _provider(
            {
                "timeline": {
                    "date": [20261019],
                    "line": [{"minute": 1300, "last_px": 1330.0, "business_amount": 100}],
                }
            }
        )
        df = provider.get_minute(["600519.SH"], "2026-10-19", "2026-10-19")
        assert df.height == 1
        assert df["datetime"][0] == datetime(2026, 10, 19, 13, 0, 0)


# ────────────────── 3. 缺字段置 None 不伪造 ──────────────────


class TestMissingFields:
    def test_missing_daily_fields_are_none(self, kline_provider):
        """缺字段 → None (不填 0, 不启发式推算)."""
        bar = kline_provider.map_daily_bar("600519.SH", {"date": 20261015, "open_px": 10.0})
        assert bar["high"] is None
        assert bar["low"] is None
        assert bar["close"] is None
        assert bar["volume"] is None
        assert bar["amount"] is None

    def test_missing_daily_fields_in_frame_are_null(self, kline_provider):
        provider = _provider({"kline": [{"date": 20261015, "open_px": 10.0, "close_px": 10.5}]})
        df = provider.get_daily(["600519.SH"], "2026-10-01", "2026-10-31")
        assert df.height == 1
        assert df["open"][0] == pytest.approx(10.0)
        assert df["close"][0] == pytest.approx(10.5)
        assert df["high"][0] is None
        assert df["volume"][0] is None

    def test_missing_minute_amount_is_none(self, timeline_provider):
        """分钟 amount 不可靠时置 None, 不伪造."""
        bar = timeline_provider.map_minute_bar(
            "600519.SH", {"date": 20261019, "minute": 930, "last_px": 100.0}
        )
        assert bar["amount"] is None
        assert bar["volume"] is None

    def test_minute_high_low_are_none(self, timeline_provider):
        """CLS 分时没有 high/low → None (不拿 last_px 顶替)."""
        bar = timeline_provider.map_minute_bar("600519.SH", SAMPLE_TIMELINE["line"][0])
        assert bar["high"] is None
        assert bar["low"] is None

    def test_empty_response_gives_empty_frame_with_schema(self, kline_provider):
        provider = _provider({"kline": []})
        df = provider.get_daily(["600519.SH"], "2026-10-01", "2026-10-31")
        assert df.is_empty()
        assert df.schema == DAILY_SCHEMA


class TestInvalidPriceGuard:
    """上游脏数据 (负价格) 必须在 provider 层拦掉, 且必须打 warning."""

    def test_negative_price_bars_are_dropped(self):
        provider = _provider(
            {
                "kline": [
                    {
                        "date": 20120515,
                        "open_px": -131.23,
                        "high_px": -129.11,
                        "low_px": -132.05,
                        "close_px": -129.57,
                        "business_amount": 1616874,
                    },
                    {
                        "date": 20261019,
                        "open_px": 1322.5,
                        "high_px": 1348.6,
                        "low_px": 1321.0,
                        "close_px": 1324.97,
                        "business_amount": 3972894,
                    },
                ]
            }
        )
        df = provider.get_daily(["600519.SH"], "2012-01-01", "2026-10-31")
        assert df.height == 1
        assert df["date"][0] == date(2026, 10, 19)

    def test_zero_price_bars_are_dropped(self):
        """停牌日 (open/high=0) 也丢掉, 与 vendor filter_halt_days 口径一致."""
        provider = _provider(
            {
                "kline": [
                    {
                        "date": 20261016,
                        "open_px": 0,
                        "high_px": 0,
                        "low_px": 0,
                        "close_px": 1329.0,
                        "business_amount": 0,
                        "business_balance": 0,
                    },
                ]
            }
        )
        df = provider.get_daily(["600519.SH"], "2026-10-01", "2026-10-31")
        assert df.is_empty()

    def test_missing_price_is_kept(self):
        """缺价格 (None) 不是"非法价格", 保留该行 (缺字段置 None 不伪造)."""
        provider = _provider(
            {"kline": [{"date": 20261019, "open_px": 1322.5, "close_px": 1324.97}]}
        )
        df = provider.get_daily(["600519.SH"], "2026-10-01", "2026-10-31")
        assert df.height == 1
        assert df["high"][0] is None

    def test_invalid_minute_price_is_dropped(self):
        provider = _provider(
            {
                "timeline": {
                    "date": [20261019],
                    "line": [
                        {"date": 20261019, "minute": 930, "last_px": 0, "open_px": 0},
                        {
                            "date": 20261019,
                            "minute": 931,
                            "last_px": 1329.0,
                            "open_px": 1322.99,
                        },
                    ],
                }
            }
        )
        df = provider.get_minute(["600519.SH"], "2026-10-19", "2026-10-19")
        assert df.height == 1
        assert df["datetime"][0] == datetime(2026, 10, 19, 9, 31, 0)


# ────────────────── 4. symbol 归一 (含北交所二位前缀) ──────────────────


class TestSymbolNormalize:
    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            ("600519", "600519.SH"),
            ("688981", "688981.SH"),
            ("900001", "900001.SH"),  # B 股段, 不是北交所
            ("000001", "000001.SZ"),
            ("300750", "300750.SZ"),
            ("430047", "430047.BJ"),  # 北交所二位前缀
            ("830799", "830799.BJ"),
            ("871981", "871981.BJ"),
            ("920001", "920001.BJ"),
        ],
    )
    def test_bare_code(self, code: str, expected: str):
        assert normalize_symbol(code) == expected

    def test_already_suffixed(self):
        assert normalize_symbol("600519.SH") == "600519.SH"
        assert normalize_symbol("000001.SZ") == "000001.SZ"
        assert normalize_symbol("600519.sh") == "600519.SH"

    def test_bj_prefix_set(self):
        """北交所口径是二位前缀 {43,83,87,92}, 不是 ("4","8")."""
        assert frozenset({"43", "83", "87", "92"}) == BJ_PREFIXES

    def test_unrecognizable_returns_none(self):
        # 9 开头是沪市 B 股段 (900001 → .SH), 不是"无法识别"
        assert normalize_symbol("900001") == "900001.SH"
        assert normalize_symbol("") is None
        assert normalize_symbol(None) is None
        assert normalize_symbol("12345") is None
        assert normalize_symbol("123456") is None  # 1 开头无对应交易所

    def test_strip_suffix(self):
        assert strip_suffix("600519.SH") == "600519"
        assert strip_suffix("430047.BJ") == "430047"


# ────────────────── 5. secu_code 双向转换 ──────────────────


class TestSecuCode:
    @pytest.mark.parametrize(
        ("symbol", "secu_code"),
        [
            ("600519.SH", "sh600519"),
            ("000001.SZ", "sz000001"),
            ("300750.SZ", "sz300750"),
            ("430047.BJ", "bj430047"),
            ("920001.BJ", "bj920001"),
            ("600519", "sh600519"),
        ],
    )
    def test_to_secu_code(self, symbol: str, secu_code: str):
        assert to_secu_code(symbol) == secu_code

    @pytest.mark.parametrize(
        ("secu_code", "symbol"),
        [
            ("sh600519", "600519.SH"),
            ("sz000001", "000001.SZ"),
            ("bj430047", "430047.BJ"),
            ("bj920001", "920001.BJ"),
        ],
    )
    def test_from_secu_code(self, secu_code: str, symbol: str):
        assert from_secu_code(secu_code) == symbol

    def test_roundtrip(self):
        for symbol in ("600519.SH", "000001.SZ", "430047.BJ", "920001.BJ"):
            assert from_secu_code(to_secu_code(symbol)) == symbol

    def test_invalid_secu_code(self):
        assert from_secu_code("") is None
        assert from_secu_code("xx600519") is None
        assert to_secu_code("123456") is None


# ────────────────── 6. 软失败 ──────────────────


class TestSoftFailure:
    def test_single_symbol_failure_does_not_block_batch(self):
        """单标的失败 → warning + 继续, 不炸整批."""
        provider = _provider({"kline": SAMPLE_KLINE})
        provider.client.fail = {"sh600519"}
        df = provider.get_daily(["600519.SH", "000001.SZ"], "2026-10-01", "2026-10-31")
        # 600519.SH 失败, 000001.SZ 照常拿到 2 根 K 线
        assert df.height == 2
        assert set(df["symbol"].to_list()) == {"000001.SZ"}

    def test_all_failures_return_empty_frame(self):
        provider = _provider({"kline": SAMPLE_KLINE})
        provider.client.fail = {"sh600519", "sz000001"}
        df = provider.get_daily(["600519.SH", "000001.SZ"], "2026-10-01", "2026-10-31")
        assert df.is_empty()
        assert df.schema == DAILY_SCHEMA

    def test_minute_single_failure_does_not_block_batch(self):
        provider = _provider({"timeline": SAMPLE_TIMELINE})
        provider.client.fail = {"sh600519"}
        df = provider.get_minute(["600519.SH", "000001.SZ"], "2026-10-19", "2026-10-19")
        assert df.height == 2
        assert set(df["symbol"].to_list()) == {"000001.SZ"}

    def test_unrecognizable_symbol_is_skipped_not_fatal(self, kline_provider):
        df = kline_provider.get_daily(["123456", "600519.SH"], "2026-10-01", "2026-10-31")
        assert df.height == 2
        assert "123456" not in df["symbol"].to_list()
        assert set(df["symbol"].to_list()) == {"600519.SH"}


# ────────────────── 7. on_chunk_done 契约 ──────────────────


class TestChunkProgress:
    def test_empty_batch_still_calls_back(self, kline_provider):
        """空批次也必须回调 (否则进度条永远走不到 100%)."""
        seen: list[tuple[int, int]] = []
        df = kline_provider.get_daily([], "2026-10-01", "2026-10-31", on_chunk_done=lambda c, t: seen.append((c, t)))
        assert df.is_empty()
        assert seen == [(0, 0)]

    def test_minute_empty_batch_still_calls_back(self, timeline_provider):
        seen: list[tuple[int, int]] = []
        timeline_provider.get_minute(
            [], "2026-10-19", "2026-10-19", on_chunk_done=lambda c, t: seen.append((c, t))
        )
        assert seen == [(0, 0)]

    def test_final_cur_equals_total(self, kline_provider):
        seen: list[tuple[int, int]] = []
        kline_provider.get_daily(
            ["600519.SH", "000001.SZ", "300750.SZ"],
            "2026-10-01",
            "2026-10-31",
            on_chunk_done=lambda c, t: seen.append((c, t)),
        )
        assert seen == [(1, 3), (2, 3), (3, 3)]
        assert seen[-1][0] == seen[-1][1]

    def test_callback_fires_even_when_symbol_fails(self):
        provider = _provider({"kline": SAMPLE_KLINE})
        provider.client.fail = {"sh600519"}
        seen: list[tuple[int, int]] = []
        provider.get_daily(
            ["600519.SH", "000001.SZ"],
            "2026-10-01",
            "2026-10-31",
            on_chunk_done=lambda c, t: seen.append((c, t)),
        )
        assert seen == [(1, 2), (2, 2)]


# ────────────────── 8. 能力声明 ──────────────────


class TestCapabilityDeclaration:
    def test_only_minute_declared(self, kline_provider):
        """只声明 minute.

        ⚠️ daily **故意不声明**: CLS 的 kline 价格是减法式前复权价
        (真实价 - 累计每股分红), 声明 daily 会与框架 adj_factor 管道双重复权
        → 静默产出错误价格.见 README §4.2.
        """
        assert set(kline_provider.config.datasets) == {"minute"}
        assert kline_provider.config.datasets["minute"] is True

    def test_daily_must_not_be_declared(self, kline_provider):
        """安全断言: daily 绝不能出现在声明里 (双重复权风险)."""
        assert "daily" not in kline_provider.config.datasets

    @pytest.mark.parametrize(
        "dataset", ["realtime", "depth5", "adj_factor", "financial", "full_minute", "daily"]
    )
    def test_other_datasets_not_declared(self, kline_provider, dataset: str):
        """未声明 → provider_has_dataset False → 回退 tickflow."""
        assert dataset not in kline_provider.config.datasets

    def test_plugin_yaml_datasets(self):
        import yaml

        manifest = Path(cls_provider.__file__).resolve().parent / "plugin.yaml"
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        assert data["datasets"] == ["minute"]
        assert data["name"] == "cls"
        assert data["entry"] == "app.plugins.cls.provider:ClsProvider"
        assert data["check"] == "app.plugins.cls.bridge:availability"
        assert data.get("api_key_env") is None

    def test_provider_identity(self, kline_provider):
        assert kline_provider.name == "cls"
        # 随仓库分发的内置插件 → builtin=True (不可被用户编辑/删除).
        # 见 docs/plugin-development.md §"plugin.yaml 字段" 与 fuyao/stocksdk 的取值.
        assert kline_provider.builtin is True


# ────────────────── 9. test_dataset 两种形状 ──────────────────


class TestDatasetProbe:
    def test_success_shape(self, kline_provider):
        result = kline_provider.test_dataset("daily", ["600519.SH"])
        # daily 未声明 → 明确回退提示 (而不是假装能提供)
        assert result["provider"] == "cls"
        assert result["dataset"] == "daily"
        assert result["rows"] == 0
        assert "回退 tickflow" in result["error"]

    def test_minute_success_shape(self, timeline_provider):
        result = timeline_provider.test_dataset("minute", ["600519.SH"])
        assert result["rows"] == 2
        assert result.get("error") is None

    def test_undeclared_dataset_shape(self, kline_provider):
        result = kline_provider.test_dataset("realtime", ["600519.SH"])
        assert result["rows"] == 0
        assert result["columns"] == []
        assert "回退 tickflow" in result["error"]

    def test_failure_shape(self, monkeypatch):
        """provider 抛异常 → 返回带 error 的形状 (不炸设置页)."""
        provider = _provider({"timeline": SAMPLE_TIMELINE})

        def boom(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("上游炸了")

        monkeypatch.setattr(provider, "get_minute", boom)
        result = provider.test_dataset("minute", ["600519.SH"])
        assert result["rows"] == 0
        assert result["preview"] == []
        assert "上游炸了" in result["error"]

    def test_soft_failure_still_returns_ok_shape(self):
        """单标的软失败不算 probe 失败 (空表, 无 error)."""
        provider = _provider({"timeline": SAMPLE_TIMELINE})
        provider.client.fail = {"sh600519"}
        result = provider.test_dataset("minute", ["600519.SH"])
        assert result["rows"] == 0
        assert result.get("error") is None

    def test_forward_adjusted_price_warning(self, caplog):
        """close_px 与真实均价(成交额/成交量)偏离超阈值 → 打前复权告警.

        实测: CLS 的 kline 价格是减法式前复权价 (真实价 - 累计每股分红),
        深历史偏离可达 40%+, 2016 年甚至为负.
        """
        import logging

        provider = _provider({"kline": SAMPLE_KLINE})
        # 构造一根"复权后"的价格: 真实成交额/成交量 = 721.52, close_px = 437.75
        bar = {
            "date": 20180702,
            "secu_code": "sh600519",
            "open_px": 430.0,
            "high_px": 445.0,
            "low_px": 425.0,
            "close_px": 437.75,
            "preclose_px": 440.0,
            "change": -0.0435,
            "business_amount": 3_755_811.0,
            "business_balance": 2_709_882_026.0,
            "tr": 0.01,
            "amp": 0.02,
        }
        with caplog.at_level(logging.WARNING):
            provider._warn_forward_adjusted([bar], "600519.SH")
        assert any("前复权" in rec.getMessage() for rec in caplog.records)

    def test_forward_adjusted_no_warning_when_prices_agree(self, caplog):
        """close_px 与真实均价一致 → 不告警 (近期数据正常)."""
        import logging

        provider = _provider({"kline": SAMPLE_KLINE})
        bar = {
            "date": 20261016,
            "secu_code": "sh600519",
            "close_px": 1329.0,
            "business_amount": 3_850_175.0,
            "business_balance": 5_075_528_306.0,
        }
        with caplog.at_level(logging.WARNING):
            provider._warn_forward_adjusted([bar], "600519.SH")
        assert not [r for r in caplog.records if "前复权" in r.getMessage()]


# ────────────────── 10. sector_rotation 逐项 isinstance ──────────────────


class TestSectorRotation:
    def test_non_dict_elements_skipped(self):
        """上游偶尔混进非 dict 元素 → 跳过, 不能 AttributeError."""
        provider = _provider(
            {
                "plate_rotation": [
                    SAMPLE_ROTATION[0],
                    "垃圾字符串",
                    None,
                    123,
                    {"trade_date": "2026-10-18", "plates": ["坏元素", {"plate_code": "x"}]},
                ]
            }
        )
        rows = provider.get_sector_rotation(days=4)
        assert len(rows) == 2
        assert rows[0]["trade_date"] == "2026-10-19"
        assert rows[0]["plates"][0]["plate_name"] == "物理AI"
        assert rows[0]["plates"][0]["change_pct"] == pytest.approx(5.63)
        assert rows[1]["plates"] == [
            {"plate_code": "x", "plate_name": None, "change_pct": None}
        ]

    def test_data_is_dict_returns_empty(self):
        """days 不支持时上游把 data 换成 dict → 不炸, 返回空."""
        provider = _provider({"plate_rotation": {"days": "支持 4/30"}})
        assert provider.get_sector_rotation(days=4) == []

    def test_bad_days_raises_validation_error(self):
        provider = _provider({"plate_rotation": []})
        with pytest.raises(ClsValidationError):
            provider.get_sector_rotation(days=6)


# ────────────────── 11. sign 计算 ──────────────────


class TestSign:
    def test_make_sign_fixed_vectors(self):
        """固定输入 → 确定值 (md5(sha1(...)))."""
        assert make_sign({}) == "0144712dd81be0c3d9724f5e56ce6685"
        assert make_sign({"a": "1", "b": "2"}) == "4e13ecb1934db939e390065a9b63ecd8"
        # 参数顺序不影响结果 (上游按 key 排序)
        assert make_sign({"z": "1", "a": "2"}) == make_sign({"a": "2", "z": "1"})

    def test_make_sign_matches_manual_computation(self):
        import hashlib

        params = {"app": "cailianpress", "os": "android", "sv": "8.7.4"}
        text = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        expected = hashlib.md5(hashlib.sha1(text.encode()).hexdigest().encode()).hexdigest()
        assert make_sign(params) == expected
        assert make_sign(params) == "b60644f04b819c2e12cb4fc6cd80d84d"

    def test_signed_params_adds_sign(self):
        params = signed_params({"type": "concept"})
        assert params["app"] == "cailianpress"
        assert params["os"] == "android"
        assert params["type"] == "concept"
        assert params["sign"] == make_sign({k: v for k, v in params.items() if k != "sign"})

    def test_web_params_use_hardcoded_sign(self):
        params = web_params({"secu_code": "sh600519"}, "deadbeef")
        assert params["app"] == "CailianpressWeb"
        assert params["os"] == "web"
        assert params["sv"] == "8.4.6"
        assert params["sign"] == "deadbeef"


# ────────────────── CLS 独有短线数据 ──────────────────


class TestShortTermCapabilities:
    def test_market_emotion_limit_up_board(self):
        provider = _provider({"stock_emotion": SAMPLE_EMOTION})
        emotion = provider.get_market_emotion()
        assert emotion["up_down_dis"]["up_num"] == 49
        # 连板梯队: row1=梯队名 row2=家数 row3=连板率
        assert emotion["limit_up_board"]["row1"] == ["一板", "二板", "三板", "高度板"]
        assert emotion["limit_up_board"]["row2"] == ["35", "9", "2", "0"]
        assert emotion["limit_up_board"]["row3"][0] == "连板率"

    def test_limit_up_pool_splits_theme_and_reason(self):
        provider = _provider({"limit_up_pool": SAMPLE_LIMIT_UP})
        rows = provider.get_limit_up_pool()
        assert len(rows) == 1
        row = rows[0]
        assert row["symbol"] == "301666.SZ"
        assert row["name"] == "大普微-UW"
        assert row["change_pct"] == pytest.approx(0.2)  # 小数制, 不再 /100
        assert row["theme"] == "存储芯片"
        assert row["reason"] == "大普微是国内领先的本土企业级SSD供应商"
        assert row["plates"][0]["plate_code"] == "cls80098"

    def test_sector_heat(self):
        provider = _provider({"plate_heat": SAMPLE_HEAT})
        rows = provider.get_sector_heat()
        assert rows[0]["plate_code"] == "cls80094"
        assert rows[0]["plate_name"] == "特斯拉概念"
        assert rows[0]["rank"] == 1
        assert rows[0]["cur_heat"] == pytest.approx(4331.8042408835)

    def test_sector_industry(self):
        provider = _provider({"plate_list": SAMPLE_PLATE_LIST})
        rows = provider.get_sector_industry()
        assert rows[0]["plate_code"] == "cls81901"
        assert rows[0]["plate_name"] == "半导体"
        assert rows[0]["change_pct"] == pytest.approx(0.0169)
        assert rows[0]["first_stock"]["symbol"] == "003021.SZ"

    def test_market_wind(self):
        provider = _provider({"today_tuyere": SAMPLE_TUYERE})
        rows = provider.get_market_wind()
        assert rows[0]["plate_code"] == "cls82604"
        assert rows[0]["title"] == "物理AI"

    def test_market_mainline_handles_mixed_types(self):
        """chances/winds 是 list, styles 是 dict → 都要能解析."""
        provider = _provider({"market_mainline": SAMPLE_MAINLINE})
        rows = provider.get_market_mainline()
        sections = [row["section"] for row in rows]
        assert sections == ["chance", "wind", "style"]
        assert rows[0]["title"] == "影视传媒"
        assert rows[1]["plate_code"] == "cls82604"
        assert rows[2]["title"] == "A股风格重构深化"

    def test_telegraph_filters_by_date_and_level(self):
        provider = _provider({"telegraph": SAMPLE_TELEGRAPH})
        rows = provider.get_telegraph(date="2026-10-19", category="important", limit=10)
        assert len(rows) == 2  # level=="B" 的那条被过滤掉
        assert rows[0]["id"] == 2508990
        assert rows[0]["ctime_datetime"] == "2026-10-19 22:07:33"
        assert rows[0]["level"] == "C"

    def test_telegraph_all_category(self):
        provider = _provider({"telegraph": SAMPLE_TELEGRAPH})
        rows = provider.get_telegraph(category="all", limit=10)
        assert len(rows) == 3

    def test_telegraph_limit(self):
        provider = _provider({"telegraph": SAMPLE_TELEGRAPH})
        assert len(provider.get_telegraph(category="all", limit=1)) == 1

    def test_telegraph_bad_limit(self):
        provider = _provider({"telegraph": SAMPLE_TELEGRAPH})
        with pytest.raises(ClsValidationError):
            provider.get_telegraph(limit=0)

    def test_plate_stocks_percentage_string(self):
        """change 是 "+20.00%" 字符串 → 小数制 0.2."""
        provider = _provider({"tuyere_stocks": SAMPLE_TUYERE_STOCKS})
        rows = provider.get_plate_stocks("cls80098")
        assert rows[0]["symbol"] == "301666.SZ"
        assert rows[0]["change_pct"] == pytest.approx(0.2)
        assert rows[0]["last_price"] == pytest.approx(328.51)


# ────────────────── availability 两态 ──────────────────


class TestAvailability:
    def test_availability_is_two_state(self, monkeypatch):

        assert cls_bridge.availability() == (True, "ok")

    def test_availability_returns_false_on_error(self, monkeypatch):

        class BoomClient:
            def __init__(self, **kwargs: Any) -> None:
                pass

            def stock_emotion(self) -> Any:
                raise ClsError("上游 502")

            def close(self) -> None:
                pass

        monkeypatch.setattr(cls_bridge, "ClsClient", BoomClient, raising=False)
        cls_client = sys.modules[ClsProvider.__module__.rsplit(".", 1)[0] + ".client"]

        monkeypatch.setattr(cls_client, "ClsClient", BoomClient)
        ok, reason = cls_bridge.availability()
        assert ok is False
        assert "财联社不可达" in reason
