"""Provider contracts for external market data sources.

The first implementation wraps TickFlow. Other providers (Tushare/AkShare/etc.)
should return the same normalized Polars schemas so storage, indicators and
backtests stay data-source agnostic.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, Protocol

import polars as pl

AssetType = Literal["stock", "index", "etf"]

# limit_board (涨停板) canonical schema — 28 列宽联合设计。
# board_type 是判别列: up / down / wind_vane / ladder / emotion / sector / resumption。
# 与该行无关的列一律 None (板块级/情绪级行没有 symbol/封单, 不编造数值)。
# 注意 limit_count (个股连板数) 与 market_limit_up_count (全市场涨停家数)
# 是两个不同概念, 命名已刻意区分, 实现时不要混用。
LIMIT_BOARD_COLUMNS: tuple[str, ...] = (
    "symbol",
    "name",
    "trade_date",
    "board_type",
    "limit_count",
    "total_board_count",
    "limit_time",
    "plate_id",
    "plate_name",
    "seal_amount",
    "seal_money",
    "themes",
    "reason",
    "amount",
    "turnover_rate",
    "change_pct",
    "last_price",
    "amplitude",
    "net_inflow",
    "plate_heat_score",
    "market_cap",
    "one_word",
    "popular",
    "market_limit_up_count",
    "market_limit_down_count",
    "rise_count",
    "fall_count",
    "is_exploded",
)

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

# board_type 的合法取值 (判别列)。查询参数另允许 "all" = 合并全部类型。
LIMIT_BOARD_TYPES: tuple[str, ...] = (
    "up",
    "down",
    "wind_vane",
    "ladder",
    "emotion",
    "sector",
    "resumption",
)
# board_type="all" 时合并的类型集合 (不含 resumption: 复牌池是独立口径)
LIMIT_BOARD_ALL_TYPES: tuple[str, ...] = (
    "up",
    "down",
    "wind_vane",
    "ladder",
    "emotion",
    "sector",
)

# market_events (盘面梳理事件流) canonical schema — 9 列。
# 与 limit_board 是**两个独立数据集**: event 是"事件"不是"板", 一条事件可涉及
# 多只个股 (StockList), 语义上装不进 limit_board 的宽联合表, 故单列一份 schema。
# symbol/name 是从事件关联个股展开出来的, 板块级事件 (无关联个股) 这两列为 None。
MARKET_EVENTS_COLUMNS: tuple[str, ...] = (
    "symbol",
    "name",
    "trade_date",
    "event_time",
    "tag_id",
    "tag_name",
    "plate_id",
    "plate_name",
    "detail",
)

MARKET_EVENTS_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.Utf8,        # 个股代码 (600519.SH / 000001.SZ / 430047.BJ)
    "name": pl.Utf8,          # 个股名称
    "trade_date": pl.Date,    # 交易日
    "event_time": pl.Int64,   # 事件时间 (unix 秒) —— 上游字段名 TimeMin 但值是秒
    "tag_id": pl.Int32,       # 标签 ID
    "tag_name": pl.Utf8,      # 标签名 (大单一字/人气股杀跌/尾盘炸板回封/北交所/...)
    "plate_id": pl.Utf8,      # 板块 ID
    "plate_name": pl.Utf8,    # 板块名
    "detail": pl.Utf8,        # 事件描述全文
}

# limit_board 的日期解析: polars 不能把字符串直接 cast 成 Date, 必须显式解析。
# 逐格式尝试, 都不匹配则置 None (宽松语义, 不编造)。
_LIMIT_BOARD_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
)


def _limit_board_date_expr() -> pl.Expr:
    """trade_date 字符串 → Date 的表达式 (逐格式 coalesce, 逐行取首个能解析的格式)。"""
    col = pl.col("trade_date").cast(pl.Utf8, strict=False)
    return pl.coalesce(*[
        col.str.strptime(pl.Date, format=fmt, strict=False)
        for fmt in _LIMIT_BOARD_DATE_FORMATS
    ])


def _limit_board_time_expr() -> pl.Expr:
    """limit_time → HHMMSS 整数 (093012 = 09:30:12) 的表达式。

    源给字符串时按 HH:MM:SS / HHMMSS / HHMM / HH 归一; 解析不了置 None。
    源本来就是数值列时不做任何换算 (原样 cast Int64)。
    """
    text = pl.col("limit_time").cast(pl.Utf8, strict=False)
    digits = text.str.replace_all(r"[^0-9]", "")
    length = digits.str.len_chars()
    clean = pl.when(length == 0).then(pl.lit(None, dtype=pl.Utf8)).otherwise(digits)
    hhmm = clean.str.pad_start(length=4, fill_char="0").str.slice(0, 4)
    return (
        pl.when(length == 6).then(clean)
        .otherwise(hhmm + "00")
        .cast(pl.Int64, strict=False)
    )


def cast_limit_board(
    df: pl.DataFrame,
    board_type: str,
    trade_date: str | None = None,
) -> pl.DataFrame:
    """把任意来源的涨停板数据规范成 canonical 28 列。

    规则 (缺失列补 None, 不编造数值):
      - board_type 是判别列: 源提供了就保留源值 (上游才知道每行属于哪块板),
        没提供则注入本次查询的板型;
      - trade_date: 源没提供则注入请求的交易日, 都没给则保持 None;
      - 其余列按 canonical dtype cast, cast 失败置 None (strict=False);
      - 源里的额外列被丢弃 (canonical 形状是硬约束)。
    """
    if df.is_empty():
        return pl.DataFrame(schema=LIMIT_BOARD_SCHEMA)
    out = df
    if "board_type" not in out.columns:
        out = out.with_columns(pl.lit(board_type).alias("board_type"))
    if "trade_date" not in out.columns and trade_date:
        out = out.with_columns(pl.lit(date.fromisoformat(trade_date)).alias("trade_date"))
    exprs: list[pl.Expr] = []
    for col, dtype in LIMIT_BOARD_SCHEMA.items():
        if col not in out.columns:
            exprs.append(pl.lit(None, dtype=dtype).alias(col))
        elif col == "trade_date" and out.schema[col] == pl.Utf8:
            exprs.append(_limit_board_date_expr().alias(col))
        elif col == "limit_time" and out.schema[col] == pl.Utf8:
            exprs.append(_limit_board_time_expr().alias(col))
        else:
            exprs.append(pl.col(col).cast(dtype, strict=False).alias(col))
    return out.select(exprs)


def cast_market_events(
    df: pl.DataFrame,
    trade_date: str | None = None,
) -> pl.DataFrame:
    """把任意来源的盘面事件流规范成 canonical 9 列。

    规则 (缺失列补 None, 不编造数值):
      - trade_date: 源没提供则注入请求的交易日, 都没给则保持 None;
      - 其余列按 canonical dtype cast, cast 失败置 None (strict=False);
      - 源里的额外列被丢弃 (canonical 形状是硬约束)。
    """
    if df.is_empty():
        return pl.DataFrame(schema=MARKET_EVENTS_SCHEMA)
    out = df
    if "trade_date" not in out.columns and trade_date:
        out = out.with_columns(pl.lit(date.fromisoformat(trade_date)).alias("trade_date"))
    exprs: list[pl.Expr] = []
    for col, dtype in MARKET_EVENTS_SCHEMA.items():
        if col not in out.columns:
            exprs.append(pl.lit(None, dtype=dtype).alias(col))
        elif col == "trade_date" and out.schema[col] == pl.Utf8:
            exprs.append(_limit_board_date_expr().alias(col))
        else:
            exprs.append(pl.col(col).cast(dtype, strict=False).alias(col))
    return out.select(exprs)


@dataclass(frozen=True)
class ProviderCapabilities:
    instruments: bool = False
    daily: bool = False
    adj_factor: bool = False
    minute: bool = False
    realtime: bool = False
    depth5: bool = False
    financial: bool = False
    limit_board: bool = False
    market_events: bool = False


class MarketDataProvider(Protocol):
    name: str
    capabilities: ProviderCapabilities

    def get_instruments(self, asset_type: AssetType) -> pl.DataFrame:
        """Return normalized instruments: symbol/name/code/exchange/asset_type/source."""

    def get_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: AssetType,
    ) -> pl.DataFrame:
        """Return normalized daily K rows."""

    def get_adj_factors(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: AssetType,
    ) -> pl.DataFrame:
        """Return normalized adjustment factors: symbol/trade_date/ex_factor."""

    def get_minute(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: AssetType,
        freq: str = "1m",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """Return normalized minute K rows. Implementations may return empty.

        on_chunk_done 契约: provider 实现内部以 2 参 (cur, total) 调用;
        3 参 seg_label 适配由 kline_sync._try_custom_minute 包装层负责,
        不应泄漏到 provider 契约层。
        """

    def get_realtime(
        self,
        universes: list[str] | None = None,
        symbols: list[str] | None = None,
    ) -> pl.DataFrame:
        """Return normalized realtime quotes. Implementations may return empty."""

    def get_depth_batch(self, symbols: list[str]) -> dict[str, dict]:
        """Return five-level order books keyed by symbol."""

    def get_limit_board(
        self,
        trade_date: str | None = None,
        board_type: str = "up",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """Return normalized limit-board rows.

        Canonical columns (28): symbol/name/trade_date/board_type/limit_count/
        total_board_count/limit_time/plate_id/plate_name/seal_amount/seal_money/
        themes/reason/amount/turnover_rate/change_pct/last_price/amplitude/
        net_inflow/plate_heat_score/market_cap/one_word/popular/
        market_limit_up_count/market_limit_down_count/rise_count/fall_count/
        is_exploded。

        board_type 取值 up/down/wind_vane/ladder/emotion/sector/resumption/all;
        "all" = 合并全部类型 (不含 resumption), 用 board_type 列区分来源。
        change_pct / turnover_rate / amplitude 走小数制 (0.0366 = 3.66%)。
        """

    def get_market_events(
        self,
        trade_date: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> pl.DataFrame:
        """Return normalized market-event rows (盘面梳理事件流)。

        Canonical columns (9): symbol/name/trade_date/event_time/tag_id/
        tag_name/plate_id/plate_name/detail。

        与 get_limit_board 是两个独立数据集: 一条事件可关联多只个股, provider
        侧已展开成多行个股 (无关联个股的事件 symbol/name 为 None)。
        event_time 是 unix 秒时间戳 (上游字段名 TimeMin 但值是秒, 不是分钟)。
        limit/offset 是分页 (默认取前 100 条)。
        """
