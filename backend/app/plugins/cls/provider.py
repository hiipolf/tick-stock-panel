"""财联社 (CLS) provider。

数据集边界 (plugin-development.md §"能力声明的粒度" 强约束):
  - daily       来自 cls_stock_kline (cls.cn 个股日 K)
  - minute      来自 cls_stock_timeline (cls.cn 个股当日分时)
  - 未声明 realtime/depth5/adj_factor/financial/full_minute
    → provider_has_dataset 返回 False, 自动回退 tickflow / tdxquant

单位与口径 (CONTRIBUTING §3.1, 不可凭字段名推断):
  - symbol 必带交易所后缀 (600519.SH / 000001.SZ / BJ.830799)
  - change_pct 内部契约为小数制 (0.0172 = +1.72%), 财联社百分数字段需 /100
  - volume 内部契约手, 财联社股字段需 /100
  - amount 元 (财联社返回元, 直接透传)

endpoint URL (api3.cls.cn/quote/stock/{kline,timeline}) 与响应结构
是依据 AxData README 推断, vendor 启用前必须用浏览器抓包核对。
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import polars as pl

from app.plugins.cls.client import BASE_URL, ClsClient

logger = logging.getLogger(__name__)

# 只声明真实提供的数据集; 其余 provider_has_dataset 返回 False → 回退 tickflow
_DATASETS = ("daily", "minute")

# canonical schema (跟 tdxquant/fuyao 一致; plugin-development.md §"字段映射")
DAILY_SCHEMA = {
    "symbol": pl.Utf8,
    "date": pl.Date,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,  # 手
    "amount": pl.Float64,  # 元
}

MINUTE_SCHEMA = {
    "symbol": pl.Utf8,
    "datetime": pl.Datetime,  # 北京墙钟 naive (vendor 强契约, 入口守卫 _enforce_minute_beijing_wallclock)
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,  # 手
    "amount": pl.Float64,  # 元, 可空
}


@dataclass
class _ClsConfig:
    """轻量 config shim, 让 custom loader 的 provider_has_dataset 能识别本 provider。"""

    name: str = "cls"
    display_name: str = "财联社 (cls.cn)"
    datasets: dict = field(default_factory=lambda: dict.fromkeys(_DATASETS))
    path: None = None
    builtin: bool = True


def availability() -> tuple[bool, str]:
    """loader 启动自检: CLS 公开接口, 无 Key 依赖, 永远可用。"""
    return True, "ok"


def normalize_symbol(code: str) -> str:
    """裸代码 → 600519.SH / 000001.SZ / 830799.BJ 等带后缀 (vendor §"代码格式" 强约束)。

    北交所代码段: 43/83/87/92 (与 app/services/abnormal_moves.py board_of 同口径)。
    """
    if not code:
        raise ValueError("empty code")
    code = str(code).strip()
    if "." in code:  # 已带后缀
        return code.upper()
    if code[:2] in {"43", "83", "87", "92"}:
        return f"{code.upper()}.BJ"
    if code.startswith("6"):
        return f"{code.upper()}.SH"
    if code.startswith(("0", "3")):
        return f"{code.upper()}.SZ"
    raise ValueError(f"unrecognized code prefix: {code}")


def strip_suffix(code: str) -> str:
    """600519.SH → 600519"""
    return code.split(".")[0]


def _to_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class ClsProvider:
    """财联社数据源。daily + minute 两个数据集; 无 API Key。"""

    name = "cls"
    builtin = True

    def __init__(self) -> None:
        self.config = _ClsConfig()
        self.client: ClsClient | None = None

    def close(self) -> None:  # loader.load_all 重建注册表时会对每个 provider 调 close
        if self.client is not None:
            self.client.close()
            self.client = None

    def _ensure_client(self) -> ClsClient:
        if self.client is None:
            self.client = ClsClient(base_url=BASE_URL)
        return self.client

    # ────────────────── daily ──────────────────

    def get_daily(
        self,
        symbols: list[str],
        start_time: str,
        end_time: str,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """日 K (原始价, 不复权)。

        Args:
            symbols: 带后缀代码列表 (e.g. ["600519.SH", "000001.SZ"])
            start_time / end_time: "YYYY-MM-DD" (财联社 K 线接口无日期入参,
                接口返回整段历史, 由调用方按日期过滤)
            asset_type: stock / etf / index (财联社 K 线仅 stock 实现)
            on_chunk_done: (cur, total) 回调, 必须覆盖空批次
        """
        client = self._ensure_client()
        rows: list[dict] = []
        total = len(symbols)
        for i, sym in enumerate(symbols, start=1):
            bare = strip_suffix(sym)
            try:
                payload = client.get(
                    "/quote/stock/kline",
                    params={"code": bare, "type": "day"},
                )
                for bar in (payload.get("data") or []):
                    row = self._map_daily_bar(bare, bar)
                    # 日期窗口过滤 (start_time / end_time)
                    if start_time and row["date"] is not None and str(row["date"]) < start_time:
                        continue
                    if end_time and row["date"] is not None and str(row["date"]) > end_time:
                        continue
                    rows.append(row)
            except Exception as exc:  # noqa: BLE001
                # vendor §"异常语义": 单标的不阻断其他, 记 warning 由 service 层捕获
                logger.warning("cls.get_daily %s failed: %s", sym, exc)
            finally:
                if on_chunk_done:
                    on_chunk_done(i, total)

        if not rows:
            return pl.DataFrame(schema=DAILY_SCHEMA)
        return pl.DataFrame(rows, schema=DAILY_SCHEMA)

    def _map_daily_bar(self, bare_code: str, bar: dict[str, Any]) -> dict[str, Any]:
        # 字段名严格按 AxData 文档 (cls_stock_kline.html) 对齐:
        #   trade_date / open / high / low / close / volume / amount /
        #   change (涨跌额) / change_pct (涨跌幅)
        # volume 单位由 cls.cn 真实接口定 — AxData 文档未明确,
        # 暂按 "股" 处理 (跟 fuyao/tdxquant 风格一致), ÷100 转手.
        # 真实启用前必须用浏览器抓包核对单位.
        raw_date = bar.get("trade_date")
        if isinstance(raw_date, str):
            if len(raw_date) == 8 and raw_date.isdigit():
                d = date(int(raw_date[:4]), int(raw_date[4:6]), int(raw_date[6:8]))
            else:
                d = date.fromisoformat(raw_date[:10])
        elif isinstance(raw_date, (int, float)):
            d = date.fromtimestamp(int(raw_date) / 1000)
        else:
            d = None

        volume_shares = bar.get("volume") or 0
        volume_lots = volume_shares / 100.0

        return {
            "symbol": normalize_symbol(bare_code),
            "date": d,
            "open": _to_float(bar.get("open")) or 0.0,
            "high": _to_float(bar.get("high")) or 0.0,
            "low": _to_float(bar.get("low")) or 0.0,
            "close": _to_float(bar.get("close")) or 0.0,
            "volume": volume_lots,
            "amount": _to_float(bar.get("amount")) or 0.0,
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
        """当日 1m 分钟 K。

        vendor 强契约 (plugin-development.md §"get_minute 的 datetime 时区契约"):
        datetime 必须是北京时间墙钟 naive, 入口守卫 _enforce_minute_beijing_wallclock
        兜底但源头必须写对。amount 单位元, 不可靠时返回 None 不伪造。
        """
        client = self._ensure_client()
        rows: list[dict] = []
        total = len(symbols)
        for i, sym in enumerate(symbols, start=1):
            bare = strip_suffix(sym)
            try:
                payload = client.get("/quote/stock/timeline", params={"code": bare})
                for bar in (payload.get("data") or []):
                    rows.append(self._map_minute_bar(bare, bar))
            except Exception as exc:  # noqa: BLE001
                logger.warning("cls.get_minute %s failed: %s", sym, exc)
            finally:
                if on_chunk_done:
                    on_chunk_done(i, total)

        if not rows:
            return pl.DataFrame(schema=MINUTE_SCHEMA)
        return pl.DataFrame(rows, schema=MINUTE_SCHEMA)

    def _map_minute_bar(self, bare_code: str, bar: dict[str, Any]) -> dict[str, Any]:
        """timeline 接口单根分钟 K → canonical dict。

        字段名严格按 AxData 文档 (cls_stock_timeline.html):
          trade_date / minute / last_price / open / pre_close /
          avg_price / volume / amount
        minute 字段三种格式: "HH:MM" / "HH:MM:SS" / "YYYY-MM-DD HH:MM:SS"。
        """
        minute_str = bar.get("minute")
        if minute_str:
            if " " in minute_str:
                dt = datetime.fromisoformat(minute_str)
            elif len(minute_str) == 5:  # "09:35"
                today = date.today()
                dt = datetime(today.year, today.month, today.day,
                              int(minute_str[:2]), int(minute_str[3:]))
            elif len(minute_str) == 8:  # "09:35:00"
                today = date.today()
                dt = datetime(today.year, today.month, today.day,
                              int(minute_str[:2]), int(minute_str[3:5]),
                              int(minute_str[6:8]))
            else:
                dt = None
        else:
            dt = None

        volume_shares = bar.get("volume") or 0
        volume_lots = volume_shares / 100.0

        return {
            "symbol": normalize_symbol(bare_code),
            "datetime": dt,
            "open": _to_float(bar.get("open")) or 0.0,
            "high": _to_float(bar.get("high")) or 0.0,
            "low": _to_float(bar.get("low")) or 0.0,
            "close": _to_float(bar.get("last_price")) or 0.0,
            "volume": volume_lots,
            "amount": _to_float(bar.get("amount")),
        }

    # ────────────────── 设置页试拉 ──────────────────

    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        """设置页"试拉"按钮: 拉一次数据集, 返回 {rows, columns, preview, error?}。"""
        if dataset not in _DATASETS:
            return {
                "provider": self.name,
                "dataset": dataset,
                "rows": 0,
                "columns": [],
                "preview": [],
                "error": f"{dataset} 不在 cls 声明的数据集, 自动回退 tickflow",
            }
        syms = (symbols or ["600519.SH"])[:3]
        try:
            if dataset == "daily":
                df = self.get_daily(syms, "2024-01-01", "2024-12-31")
            elif dataset == "minute":
                df = self.get_minute(syms, "2024-01-01", "2024-12-31")
            else:
                return {"provider": self.name, "dataset": dataset, "rows": 0,
                        "columns": [], "preview": [], "error": f"unsupported {dataset}"}
            return {
                "provider": self.name,
                "dataset": dataset,
                "rows": df.height,
                "columns": df.columns,
                "preview": df.head(5).to_dicts() if df.height else [],
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "provider": self.name,
                "dataset": dataset,
                "rows": 0,
                "columns": [],
                "preview": [],
                "error": str(exc),
            }