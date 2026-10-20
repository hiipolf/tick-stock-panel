"""开盘红 (KPH) Provider 实现.

vendor 契约 (docs/plugin-development.md §"Provider 接口契约"):
- 只实现已声明数据集对应的方法, 其余可缺省 (未声明 → provider_has_dataset False → 回退 tickflow)
- 软失败: get_realtime 返回 [] 不抛异常; 本插件无 realtime
- 缺字段返回 None, 禁止启发式补全 (CONTRIBUTING §3.1)
- symbol 统一带交易所后缀, 在 client 层归一

数据集声明 (plugin.yaml):
- ✅ limit_board  — 涨停池 / 跌停池 / 风向标 / 连板天梯 / 市场情绪 / 板块排行 / 涨停复盘
- ❌ daily / adj_factor / realtime / minute / depth5 / full_minute / financial
  (KPH 无 OHLCV / 盘口 / 财务, 强行声明会迫使 provider 伪造必填字段)

limit_board 数据集契约 (框架侧新增, 见 framework-patch/README.md):
- canonical 27 列宽联合 schema, `board_type` 列做判别
- 每行只填与 board_type 相关的列, 其余一律 None
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import polars as pl

# 相对导入: 同一份代码在 vendor (app.plugins.kph.provider) 和暂存区 (kph.provider) 都能用
from .client import (
    KphClient,
    KphError,
    KphValidationError,
    at,
    clean_text,
    exchange_of,
    historical_date,
    is_non_trading_error,
    normalize_date,
    normalize_symbol,
    parse_bool_value,
    parse_float,
    parse_int,
    previous_weekday,
    today_dash,
)

logger = logging.getLogger(__name__)

# 只声明真实提供的数据集; 其余 provider_has_dataset 返回 False → 回退 tickflow
# ⚠️ 2026-10-20: market_events (盘面梳理事件流) 单独声明为独立数据集。
#    框架的 provider_has_dataset 读的就是这个字段 —— 漏了它, 路由层就认不出。
_DATASETS = ("limit_board", "market_events")

# ────────────────── 单位口径策略 ──────────────────
#
# ⚠️ KPH 没有公开 API 文档, 下面的单位是**基于 KPH 网页端显示口径**的显式假设,
#    不是猜测性补全 —— 每个字段都独立声明, 不用启发式推断。
#
# 落地要求: 接入真实环境后, 用一次真实响应核对 (见 README §"上线前必须核对"),
# 核对不通过就改这里的常量, 不要在 provider 里加 if/else 兼容两种单位。

# 涨跌幅 / 振幅 / 换手率: KPH 网页显示百分数 (3.66 表示 3.66%) → 项目契约要求小数制
_PCT_SCALE = 0.01

# 成交额: KPH 行情接口返回单位为元
_AMOUNT_SCALE = 1.0

# ⚠️ **市场情绪截面的成交额是万元, 不是元** —— 与行情接口的成交额单位不同,
# 不能共用 _AMOUNT_SCALE。
# 判定方法 (2026-10-20 实测, 见 scripts/verify_kph_emotion_units.py):
#     KPH emotion.info["qscln"] = 155,541,428
#     ELTDX 市场概况 N007 (成交额) = 1,569,066,057,728 元 = 1.569 万亿
#     155,541,428 x 1e4 = 1.5554 万亿  → 差 0.87% (口径/时点微差) ✓
#     若按"元"理解, 则差 1e4 倍 ✗
# 同字段 szln = 73,247,519 -> 总量 73.2 亿股; x 均价 ≈ 1.55 万亿, 与成交额自洽 ✓
_EMOTION_AMOUNT_SCALE = 10_000.0

# 净流入: 元
_NET_INFLOW_SCALE = 1.0

# 封单量: KPH 返回的是股数, 项目内封单量口径为手 (1 手 = 100 股)
_SEAL_AMOUNT_SCALE = 0.01

# 封单额: 元
_SEAL_MONEY_SCALE = 1.0


def _pct(value: Any) -> float | None:
    """百分数 → 小数制。None 透传 None (不伪造 0)。"""
    parsed = parse_float(value)
    return None if parsed is None else parsed * _PCT_SCALE


def _scaled(value: Any, scale: float) -> float | None:
    parsed = parse_float(value)
    return None if parsed is None else parsed * scale


def _parse_consecutive_board(label: Any) -> int | None:
    """从 KPH 的**连板描述文本** (idx9) 解析「当前连板天数」。

    文本形态 (2026-10-16 实测, up 池 5 种 / resumption 池 7 种):
        "首板"   -> 1
        "3连板"  -> 3
        "11连板" -> 11
        "4天3板" -> **None**  ← 跨板形态
        "6天5板" -> **None**

    ⚠️ **跨板形态返回 None 而不是猜一个值**: "N天M板" 只说明区间内有 M 个板,
    当前连板天数在 1..M 之间**不可知** (实测 002917「4天3板」ELTDX lbts=1,
    601811「6天5板」lbts=3 —— 同样形态对应不同连板数)。
    宁可留空, 也不能编一个数。

    实测命中率 (2026-10-16, resumption 59 只): 56/59 与 ELTDX `board_level`
    (当前连板天数) 精确一致, 剩下 3 只全是跨板形态 → 正确地留 None。
    """
    text = str(label or "").strip()
    if not text or text in {"无", "-", "--"}:
        return None
    if "天" in text:
        return None  # 跨板形态: 区间板数 != 当前连板天数, 不可推
    if "连板" in text:
        return parse_int(text.replace("连板", ""))
    if text == "首板":
        return 1
    return None


@dataclass
class KphConfig:
    """Provider 配置. config.datasets 是路由判定关键 (provider_has_dataset 看它)。"""

    datasets: dict[str, Any] = field(default_factory=lambda: dict.fromkeys(_DATASETS))
    base_url: str = "https://apphwshhq.kaipanhong.com/w1/api/index.php"
    history_url: str = "https://apphis.kaipanhong.com/w1/api/index.php"
    min_interval: float = 1.0
    timeout: float = 20.0
    # 板块排行是否拉全部分页 (st=50/页, 翻到短页为止)。
    # ⚠️ 默认 True —— 2026-10-20 实测: 上游共 **258** 个板块, 只拉第一页只有
    # **50** 个, 漏 208 个 (80.6%), 且漏掉的是热度积分**连续的中段**
    # (第 51 名 heat=101 → 第 65 名 heat=95: 培育钻石/风电/垃圾发电/智慧停车…),
    # 不是无意义尾部。IRP 的「主线集中度」要按全量算, 漏 80% 会直接算错。
    # 代价实测: 全量 5.06s vs 单页 1.09s (多 4s/次), 可接受。
    # 置 False 可退回"只取第一页"(省 4s, 但热度榜只覆盖前 50 名)。
    sector_fetch_all: bool = True
    # 涨停/跌停池是否包含 ST 股。
    # ⚠️ 上游 Is_st 参数语义反直觉: Is_st=1 排除 ST, Is_st=0 包含 ST (实测 2026-10-13)。
    # 短线连板场景 ST 涨停是关键信号 → 默认包含。
    include_st: bool = True


# canonical schema: 28 列宽联合, board_type 判别
LIMIT_BOARD_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.Utf8,
    "name": pl.Utf8,
    "trade_date": pl.Date,
    "board_type": pl.Utf8,
    "limit_count": pl.Int32,
    "total_board_count": pl.Int32,
    "limit_time": pl.Int64,
    "plate_id": pl.Utf8,
    "plate_name": pl.Utf8,
    "seal_amount": pl.Float64,
    "seal_money": pl.Float64,
    "themes": pl.Utf8,
    "reason": pl.Utf8,
    "amount": pl.Float64,
    "turnover_rate": pl.Float64,
    "change_pct": pl.Float64,
    "last_price": pl.Float64,
    "amplitude": pl.Float64,
    "net_inflow": pl.Float64,
    "plate_heat_score": pl.Int32,
    "market_cap": pl.Float64,
    "one_word": pl.Boolean,
    "popular": pl.Boolean,
    "market_limit_up_count": pl.Int32,
    "market_limit_down_count": pl.Int32,
    "rise_count": pl.Int32,
    "fall_count": pl.Int32,
    "is_exploded": pl.Boolean,
}

# "all" 合并的 board_type 集合 (resumption 是复盘明细, 不进合并)
ALL_BOARD_TYPES: tuple[str, ...] = ("up", "down", "wind_vane", "ladder", "emotion", "sector")

# ────────────────── 独立数据集: market_events (盘面梳理事件流) ──────────────────
# 2026-10-20 用户拍板: event **单独声明为独立数据集**, 不并入 limit_board。
# 理由: event 是 dict 行 (不是位置数组), 一条事件可涉及多只股票 (StockList),
# 语义上是"事件"而非"板", 装不进 limit_board 的宽联合表。
MARKET_EVENTS_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.Utf8,          # 个股代码 (从 StockList 展开; 无关联个股的事件为 None)
    "name": pl.Utf8,            # 个股名称
    "trade_date": pl.Date,      # 交易日
    "event_time": pl.Int64,     # 事件时间 (unix 秒) —— 上游字段名 TimeMin, 但值是秒不是分
    "tag_id": pl.Int32,         # 标签 ID
    "tag_name": pl.Utf8,        # 标签名 (大单一字/人气股杀跌/尾盘炸板回封/北交所/...)
    "plate_id": pl.Utf8,        # 板块 ID
    "plate_name": pl.Utf8,      # 板块名
    "detail": pl.Utf8,          # 事件描述全文
}

VALID_BOARD_TYPES: frozenset[str] = frozenset(
    {"up", "down", "wind_vane", "ladder", "emotion", "sector", "resumption", "all"}
)


class KphProvider:
    """开盘红 Provider. 零 API Key (公开接口, 但需节流)."""

    name = "kph"
    builtin = True  # 内置插件 (随仓库分发, 不可被用户编辑/删除)

    def __init__(self, config: KphConfig | None = None) -> None:
        self.config = config or KphConfig()
        self.client = KphClient(
            base_url=self.config.base_url,
            history_url=self.config.history_url,
            min_interval=self.config.min_interval,
            timeout=self.config.timeout,
        )

    def close(self) -> None:
        """vendor §"Provider 接口契约": 清理资源 (load_all 重建注册表时会调)。"""
        self.client.close()

    # ────────────────── limit_board ──────────────────

    def get_limit_board(
        self,
        trade_date: str | None = None,
        board_type: str = "up",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """涨停板数据。

        Args:
            trade_date: "YYYY-MM-DD" / "YYYYMMDD"; None = 今天
            board_type: up / down / wind_vane / ladder / emotion / sector / resumption / all
            on_chunk_done: (cur, total) 进度回调, 必须覆盖空批次 (cur == total)

        Returns:
            27 列 DataFrame, 见 LIMIT_BOARD_SCHEMA。

        ⚠️ **"all" 不等于"全部板型"**: 它只合并 `ALL_BOARD_TYPES` 的 6 个
        (up / down / wind_vane / ladder / emotion / sector)。
        `resumption` (涨停复盘) 与 `event` (盘面梳理事件流) 是**明细型**数据,
        不进宽联合表 —— resumption 要单独取 `board_type="resumption"`,
        event 走独立方法 `get_market_events()` (dict 行, 装不进本 schema)。
        """
        if board_type not in VALID_BOARD_TYPES:
            raise KphValidationError(
                f"board_type 必须是 {sorted(VALID_BOARD_TYPES)} 之一, 收到 {board_type!r}"
            )

        day = self._resolve_day(trade_date, board_type)

        targets = ALL_BOARD_TYPES if board_type == "all" else (board_type,)
        batch_mode = board_type == "all"
        # 显式指定日期时不回退 (调用方要的就是那天); 缺省日期才自动回退到最近有数据的交易日
        allow_backtrack = trade_date is None
        rows: list[dict[str, Any]] = []
        total = len(targets)
        for idx, target in enumerate(targets, start=1):
            try:
                rows.extend(self._fetch_board(target, day, allow_backtrack=allow_backtrack))
            except KphValidationError:
                # 参数错误是调用方的错, 应当 fail-fast 抛出去, 不静默跳过。
                # 但 "all" 是批量语义: 单个子类型不合规只跳过该类型, 不炸整批。
                if not batch_mode:
                    raise
                logger.warning("kph.board_type=%s 参数不合法, 已跳过", target)
            except KphError as exc:
                # 单个 board_type 运行时失败不拖垮整批: 记录告警, 继续下一个。
                # 契约对齐 vendor §"异常语义": get_daily / get_financials 类的
                # 异常由上层同步流程捕获记录 (这里向上抛), 只有批量 "all" 才软失败。
                if not batch_mode:
                    raise
                logger.warning("kph.board_type=%s 拉取失败: %s", target, exc)
            if on_chunk_done is not None:
                on_chunk_done(idx, total)

        return self._to_frame(rows)

    @staticmethod
    def _resolve_day(trade_date: str | None, board_type: str) -> str:
        """解析默认交易日。

        - 历史类 (up/down/wind_vane/resumption): 上游只接受**早于今天**的日期。
          显式传了今天/未来 → 直接报错 (调用方的错)。
          没传 → 用**上一个工作日** (跳过周末), 再配合 _fetch_with_backtrack
          兜底法定节假日 (不依赖日历, 靠上游 errcode=1020 触发回退)。
        - 实时类 (ladder/emotion/sector): 缺省用今天; 传过去日期会自动走历史域名。
        - "all": 合并两类, 缺省用上一个工作日。
        """
        if trade_date is not None:
            day = normalize_date(trade_date) or today_dash()
            if board_type in {"up", "down", "wind_vane", "resumption"} and day >= today_dash():
                raise KphValidationError("该 KPH 历史接口只接受早于今天的交易日")
            return day
        if board_type in {"ladder", "emotion", "sector"}:
            return today_dash()
        return previous_weekday(today_dash())

    def _fetch_board(self, board_type: str, day: str, *, allow_backtrack: bool = False) -> list[dict[str, Any]]:
        if board_type in {"up", "down", "wind_vane"}:
            rows, actual = self._fetch_with_backtrack(
                lambda d: self.client.limit_history(
                    board_type, d, include_st=self.config.include_st
                ),
                day,
                allow_backtrack=allow_backtrack,
            )
            return [self._row_limit_history(raw, actual, board_type) for raw in rows]
        if board_type == "ladder":
            raw_rows = self.client.limit_ladder(day)
            return [self._row_ladder(raw, day) for raw in raw_rows]
        if board_type == "emotion":
            info = self.client.market_emotion(day)
            return [self._row_emotion(info, day)]
        if board_type == "sector":
            raw_rows = self.client.sector_ranking(
                day, "selected", fetch_all=self.config.sector_fetch_all
            )
            return [self._row_sector(raw, day) for raw in raw_rows]
        if board_type == "resumption":
            entries, actual = self._fetch_with_backtrack(
                lambda d: self.client.limit_resumption(d),
                day,
                allow_backtrack=allow_backtrack,
            )
            return [self._row_resumption(entry, actual) for entry in entries]
        raise KphValidationError(f"未知 board_type: {board_type!r}")

    def _fetch_with_backtrack(
        self,
        fetch: Any,
        day: str,
        *,
        allow_backtrack: bool,
        max_attempts: int = 10,
    ) -> tuple[list[Any], str]:
        """历史接口在非交易日返回 errcode=1020, 自动向前回退到最近有数据的交易日。

        实测 (2026-10-19 周一):
            查 2026-10-18 (周日) → errcode=1020
            查 2026-10-16 (周五) → 正常返回 50 行

        仅在**未显式指定日期**时回退 —— 调用方明确要某天时, 拿不到就该报错,
        不能静默换成别的日期。返回 (rows, 实际数据日期)。
        """
        current = day
        last_exc: KphError | None = None
        for _ in range(max_attempts):
            try:
                rows = fetch(current)
            except KphError as exc:
                if not is_non_trading_error(exc):
                    raise
                last_exc = exc
                rows = []
            if rows:
                return rows, current
            if not allow_backtrack:
                break
            logger.info("kph %s 无数据, 回退到前一个交易日", current)
            current = previous_weekday(current)
        if last_exc is not None:
            raise last_exc
        return [], day

    # ────────────────── 行构造 (协议层位置数组 → canonical) ──────────────────

    def _row_limit_history(self, raw: Any, day: str, board_type: str) -> dict[str, Any]:
        """涨停池 / 跌停池 / 风向标。

        ⚠️ 字段下标是**实测确认**的 (2026-10-16 真实响应, 行长 34):
            idx 0  → 裸代码     idx 1  → 名称       idx 6  → 涨停时间 (unix 秒)
            idx 8  → 封单量(**股**) idx 9  → 涨停标签  idx 10 → 连板数
            idx 11 → 题材       idx 12 → 净流入(元) idx 13 → 成交额(元)
            idx 14 → 换手率(%)  idx 15 → 流通市值     idx 16 → 涨停原因
            idx 23 → 封单额(元) idx 26 → 行业代码   idx 27 → 行业内涨停家数
            **idx 24 → 是否炸过板 (布尔 0/1)**
        跌停池的 idx 9/10/16 为空 → reason / limit_count 置 None。

        **idx 24 = 「是否炸过板」** (2026-10-20 晚用 ELTDX `kbcs` 当标尺实测确认):
        以 ELTDX F10 `raw["kbcs"]` (当日开板次数) 和东财 `getTopicZTPool` 的 `zbc` 做
        双重标定, 在涨停池 55 只上**逐只零分歧**:
            idx24=0 → kbcs=0 (25 只)   |   idx24=1 → kbcs>0 (30 只)
        → `is_exploded` 列直接可填, **不需要再拿 `last_close == high` 去推**。
        ⚠️ **KPH 只给布尔, 不给次数** (idx24 取值集合 = {0, 1}); 要开板**次数**只有
        ELTDX `kbcs` / 东财 `zbc` 有。
        ⚠️ 跌停池 (11 只) 与风向标 (22 只) 的 idx24 **实测全为 0** → 这两个池填 None,
        不编造 (跌停股本来就在跌, "炸板"对它无意义)。
        ⚠️ 别再去找 idx 7 —— 它恒为 0 (实测 434 行全 0), 是垃圾列;
        之前误把 idx7 当"开板时间戳"并据此断言"KPH 给不了开板痕迹", 是**找错了列**。

        (字段名与单位口径对齐 AxData `sources/kph/catalog.py` 的 `LIMIT_HISTORY_FIELDS`,
         但**下标**是本插件实测的 —— AxData 的天梯/涨停池下标与它一致, 板块排行的则错了 3 个,
         见 README §"与 AxData catalog 的差异"。)

        **单位交叉验证** (KPH vs 财联社 同股同日, 2026-10-16):
            - idx 13 与 CLS 成交额**字节级一致** (佳力奇 234,615,415 / 英搏尔 593,115,163)
            - idx 8 与 CLS **成交量(股)** 同量级, 且 5/6 只 ≤ 成交量 —— 若 idx 8 是"手"
              就会比成交量大 100 倍 (物理不可能) → **idx 8 的单位是股**,
              本插件 /100 → 手 正确 (AxData 的 catalog 只写"封单量", **没给单位**)
        ⚠️ idx 23 / idx 8 **不是同一时点/同口径** (只有 1/45 只的比值 ≈ 涨停价),
        不能用除法互推封单价。
        """
        is_up_like = board_type in {"up", "wind_vane"}
        # ⚠️ **风向标 (PidType=6) 的列语义与涨停池/跌停池不同** (2026-10-20 实测):
        #   idx4  = 涨跌幅(%)   ← 涨停/跌停池里这一位恒为 0, 只有风向标有值
        #   idx6/idx8/idx10/idx23 = 恒为 0  ← 涨停池里分别是涨停时间/封单量/连板数/封单额
        #   idx9  = 恒为 ""     ← 涨停池里是连板描述文本
        #   idx16 = 恒为 "无"   ← 涨停池里是涨停原因
        #   idx26 = 恒为 ""     ← 涨停池里是板块 ID
        # 所以风向标**不能**套用涨停池的映射, 否则会输出 6 个"有值但是垃圾"的零值,
        # 下游拿 limit_time=0 当涨停时刻会得到 1970-01-01。
        is_wind_vane = board_type == "wind_vane"
        if is_wind_vane:
            return {
                "symbol": normalize_symbol(at(raw, 0)),
                "name": clean_text(at(raw, 1)),
                "trade_date": day,
                "board_type": "wind_vane",
                # 风向标没有连板/涨停时刻/封单这些语义 → 一律 None, 不输出垃圾零值
                "limit_count": None,
                "total_board_count": None,
                "limit_time": None,
                "plate_id": None,
                "plate_name": None,
                "seal_amount": None,
                "seal_money": None,
                "themes": clean_text(at(raw, 11)),
                "reason": None,  # idx16 恒为 "无", 不是真实原因
                "amount": None,
                "turnover_rate": None,
                "change_pct": _pct(at(raw, 4)),  # ← 风向标独有: 涨跌幅
                "last_price": None,
                "amplitude": None,
                "net_inflow": None,
                "plate_heat_score": None,
                "market_cap": None,
                "one_word": None,
                "popular": None,
                "market_limit_up_count": None,
                "market_limit_down_count": None,
                "rise_count": None,
                "fall_count": None,
                "is_exploded": None,
            }
        return {
            "symbol": normalize_symbol(at(raw, 0)),
            "name": clean_text(at(raw, 1)),
            "trade_date": day,
            "board_type": board_type,
            # ⚠️ **两个指标, 语义不同** (2026-10-20 用户拍板拆分):
            #   limit_count       = 当前连板天数 (从 idx9 连板描述文本解析)
            #   total_board_count = 区间总板数 (idx10, 如 "4天3板" → 3)
            # 跨板形态 ("N天M板") 的当前连板天数不可知 → limit_count 留 None。
            "limit_count": _parse_consecutive_board(at(raw, 9)) if is_up_like else None,
            "total_board_count": parse_int(at(raw, 10)) if is_up_like else None,
            "limit_time": parse_int(at(raw, 6)),
            # 涨停/跌停池的 idx26 = 板块 ID (2026-10-20 实测: 16 个不同板块,
            # 801829 低空经济 19 只 / 801062 军工 11 只; idx27 = 板块内涨停家数)
            "plate_id": clean_text(at(raw, 26)),
            "plate_name": None,  # 需要板块名表反查 (可用 ladder 的 ZhuShuList), 暂缺
            "seal_amount": _scaled(at(raw, 8), _SEAL_AMOUNT_SCALE),
            "seal_money": _scaled(at(raw, 23), _SEAL_MONEY_SCALE),
            "themes": clean_text(at(raw, 11)),
            "reason": clean_text(at(raw, 16)) if is_up_like else None,
            "amount": _scaled(at(raw, 13), _AMOUNT_SCALE),
            "turnover_rate": _pct(at(raw, 14)),
            "change_pct": None,
            "last_price": None,
            "amplitude": None,
            "net_inflow": _scaled(at(raw, 12), _NET_INFLOW_SCALE),
            "plate_heat_score": None,
            "market_cap": parse_float(at(raw, 15)),
            "one_word": None,
            "popular": None,
            "market_limit_up_count": None,
            "market_limit_down_count": None,
            "rise_count": None,
            "fall_count": None,
            "is_exploded": parse_bool_value(at(raw, 24)) if board_type == "up" else None,
        }

    def _row_ladder(self, raw: Any, day: str) -> dict[str, Any]:
        """连板天梯。

        字段名与单位口径对齐 AxData `sources/kph/catalog.py` 的 `kph_limit_ladder`:
        idx 2=连板数 / 3=涨停时间戳 / 4=板块 ID / 5=板块名 / **6=是否大单一字** /
        **7=是否有人气** / 8=板块涨停股数量 / 9=个股成交额 / 10=板块成交额。
        **AxData 的天梯下标与本插件完全一致 (11/11)。**
        """
        return {
            "symbol": normalize_symbol(at(raw, 0)),
            "name": clean_text(at(raw, 1)),
            "trade_date": day,
            "board_type": "ladder",
            "limit_count": parse_int(at(raw, 2)),
            "total_board_count": None,
            "limit_time": parse_int(at(raw, 3)),
            "plate_id": clean_text(at(raw, 4)),
            "plate_name": clean_text(at(raw, 5)),
            "seal_amount": None,
            "seal_money": None,
            "themes": None,
            "reason": None,
            "amount": _scaled(at(raw, 9), _AMOUNT_SCALE),
            "turnover_rate": None,
            "change_pct": None,
            "last_price": None,
            "amplitude": None,
            "net_inflow": None,
            "plate_heat_score": None,
            "market_cap": None,
            "one_word": parse_bool_value(at(raw, 6)),
            "popular": parse_bool_value(at(raw, 7)),            "market_limit_up_count": None,
            "market_limit_down_count": None,
            "rise_count": None,
            "fall_count": None,
            "is_exploded": None,
        }

    def _row_emotion(self, info: Any, day: str) -> dict[str, Any]:
        """市场情绪截面 (单行, symbol=None)。

        ⚠️ **单位: 成交额是万元, 不是元** (见 _EMOTION_AMOUNT_SCALE 的判定)。
        涨停池/天梯/板块排行的成交额是**元**, 两个接口单位不同, 别混用同一个 scale。

        ⚠️ **口径: DT 含 ST, SJDT 不含 ST** (2026-10-16 实测):
            ZT=61 / SJZT=61 / STZT=0   → 涨停家数 61 (与 ELTDX limit_up 61 一致 ✓)
            DT=11 / SJDT=10 / STDT=1   → 跌停 11 = 10 (非ST) + 1 (ST)
        → `market_limit_down_count` 取的是**含 ST** 的 DT (11), 与 KPH 自己的
          跌停池 (11 只, 含 *ST威领) 一致; ELTDX 的跌停池只有 10 (不含 ST)。
          要跨源比对 ST 口径时, 用 ELTDX 的 10 或另算。

        info 里还有一批**未映射**的字段 (宁缺勿滥, 语义未确认不瞎填):
            -22 个涨跌分档 ("-10".."-1", "0", "1".."10")  → 分档家数, 语义未确认
            - "ZSZDFB" 涨跌幅分布 (逗号分隔)              → 语义未确认
            - "q_zrcs"/"q_zrtj"/"s_zrcs"/"s_zrtj"        → 万元级成交额拆分
        上面这些是 IRP「涨跌家数分布」的真实来源, 需要时另开字段暴露。
        """
        get = info.get if isinstance(info, dict) else (lambda _k: None)
        return {
            "symbol": None,
            "name": None,
            "trade_date": day,
            "board_type": "emotion",
            "limit_count": None,
            "total_board_count": None,
            "limit_time": None,
            "plate_id": None,
            "plate_name": None,
            "seal_amount": None,
            "seal_money": None,
            "themes": None,
            "reason": clean_text(get("sign")),
            "amount": _scaled(get("qscln"), _EMOTION_AMOUNT_SCALE),
            "turnover_rate": None,
            "change_pct": None,
            "last_price": None,
            "amplitude": None,
            "net_inflow": None,
            "plate_heat_score": None,
            "market_cap": None,
            "one_word": None,
            "popular": None,
            "market_limit_up_count": parse_int(get("ZT")),
            "market_limit_down_count": parse_int(get("DT")),
            "rise_count": parse_int(get("SZJS")),
            "fall_count": parse_int(get("XDJS")),
        }

    def _row_sector(self, raw: Any, day: str) -> dict[str, Any]:
        """板块排行 (板块级, symbol=None)。

        ⚠️ 字段下标是**实测确认**的 (2026-10-16 真实响应, 258 个板块逐项验证):

        | idx | 语义 | 验证方法 |
        |---|---|---|
        | 0 / 1 | 板块 id / 名 | — |
        | 2 | **板块热度积分 (排行排序键)** | 全 **258** 行**严格降序** (15400 → -7246) |
        | 3 | 涨跌幅 (%) | == idx18, 258/258 成立 |
        | 4 | 振幅 (%) | 量级 < 2 且 > 0 |
        | 5 | 板块成交额 (元) | 与成分股成交额**逐分逐厘吻合** (139,910,210,811 vs 891) |
        | 8 | 净流入 (元), 负 = 净流出 | 符号与板块涨跌一致 |
        | 9 | 换手率 (%) | 与成分股换手率合计同量级 |
        | 10 | 板块总市值 (元) | 与成分股总市值合计同量级 |
        | 17 | == idx 2 (重复字段) | 258/258 成立 |
        | 18 | == idx 3 (重复字段) | 258/258 成立 |

        ⚠️ **`plate_heat_score` 可以是负数**: 258 个板块里 **100 个是负数**
        (min=-7246, max=15400) —— 负值集中在热度榜尾部。
        它作为**排序键**没问题 (严格降序), 但当"热度大小"用时
        (比如求和/取绝对值/算占比) 要注意负数会抵消, 建议 `max(0, x)`。

        ⚠️ **idx 2 不是成分股数** —— 它逐日剧烈变化 (体育产业 10-13/10-14/10-16
        分别为 5387 / 1090 / 425), 而成分股接口实测只返回 453 只 (低空经济),
        且 15400 > 全市场股票总数 (~5400)。它是**板块热度/人气积分**, 即排行排序键。

        ⚠️ idx 6 / 7 / 11 / 12 / 15 / 16 语义**未能确认** → **不映射, 置 None**
        (已确认的特征: idx11 是可正可负的日度比率; idx12 是可正可负的日度金额;
         idx15 / idx16 是**跨日恒定**的板块级静态常量, 且与排行名次不单调;
         某些新概念板块的 idx15/idx16 为 0)。
        不靠字段名猜; 未确认的宁缺勿滥, 见 CONTRIBUTING §3.1。
        """
        return {
            "symbol": None,
            "name": None,
            "trade_date": day,
            "board_type": "sector",
            "limit_count": None,
            "total_board_count": None,
            "limit_time": None,
            "plate_id": clean_text(at(raw, 0)),
            "plate_name": clean_text(at(raw, 1)),
            "seal_amount": None,
            "seal_money": None,
            "themes": None,
            "reason": None,
            "amount": _scaled(at(raw, 5), _AMOUNT_SCALE),
            "turnover_rate": _pct(at(raw, 9)),
            "change_pct": _pct(at(raw, 3)),
            "last_price": None,
            "amplitude": _pct(at(raw, 4)),
            "net_inflow": _scaled(at(raw, 8), _NET_INFLOW_SCALE),
            "plate_heat_score": parse_int(at(raw, 2)),
            "market_cap": parse_float(at(raw, 10)),
            "one_word": None,
            "popular": None,
            "market_limit_up_count": None,
            "market_limit_down_count": None,
            "rise_count": None,
            "fall_count": None,
            "is_exploded": None,
        }

    def _row_resumption(self, entry: Any, day: str) -> dict[str, Any]:
        """涨停复盘 (板块 → 个股)。entry = {plate_id, plate_name, row}。

        2026-10-20 实测补齐 (脚本 scripts/verify_kph_resumption_idx18.py):
        行长 20, 逐列验证后补上 8 个**有值但之前没映射**的列:
            idx6  涨停时间戳 (unix 秒)  → limit_time
            idx8  封单量 (股)           → seal_amount (同 up 板型, /100 → 手)
            idx12 净流入 (元)           → net_inflow
            idx13 成交额 (元)           → amount
            idx14 换手率 (%)            → turnover_rate
            idx15 流通市值 (元)         → market_cap
            idx16 简明题材              → reason   ← 之前错取了 idx17!
            idx18 0/1 标志              → is_exploded (与 ELTDX kbcs>0 逐只零分歧: 30 只 0/0 + 29 只 1/1)

        ⚠️ **reason 取 idx16 不取 idx17**: idx17 是 200+ 字的异动公告全文,
        idx16 才是简明题材 —— 之前写反了, 导致同一 canonical 列在 up 板型是 1 个题材词、
        在 resumption 却是长段落。idx17 的全文目前 canonical 无处安放, 暂不映射。
        """
        row = entry.get("row", []) if isinstance(entry, dict) else []
        return {
            "symbol": normalize_symbol(at(row, 0)),
            "name": clean_text(at(row, 1)),
            "trade_date": day,
            "board_type": "resumption",
            # ⚠️ **两个指标, 语义不同** (2026-10-20 用户拍板拆分):
            #   limit_count       = 当前连板天数 (从 idx9 连板描述文本解析)
            #   total_board_count = 区间总板数 (idx10, 如 "4天3板" → 3)
            # 跨板形态 ("N天M板") 的当前连板天数不可知 → limit_count 留 None。
            #   算连板高度/晋级率要用 ladder 板型 (它的 idx2 就是当前连板天数)。
            "limit_count": _parse_consecutive_board(at(row, 9)),
            "total_board_count": parse_int(at(row, 10)),
            "limit_time": parse_int(at(row, 6)),
            "plate_id": clean_text(entry.get("plate_id")) if isinstance(entry, dict) else None,
            "plate_name": clean_text(entry.get("plate_name")) if isinstance(entry, dict) else None,
            "seal_amount": _scaled(at(row, 8), _SEAL_AMOUNT_SCALE),
            "seal_money": None,
            "themes": clean_text(at(row, 11)),
            "reason": clean_text(at(row, 16)),
            "amount": _scaled(at(row, 13), _AMOUNT_SCALE),
            "turnover_rate": _pct(at(row, 14)),
            "change_pct": None,
            "last_price": None,
            "amplitude": None,
            "net_inflow": _scaled(at(row, 12), _NET_INFLOW_SCALE),
            "plate_heat_score": None,
            "market_cap": parse_float(at(row, 15)),
            "one_word": None,
            "popular": None,
            "market_limit_up_count": None,
            "market_limit_down_count": None,
            "rise_count": None,
            "fall_count": None,
            "is_exploded": parse_bool_value(at(row, 18)),
        }

    @staticmethod
    def _to_frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
        """按 canonical schema 构表: 缺列补 None, 类型严格对齐, 空行也返回有 schema 的空表。

        ⚠️ 必须**逐列按目标 dtype 构造**, 不能直接 `pl.DataFrame(rows)`:
           polars 从首行推断 schema, 而宽联合数据里同一列在首行可能是 None (Null dtype),
           后面才出现具体类型 → 抛 "could not append value: 61 of type: i64"。
           显式给 dtype 由 polars 逐值转换, 不会炸。
        """
        if not rows:
            return pl.DataFrame(schema=LIMIT_BOARD_SCHEMA)
        columns: dict[str, pl.Series] = {}
        for column, dtype in LIMIT_BOARD_SCHEMA.items():
            values = [row.get(column) for row in rows]
            columns[column] = pl.Series(column, values, dtype=dtype, strict=False)
        return pl.DataFrame(columns).select(list(LIMIT_BOARD_SCHEMA.keys()))

    # ────────────────── 设置页试拉 ──────────────────

    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        """设置页「试拉」按钮。返回 {provider, dataset, rows, columns, preview, error?}。"""
        if dataset not in {"limit_board", "market_events"}:
            return {
                "provider": self.name,
                "dataset": dataset,
                "rows": 0,
                "columns": [],
                "preview": [],
                "error": (
                    f"kph 不支持数据集 {dataset!r} "
                    f"(仅支持 limit_board / market_events)"
                ),
            }
        try:
            if dataset == "market_events":
                frame = self.get_market_events()
                columns = list(MARKET_EVENTS_SCHEMA.keys())
            else:
                frame = self.get_limit_board(board_type="up")
                columns = list(LIMIT_BOARD_SCHEMA.keys())
        except Exception as exc:
            logger.warning("kph.test_dataset 失败: %s", exc)
            return {
                "provider": self.name,
                "dataset": dataset,
                "rows": 0,
                "columns": list(MARKET_EVENTS_SCHEMA.keys())
                if dataset == "market_events"
                else list(LIMIT_BOARD_SCHEMA.keys()),
                "preview": [],
                "error": str(exc),
            }
        return {
            "provider": self.name,
            "dataset": dataset,
            "rows": frame.height,
            "columns": columns,
            "preview": frame.head(5).to_dicts(),
        }

    # ────────────────── 独立数据集: market_events (盘面梳理事件流) ──────────────────

    def get_market_events(
        self,
        trade_date: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> pl.DataFrame:
        """盘面梳理事件流。

        ⚠️ **独立数据集, 不并入 limit_board** (2026-10-20 用户拍板):
        event 返回的是 **dict 行** (不是位置数组), 语义上是"事件"而不是"板" ——
        一条事件可以涉及多只股票 (StockList), 装不进 limit_board 的宽联合表。

        2026-10-20 实测 (KPH `GetPMSL_PMLD`):
            2026-10-16 → st=30 取 30 条, st=100 取 31 条 (上游当天共 31 条)
            每条 item 8 个键: Detail / StockList / TagID / TagName /
                             TagShuXing / TimeMin / ZSCode / ZSName
            `TimeMin` 名字像分钟, 实际是 **unix 秒时间戳**
              (1792113900 → 2026-10-16 09:25:00 开盘集合竞价;
               1792134000 → 2026-10-16 15:00:00 收盘) ✓
            `StockList` = [[代码, 名称], ...] → **一条事件展开成多行个股**
            tag_name 实测分布: 人气股杀跌9/大单一字5/领先身位4/权重杀跌3/
                               权重拉升3/直线拉升2/北交所2/大长腿1/尾盘炸板回封1

        💡 **「大单一字」是唯一能补上 KPH 涨停池缺口的事件标签**:
        涨停池 55 只 vs ELTDX/东财 61 只, 缺的 6 只里 **5 只**在 event 流被标
        「大单一字」(一字板不在涨停池里, 09:25 集合竞价封板)。
        """
        day = normalize_date(trade_date) or today_dash()
        rows: list[dict[str, Any]] = []
        for item in self.client.market_review_events(day, limit=limit, offset=offset):
            base = {
                "trade_date": day,
                "event_time": parse_int(item.get("TimeMin")),
                "tag_id": parse_int(item.get("TagID")),
                "tag_name": clean_text(item.get("TagName")),
                "plate_id": clean_text(item.get("ZSCode")),
                "plate_name": clean_text(item.get("ZSName")),
                "detail": clean_text(item.get("Detail")),
            }
            stocks = item.get("StockList")
            pairs = [
                (str(s[0]), str(s[1]) if len(s) > 1 else None)
                for s in (stocks if isinstance(stocks, list) else [])
                if isinstance(s, (list, tuple)) and s
            ]
            if not pairs:
                # 没有关联个股的事件 (如板块级异动) → 保留一行, symbol=None
                rows.append({**base, "symbol": None, "name": None})
                continue
            for code, name in pairs:
                rows.append({**base, "symbol": normalize_symbol(code), "name": clean_text(name)})
        return self._to_events_frame(rows)

    @staticmethod
    def _to_events_frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
        """按 MARKET_EVENTS_SCHEMA 构表 (缺列补 None, 类型严格对齐)。"""
        if not rows:
            return pl.DataFrame(schema=MARKET_EVENTS_SCHEMA)
        columns: dict[str, pl.Series] = {}
        for column, dtype in MARKET_EVENTS_SCHEMA.items():
            values = [row.get(column) for row in rows]
            columns[column] = pl.Series(column, values, dtype=dtype, strict=False)
        return pl.DataFrame(columns).select(list(MARKET_EVENTS_SCHEMA.keys()))

    def get_sector_constituents(self, trade_date: str | None, plate_id: str) -> list[dict[str, Any]]:
        """板块成分股 (板块级数据, 不并入 limit_board 数据集)。"""
        day = historical_date(trade_date or today_dash())
        out: list[dict[str, Any]] = []
        for raw in self.client.sector_constituents(day, plate_id):
            symbol = normalize_symbol(at(raw, 0))
            out.append(
                {
                    "trade_date": day,
                    "plate_id": plate_id,
                    "symbol": symbol,
                    "exchange": exchange_of(symbol),
                    "name": clean_text(at(raw, 1)),
                    "tags": clean_text(at(raw, 4)),
                    "last_price": parse_float(at(raw, 5)),
                    "change_pct": _pct(at(raw, 6)),
                    "amount": _scaled(at(raw, 7), _AMOUNT_SCALE),
                    "turnover_rate": _pct(at(raw, 8)),
                    "float_market_value": parse_float(at(raw, 10)),
                    "main_net": _scaled(at(raw, 13), _NET_INFLOW_SCALE),
                    "limit_tag": clean_text(at(raw, 23)),
                    "rank_tag": clean_text(at(raw, 24)),
                    "limit_count": parse_int(at(raw, 40)),
                }
            )
        return out
