"""财联社 (CLS) Provider 实现.

vendor 契约 (docs/plugin-development.md §"Provider 接口契约"):
- get_daily / get_minute 返回 canonical 8 列 DataFrame
- 软失败: 单标的失败只 warning, 不阻断整批; 全部失败返回空 DataFrame (schema 仍合法)
- 单位: volume 手 (1手=100股) / amount 元 / change_pct 小数制 / OHLC 不复权原始价
- 缺字段置 None, **不启发式补全** (CONTRIBUTING §3.1)
- minute 的 datetime 必须是**北京墙钟 naive** (UTC 会让分时图整片空白)

数据集声明 (plugin.yaml): **只有 [daily, minute]**
- 其余 10 个 CLS 接口 (emotion / wind / mainline / sector_* / limit_up_pool / telegraph)
  是 CLS 独有的短线数据, **不符合任何 vendor 标准数据集** → 不声明,
  作为 Provider 的独立公开方法暴露 (见下方 §"CLS 独有短线数据"),
  由上层 API/前端直接调用, 不进 provider_has_dataset 路由。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date as date_type
from datetime import datetime, timedelta, timezone
from typing import Any

import polars as pl

# 相对导入: 同一份代码在 vendor (app.plugins.cls.provider) 和暂存区 (cls.provider) 都能用
from .client import (
    KLINE_MAX_LIMIT,
    ROTATION_DAYS,
    TELEGRAPH_MAX_RN,
    ClsClient,
    ClsValidationError,
)

logger = logging.getLogger(__name__)

# 只声明真实提供的数据集; 其余 provider_has_dataset 返回 False → 回退 tickflow
#
# ⚠️ **daily 故意不声明**: CLS 的 kline 价格字段是**减法式前复权价**
#    (真实价 - 上市至今累计每股分红), 不是 vendor 契约要求的"不复权原始价"。
#    深历史 (2016 年前后) 累计分红已超过股价本身, 复权后 close_px 甚至是负数。
#    若声明 daily, 框架的 adj_factor 管道会再叠一次前复权 → **双重复权**,
#    产出静默错误的价格。宁可少一个数据集, 也不要污染 core/daily。
#    `get_daily()` 方法仍然保留, 供显式调用 (会打前复权告警), 见 README §4.2。
_DATASETS = ("minute",)

# canonical schema (vendor plugin-development.md §"Provider 接口契约")
DAILY_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.Utf8,
    "date": pl.Date,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,  # 手
    "amount": pl.Float64,  # 元
}

MINUTE_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.Utf8,
    "datetime": pl.Datetime("us"),  # 北京墙钟 naive (vendor 强契约)
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,  # 手
    "amount": pl.Float64,  # 元, 可空
}

# 周期名 → CLS kline 的 type 参数
PERIOD_MAP: dict[str, str] = {
    "daily": "fd1",
    "weekly": "fw",
    "monthly": "fm",
    "yearly": "fy",
}

# 北交所判定: 二位前缀 {43, 83, 87, 92}
# 口径来源 backend/app/services/abnormal_moves.py:75 (board_of) —— 与项目内其它模块同源,
# 不能用简单的 ("4", "8") (会把 4xxxxx / 8xxxxx 全吞, 也不含 920xxx)
BJ_PREFIXES: frozenset[str] = frozenset({"43", "83", "87", "92"})

# 1 手 = 100 股
SHARES_PER_LOT = 100.0

_BEIJING = timezone(timedelta(hours=8))

# 电报向前翻页的最大页数 (实测覆盖一个交易日需 11~12 页)
_TELEGRAPH_MAX_PAGES = 30


# ────────────────── symbol 归一 ──────────────────


def normalize_symbol(code: Any) -> str | None:
    """裸代码 → 带交易所后缀的标准代码 (600519.SH / 000001.SZ / 430047.BJ)。

    口径来源 backend/app/services/abnormal_moves.py:75:
        if symbol.endswith(".BJ") or code[:2] in {"43", "83", "87", "92"}: → 北交所

    北交所判定必须**先于** 9 开头判定:
        920001 → "92" 命中 BJ 前缀 → BJ
        900001 → "90" 不命中 → 落到 9 开头 → SH (B 股段, 不是北交所)

    返回 None 表示代码无法识别 (交给调用方按"缺字段置 None"处理, 不抛异常)。
    """
    if code in (None, ""):
        return None
    text = str(code).strip().upper()
    if "." in text:
        head, _, tail = text.partition(".")
        if not head.isdigit() or len(head) != 6:
            return None
        if tail not in {"SH", "SZ", "BJ"}:
            return None
        if tail == "BJ" or head[:2] in BJ_PREFIXES:
            return f"{head}.BJ"
        if tail == "SH":
            return f"{head}.SH"
        return f"{head}.SZ"
    if not text.isdigit() or len(text) != 6:
        return None
    if text[:2] in BJ_PREFIXES:
        return f"{text}.BJ"
    if text.startswith(("6", "5", "9")):
        return f"{text}.SH"
    if text.startswith(("0", "2", "3")):
        return f"{text}.SZ"
    return None


def to_secu_code(symbol: Any) -> str | None:
    """标准 symbol → CLS `secu_code` (小写前缀 + 6 位, 如 sh600519 / sz000001 / bj430047)。"""
    normalized = normalize_symbol(symbol)
    if normalized is None:
        return None
    code, _, suffix = normalized.partition(".")
    return f"{suffix.lower()}{code}"


def from_secu_code(secu_code: Any) -> str | None:
    """CLS `secu_code` → 标准 symbol (sh600519 → 600519.SH)。"""
    if not secu_code:
        return None
    text = str(secu_code).strip().lower()
    if "." in text:
        return normalize_symbol(text)
    if len(text) < 7:
        return None
    prefix, code = text[:2], text[2:]
    if not code.isdigit() or len(code) != 6:
        return None
    if prefix == "bj":
        return f"{code}.BJ"
    if prefix == "sh":
        return f"{code}.SH"
    if prefix == "sz":
        return f"{code}.SZ"
    return None


def strip_suffix(symbol: Any) -> str | None:
    """600519.SH → 600519。"""
    normalized = normalize_symbol(symbol)
    if normalized is None:
        return None
    return normalized.partition(".")[0]


# ────────────────── 取值helper ──────────────────


def _decimal(value: Any) -> float | None:
    """数值 → float; 缺失/非数值 → None (不伪造 0)。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text or text in {"-", "--", "null", "None"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _percent_to_decimal(value: Any) -> float | None:
    """百分比 → 小数制。

    ⚠️ 口径 (按端点分别实测, 不做启发式):
    - **字符串** (`"+20.00%"` / `"-4.99%"`) 是网页展示百分数 → ÷100
    - **数字** (0.2) 已经是小数制 → 原样
    混用同一个端点 (如 kline 的 change=0.021) 时不会走这个函数。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    if text.endswith("%"):
        text = text[:-1].strip()
    parsed = _decimal(text)
    if parsed is None:
        return None
    return parsed / 100.0


def _shares_to_lots(value: Any) -> float | None:
    """股 → 手 (÷100)。缺失 → None。"""
    parsed = _decimal(value)
    if parsed is None:
        return None
    return parsed / SHARES_PER_LOT


def _parse_day(value: Any) -> date_type | None:
    """YYYYMMDD / YYYY-MM-DD / 带时间 → date。认不出 → None。"""
    if value is None or value == "":
        return None
    if isinstance(value, date_type) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(int(value)) if isinstance(value, (int, float)) else str(value).strip()
    if not text:
        return None
    if " " in text:
        text = text.split(" ", 1)[0]
    if "T" in text:
        text = text.split("T", 1)[0]
    digits = re.sub(r"\D", "", text)
    if len(digits) == 8:
        try:
            return date_type(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
        except ValueError:
            return None
    try:
        return date_type.fromisoformat(text[:10])
    except ValueError:
        return None


def _minute_to_datetime(day: date_type | None, minute: Any) -> datetime | None:
    """(交易日, minute int HHMM) → 北京墙钟 naive datetime。

    930 → 09:30:00; 1130 → 11:30:00; 1300 → 13:00:00。
    """
    if day is None or minute in (None, ""):
        return None
    if isinstance(minute, str):
        digits = re.sub(r"\D", "", minute)
    elif isinstance(minute, (int, float)) and not isinstance(minute, bool):
        digits = str(int(minute))
    else:
        return None
    if len(digits) not in (3, 4):
        return None
    hours = int(digits[:-2])
    mins = int(digits[-2:])
    if not (0 <= hours <= 23 and 0 <= mins <= 59):
        return None
    return datetime(day.year, day.month, day.day, hours, mins, 0)


# ────────────────── 配置 ──────────────────


@dataclass
class ClsConfig:
    """Provider 配置. config.datasets 是路由判定关键 (provider_has_dataset 看它)。"""

    datasets: dict[str, Any] = field(default_factory=lambda: {name: True for name in _DATASETS})
    min_interval: float = 1.0
    timeout: float = 15.0
    # 单次 kline limit (上游上限 500)
    daily_limit_per_call: int = KLINE_MAX_LIMIT
    # kline 翻页上限 (offset 翻页, 500/页)
    daily_max_pages: int = 8
    # close_px 与真实均价(成交额/成交量)的最大允许偏离; 超过即判定为前复权口径并告警
    adjusted_price_warn_threshold: float = 0.05


# ────────────────── Provider ──────────────────


class ClsProvider:
    """财联社 Provider. 零 API Key (公开接口, 但需节流)."""

    name = "cls"
    builtin = True  # 内置插件 (随仓库分发, 不可被用户编辑/删除)

    def __init__(self, config: ClsConfig | None = None) -> None:
        self.config = config or ClsConfig()
        self.client = ClsClient(
            min_interval=self.config.min_interval,
            timeout=self.config.timeout,
        )

    def close(self) -> None:
        """vendor §"Provider 接口契约": 清理资源 (load_all 重建注册表时会调)。"""
        self.client.close()

    # ────────────────── daily ──────────────────

    def get_daily(
        self,
        symbols: list[str],
        start_time: str,
        end_time: str,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """日 K。

        ⚠️ **返回的是减法式前复权价, 不是不复权原始价** —— 与 vendor §"内部数据契约"
        ("日K OHLC 必须是不复权原始价") **冲突**。实测 (sh600519, 2500 根 2016-06~2026-10):
        `close_px` 与自身 `change` 字段 100% 自洽, 但与**真实均价** (成交额÷成交量) 的
        比值随时间单调上升 (2016-06 ≈ -0.02 → 2026-09 ≈ 1.000), 差值即**上市至今累计
        每股分红** (2016 年累计分红已超股价本身, 复权后 close_px 为负)。

        因此:
          - 涨跌幅 / 成交量 / 成交额 **全区间可信**
          - 绝对价位**深历史不可用** (偏差 8%~40%+), 近期约 1 年内可用
          - 本插件**不声明** daily 数据集, 避免框架 adj_factor 管道双重复权

        Args:
            symbols: ["600519.SH", "000001.SZ"] 等带后缀代码 (裸 6 位也接受)
            start_time: "YYYY-MM-DD" (或 "YYYYMMDD")
            end_time: "YYYY-MM-DD"
            asset_type: stock (CLS 只覆盖 A 股; 其它类型按同一套股票代码处理)
            on_chunk_done: (cur, total) 进度回调, 必须覆盖空批次 (最终 cur == total)

        Returns:
            DataFrame 8 列: [symbol, date, open, high, low, close, volume, amount]
            volume 单位手, amount 单位元, OHLC 为**前复权价** (见上)。
        """
        rows: list[dict[str, Any]] = []
        total = len(symbols)
        if total == 0:
            # 空批次也要回调, 否则进度条永远走不到 100%
            if on_chunk_done is not None:
                on_chunk_done(0, 0)
            return self._to_frame([], DAILY_SCHEMA)

        start_day = _parse_day(start_time)
        end_day = _parse_day(end_time)
        for index, raw_symbol in enumerate(symbols, start=1):
            try:
                symbol = normalize_symbol(raw_symbol)
                if symbol is None:
                    logger.warning("cls.get_daily 无法识别代码 %r, 已跳过", raw_symbol)
                else:
                    secu_code = to_secu_code(symbol)
                    for bar in self._fetch_kline(secu_code, start_day, end_day, symbol):
                        rows.append(self.map_daily_bar(symbol, bar))
            except ClsValidationError:
                # 参数错误是调用方的错 → fail-fast, 不静默跳过
                raise
            except Exception as exc:
                # vendor §"异常语义": 单标的运行时失败不阻断整批, 记 warning 继续
                logger.warning("cls.get_daily %s 失败: %s", raw_symbol, exc)
            finally:
                if on_chunk_done is not None:
                    on_chunk_done(index, total)

        return self._to_frame(rows, DAILY_SCHEMA)

    def _fetch_kline(
        self,
        secu_code: str | None,
        start_day: date_type | None,
        end_day: date_type | None,
        symbol: str | None = None,
    ) -> list[dict[str, Any]]:
        """拉 kline 并按 offset 翻页。

        CLS 的 kline 是**升序**返回最近 N 根; `offset` 从**最新一根**往回数
        (实测 limit=2/offset=0 → 最近 2 根, offset=2 → 再往前 2 根),
        所以翻页是 `offset += limit` 往更早翻。
        翻页终止条件: 短页 (上游没有更多) 或 最早一根已早于 start_day。
        """
        if secu_code is None:
            return []
        limit = max(1, min(int(self.config.daily_limit_per_call), KLINE_MAX_LIMIT))
        bars: list[dict[str, Any]] = []
        offset = 0
        for _ in range(max(1, int(self.config.daily_max_pages))):
            page = self.client.kline(
                secu_code, kline_type=PERIOD_MAP["daily"], limit=limit, offset=offset
            )
            if not isinstance(page, list):
                break
            rows = [item for item in page if isinstance(item, Mapping)]
            bars.extend(rows)
            if len(page) < limit:
                break
            days = [d for d in (_parse_day(item.get("date")) for item in rows) if d is not None]
            if not days:
                break
            if start_day is not None and min(days) <= start_day:
                break
            offset += limit
        bars = self._filter_by_day(bars, start_day, end_day)
        bars = self._drop_invalid_price(bars, symbol or secu_code, kind="daily")
        self._warn_forward_adjusted(bars, symbol or secu_code)
        return bars

    def _warn_forward_adjusted(self, bars: list[dict[str, Any]], label: str) -> None:
        """检测并告警: CLS 的价格字段是**减法式前复权价**, 不是不复权原始价。

        实测 (sh600519, offset 翻页取 2500 根, 2016-06 ~ 2026-10):
          - `close_px` 与自身 `change` 字段 **100% 自洽** → 序列本身一致, 不是随机脏数据
          - 但 `close_px` 与**真实均价** (成交额 business_balance ÷ 成交量 business_amount)
            的比值随时间**单调上升**: 2016-06 ≈ -0.02 (负价!) → 2026-09 ≈ 1.000
          - 差值 (真实均价 - close_px) 随时间递减到 0 → 差值就是**上市至今累计每股分红**
            (2016 年累计分红已超过股价本身, 减法式前复权后成负数)

        后果:
          - **涨跌幅 / 成交量 / 成交额 全区间可信**
          - **绝对价位在深历史不可用** (偏差 8% ~ 40%+)
          - 违反 vendor "daily 必须是不复权原始价" 契约 → 本插件**不声明** daily 数据集,
            避免框架的 adj_factor 管道再叠一次前复权 (双重复权 → 静默错价格)
          - 想要不复权价: 真实价 ≈ close_px + 累计分红; 累计分红可由
            (成交额 ÷ 成交量 - close_px) 在每个除权区间取中位数还原

        这里只告警不丢弃: 丢弃会同时丢掉可信的涨跌幅和成交量。
        """
        threshold = float(self.config.adjusted_price_warn_threshold)
        deviations: list[tuple[str, float]] = []
        for bar in bars:
            close = _decimal(bar.get("close_px"))
            amount = _decimal(bar.get("business_balance"))
            volume = _decimal(bar.get("business_amount"))
            if close is None or not amount or not volume:
                continue
            vwap = amount / volume
            if vwap <= 0:
                continue
            ratio = abs(close - vwap) / vwap
            if ratio > threshold:
                deviations.append((str(bar.get("date") or "?"), ratio))
        if not deviations:
            return
        ratios = [r for _, r in deviations]
        logger.warning(
            "cls %s 有 %d/%d 根 bar 的 close_px 与真实均价(成交额/成交量) 偏离 >%.0f%% "
            "(最大 %.1f%%) —— CLS 价格字段是**减法式前复权价**(真实价 - 累计每股分红), "
            "深历史绝对价位不可用 (涨跌幅/成交量/成交额仍可信)。涉及日期: %s%s",
            label,
            len(deviations),
            len(bars),
            threshold * 100,
            max(ratios) * 100,
            ", ".join(d for d, _ in deviations[:5]),
            " ..." if len(deviations) > 5 else "",
        )

    @staticmethod
    def _filter_by_day(
        bars: list[dict[str, Any]], start_day: date_type | None, end_day: date_type | None
    ) -> list[dict[str, Any]]:
        """按交易日裁剪到 [start_day, end_day] (任一端 None = 不限)。"""
        if start_day is None and end_day is None:
            return bars
        out: list[dict[str, Any]] = []
        for bar in bars:
            day = _parse_day(bar.get("date"))
            if day is None:
                continue
            if start_day is not None and day < start_day:
                continue
            if end_day is not None and day > end_day:
                continue
            out.append(bar)
        return out

    @staticmethod
    def _drop_invalid_price(
        bars: list[dict[str, Any]], label: str, *, kind: str
    ) -> list[dict[str, Any]]:
        """丢掉**价格非法**的 bar (OHLC 存在且 <= 0), 并打 warning。

        ⚠️ 起因 (2026-10-19 实测): CLS 的 kline 深历史对 sh600519 返回**负价格**
        (2011~2014 段 open/high/low/close 全为负, 同期 sz000001 正常)。
        这是**上游脏数据**, 不是映射问题。
        vendor 的 `indicators/pipeline.filter_halt_days` 只过滤 open==0 & high==0
        (停牌日), 拦不住负价, 负价进 MA/ATR 会污染指标 → 在 provider 层拦掉。

        不做"静默丢弃": 每次都打 WARNING (条数 + 涉及日期), 便于上游发现数据问题。
        """
        if not bars:
            return bars
        kept: list[dict[str, Any]] = []
        dropped: list[str] = []
        for bar in bars:
            prices = [
                _decimal(bar.get(key))
                for key in ("open_px", "high_px", "low_px", "close_px", "last_px")
            ]
            present = [p for p in prices if p is not None]
            if present and min(present) <= 0:
                dropped.append(str(bar.get("date") or bar.get("minute") or "?"))
                continue
            kept.append(bar)
        if dropped:
            logger.warning(
                "cls %s %s 有 %d 根价格非法 (OHLC<=0) 的 bar 已丢弃, 涉及日期/分钟: %s%s",
                kind,
                label,
                len(dropped),
                ", ".join(dropped[:5]),
                " ..." if len(dropped) > 5 else "",
            )
        return kept

    def map_daily_bar(self, symbol: str, bar: Mapping[str, Any]) -> dict[str, Any]:
        """单根 K 线 → canonical dict (附 CLS 独有的指标字段)。

        字段映射 (2026-10-19 实测):
        | CLS | → | 单位 |
        |---|---|---|
        | `date` (int YYYYMMDD) | date | Date |
        | `open_px`/`high_px`/`low_px`/`close_px` | open/high/low/close | 元, 不复权 |
        | `business_amount` | volume | **股** → /100 → 手 |
        | `business_balance` | amount | 元 |
        | `change` | change_pct | **已经是小数制** (0.021 = 2.1%) → 直接用, **不再 /100** |
        | `tr` | turnover_rate | 小数制 |
        | `amp` | amplitude | 小数制 |

        ⚠️ `change_pct` / `turnover_rate` / `amplitude` 是 CLS 独有的附加信息,
        **不属于 canonical daily 8 列**, 构表时被裁掉 (vendor 契约固定 8 列)。
        """
        day = _parse_day(bar.get("date"))
        return {
            "symbol": symbol,
            "date": day,
            "open": _decimal(bar.get("open_px")),
            "high": _decimal(bar.get("high_px")),
            "low": _decimal(bar.get("low_px")),
            "close": _decimal(bar.get("close_px")),
            "volume": _shares_to_lots(bar.get("business_amount")),
            "amount": _decimal(bar.get("business_balance")),
            # ↓ 以下不进入 canonical 8 列
            "change_pct": _decimal(bar.get("change")),
            "turnover_rate": _decimal(bar.get("tr")),
            "amplitude": _decimal(bar.get("amp")),
        }

    # ────────────────── minute ──────────────────

    def get_minute(
        self,
        symbols: list[str],
        start_time: str,
        end_time: str,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
        freq: str = "1m",
    ) -> pl.DataFrame:
        """1m 分钟 K。

        vendor 强契约 (plugin-development.md §"get_minute 的 datetime 时区契约"):
        - datetime 必须是**北京时间墙钟 naive**, 如 2026-08-28 09:35:00
          (UTC 口径会导致分时图整片空白)
        - amount 单位元; 不可靠时返回 None, 不伪造

        ⚠️ CLS 分时只提供 `open_px` / `last_px` / `av_px`, **没有分时 high/low** →
        high/low 置 None (不启发式补全)。

        Args:
            symbols: ["600519.SH", ...]
            start_time / end_time: "YYYY-MM-DD"
            on_chunk_done: (cur, total) 进度回调, 必须覆盖空批次
            freq: 仅支持 "1m"

        Returns:
            DataFrame 8 列: [symbol, datetime, open, high, low, close, volume, amount]
        """
        if freq not in {"1m", "1min", "minute"}:
            raise ClsValidationError(f"cls 只支持 freq='1m', 收到 {freq!r}")

        rows: list[dict[str, Any]] = []
        total = len(symbols)
        if total == 0:
            if on_chunk_done is not None:
                on_chunk_done(0, 0)
            return self._to_frame([], MINUTE_SCHEMA)

        start_day = _parse_day(start_time)
        end_day = _parse_day(end_time)
        for index, raw_symbol in enumerate(symbols, start=1):
            try:
                symbol = normalize_symbol(raw_symbol)
                if symbol is None:
                    logger.warning("cls.get_minute 无法识别代码 %r, 已跳过", raw_symbol)
                else:
                    secu_code = to_secu_code(symbol)
                    for bar in self._fetch_timeline(secu_code, start_day, end_day, symbol):
                        rows.append(self.map_minute_bar(symbol, bar))
            except ClsValidationError:
                # 参数错误是调用方的错 → fail-fast, 不静默跳过
                raise
            except Exception as exc:
                logger.warning("cls.get_minute %s 失败: %s", raw_symbol, exc)
            finally:
                if on_chunk_done is not None:
                    on_chunk_done(index, total)

        return self._to_frame(rows, MINUTE_SCHEMA)

    def _fetch_timeline(
        self,
        secu_code: str | None,
        start_day: date_type | None,
        end_day: date_type | None,
        symbol: str | None = None,
    ) -> list[dict[str, Any]]:
        """拉当日分时。

        ⚠️ 实测 `data` 是 dict: `{"date": [YYYYMMDD, ...], "line": [...]}`;
        `data.date` 是**list** 不是 int (早期文档写 int, 以实测为准)。
        """
        if secu_code is None:
            return []
        data = self.client.timeline(secu_code)
        if not isinstance(data, Mapping):
            return []
        line = data.get("line")
        if not isinstance(line, list):
            return []
        fallback_day = self._timeline_date(data)
        out: list[dict[str, Any]] = []
        for item in line:
            if not isinstance(item, Mapping):
                continue
            bar = dict(item)
            if bar.get("date") in (None, ""):
                bar["date"] = fallback_day
            day = _parse_day(bar.get("date"))
            if day is None:
                continue
            if start_day is not None and day < start_day:
                continue
            if end_day is not None and day > end_day:
                continue
            out.append(bar)
        return self._drop_invalid_price(out, symbol or secu_code, kind="minute")

    @staticmethod
    def _timeline_date(data: Mapping[str, Any]) -> date_type | None:
        """从 data["date"] 取交易日 (list / int 都认)。"""
        raw = data.get("date")
        if isinstance(raw, (list, tuple)):
            raw = raw[0] if raw else None
        return _parse_day(raw)

    def map_minute_bar(self, symbol: str, bar: Mapping[str, Any]) -> dict[str, Any]:
        """单根分时 → canonical dict。

        字段映射 (2026-10-19 实测):
        | CLS | → | 单位 |
        |---|---|---|
        | `minute` (int HHMM) + `date` (int YYYYMMDD) | datetime | 北京墙钟 naive |
        | `last_px` | close | 元 |
        | `open_px` | open | 元 (该分钟的成交价) |
        | `business_amount` | volume | **股** → /100 → 手 |
        | `business_balance` | amount | 元 |
        | `av_px` | avg_price | 元 (均价, 不进 canonical 8 列) |

        ⚠️ CLS 分时**不提供分时 high/low** → high/low 置 None (不伪造)。
        """
        day = _parse_day(bar.get("date"))
        stamp = _minute_to_datetime(day, bar.get("minute"))
        return {
            "symbol": symbol,
            "datetime": stamp,
            "open": _decimal(bar.get("open_px")),
            "high": None,
            "low": None,
            "close": _decimal(bar.get("last_px")),
            "volume": _shares_to_lots(bar.get("business_amount")),
            "amount": _decimal(bar.get("business_balance")),
            # ↓ 不进 canonical 8 列
            "avg_price": _decimal(bar.get("av_px")),
        }

    # ────────────────── CLS 独有短线数据 (不是标准数据集) ──────────────────

    def get_market_emotion(self) -> dict[str, Any]:
        """市场情绪截面。

        **返回 dict** (不是 list) —— 这是一个截面, 不是一批行。

        结构 (2026-10-19 实测):
        - `market_degree` / `shsz_balance` / `preview_balance` / `up_ratio` /
          `up_open_num` / `performance` / `up_open_ratio` / `profit_ratio`
        - `up_down_dis`: 涨跌家数分布 (up_num / down_num / rise_num / fall_num /
          flat_num / down_10 / down_8 / down_6 / down_4 / down_2 / up_2 ... )
        - `limit_up_board`: **连板梯队** —— row1 = ["一板","二板","三板","高度板"],
          row2 = ["35","9","2","0"] (家数), row3 = ["连板率","17%","50%","0%"]
        """
        data = self.client.stock_emotion()
        return dict(data) if isinstance(data, Mapping) else {}

    def get_limit_up_pool(
        self, pool_type: str = "up_pool", trade_date: str | None = None
    ) -> list[dict[str, Any]]:
        """涨停池 (pool_type="down_pool" 则为跌停池)。

        返回 list[dict], 每项:
        symbol / name / change_pct(小数制) / last_price / limit_time /
        theme(题材) / reason(涨停原因) / plates(所属题材 list) / stock_count。

        `up_reason` 原始格式是 `题材|原因` 两段 (实测 2026-10-19 涨停 49 只)。

        ⚠️ **该接口是实时的, 没有历史版**。实测 (2026-10-19): 传 date / trade_date /
        day / Date 四种日期参数, 返回结果完全一样 (都是当天的池子, 49 只, time 字段
        是当天时间); `type=up_pool_history` 直接返回 0 条。
        所以传 `trade_date` 只会打一条 warning, 拿到的**永远是当天**的池子。
        需要历史涨停池请用 KPH 插件 (它有历史接口)。
        """
        if trade_date:
            want = _parse_day(trade_date)
            today = date_type.today()
            if want is not None and want != today:
                logger.warning(
                    "cls.get_limit_up_pool 上游只有实时接口, 没有历史版 —— "
                    "忽略 trade_date=%s, 返回的是 %s 的池子 (要历史涨停池请用 KPH 插件)",
                    want.isoformat(),
                    today.isoformat(),
                )
        data = self.client.limit_up_pool(pool_type)
        if not isinstance(data, list):
            return []
        out: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, Mapping):
                continue
            theme, _, reason = str(item.get("up_reason") or "").partition("|")
            out.append(
                {
                    "symbol": from_secu_code(item.get("secu_code")),
                    "name": item.get("secu_name"),
                    "change_pct": _decimal(item.get("change")),
                    "last_price": _decimal(item.get("last_px")),
                    "limit_time": item.get("time"),
                    "theme": theme or None,
                    "reason": reason or None,
                    "stock_count": item.get("num_stocks"),
                    "plates": [
                        {
                            "plate_code": plate.get("secu_code"),
                            "plate_name": plate.get("secu_name"),
                            "change_pct": _decimal(plate.get("change")),
                        }
                        for plate in (item.get("plate") or [])
                        if isinstance(plate, Mapping)
                    ],
                }
            )
        return out

    def get_telegraph(
        self,
        date: str | None = None,
        category: str = "important",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """电报快讯。

        Args:
            date: "YYYY-MM-DD" / "YYYYMMDD" —— 取该**北京日**内的电报; None = 只要最新一批
            category: "important" → 只留 level=="C"; "all" → 不过滤
            limit: 最多返回条数

        ⚠️ 实测要点 (2026-10-19):
        - 上游**不认** category / level 参数, 过滤只能在客户端做
        - 上游的 `last_time` 是**向前翻页的锚点** (返回 ctime <= last_time 的最近 N 条),
          且**必须**同时带 `refresh_type=1` 才生效
        - 单页上限 50 条, 要覆盖一整天必须**连续向前翻页**:
          锚点 = 当天 23:59:59 → 翻到本页最小 ctime - 1 → 继续, 直到覆盖当天 00:00
          实测 2026-10-13 需 11 页取到 549 条, 2026-10-16 需 12 页取到 551 条
        - 锚点不前进时 (上游不认更早锚点) 安全退出, 不死循环

        返回 list[dict], 每项: id / title / content / brief / ctime /
        ctime_datetime(北京墙钟字符串) / level / reading_num。
        """
        if limit < 1:
            raise ClsValidationError(f"limit 必须 >= 1, 收到 {limit}")
        want_day = _parse_day(date)

        if want_day is None:
            # 不指定日期: 只取最新一批
            data = self.client.telegraph(rn=min(max(limit, 20), TELEGRAPH_MAX_RN))
            roll = (data or {}).get("roll_data") if isinstance(data, Mapping) else None
            return self._filter_telegraph(roll if isinstance(roll, list) else [], None, category, limit)

        # 指定日期: 从当天 23:59:59 往前翻页
        day_start = int(
            datetime.combine(want_day, datetime.min.time(), tzinfo=_BEIJING).timestamp()
        )
        day_end = int(
            datetime.combine(want_day, datetime.max.time().replace(microsecond=0), tzinfo=_BEIJING).timestamp()
        )
        collected: dict[str, Any] = {}
        anchor = day_end
        for _ in range(_TELEGRAPH_MAX_PAGES):
            data = self.client.telegraph(rn=TELEGRAPH_MAX_RN, last_time=anchor)
            roll = (data or {}).get("roll_data") if isinstance(data, Mapping) else None
            if not isinstance(roll, list) or not roll:
                break
            ctimes = [
                int(item["ctime"])
                for item in roll
                if isinstance(item, Mapping) and item.get("ctime") not in (None, "")
            ]
            if not ctimes:
                break
            lowest = min(ctimes)
            for item in roll:
                if isinstance(item, Mapping):
                    collected.setdefault(str(item.get("id") or id(item)), item)
            if lowest <= day_start:
                break  # 已覆盖到当天 00:00 之前
            if lowest >= anchor:
                break  # 锚点没前进, 上游不认更早锚点 → 安全退出
            anchor = lowest - 1
        return self._filter_telegraph(list(collected.values()), want_day, category, limit)

    @staticmethod
    def _filter_telegraph(
        roll: list[Any],
        want_day: date_type | None,
        category: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """按 level / 日期过滤电报并裁剪条数。"""
        out: list[dict[str, Any]] = []
        for item in roll:
            if not isinstance(item, Mapping):
                continue
            ctime = item.get("ctime")
            if category == "important" and item.get("level") != "C":
                continue
            stamp = ClsProvider._ctime_to_beijing(ctime)
            if want_day is not None and (stamp is None or stamp.date() != want_day):
                continue
            out.append(
                {
                    "id": item.get("id"),
                    "title": item.get("title"),
                    "content": item.get("content"),
                    "brief": item.get("brief"),
                    "ctime": ctime,
                    "ctime_datetime": stamp.strftime("%Y-%m-%d %H:%M:%S") if stamp else None,
                    "level": item.get("level"),
                    "reading_num": item.get("reading_num"),
                }
            )
            if len(out) >= limit:
                break
        return out

    @staticmethod
    def _ctime_to_beijing(ctime: Any) -> datetime | None:
        """unix 秒 → 北京墙钟 naive datetime。"""
        if ctime in (None, ""):
            return None
        try:
            seconds = int(ctime)
        except (TypeError, ValueError):
            return None
        return datetime.fromtimestamp(seconds, _BEIJING).replace(tzinfo=None)

    def get_sector_heat(self, plate_type: str = "concept") -> list[dict[str, Any]]:
        """板块热度排行 (实测 20 条)。

        返回 list[dict], 每项: plate_code / plate_name / rank / cur_heat /
        rank_change / is_new。
        """
        data = self.client.plate_heat(plate_type=plate_type)
        if not isinstance(data, list):
            return []
        return [
            {
                "plate_code": item.get("plate_code"),
                "plate_name": item.get("plate_name"),
                "rank": item.get("rank"),
                "cur_heat": _decimal(item.get("cur_heat")),
                "rank_change": item.get("rank_change"),
                "is_new": item.get("is_new"),
            }
            for item in data
            if isinstance(item, Mapping)
        ]

    def get_sector_industry(self, page: int = 1) -> list[dict[str, Any]]:
        """行业板块列表 (含涨停家数 / 主力净流入 / 领涨股)。

        返回 list[dict], 每项: plate_code / plate_name / change_pct(小数制) /
        main_fund_diff(元) / limit_up / limit_down / limit_up_num / limit_down_num /
        trade_status / first_stock(dict)。
        """
        data = self.client.plate_list(page=page)
        if not isinstance(data, Mapping):
            return []
        plate_data = data.get("plate_data")
        if not isinstance(plate_data, list):
            return []
        out: list[dict[str, Any]] = []
        for item in plate_data:
            if not isinstance(item, Mapping):
                continue
            first = item.get("first_stock")
            out.append(
                {
                    "plate_code": item.get("secu_code"),
                    "plate_name": item.get("secu_name"),
                    "change_pct": _decimal(item.get("change")),
                    "main_fund_diff": _decimal(item.get("main_fund_diff")),
                    "limit_up": item.get("limit_up"),
                    "limit_down": item.get("limit_down"),
                    "limit_up_num": item.get("limit_up_num"),
                    "limit_down_num": item.get("limit_down_num"),
                    "trade_status": item.get("trade_status"),
                    "first_stock": (
                        {
                            "symbol": from_secu_code(first.get("secu_code")),
                            "name": first.get("secu_name"),
                            "last_price": _decimal(first.get("last_px")),
                            "change_pct": _decimal(first.get("change")),
                        }
                        if isinstance(first, Mapping)
                        else None
                    ),
                }
            )
        return out

    def get_sector_rotation(self, days: int = 4) -> list[dict[str, Any]]:
        """板块轮动 (days 只支持 4 / 30, 上游限制)。

        返回 list[dict], 每项: trade_date / plates(list of
        {plate_code, plate_name, change_pct(小数制)})。

        ⚠️ 实测上游在 days 不支持时会把 data 换成 dict (`{"days": "支持 4/30"}`),
        这里**逐项 isinstance 判断**, 非 dict 直接跳过, 不会 AttributeError。
        """
        if days not in ROTATION_DAYS:
            raise ClsValidationError(
                f"days 必须是 {sorted(ROTATION_DAYS)} 之一 (上游只支持这两个), 收到 {days}"
            )
        data = self.client.plate_rotation(days=days)
        if not isinstance(data, list):
            return []
        out: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, Mapping):
                continue
            plates = item.get("plates")
            rows: list[dict[str, Any]] = []
            if isinstance(plates, list):
                for plate in plates:
                    if not isinstance(plate, Mapping):
                        continue
                    rows.append(
                        {
                            "plate_code": plate.get("plate_code"),
                            "plate_name": plate.get("plate_name"),
                            "change_pct": _decimal(plate.get("change")),
                        }
                    )
            out.append({"trade_date": item.get("trade_date"), "plates": rows})
        return out

    def get_market_wind(self) -> list[dict[str, Any]]:
        """今日题材 / 市场风向标。

        返回 list[dict], 每项: plate_code / title / interpret / id / subject_id。
        """
        data = self.client.today_tuyere()
        if not isinstance(data, Mapping):
            return []
        tuyere = data.get("today_tuyere")
        if not isinstance(tuyere, list):
            return []
        return [
            {
                "plate_code": item.get("plate_code"),
                "title": item.get("title"),
                "interpret": item.get("interpret"),
                "id": item.get("id"),
                "subject_id": item.get("subject_id"),
            }
            for item in tuyere
            if isinstance(item, Mapping)
        ]

    def get_market_mainline(self) -> list[dict[str, Any]]:
        """主线机会。

        ⚠️ 上游 `data` 的三个键**类型不同** (实测): `chances` / `winds` 是 list,
        `styles` 是 **dict**。这里统一摊平成 list[dict], 每项带 `section` 标记:
        - section="chance": title / interpret(=mainLine_desc) / plates / hot / continued
        - section="wind":   title / interpret / plate_code / plate_change / stocks
        - section="style":  title / interpret / id
        """
        data = self.client.market_mainline()
        if not isinstance(data, Mapping):
            return []
        out: list[dict[str, Any]] = []
        chances = data.get("chances")
        if isinstance(chances, list):
            for item in chances:
                if not isinstance(item, Mapping):
                    continue
                out.append(
                    {
                        "section": "chance",
                        "title": item.get("title"),
                        "interpret": item.get("mainLine_desc") or item.get("reason"),
                        "hot": item.get("hot"),
                        "continued": item.get("continued"),
                        "plates": item.get("plates") if isinstance(item.get("plates"), list) else [],
                    }
                )
        winds = data.get("winds")
        if isinstance(winds, list):
            for item in winds:
                if not isinstance(item, Mapping):
                    continue
                out.append(
                    {
                        "section": "wind",
                        "title": item.get("title"),
                        "interpret": item.get("interpret"),
                        "plate_code": item.get("plate_code"),
                        "plate_change": _decimal(item.get("plate_change")),
                        "hot": item.get("hot"),
                        "stocks": (
                            item.get("faucet_stocks")
                            if isinstance(item.get("faucet_stocks"), list)
                            else []
                        ),
                    }
                )
        styles = data.get("styles")
        if isinstance(styles, Mapping):
            out.append(
                {
                    "section": "style",
                    "title": styles.get("title"),
                    "interpret": styles.get("interpret"),
                    "id": styles.get("id"),
                }
            )
        elif isinstance(styles, list):
            for item in styles:
                if not isinstance(item, Mapping):
                    continue
                out.append(
                    {
                        "section": "style",
                        "title": item.get("title"),
                        "interpret": item.get("interpret"),
                        "id": item.get("id"),
                    }
                )
        return out

    def get_plate_stocks(self, plate_code: str) -> list[dict[str, Any]]:
        """题材成分股。

        返回 list[dict], 每项: symbol / name / change_pct / change_px / last_price /
        limit_up_days(连板数) / trade_status。
        """
        data = self.client.tuyere_stocks(plate_code)
        if not isinstance(data, list):
            return []
        return [
            {
                "symbol": from_secu_code(item.get("secu_code")),
                "name": item.get("secu_name"),
                "change_pct": _percent_to_decimal(item.get("change")),
                "change_px": _decimal(item.get("change_px")),
                "last_price": _decimal(item.get("last_px")),
                "limit_up_days": item.get("limit_up_days"),
                "trade_status": item.get("trade_status"),
            }
            for item in data
            if isinstance(item, Mapping)
        ]

    def get_popular_stocks(self, plate_code: str) -> list[dict[str, Any]]:
        """板块人气股。

        返回 list[dict], 每项: symbol / name / change_pct / change_px / last_price /
        tbm(题材标签) / head_num(讨论热度)。
        """
        data = self.client.popular_stocks(plate_code)
        if not isinstance(data, list):
            return []
        return [
            {
                "symbol": from_secu_code(item.get("secu_code")),
                "name": item.get("secu_name"),
                "change_pct": _percent_to_decimal(item.get("change")),
                "change_px": _decimal(item.get("change_px")),
                "last_price": _decimal(item.get("last_px")),
                "tbm": item.get("tbm"),
                "head_num": item.get("head_num"),
            }
            for item in data
            if isinstance(item, Mapping)
        ]

    # ────────────────── 设置页试拉 ──────────────────

    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict[str, Any]:
        """设置页「试拉」按钮。返回 {provider, dataset, rows, columns, preview, error?}。"""
        if dataset not in self.config.datasets:
            return {
                "provider": self.name,
                "dataset": dataset,
                "rows": 0,
                "columns": [],
                "preview": [],
                "error": f"{dataset} 不在 cls 声明的数据集中, 会回退 tickflow",
            }
        try:
            test_symbols = symbols or ["600519.SH"]
            today = date_type.today()
            if dataset == "daily":
                frame = self.get_daily(test_symbols, "2020-01-01", today.isoformat())
            elif dataset == "minute":
                frame = self.get_minute(test_symbols, "2020-01-01", today.isoformat())
            else:
                return {
                    "provider": self.name,
                    "dataset": dataset,
                    "rows": 0,
                    "columns": [],
                    "preview": [],
                    "error": f"unsupported {dataset}",
                }
        except Exception as exc:
            logger.warning("cls.test_dataset 失败: %s", exc)
            return {
                "provider": self.name,
                "dataset": dataset,
                "rows": 0,
                "columns": [],
                "preview": [],
                "error": str(exc),
            }
        return {
            "provider": self.name,
            "dataset": dataset,
            "rows": frame.height,
            "columns": frame.columns,
            "preview": frame.head(5).to_dicts(),
        }

    # ────────────────── 构表 ──────────────────

    @staticmethod
    def _to_frame(rows: list[dict[str, Any]], schema: dict[str, pl.DataType]) -> pl.DataFrame:
        """按 canonical schema 构表: 缺列补 None, 类型严格对齐, 空行也返回有 schema 的空表。

        ⚠️ 必须**逐列按目标 dtype 构造**, 不能直接 `pl.DataFrame(rows)`:
           polars 从首行推断 schema, 同一列在首行可能是 None (Null dtype),
           后面才出现具体类型 → 抛 "could not append value: 61 of type: i64"。
           显式给 dtype 由 polars 逐值转换, 不会炸。
        """
        if not rows:
            return pl.DataFrame(schema=schema)
        columns: dict[str, pl.Series] = {}
        for column, dtype in schema.items():
            values = [row.get(column) for row in rows]
            columns[column] = pl.Series(column, values, dtype=dtype, strict=False)
        return pl.DataFrame(columns).select(list(schema.keys()))
