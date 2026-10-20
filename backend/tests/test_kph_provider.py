"""KPH Provider 契约与单位标准化测试.

不依赖真实网络: 用假 KphClient 注入位置数组响应, 验证
1. 字段映射与单位转换 (百分数→小数制 / 封单量股→手 / 缺字段置 None 不伪造)
2. 接口响应结构变体 (位置数组行 vs dict 行)
3. 分页 (空页终止 / 页数上限)
4. 软失败 (单 board_type 失败不拖垮整批)
5. 能力声明 (只声明 limit_board, 其余数据集 provider_has_dataset = False)
6. Key 语义 (无 Key 插件, availability 两态)
7. loader 集成 (plugin.yaml 解析)
8. symbol 归一 (北交所二位前缀 {43,83,87,92})
9. canonical schema 严格对齐 (27 列 / 类型)
10. board_type 参数校验
11. is_exploded (idx 24 = 是否炸过板) 的映射与板型边界

vendor 规范: docs/plugin-development.md §"测试要求"
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import polars as pl
import pytest

try:  # 装进 TSP 仓库后走正式包路径
    from app.plugins.kph import bridge as kph_bridge
    from app.plugins.kph import client as kph_client
    from app.plugins.kph import provider as kph_provider
except ModuleNotFoundError:  # 暂存区: 插件还在 plugins/ 下
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from kph import bridge as kph_bridge
    from kph import client as kph_client
    from kph import provider as kph_provider
BASE_PARAMS = kph_client.BASE_PARAMS
KphClient = kph_client.KphClient
KphError = kph_client.KphError
KphValidationError = kph_client.KphValidationError
_LIMIT_PAGE_SIZE = kph_client._LIMIT_PAGE_SIZE
_SECTOR_MAX_PAGES = kph_client._SECTOR_MAX_PAGES
extract_payload_date = kph_client.extract_payload_date
historical_date = kph_client.historical_date
is_non_trading_error = kph_client.is_non_trading_error
normalize_symbol = kph_client.normalize_symbol
parse_float = kph_client.parse_float
parse_int = kph_client.parse_int
previous_weekday = kph_client.previous_weekday
today_dash = kph_client.today_dash
LIMIT_BOARD_SCHEMA = kph_provider.LIMIT_BOARD_SCHEMA
MARKET_EVENTS_SCHEMA = kph_provider.MARKET_EVENTS_SCHEMA
KphConfig = kph_provider.KphConfig
KphProvider = kph_provider.KphProvider
_parse_consecutive_board = kph_provider._parse_consecutive_board
_pct = kph_provider._pct

# ────────────────── fixtures ──────────────────


class FakeKphClient:
    """按 board_type 返回预置响应, 记录调用供断言."""

    def __init__(
        self,
        limit_history: dict[str, list[list[Any]]] | None = None,
        ladder: list[list[Any]] | None = None,
        emotion: dict[str, Any] | None = None,
        sector: list[list[Any]] | None = None,
        sector_pages: list[list[list[Any]]] | None = None,
        resumption: list[dict[str, Any]] | None = None,
        events: list[dict[str, Any]] | None = None,
        error: Exception | None = None,
        fail_types: set[str] | None = None,
    ) -> None:
        self.limit_history_rows = limit_history or {}
        self.ladder = ladder or []
        self.emotion = emotion or {}
        self.sector = sector or []
        self.sector_pages = sector_pages
        self.resumption = resumption or []
        self.events = events or []
        self.error = error
        self.fail_types = fail_types or set()
        self.calls: list[tuple[str, Any]] = []

    def _maybe_fail(self, kind: str) -> None:
        if self.error is not None:
            raise self.error
        if kind in self.fail_types:
            raise KphError(f"fake failure: {kind}")

    def limit_history(self, board_type: str, trade_date: str, *, include_st: bool = True) -> list[list[Any]]:
        self.calls.append(("limit_history", (board_type, trade_date, include_st)))
        self._maybe_fail(board_type)
        return self.limit_history_rows.get(board_type, [])

    def limit_ladder(self, trade_date: str) -> list[list[Any]]:
        self.calls.append(("limit_ladder", trade_date))
        self._maybe_fail("ladder")
        return self.ladder

    def market_emotion(self, trade_date: str) -> dict[str, Any]:
        self.calls.append(("market_emotion", trade_date))
        self._maybe_fail("emotion")
        return self.emotion

    def sector_ranking(
        self, trade_date: str, sector_type: str = "selected", *, fetch_all: bool = False
    ) -> list[list[Any]]:
        self.calls.append(("sector_ranking", (trade_date, sector_type, fetch_all)))
        self._maybe_fail("sector")
        return self.sector

    def limit_resumption(self, trade_date: str) -> list[dict[str, Any]]:
        self.calls.append(("limit_resumption", trade_date))
        self._maybe_fail("resumption")
        return self.resumption

    def market_review_events(self, trade_date: str, limit: int = 30, offset: int = 0) -> list[dict[str, Any]]:
        self.calls.append(("market_review_events", trade_date))
        self._maybe_fail("event")
        return self.events

    def sector_constituents(self, trade_date: str, plate_id: str) -> list[list[Any]]:
        self.calls.append(("sector_constituents", (trade_date, plate_id)))
        self._maybe_fail("sector_constituents")
        return []

    def close(self) -> None:
        pass


def _provider(monkeypatch: pytest.MonkeyPatch, fake: FakeKphClient, **cfg: Any) -> KphProvider:
    """注入假 client 造 provider."""
    kp = kph_provider

    monkeypatch.setattr(kp, "KphClient", lambda **kw: fake)
    provider = KphProvider(config=KphConfig(**cfg))
    provider.client = fake
    return provider


# ────────────────── 样例数据 ──────────────────

# 涨停池位置数组 (口径: AxData _parse_limit_history_row)
# 真实响应行长 34, 这里**逐位对齐真实下标** (2026-10-20 晚核对修正过偏移):
#  0 code / 1 name / 6 limit_time / 7 (恒 0, 垃圾列) / 8 seal_amount(股)
#  9 连板描述文本 / 10 区间总板数 / 11 themes / 12 net_inflow / 13 amount(元)
#  14 turnover_rate(%) / 15 market_cap / 16 reason / 23 seal_money(元)
#  **24 is_exploded (是否炸过板, 布尔)** / 26 industry_id / 27 industry_limit_up_count
SAMPLE_LIMIT_UP = [
    "600519",              # 0 code
    "贵州茅台",             # 1 name
    None, None, None, None,  # 2-5
    93000,                 # 6 limit_time
    93100,                 # 7 (恒 0)
    1234567,               # 8 seal_amount (股) → 手 = 12345.67
    "3连板",               # 9 连板描述文本 → 当前连板天数 = 3
    3,                     # 10 区间总板数 (3)
    "白酒;消费",            # 11 themes
    -5678900,              # 12 net_inflow (元)
    1830000000,            # 13 amount (元)
    3.66,                  # 14 turnover_rate (%) → 小数制 0.0366
    210000000000,          # 15 market_cap
    "业绩超预期",           # 16 reason
    None, None, None, None, None, None,  # 17-22
    123456789,             # 23 seal_money (元)
    1,                     # 24 is_exploded (炸过板 → True)
    None,                  # 25
    "BK0012",              # 26 industry_id
    5,                     # 27 industry_limit_up_count
    None, None, None, None, None, None,  # 28-33
]

SAMPLE_LIMIT_DOWN = [
    "002594",              # 0 code
    "比亚迪",               # 1 name
    None, None, None, None,  # 2-5
    93000,                 # 6 limit_time
    93100,                 # 7
    500000,                # 8 seal_amount
    "跌停",                # 9 limit_tag
    None,                  # 10 limit_count (跌停池无连板数)
    "新能源",               # 11 themes
    1000000,               # 12 net_inflow
    500000000,             # 13 amount
    5.0,                   # 14 turnover_rate
    300000000000,          # 15 market_cap
    None,                  # 16 reason (跌停池无涨停原因)
    None, None, None, None, None, None,  # 17-22
    5000000,               # 23 seal_money
    0,                     # 24 is_exploded (跌停池实测恒 0)
    None,                  # 25
    "BK0012",              # 26 industry_id
    None,                  # 27
    None, None, None, None, None, None,  # 28-33
]

# 连板天梯: 0 code / 1 name / 2 limit_count / 3 limit_time / 4 plate_id
# 5 plate_name / 6 one_word / 7 popular / 8 plate_limit_up_count
# 9 amount / 10 plate_amount
SAMPLE_LADDER = [
    "600519", "贵州茅台", 3, 93000, "BK0012", "白酒",
    1, 1, 12, 5000000000, 12000000000,
]

# 板块排行的真实响应: idx2 = 板块热度积分 (排行排序键, 严格降序), idx5 = 板块成交额,
# idx8 = 净流入 (负=净流出), idx10 = 总市值, idx15/16 = 跨日恒定的板块静态常量
SAMPLE_SECTOR = [
    "801829", "低空经济",
    15400,             # 2 plate_heat_score (板块热度积分, 排行排序键)
    2.092,             # 3 change_pct (%)
    0.992,             # 4 amplitude (%)
    139910210891,      # 5 amount 板块成交额 (元)
    3369483227,        # 6 未确认
    8318438917,        # 7 未确认
    -4948955690,       # 8 net_inflow (元, 负=净流出)
    1.083,             # 9 turnover_rate (%)
    5875912964544,     # 10 market_cap (元)
    0.56,              # 11 未确认 (可正可负的日度比率)
    2204700000,        # 12 未确认 (可正可负的日度金额)
    None,              # 13
    None,              # 14
    35.6328,           # 15 未确认 (跨日恒定的板块静态常量)
    31.8739,           # 16 未确认 (跨日恒定的板块静态常量)
    15400,             # 17 == idx2 (重复)
    2.092,             # 18 == idx3 (重复)
]

# 市场情绪 (dict payload, 键是上游 info 下的字段)
# ⚠️ `qscln` 的单位是**万元** (2026-10-20 实测, 见 scripts/verify_kph_emotion_units.py),
# 不是元 —— provider 内部要 x1e4 换算成元.
# ⚠️ `DT` 含 ST (11), `SJDT` 不含 ST (10) —— 跨源比对时注意口径.
SAMPLE_EMOTION = {
    "ZT": 68, "DT": 5, "SJZT": 61, "SJDT": 4,
    "STZT": 7, "STDT": 1,
    "SZJS": 3200, "XDJS": 1800, "0": 200,
    "sign": "情绪回暖", "qscln": 155541428, "szln": 73247519,
}

# 涨停复盘: {plate_id, plate_name, row}, row 位置 0 code / 1 name
# 6 limit_time / 8 seal_amount(股) / 9 连板文本 / 10 总板数 / 11 themes
# 12 net_inflow / 13 amount / 14 turnover_rate(%) / 15 market_cap
# 16 reason(简明题材) / 17 异动公告全文(不映射) / 18 is_exploded
SAMPLE_RESUMPTION = [
    {
        "plate_id": "BK0012",
        "plate_name": "白酒",
        "row": ["600519", "贵州茅台", None, None, None, None, 1792113900, None, None,
                "3连板", 3, "白酒;消费", -5678900, 1830000000, 3.66, 210000000000,
                "业绩超预期", "业绩超预期, 白酒板块整体走强", 1],
    }
]


# ────────────────── 1. 字段映射与单位转换 ──────────────────


def test_limit_up_units_and_field_mapping(monkeypatch):
    """核心口径: turnover_rate 3.66(%) → 0.0366; seal_amount 1234567(股) → 12345.67(手)."""
    fake = FakeKphClient(limit_history={"up": [SAMPLE_LIMIT_UP]})
    provider = _provider(monkeypatch, fake)

    frame = provider.get_limit_board(trade_date="2026-08-14", board_type="up")
    assert frame.height == 1
    row = frame.to_dicts()[0]

    assert row["symbol"] == "600519.SH"
    assert row["name"] == "贵州茅台"
    assert row["board_type"] == "up"
    assert row["trade_date"].isoformat() == "2026-08-14"

    # 连板数 (个股级) 原样
    assert row["limit_count"] == 3
    # 涨停原因 —— KPH 独有价值字段
    assert row["reason"] == "业绩超预期"
    assert row["themes"] == "白酒;消费"
    assert row["limit_time"] == 93000

    # turnover_rate: 百分数 3.66 → 小数制 0.0366
    assert row["turnover_rate"] == pytest.approx(0.0366)
    # seal_amount: 股 1234567 → 手 12345.67
    assert row["seal_amount"] == pytest.approx(12345.67)
    # seal_money / amount / net_inflow: 元, 原样
    assert row["seal_money"] == pytest.approx(123456789.0)
    assert row["amount"] == pytest.approx(1830000000.0)
    assert row["net_inflow"] == pytest.approx(-5678900.0)
    assert row["market_cap"] == pytest.approx(210000000000.0)

    # 源端没给的字段必须是 None, 不启发式伪造
    assert row["change_pct"] is None
    assert row["last_price"] is None
    assert row["amplitude"] is None
    # plate_id ← idx26 (板块 ID, 2026-10-20 实测: 涨停池 55 行有 16 个不同板块)
    assert row["plate_id"] == "BK0012"
    assert row["plate_name"] is None  # 需板块名表反查, 暂缺
    assert row["market_limit_up_count"] is None


def test_limit_down_has_no_reason_or_limit_count(monkeypatch):
    """跌停池: reason / limit_count 语义上不存在 → None, 不复用涨停口径."""
    fake = FakeKphClient(limit_history={"down": [SAMPLE_LIMIT_DOWN]})
    provider = _provider(monkeypatch, fake)

    row = provider.get_limit_board(trade_date="2026-08-14", board_type="down").to_dicts()[0]
    assert row["symbol"] == "002594.SZ"
    assert row["board_type"] == "down"
    assert row["reason"] is None
    assert row["limit_count"] is None
    # 跌停也有封单和换手
    assert row["seal_amount"] == pytest.approx(5000.0)
    assert row["turnover_rate"] == pytest.approx(0.05)


def test_missing_fields_stay_none_not_fabricated(monkeypatch):
    """上游整行只有代码时, 其余列全部 None —— 禁止补 0."""
    fake = FakeKphClient(limit_history={"up": [["600519"]]})
    provider = _provider(monkeypatch, fake)

    row = provider.get_limit_board(trade_date="2026-08-14", board_type="up").to_dicts()[0]
    assert row["symbol"] == "600519.SH"
    assert row["name"] is None
    assert row["limit_count"] is None
    assert row["reason"] is None
    assert row["amount"] is None
    assert row["turnover_rate"] is None
    assert row["seal_amount"] is None


def test_unparseable_code_yields_null_symbol(monkeypatch):
    """代码位数不对 → symbol=None, 不抛异常, 不猜交易所."""
    fake = FakeKphClient(limit_history={"up": [["12345", "异常代码"]]})
    provider = _provider(monkeypatch, fake)

    row = provider.get_limit_board(trade_date="2026-08-14", board_type="up").to_dicts()[0]
    assert row["symbol"] is None
    assert row["name"] == "异常代码"


# ────────────────── 2. 响应结构变体 ──────────────────


def test_ladder_is_nested_two_dimensional(monkeypatch):
    """天梯是二维数组 (按连板高度分组), 客户端已展平成一维."""
    fake = FakeKphClient(ladder=[SAMPLE_LADDER])
    provider = _provider(monkeypatch, fake)

    row = provider.get_limit_board(trade_date="2026-08-14", board_type="ladder").to_dicts()[0]
    assert row["symbol"] == "600519.SH"
    assert row["board_type"] == "ladder"
    assert row["limit_count"] == 3
    assert row["plate_id"] == "BK0012"
    assert row["plate_name"] == "白酒"
    assert row["amount"] == pytest.approx(5000000000.0)
    # 实测 idx6 = 一字板, idx7 = 热门股
    assert row["one_word"] is True
    assert row["popular"] is True


def test_ladder_bool_fields_can_be_none(monkeypatch):
    """上游给空串时 one_word/popular 必须是 None, 不是 False."""
    fake = FakeKphClient(ladder=[["600519", "贵州茅台", 3, 93000, "BK0012", "白酒", "", "", 12, 1, 2]])
    provider = _provider(monkeypatch, fake)

    row = provider.get_limit_board(trade_date="2026-08-14", board_type="ladder").to_dicts()[0]
    assert row["one_word"] is None
    assert row["popular"] is None


def test_non_trading_day_backtracks_when_date_not_explicit(monkeypatch):
    """未显式指定日期时, 非交易日 (errcode=1020) 自动回退到最近有数据的交易日.

    实测: 2026-10-19 (周一) 查 2026-10-18 (周日) → errcode=1020;
          2026-10-16 (周五) → 正常返回 50 行.
    默认日期已由 previous_weekday 跳过周末, 这里的回退兜底的是**法定节假日**.

    ⚠️ 本测试**不能写死日期** —— 默认日期是相对"今天"算的, 断言必须动态计算,
       否则换一天跑就红 (2026-10-20 跑时默认日期已变成 10-19).
    """

    first_try = previous_weekday(today_dash())   # 默认日期 = 上一个工作日
    second_try = previous_weekday(first_try)     # 回退后的日期

    calls: list[str] = []

    class BacktrackClient(FakeKphClient):
        def limit_history(self, board_type, trade_date, *, include_st=True):
            calls.append(trade_date)
            if trade_date == first_try:  # 模拟法定节假日
                raise KphError("KPH up 上游返回 errcode=1020")
            return [SAMPLE_LIMIT_UP]

    fake = BacktrackClient()
    provider = _provider(monkeypatch, fake)

    frame = provider.get_limit_board(board_type="up")  # 不传日期 → 允许回退

    assert calls[0] == first_try, "第一次应试最近的工作日"
    assert len(calls) == 2, "非交易日必须回退一次"
    assert calls[1] == second_try
    assert frame.height == 1
    # 行的 trade_date 反映**实际数据日期**, 不是请求日期
    assert frame.to_dicts()[0]["trade_date"].isoformat() == second_try


def test_explicit_non_trading_day_does_not_backtrack(monkeypatch):
    """显式指定日期时**不回退** —— 调用方要的就是那天, 拿不到就报错."""
    calls: list[str] = []

    class BacktrackClient(FakeKphClient):
        def limit_history(self, board_type, trade_date, *, include_st=True):
            calls.append(trade_date)
            raise KphError("KPH up 上游返回 errcode=1020")

    fake = BacktrackClient()
    provider = _provider(monkeypatch, fake)

    with pytest.raises(KphError, match="errcode=1020"):
        provider.get_limit_board(trade_date="2026-10-18", board_type="up")
    assert len(calls) == 1, "显式日期不应回退"


def test_single_board_type_runtime_error_propagates(monkeypatch):
    """单类型 board_type 的运行时错误向上抛 (对齐 vendor get_daily 的异常契约)."""
    fake = FakeKphClient(error=KphError("网络不可达"))
    provider = _provider(monkeypatch, fake)

    with pytest.raises(KphError, match="网络不可达"):
        provider.get_limit_board(trade_date="2026-08-14", board_type="up")


def test_emotion_is_dict_payload_not_array(monkeypatch):
    """市场情绪是 dict (payload["info"]), 产出单行, symbol=None."""
    fake = FakeKphClient(emotion=SAMPLE_EMOTION)
    provider = _provider(monkeypatch, fake)

    frame = provider.get_limit_board(trade_date="2026-08-14", board_type="emotion")
    assert frame.height == 1
    row = frame.to_dicts()[0]
    assert row["board_type"] == "emotion"
    assert row["symbol"] is None
    assert row["market_limit_up_count"] == 68
    assert row["market_limit_down_count"] == 5
    assert row["rise_count"] == 3200
    assert row["fall_count"] == 1800
    assert row["reason"] == "情绪回暖"
    # qscln 单位是**万元** -> 换算成元 (x1e4)
    assert row["amount"] == pytest.approx(1_555_414_280_000.0)
    # 个股级列必须是 None
    assert row["limit_count"] is None
    assert row["symbol"] is None


def test_sector_rows_have_no_symbol(monkeypatch):
    """板块排行是板块级数据, symbol=None, 板块 id/name 有值.

    下标口径按**实测真实响应** (2026-10-16): idx2=成分股数, idx5=成交额,
    idx8=净流入(负=净流出), idx3=涨跌幅(%), idx4=振幅, idx9=换手率, idx10=总市值.
    """
    fake = FakeKphClient(sector=[SAMPLE_SECTOR])
    provider = _provider(monkeypatch, fake)

    row = provider.get_limit_board(trade_date="2026-08-14", board_type="sector").to_dicts()[0]
    assert row["symbol"] is None
    assert row["plate_id"] == "801829"
    assert row["plate_name"] == "低空经济"
    # change_pct / amplitude / turnover_rate 都是百分数 → 小数制
    assert row["change_pct"] == pytest.approx(0.02092)
    assert row["amplitude"] == pytest.approx(0.00992)
    assert row["turnover_rate"] == pytest.approx(0.01083)
    # idx2 是**板块热度积分** (排行排序键), 不是成分股数 (实测 50 行严格降序)
    assert row["plate_heat_score"] == 15400
    # idx5 才是成交额(元)
    assert row["amount"] == pytest.approx(139910210891.0)
    # idx8 是净流入(元), 负值 = 净流出
    assert row["net_inflow"] == pytest.approx(-4948955690.0)
    assert row["market_cap"] == pytest.approx(5875912964544.0)
    # 未确认语义的列一律 None, 不猜
    assert row["last_price"] is None
    assert row["limit_count"] is None


def test_resumption_expands_plate_to_stocks(monkeypatch):
    """涨停复盘: 板块 → 个股, reason 取 **idx16 简明题材** (不是 idx17 公告全文).

    ⚠️ 2026-10-20 更正: 之前写的是 `idx17 or idx16` (长文本优先), 顺序反了 ——
    idx17 是 200+ 字异动公告全文, idx16 才是简明题材.同一 canonical 列在 up 板型
    是 1 个题材词, 在 resumption 也应是题材词, 否则下游字符串匹配必踩坑.
    """
    fake = FakeKphClient(resumption=SAMPLE_RESUMPTION)
    provider = _provider(monkeypatch, fake)

    row = provider.get_limit_board(trade_date="2026-08-14", board_type="resumption").to_dicts()[0]
    assert row["board_type"] == "resumption"
    assert row["symbol"] == "600519.SH"
    assert row["plate_id"] == "BK0012"
    assert row["plate_name"] == "白酒"
    assert row["limit_count"] == 3
    assert row["reason"] == "业绩超预期"
    assert row["themes"] == "白酒;消费"
    # 2026-10-20 补齐的 8 个有值列
    assert row["limit_time"] == 1792113900
    assert row["amount"] == pytest.approx(1830000000.0)
    assert row["turnover_rate"] == pytest.approx(0.0366)
    assert row["net_inflow"] == pytest.approx(-5678900.0)
    assert row["market_cap"] == pytest.approx(210000000000.0)
    # idx18 = is_exploded (与 ELTDX kbcs>0 逐只零分歧)
    assert row["is_exploded"] is True


# ────────────────── 3. 分页 ──────────────────


def test_sector_paging_stops_on_short_page(monkeypatch):
    """板块排行分页 (在 KphClient 层): 满页继续, 短页终止, 且有页数上限.

    分页逻辑属于 client 层 (KphClient.sector_ranking), provider 只调一次,
    所以这里直接测 client, 用假 _post 注入分页响应.
    """

    pages = [
        [[f"BK{i:04d}", f"板块{i}"] + [None] * 20 for i in range(50)],
        [[f"BK{i:04d}", f"板块{i}"] + [None] * 20 for i in range(3)],
    ]
    calls: list[dict] = []

    client = KphClient(min_interval=0.0)
    try:
        def fake_post(url, params, *, context):
            calls.append(dict(params))
            idx = min(len(calls) - 1, len(pages) - 1)
            return {"errcode": 0, "list": pages[idx]}

        monkeypatch.setattr(client, "_post", fake_post)
        rows = client.sector_ranking("2026-08-14", "selected", fetch_all=True)
    finally:
        client.close()

    assert len(calls) == 2  # 第一页满 → 继续; 第二页短 → 停
    assert len(rows) == 53
    # Index 参数随页推进
    assert calls[0]["Index"] == "0"
    assert calls[1]["Index"] == "50"
    # 板块类型映射: selected → ZSType=7
    assert calls[0]["ZSType"] == "7"


def test_sector_paging_respects_max_pages(monkeypatch):
    """页数上限: 上游一直返回满页也不能死循环."""

    full_page = [[f"BK{i:04d}", f"板块{i}"] + [None] * 20 for i in range(50)]
    calls: list[dict] = []

    client = KphClient(min_interval=0.0)
    try:
        def fake_post(url, params, *, context):
            calls.append(dict(params))
            return {"errcode": 0, "list": full_page}

        monkeypatch.setattr(client, "_post", fake_post)
        client.sector_ranking("2026-08-14", "selected", fetch_all=True)
    finally:
        client.close()

    assert len(calls) == _SECTOR_MAX_PAGES


def test_client_limit_history_paginates(monkeypatch):
    """涨停池必须翻页: 实测单页 st=50 只返回 50 只, 当日实际 55 只, 不翻页会漏 5 只.

    (在 KphClient 层测 —— 分页逻辑属于 client, provider 只调一次)
    """

    def make_row(i: int) -> list[Any]:
        return [f"60051{i}", f"股票{i}"] + [None] * 32

    pages = [
        [make_row(i) for i in range(_LIMIT_PAGE_SIZE)],   # 满页 → 继续
        [make_row(i) for i in range(5)],                  # 短页 → 停
    ]
    calls: list[dict] = []

    client = KphClient(min_interval=0.0)
    try:
        def fake_post(url, params, *, context):
            calls.append(dict(params))
            return {"errcode": 0, "list": pages[min(len(calls) - 1, len(pages) - 1)]}

        monkeypatch.setattr(client, "_post", fake_post)
        rows = client.limit_history("up", "2026-10-16")
    finally:
        client.close()

    assert len(calls) == 2
    assert len(rows) == _LIMIT_PAGE_SIZE + 5
    assert calls[0]["Index"] == "0"
    assert calls[1]["Index"] == str(_LIMIT_PAGE_SIZE)
    assert calls[0]["PidType"] == "4"  # up
    assert calls[0]["a"] == "HisDaBanList"


def test_client_limit_history_includes_st_by_default(monkeypatch):
    """上游 Is_st 参数语义反直觉: "0"=包含 ST, "1"=排除 ST (实测 2026-10-13).

    短线连板场景 ST 涨停是关键信号, 默认必须包含 → 传 Is_st=0.
    """

    captured: list[dict] = []
    client = KphClient(min_interval=0.0)
    try:
        def fake_post(url, params, *, context):
            captured.append(dict(params))
            return {"errcode": 0, "list": []}

        monkeypatch.setattr(client, "_post", fake_post)
        client.limit_history("up", "2026-10-16")
        assert captured[0]["Is_st"] == "0"

        captured.clear()
        client.limit_history("up", "2026-10-16", include_st=False)
        assert captured[0]["Is_st"] == "1"
    finally:
        client.close()


def test_sector_ranking_is_sorted_by_heat_score(monkeypatch):
    """板块排行按 idx2 (板块热度积分) **严格降序** —— 实测 50 行验证.

    这证明 idx2 是**排行排序键** (热度/人气积分), 不是成分股数
    (成分股数会跨日基本不变, 且不可能 > 全市场股票总数).
    """
    class RankingClient(FakeKphClient):
        def sector_ranking(self, trade_date, sector_type="selected", *, fetch_all=False):
            self.calls.append(("sector_ranking", (trade_date, sector_type, fetch_all)))
            return [
                ["801829", "低空经济", 15400, 2.092, 0.992, 139910210891, None, None,
                 -4948955690, 1.083, 5875912964544, 0.56, 2204700000, None, None,
                 35.6328, 31.8739, 15400, 2.092],
                ["801062", "军工", 9601, 1.759, 0.592, 237692063724, None, None,
                 -12761866588, 1.089, 8108134809440, 0.23, 1483610000, None, None,
                 40.4782, 31.8792, 9601, 1.759],
                ["801843", "商业航天", 5301, 1.72, 0.491, 211766366544, None, None,
                 -3702295109, 1.108, 8775868996864, 0.14, 422551000, None, None,
                 54.5217, 40.6809, 5301, 1.72],
            ]

    fake = RankingClient()
    provider = _provider(monkeypatch, fake)
    frame = provider.get_limit_board(trade_date="2026-08-14", board_type="sector")

    scores = frame["plate_heat_score"].to_list()
    assert scores == [15400, 9601, 5301]
    assert scores == sorted(scores, reverse=True)
    # 未确认语义的列恒为 None
    for row in frame.to_dicts():
        assert row["limit_count"] is None
        assert row["last_price"] is None


def test_provider_passes_include_st_config(monkeypatch):
    """provider 的 include_st 配置要透传到 client."""
    fake = FakeKphClient(limit_history={"up": [SAMPLE_LIMIT_UP]})
    provider = _provider(monkeypatch, fake, include_st=False)
    provider.get_limit_board(trade_date="2026-08-14", board_type="up")
    assert ("limit_history", ("up", "2026-08-14", False)) in fake.calls


def test_client_post_includes_device_fingerprint(monkeypatch):
    """协议层: 请求体必须带 _BASE_PARAMS 设备指纹 + a/c 双参数路由."""

    captured: list[dict] = []

    client = KphClient(min_interval=0.0)
    try:
        class FakeResp:
            status_code = 200
            text = "{}"

            def json(self):
                return {"errcode": 0, "list": []}

        def fake_post(url, data=None, **kw):
            captured.append(dict(data or {}))
            return FakeResp()

        monkeypatch.setattr(client._http, "post", fake_post)
        client.limit_history("up", "2026-08-14")
    finally:
        client.close()

    body = captured[0]
    for key, value in BASE_PARAMS.items():
        assert body[key] == value, f"缺设备指纹字段 {key}"
    assert body["a"] == "HisDaBanList"
    assert body["c"] == "HisHomeDingPan"
    assert body["PidType"] == "4"  # up
    assert body["Day"] == "2026-08-14"


def test_client_post_raises_on_errcode(monkeypatch):
    """协议层: 上游 errcode != 0 → 抛 KphError, 不静默返回空."""

    client = KphClient(min_interval=0.0)
    try:
        class FakeResp:
            status_code = 200
            text = '{"errcode": 4001}'

            def json(self):
                return {"errcode": 4001}

        monkeypatch.setattr(client._http, "post", lambda url, **kw: FakeResp())
        with pytest.raises(KphError, match="errcode=4001"):
            client.limit_history("up", "2026-08-14")
    finally:
        client.close()


# ────────────────── 4. 软失败 ──────────────────


def test_single_board_type_failure_does_not_break_batch(monkeypatch):
    """单个 board_type 失败 → 告警并跳过, 整批其它类型照常返回."""
    fake = FakeKphClient(
        limit_history={"up": [SAMPLE_LIMIT_UP]},
        emotion=SAMPLE_EMOTION,
        fail_types={"ladder"},
    )
    provider = _provider(monkeypatch, fake)

    frame = provider.get_limit_board(trade_date="2026-08-14", board_type="all")
    types = set(frame["board_type"].to_list())
    assert "up" in types
    assert "emotion" in types
    assert "ladder" not in types  # 失败的类型被跳过, 不是空行


def test_on_chunk_done_covers_all_targets(monkeypatch):
    """on_chunk_done 必须覆盖空批次, 最终 cur == total."""
    fake = FakeKphClient(limit_history={"up": []})
    provider = _provider(monkeypatch, fake)

    seen: list[tuple[int, int]] = []
    provider.get_limit_board(
        trade_date="2026-08-14", board_type="all", on_chunk_done=lambda c, t: seen.append((c, t))
    )
    assert seen, "on_chunk_done 必须被调用"
    assert seen[-1][0] == seen[-1][1], "最终 cur 必须等于 total"


# ────────────────── 5. 能力声明 ──────────────────


def test_only_limit_board_declared(monkeypatch):
    """只声明 limit_board; 其余数据集 provider_has_dataset 应为 False."""
    fake = FakeKphClient()
    provider = _provider(monkeypatch, fake)

    assert "limit_board" in provider.config.datasets
    for other in ("daily", "adj_factor", "realtime", "minute", "depth5", "full_minute", "financial"):
        assert other not in provider.config.datasets, f"{other} 不应被声明"


# ────────────────── 6. Key 语义 ──────────────────


def test_availability_is_two_state(monkeypatch):
    """无 Key 插件: 上游可达 → (True, ok); 不可达 → (False, 原因), 不抛异常.

    两条分支都用假 client, 不依赖真实网络 (vendor §测试要求).
    """

    class Ok:
        def __init__(self, **kw): pass
        def market_emotion(self, day): return {"ZT": 1}
        def close(self): pass

    monkeypatch.setattr(kph_client, "KphClient", Ok)
    assert kph_bridge.availability() == (True, "ok")

    class Boom:
        def __init__(self, **kw): pass
        def market_emotion(self, day): raise KphError("网络不可达")
        def close(self): pass

    monkeypatch.setattr(kph_client, "KphClient", Boom)
    ok, reason = kph_bridge.availability()
    assert ok is False
    assert "开盘啦" in reason


# ────────────────── 7. loader 集成 (plugin.yaml 解析) ──────────────────


def test_plugin_manifest_parses():
    """plugin.yaml 能被解析, 且 entry 指向真实存在的类."""
    import yaml

    manifest = yaml.safe_load(
        (Path(kph_provider.__file__).resolve().parent / "plugin.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["name"] == "kph"
    # 2026-10-20 决策: event 单独声明为独立数据集 market_events
    assert manifest["datasets"] == ["limit_board", "market_events"]
    assert manifest["entry"] == "app.plugins.kph.provider:KphProvider"
    assert manifest["check"] == "app.plugins.kph.bridge:availability"

    entry_path, _, attr = manifest["entry"].partition(":")
    module_name = entry_path.rsplit(".", 1)[1]
    assert module_name == "provider"
    # entry 指向的属性必须是真实存在的 Provider 类
    assert attr == "KphProvider"
    assert hasattr(KphProvider, "get_limit_board")


# ────────────────── 8. symbol 归一 ──────────────────


def test_normalize_symbol_prefixes():
    """北交所判定用二位前缀 {43,83,87,92}, 与 abnormal_moves.py:75 同口径."""
    assert normalize_symbol("600519") == "600519.SH"
    assert normalize_symbol("000001") == "000001.SZ"
    assert normalize_symbol("300750") == "300750.SZ"
    assert normalize_symbol("688981") == "688981.SH"
    # 北交所二位前缀
    assert normalize_symbol("430047") == "430047.BJ"
    assert normalize_symbol("830799") == "830799.BJ"
    assert normalize_symbol("871981") == "871981.BJ"
    assert normalize_symbol("920001") == "920001.BJ"
    # 非北交所的 9 开头 (B 股/基金段) 归 SH, 不是 BJ
    assert normalize_symbol("900001") == "900001.SH"
    # 无法识别的位数 → None
    assert normalize_symbol("12345") is None
    assert normalize_symbol("") is None
    assert normalize_symbol(None) is None


# ────────────────── 9. canonical schema 严格对齐 ──────────────────


def test_frame_schema_is_canonical(monkeypatch):
    """返回帧必须严格是 27 列 canonical schema, 类型对齐."""
    fake = FakeKphClient(
        limit_history={"up": [SAMPLE_LIMIT_UP]},
        ladder=[SAMPLE_LADDER],
        emotion=SAMPLE_EMOTION,
        sector=[SAMPLE_SECTOR],
    )
    provider = _provider(monkeypatch, fake)

    frame = provider.get_limit_board(trade_date="2026-08-14", board_type="all")
    assert frame.columns == list(LIMIT_BOARD_SCHEMA.keys())
    assert frame.schema == LIMIT_BOARD_SCHEMA


# ────────────────── 9.1 is_exploded (idx 24 = 是否炸过板) ──────────────────
# 2026-10-20 晚实测: 用 ELTDX raw["kbcs"] + 东财 zbc 双重标定, KPH 涨停池
# idx24 与「当日开板次数 > 0」在 55 只上**逐只零分歧**.


def test_is_exploded_mapped_from_idx24(monkeypatch):
    """涨停池 idx24=1 → is_exploded=True."""
    fake = FakeKphClient(limit_history={"up": [SAMPLE_LIMIT_UP]})
    provider = _provider(monkeypatch, fake)

    frame = provider.get_limit_board(trade_date="2026-08-14", board_type="up")
    assert frame["is_exploded"].to_list() == [True]


def test_is_exploded_zero_maps_to_false(monkeypatch):
    """idx24=0 → is_exploded=False (不是 None, 该股确实没炸过板)."""
    row = list(SAMPLE_LIMIT_UP)
    row[24] = 0
    fake = FakeKphClient(limit_history={"up": [row]})
    provider = _provider(monkeypatch, fake)

    frame = provider.get_limit_board(trade_date="2026-08-14", board_type="up")
    assert frame["is_exploded"].to_list() == [False]


def test_is_exploded_only_for_up_board_type(monkeypatch):
    """跌停池的 idx24 实测恒 0 → 填 None, 不编造布尔值."""
    fake = FakeKphClient(
        limit_history={"up": [SAMPLE_LIMIT_UP], "down": [SAMPLE_LIMIT_DOWN]}
    )
    provider = _provider(monkeypatch, fake)

    frame = provider.get_limit_board(trade_date="2026-08-14", board_type="all")
    rows = dict(zip(frame["board_type"].to_list(), frame["is_exploded"].to_list(), strict=False))
    assert rows["up"] is True
    assert rows["down"] is None


def test_is_exploded_column_in_schema():
    """is_exploded 必须在 canonical schema 里, 且是 Boolean."""
    assert "is_exploded" in LIMIT_BOARD_SCHEMA
    assert LIMIT_BOARD_SCHEMA["is_exploded"] == pl.Boolean


def test_empty_result_keeps_schema(monkeypatch):
    """空结果也要返回带 schema 的空表, 不能是 schema=None."""
    fake = FakeKphClient(limit_history={"up": []})
    provider = _provider(monkeypatch, fake)

    frame = provider.get_limit_board(trade_date="2026-08-14", board_type="up")
    assert frame.height == 0
    assert frame.columns == list(LIMIT_BOARD_SCHEMA.keys())


# ────────────────── 10. 参数校验 ──────────────────


def test_invalid_board_type_raises(monkeypatch):
    """非法 board_type → 抛错, 不静默返回空."""
    fake = FakeKphClient()
    provider = _provider(monkeypatch, fake)

    with pytest.raises(KphError, match="board_type"):
        provider.get_limit_board(trade_date="2026-08-14", board_type="not_a_board")


def test_future_date_rejected_for_history_boards(monkeypatch):
    """涨停池等历史接口不接受今天/未来日期 —— 参数错误必须 fail-fast 抛出."""
    fake = FakeKphClient()
    provider = _provider(monkeypatch, fake)

    with pytest.raises(KphError, match="早于今天"):
        provider.get_limit_board(trade_date="2999-01-01", board_type="up")


def test_test_dataset_reports_error_for_unsupported(monkeypatch):
    """试拉未声明的数据集 → 返回 error 字段, 不抛异常."""
    fake = FakeKphClient()
    provider = _provider(monkeypatch, fake)

    result = provider.test_dataset("daily")
    assert result["error"]
    assert result["rows"] == 0


def test_test_dataset_success_shape(monkeypatch):
    """试拉 limit_board → 返回 rows/columns/preview."""
    fake = FakeKphClient(limit_history={"up": [SAMPLE_LIMIT_UP]})
    provider = _provider(monkeypatch, fake)

    result = provider.test_dataset("limit_board")
    assert "error" not in result
    assert result["rows"] == 1
    assert result["columns"] == list(LIMIT_BOARD_SCHEMA.keys())
    assert isinstance(result["preview"], list)


# ────────────────── 单位辅助函数 ──────────────────


def test_pct_helper_passes_through_none():
    """_pct(None) → None, 不返回 0 (禁止把缺失当 0)."""
    assert _pct(None) is None
    assert _pct("") is None
    assert _pct("--") is None
    assert _pct(3.66) == pytest.approx(0.0366)


def test_parse_helpers_are_lenient():
    """上游脏数据 (千分位 / 百分号 / 空串) 解析失败返回 None, 不抛异常."""
    assert parse_float("1,234.5") == pytest.approx(1234.5)
    assert parse_float("3.66%") == pytest.approx(3.66)
    assert parse_float("-") is None
    assert parse_float(None) is None
    assert parse_int("42") == 42
    assert parse_int("abc") is None


# ────────────────── 12. 补齐的覆盖盲区 (2026-10-20 审计) ──────────────────
# 审计发现这几个面此前**没有专门测试**:
#   - wind_vane (风向标)          —— 与 up/down 共用 _row_limit_history, 但语义独立
#   - get_market_events (event)   —— dict 行, 独立方法, 之前 0 测试
#   - get_sector_constituents     —— 独立方法, 之前 0 测试
#   - "all" 的真实范围            —— 之前只测了"有失败类型时跳过"
#   - is_non_trading_error 结构化判断 / historical_date 非法输入


def test_wind_vane_rows_are_mapped(monkeypatch):
    """风向标: 走 _row_limit_history, 但 board_type 判别列要正确, reason 语义不适用."""
    row = list(SAMPLE_LIMIT_UP)
    row[24] = 0
    fake = FakeKphClient(limit_history={"wind_vane": [row]})
    provider = _provider(monkeypatch, fake)

    frame = provider.get_limit_board(trade_date="2026-08-14", board_type="wind_vane")
    assert frame.height == 1
    assert frame["board_type"].to_list() == ["wind_vane"]
    # is_exploded 只在 up 板型填值, 风向标恒 0 → None
    assert frame["is_exploded"].to_list() == [None]


def test_all_board_type_excludes_resumption_and_event(monkeypatch):
    """`board_type="all"` 只合并 ALL_BOARD_TYPES 的 6 个, **不含** resumption/event.

    这条锁住"all != 全部板型"的口径 —— 名字有误导性, 用测试固定住真实行为.
    """
    fake = FakeKphClient(
        limit_history={"up": [SAMPLE_LIMIT_UP], "down": [SAMPLE_LIMIT_DOWN]},
        resumption=[{"plate_id": "BK1", "plate_name": "板块", "row": SAMPLE_LIMIT_UP}],
    )
    provider = _provider(monkeypatch, fake)

    frame = provider.get_limit_board(trade_date="2026-08-14", board_type="all")
    types = set(frame["board_type"].to_list())
    assert "resumption" not in types
    assert "up" in types
    # provider 侧 VALID_BOARD_TYPES 也不含 event (event 走独立方法)
    assert "event" not in types


def test_event_board_type_is_rejected_by_provider(monkeypatch):
    """provider 不接受 board_type="event" → 干净的校验错误, 不是"未知 board_type".

    event 返回 dict 行, 装不进宽联合 schema, 所以走独立方法 get_market_events().
    """
    fake = FakeKphClient()
    provider = _provider(monkeypatch, fake)

    with pytest.raises(KphValidationError, match="board_type 必须是"):
        provider.get_limit_board(trade_date="2026-08-14", board_type="event")


def test_get_market_events_maps_dict_rows(monkeypatch):
    """get_market_events: dict 行 -> canonical DataFrame, 且 StockList 展开成多行个股.

    ⚠️ 2026-10-20 决策: event **单独声明为独立数据集** (market_events),
    返回 polars DataFrame (与其它数据集方法一致), 不再返回 list[dict].
    """
    fake = FakeKphClient(events=[
        {
            "TimeMin": 1792113900, "TagID": 47, "TagName": "大单一字",
            "ZSCode": "801250", "ZSName": "并购重组", "Detail": "粤宏远Ａ大单一字",
            "StockList": [["000573", "粤宏远Ａ"], ["600607", "华媒控股"]],
        },
        {
            "TimeMin": 1792134000, "TagID": 39, "TagName": "人气股杀跌",
            "ZSCode": "801065", "ZSName": "注册制次新股", "Detail": "C联亚盘中跌超28%",
            "StockList": [["301569", "C联亚"]],
        },
    ])
    provider = _provider(monkeypatch, fake)

    frame = provider.get_market_events("2026-08-14")
    assert frame.height == 3, "一条事件涉及 2 只股票 -> 展开成 2 行"
    assert frame.columns == list(MARKET_EVENTS_SCHEMA.keys())
    assert frame.schema == MARKET_EVENTS_SCHEMA

    rows = frame.to_dicts()
    assert [r["symbol"] for r in rows] == ["000573.SZ", "600607.SH", "301569.SZ"]
    assert rows[0]["name"] == "粤宏远Ａ"
    assert rows[0]["event_time"] == 1792113900
    assert rows[0]["tag_id"] == 47
    assert rows[0]["tag_name"] == "大单一字"
    assert rows[0]["plate_id"] == "801250"
    assert rows[0]["plate_name"] == "并购重组"
    assert rows[0]["detail"] == "粤宏远Ａ大单一字"
    assert str(rows[0]["trade_date"]) == "2026-08-14"


def test_market_events_without_stock_list_keeps_one_row(monkeypatch):
    """没有关联个股的事件 (板块级异动) -> 保留一行, symbol=None, 不丢事件."""
    fake = FakeKphClient(events=[
        {
            "TimeMin": 1792113900, "TagID": 1, "TagName": "板块异动",
            "ZSCode": "801250", "ZSName": "并购重组", "Detail": "板块整体走强",
        }
    ])
    provider = _provider(monkeypatch, fake)

    frame = provider.get_market_events("2026-08-14")
    assert frame.height == 1
    assert frame.to_dicts()[0]["symbol"] is None
    assert frame.to_dicts()[0]["tag_name"] == "板块异动"


def test_market_events_is_declared_as_own_dataset():
    """event 单独声明: plugin.yaml 的 datasets 必须含 market_events."""
    from pathlib import Path

    import yaml

    manifest = yaml.safe_load(
        (Path(kph_provider.__file__).resolve().parent / "plugin.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert "market_events" in manifest["datasets"]
    assert "limit_board" in manifest["datasets"]



def test_emotion_amount_is_scaled_from_wan_yuan_to_yuan(monkeypatch):
    """⚠️ 回归: 市场情绪的成交额单位是**万元**, 必须 x1e4 换算成元.

    2026-10-20 实测 (scripts/verify_kph_emotion_units.py):
        KPH emotion.info["qscln"]      = 155,541,428
        ELTDX 市场概况 N007 (成交额)   = 1,569,066,057,728 元 (1.569 万亿)
        155,541,428 x 1e4 = 1.5554 万亿 → 差 0.87% ✓
    旧实现直接当"元"用 → amount 差 1e4 倍, 且**静默**不报错.
    """
    fake = FakeKphClient(emotion=SAMPLE_EMOTION)
    provider = _provider(monkeypatch, fake)

    frame = provider.get_limit_board(trade_date="2026-08-14", board_type="emotion")
    row = frame.to_dicts()[0]
    assert row["amount"] == pytest.approx(1_555_414_280_000.0)


def test_emotion_limit_down_count_uses_st_inclusive_dt(monkeypatch):
    """DT 含 ST, SJDT 不含 ST —— provider 取的是含 ST 的 DT, 跨源比对要看清.

    2026-10-16 实测: DT=11 / SJDT=10 / STDT=1,
    而 ELTDX 跌停池只有 10 (不含 ST), KPH 自己的跌停池 11 (含 *ST威领).
    """
    fake = FakeKphClient(emotion=SAMPLE_EMOTION)
    provider = _provider(monkeypatch, fake)

    frame = provider.get_limit_board(trade_date="2026-08-14", board_type="emotion")
    row = frame.to_dicts()[0]
    assert row["market_limit_up_count"] == 68
    assert row["market_limit_down_count"] == 5
    assert row["rise_count"] == 3200
    assert row["fall_count"] == 1800


def test_sector_fetch_all_defaults_to_true(monkeypatch):
    """⚠️ 回归: 板块排行默认必须拉全量.

    2026-10-20 实测 (scripts/verify_kph_sector_coverage.py):
    上游 258 个板块, 只拉第一页只有 50 个 → 漏 80.6%,
    且漏的是热度积分连续的中段 (第 51 名 heat=101 → 第 65 名 heat=95).
    IRP 的「主线集中度」按全量算, 漏 80% 会直接算错.
    代价: 全量 5.06s vs 单页 1.09s, 可接受.
    """

    assert KphConfig().sector_fetch_all is True

    fake = FakeKphClient(sector=[SAMPLE_SECTOR])
    provider = _provider(monkeypatch, fake)
    provider.get_limit_board(trade_date="2026-08-14", board_type="sector")
    calls = [c for c in fake.calls if c[0] == "sector_ranking"]
    assert calls, "应调用 sector_ranking"
    assert calls[0][1][2] is True, "默认必须传 fetch_all=True"


def test_sector_fetch_all_can_be_disabled(monkeypatch):
    """显式关掉时退回只取第一页 (省 4s, 但热度榜只覆盖前 50 名)."""
    fake = FakeKphClient(sector=[SAMPLE_SECTOR])
    provider = _provider(monkeypatch, fake, sector_fetch_all=False)
    provider.get_limit_board(trade_date="2026-08-14", board_type="sector")
    calls = [c for c in fake.calls if c[0] == "sector_ranking"]
    assert calls[0][1][2] is False


def test_limit_count_and_total_board_count_are_separate_metrics(monkeypatch):
    """⚠️ 决策(2026-10-20): limit_count 与 total_board_count 是**两个不同指标**.

    - limit_count       = 当前连板天数 (从 idx9 连板描述文本解析)
    - total_board_count = 区间总板数 (idx10)

    跨板形态 ("4天3板") 的当前连板天数**不可知** → limit_count 必须留 None,
    不能拿区间板数冒充 (实测 002917「4天3板」ELTDX lbts=1, 601811「6天5板」lbts=3).
    """

    assert _parse_consecutive_board("首板") == 1
    assert _parse_consecutive_board("3连板") == 3
    assert _parse_consecutive_board("11连板") == 11
    assert _parse_consecutive_board("4天3板") is None
    assert _parse_consecutive_board("6天5板") is None
    assert _parse_consecutive_board("") is None
    assert _parse_consecutive_board(None) is None

    # 跨板形态: 区间总板数有值, 当前连板天数留 None
    row = list(SAMPLE_LIMIT_UP)
    row[9] = "4天3板"
    row[10] = 3
    fake = FakeKphClient(limit_history={"up": [row]})
    provider = _provider(monkeypatch, fake)
    got = provider.get_limit_board(trade_date="2026-08-14", board_type="up").to_dicts()[0]
    assert got["limit_count"] is None
    assert got["total_board_count"] == 3


def test_total_board_count_column_in_schema():
    """total_board_count 必须在 canonical schema 里, 且是 Int32."""
    assert "total_board_count" in LIMIT_BOARD_SCHEMA
    assert LIMIT_BOARD_SCHEMA["total_board_count"] == pl.Int32


def test_ladder_limit_count_is_consecutive_days_not_total(monkeypatch):
    """ladder 的 limit_count = 当前连板天数 (idx2), total_board_count 留 None.

    实测: ladder.idx2 与 ELTDX `board_level` 59/59 完全一致 (当前连板天数).
    """
    fake = FakeKphClient(ladder=[SAMPLE_LADDER])
    provider = _provider(monkeypatch, fake)
    got = provider.get_limit_board(trade_date="2026-08-14", board_type="ladder").to_dicts()[0]
    assert got["limit_count"] == 3
    assert got["total_board_count"] is None


def test_payload_date_mismatch_raises(monkeypatch):
    """⚠️ 决策(2026-10-20): 数据日期与请求日期不一致 → **报错**, 不静默返回.

    上游实时域名完全忽略 `Date` 参数 (实测 5 日期 x 2 次 = 10/10 次都返回
    上一交易日), 以前会静默拿旧数据, 定时任务分辨不出来.
    """

    client = KphClient(min_interval=0.0)
    try:
        class FakeResp:
            status_code = 200
            text = '{"errcode": 0}'

            def json(self):
                return {"errcode": 0, "day": "2026-10-19", "list": []}

        monkeypatch.setattr(client._http, "post", lambda url, **kw: FakeResp())
        with pytest.raises(KphError, match="数据日期与请求日期不一致"):
            client.limit_history("up", "2026-10-20")
    finally:
        client.close()


def test_payload_date_match_passes(monkeypatch):
    """日期一致 → 正常返回, 不报错."""

    client = KphClient(min_interval=0.0)
    try:
        class FakeResp:
            status_code = 200
            text = '{"errcode": 0}'

            def json(self):
                return {"errcode": 0, "day": "2026-10-16", "list": [["600519", "茅台"]]}

        monkeypatch.setattr(client._http, "post", lambda url, **kw: FakeResp())
        rows = client.limit_history("up", "2026-10-16")
        assert len(rows) == 1
    finally:
        client.close()


def test_extract_payload_date_handles_all_shapes():
    """payload 日期字段名/形态各异, 提取器要都能认."""

    assert extract_payload_date({"day": "2026-10-16"}) == "2026-10-16"
    assert extract_payload_date({"Date": "2026-10-16"}) == "2026-10-16"
    assert extract_payload_date({"date": "2026-10-16"}) == "2026-10-16"
    assert extract_payload_date({"Day": ["2026-10-16"]}) == "2026-10-16"
    assert extract_payload_date({"day": "20261016"}) == "2026-10-16"
    # 取不到 → None (调用方宽容处理, 不炸)
    assert extract_payload_date({}) is None
    assert extract_payload_date({"day": None}) is None
    assert extract_payload_date({"day": ""}) is None


def test_payload_without_date_field_does_not_raise(monkeypatch):
    """上游结构变了 (没给日期字段) → 不报错, 宽容降级."""

    client = KphClient(min_interval=0.0)
    try:
        class FakeResp:
            status_code = 200
            text = '{"errcode": 0}'

            def json(self):
                return {"errcode": 0, "list": [["600519", "茅台"]]}

        monkeypatch.setattr(client._http, "post", lambda url, **kw: FakeResp())
        rows = client.limit_history("up", "2026-10-20")
        assert len(rows) == 1
    finally:
        client.close()


def test_get_sector_constituents_maps_and_normalizes_symbol(monkeypatch):
    """板块成分股: 位置数组 → 命名键, symbol 走 normalize_symbol (北交所口径)."""
    # 显式按下标赋值, 避免手数位置数组
    raw: list[Any] = [None] * 45
    raw[0] = "920001"
    raw[1] = "北交测试"
    raw[4] = "低空经济"
    raw[5] = 12.34
    raw[6] = 3.66
    raw[7] = 5_000_000_000
    raw[8] = 1.5
    raw[10] = 1_000_000_000
    raw[13] = -123456
    raw[23] = "首板"
    raw[24] = "热门"
    raw[40] = 2

    fake = FakeKphClient()
    fake.sector_constituents = lambda trade_date, plate_id: [raw]  # type: ignore[method-assign]
    provider = _provider(monkeypatch, fake)

    out = provider.get_sector_constituents("2026-08-13", "BK0815")
    assert len(out) == 1
    row = out[0]
    assert row["symbol"] == "920001.BJ"
    assert row["exchange"] == "BSE"
    assert row["name"] == "北交测试"
    assert row["tags"] == "低空经济"
    assert row["change_pct"] == pytest.approx(0.0366)
    assert row["turnover_rate"] == pytest.approx(0.015)
    assert row["limit_tag"] == "首板"
    assert row["rank_tag"] == "热门"
    assert row["limit_count"] == 2


def test_is_non_trading_error_uses_structured_errcode():
    """非交易日判定优先用结构化 errcode, 而不是匹配异常消息字符串."""

    structured = KphError("ctx 上游返回 errcode=1020", errcode=1020)
    assert is_non_trading_error(structured) is True

    other = KphError("ctx 上游返回 errcode=4001", errcode=4001)
    assert is_non_trading_error(other) is False

    # 没有 errcode 属性的异常 → 回退到消息匹配
    legacy = KphError("ctx 上游返回 errcode=1020")
    assert is_non_trading_error(legacy) is True


def test_kph_error_carries_errcode_attribute():
    """KphError 必须带上结构化 errcode (给上游错误码判断用)."""

    exc = KphError("boom", errcode=1020)
    assert exc.errcode == 1020
    assert str(exc) == "boom"


def test_historical_date_rejects_garbage():
    """非法日期 → KphValidationError (不是 TypeError).

    ⚠️ 这里锁的是"不能用 assert 判空": Python -O 模式下 assert 被剥离,
    旧写法会退化成 `None >= today_dash()` → TypeError, 破坏错误契约.
    """

    with pytest.raises(KphValidationError):
        historical_date("不是日期")
    with pytest.raises(KphValidationError):
        historical_date("2026/13/45")
