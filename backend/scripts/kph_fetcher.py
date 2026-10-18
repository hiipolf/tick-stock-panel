"""KPH (开盘红) 数据 fetcher。

角色: 后台独立脚本, **不**注册成 Provider (KPH 字段不符合 vendor 7 个标准
数据集, 强做 Provider 必伪造字段 — 详见 `backend/app/plugins/cls/README.md`
同款分析)。本脚本作为"扩展数据 HTTP 定时拉取的替代方案":
  - vendor `data/ext_data/<id>/config.json` 机制只支持单层 pull,
    KPH `sector_constituents` 这种"先取板块列表再逐板块扇出"用本脚本更顺手;
  - CI / 一次性全量回填场景本脚本也比 7 个独立 JSON config 方便。

调用方式:
    cd backend && uv run python scripts/kph_fetcher.py --date 2024-01-15

输出:
    data/extensions/kph/YYYY-MM-DD/
        ├── limit_pool.parquet          涨停池含原因
        ├── limit_down_pool.parquet     跌停池
        ├── limit_ladder.parquet        连板天梯
        ├── market_emotion.parquet      市场情绪
        ├── sector_ranking.parquet      板块排行
        ├── sector_constituents.parquet 板块成分股
        └── market_events.parquet       盘面梳理事件

字段命名贴近 vendor §"上市公司维表" 风格: symbol 带后缀, trade_date YYYY-MM-DD。
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import polars as pl

LOG = logging.getLogger("kph_fetcher")

# KPH 没有公开 API 文档; endpoint 是基于 AxData README 推断, 真实接入前
# 必须用浏览器抓包确认 / 商务对接。
DEFAULT_BASE_URL = "https://www.kphhq.com"
DEFAULT_TIMEOUT = 10.0
DEFAULT_MIN_INTERVAL = 1.5


class KphClient:
    """KPH HTTP 客户端 + 单调用节流。"""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        min_interval: float = DEFAULT_MIN_INTERVAL,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.min_interval = min_interval
        self._last_call = 0.0
        self._http = httpx.Client(
            timeout=timeout,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
                ),
                "Referer": "https://www.kphhq.com/",
            },
        )

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_call = time.monotonic()

    def get(self, path: str, params: dict | None = None) -> dict[str, Any]:
        self._throttle()
        resp = self._http.get(f"{self.base_url}{path}", params=params or {})
        resp.raise_for_status()
        return resp.json()

    def close(self) -> None:
        self._http.close()


# ────────────────── symbol 归一 (跟 cls 插件一致) ──────────────────


def normalize_symbol(code: str) -> str:
    """6/688 → SH, 0/3 → SZ, 43/83/87/92 → BJ. 已带后缀原样返回. 未知格式原样返回不抛错.

    北交所代码段 43/83/87/92 (与 app/services/abnormal_moves.py board_of 同口径)。
    """
    if not code:
        return code
    code = str(code).strip()
    if "." in code:
        return code.upper()
    if code[:2] in {"43", "83", "87", "92"}:
        return f"{code}.BJ"
    if code.startswith("6"):
        return f"{code}.SH"
    if code.startswith(("0", "3")):
        return f"{code}.SZ"
    return code


# ────────────────── 各接口的 fetcher ──────────────────


def _limit_pool_schema() -> dict[str, Any]:
    """字段名严格按 AxData 文档对齐 (kph_limit_up_history):
      trade_date, instrument_id, symbol, exchange, name,
      limit_time, open_time, seal_amount, seal_money,
      limit_tag, limit_count, themes, reason,
      net_inflow, turnover (成交额, 元), turnover_rate, market_cap,
      industry_id, industry_limit_up_count.
    注意 AxData 文档里 "amount" 字段指 "成交额", 这里按 AxData 字段
    名写 turnover (cls 的 K 线 amt 在 AxData 叫 amount, KPH 叫 turnover).
    """
    return {
        "symbol": pl.Utf8,
        "name": pl.Utf8,
        "trade_date": pl.Date,
        "limit_count": pl.Int32,
        "limit_time": pl.Int64,
        "plate_id": pl.Utf8,
        "plate_name": pl.Utf8,
        "seal_amount": pl.Float64,
        "seal_money": pl.Float64,
        "limit_tag": pl.Utf8,  # 新增: 连板标签 (AxData)
        "themes": pl.Utf8,
        "reason": pl.Utf8,
        "net_inflow": pl.Float64,  # 新增: 净流入 (AxData, 元)
        "turnover": pl.Float64,  # 成交额, 单位元 (AxData 字段名 turnover)
        "turnover_rate": pl.Float64,
        "market_cap": pl.Float64,
        "industry_id": pl.Utf8,
        "industry_limit_up_count": pl.Int32,
        "_source": pl.Utf8,
    }


def _empty_limit_pool() -> pl.DataFrame:
    return pl.DataFrame(schema=_limit_pool_schema())


def fetch_limit_pool(client: KphClient, trade_date: date) -> pl.DataFrame:
    """涨停池含原因 (kph_limit_up_history)."""
    try:
        payload = client.get("/api/limit-up/history", params={"date": trade_date.isoformat()})
    except Exception as exc:  # noqa: BLE001
        LOG.warning("kph.limit_up_history failed: %s", exc)
        return _empty_limit_pool()
    rows = []
    for item in (payload.get("data") or []):
        rows.append({
            "symbol": normalize_symbol(item.get("code", "") or item.get("symbol", "")),
            "name": item.get("name"),
            "trade_date": trade_date,
            "limit_count": item.get("limit_count"),
            "limit_time": item.get("limit_time"),
            "plate_id": item.get("plate_id"),
            "plate_name": item.get("plate_name"),
            "seal_amount": item.get("seal_amount"),
            "seal_money": item.get("seal_money"),
            "limit_tag": item.get("limit_tag"),
            "themes": item.get("themes"),
            "reason": item.get("reason"),
            "net_inflow": item.get("net_inflow"),
            "turnover": item.get("turnover") or item.get("amount"),
            "turnover_rate": item.get("turnover_rate"),
            "market_cap": item.get("market_cap"),
            "industry_id": item.get("industry_id"),
            "industry_limit_up_count": item.get("industry_limit_up_count"),
            "_source": "kph.limit_up_history",
        })
    return pl.DataFrame(rows) if rows else _empty_limit_pool()


def fetch_limit_down_pool(client: KphClient, trade_date: date) -> pl.DataFrame:
    """跌停股 (kph_limit_down_history)."""
    try:
        payload = client.get("/api/limit-down/history", params={"date": trade_date.isoformat()})
    except Exception as exc:  # noqa: BLE001
        LOG.warning("kph.limit_down_history failed: %s", exc)
        return pl.DataFrame(schema=_limit_pool_schema())
    rows = []
    for item in (payload.get("data") or []):
        row = {k: item.get(k) for k in _limit_pool_schema() if k != "trade_date"}
        row["symbol"] = normalize_symbol(item.get("code", ""))
        row["trade_date"] = trade_date
        row["_source"] = "kph.limit_down_history"
        rows.append(row)
    return pl.DataFrame(rows) if rows else pl.DataFrame(schema=_limit_pool_schema())


def fetch_limit_ladder(client: KphClient, trade_date: date) -> pl.DataFrame:
    """连板天梯 (kph_limit_ladder)."""
    try:
        payload = client.get("/api/limit-ladder", params={"date": trade_date.isoformat()})
    except Exception as exc:  # noqa: BLE001
        LOG.warning("kph.limit_ladder failed: %s", exc)
        return pl.DataFrame()
    rows = []
    for item in (payload.get("data") or []):
        rows.append({
            "symbol": normalize_symbol(item.get("code", "") or item.get("symbol", "")),
            "name": item.get("name"),
            "trade_date": trade_date,
            "limit_count": item.get("limit_count"),
            "plate_id": item.get("plate_id"),
            "plate_name": item.get("plate_name"),
            "one_word": item.get("one_word"),         # AxData 字段名 (原 is_one_word)
            "popular": item.get("popular"),            # AxData 字段名 (原 is_popular)
            "plate_limit_up_count": item.get("plate_limit_up_count"),  # AxData 新增
            "amount": item.get("amount"),              # 板块级成交额 (AxData kph_limit_ladder)
            "plate_amount": item.get("plate_amount"),  # 板块整体成交额 (AxData)
        })
    return pl.DataFrame(rows) if rows else pl.DataFrame()


def fetch_market_emotion(client: KphClient, trade_date: date) -> pl.DataFrame:
    """市场情绪 (kph_market_emotion)."""
    try:
        payload = client.get("/api/market-emotion", params={"date": trade_date.isoformat()})
    except Exception as exc:  # noqa: BLE001
        LOG.warning("kph.market_emotion failed: %s", exc)
        return pl.DataFrame()
    item = payload.get("data")
    if not item:  # 空 / 缺字段 → 返回空 df, vendor §"缺字段与空数据不启发式补全"
        return pl.DataFrame()
    return pl.DataFrame([{
        "trade_date": trade_date,
        "limit_up_count": item.get("limit_up_count"),
        "limit_down_count": item.get("limit_down_count"),
        "real_limit_up_count": item.get("real_limit_up_count"),
        "real_limit_down_count": item.get("real_limit_down_count"),
        "st_limit_up_count": item.get("st_limit_up_count"),
        "st_limit_down_count": item.get("st_limit_down_count"),
        "rise_count": item.get("rise_count"),
        "fall_count": item.get("fall_count"),
        "flat_count": item.get("flat_count"),
        "market_sign": item.get("market_sign"),
        "total_amount": item.get("total_amount"),
        "_source": "kph.market_emotion",
    }])


def fetch_sector_ranking(client: KphClient, trade_date: date, sector_type: str = "selected") -> pl.DataFrame:
    """板块排行 (kph_sector_ranking). sector_type: selected/industry/region."""
    try:
        payload = client.get(
            "/api/sector/ranking",
            params={"date": trade_date.isoformat(), "type": sector_type},
        )
    except Exception as exc:  # noqa: BLE001
        LOG.warning("kph.sector_ranking failed: %s", exc)
        return pl.DataFrame()
    rows = []
    for item in (payload.get("data") or []):
        rows.append({
            "trade_date": trade_date,
            "plate_id": item.get("plate_id"),
            "plate_name": item.get("plate_name"),
            "sector_type": sector_type,
            "change_pct": item.get("change_pct"),
            "amount": item.get("amount"),
            "amplitude": item.get("amplitude"),
            "net_inflow": item.get("net_inflow"),
            "turnover_rate": item.get("turnover_rate"),
            "market_cap": item.get("market_cap"),
            "stock_count": item.get("stock_count"),
            "_source": f"kph.sector_ranking.{sector_type}",
        })
    return pl.DataFrame(rows) if rows else pl.DataFrame()


def fetch_sector_constituents(client: KphClient, trade_date: date) -> pl.DataFrame:
    """板块成分股 (kph_sector_constituents_history).

    先取板块列表, 再逐板块拉成分股 — vendor 单层 pull 不支持, 故用本脚本。
    """
    try:
        sectors_payload = client.get("/api/sector/list", params={"date": trade_date.isoformat()})
    except Exception as exc:  # noqa: BLE001
        LOG.warning("kph.sector.list failed: %s", exc)
        return pl.DataFrame()
    rows = []
    for sector in (sectors_payload.get("data") or []):
        plate_id = sector.get("plate_id")
        try:
            payload = client.get(
                "/api/sector/constituents",
                params={"plate_id": plate_id, "date": trade_date.isoformat()},
            )
        except Exception as exc:  # noqa: BLE001
            LOG.warning("kph.sector.constituents %s failed: %s", plate_id, exc)
            continue
        for item in (payload.get("data") or []):
            rows.append({
                "trade_date": trade_date,
                "plate_id": plate_id,
                "plate_name": sector.get("plate_name"),
                "symbol": normalize_symbol(item.get("code", "") or item.get("symbol", "")),
                "name": item.get("name"),
                "last_price": item.get("last_price"),
                "change_pct": item.get("change_pct"),
                "amount": item.get("amount"),  # 板块级成交额 (AxData kph_sector_constituents_history)
                "turnover_rate": item.get("turnover_rate"),
                "float_market_value": item.get("float_market_value"),
                "main_net": item.get("main_net"),
                "tags": item.get("tags"),                # AxData 字段名 (原 themes)
                "limit_tag": item.get("limit_tag"),
                "rank_tag": item.get("rank_tag"),        # AxData 新增: 龙虎榜/龙头标签
                "limit_count": item.get("limit_count"),
                "_source": "kph.sector_constituents",
            })
    return pl.DataFrame(rows) if rows else pl.DataFrame()


def fetch_market_events(client: KphClient, trade_date: date) -> pl.DataFrame:
    """盘面梳理事件流 (kph_market_review_events)."""
    try:
        payload = client.get("/api/market/events", params={"date": trade_date.isoformat()})
    except Exception as exc:  # noqa: BLE001
        LOG.warning("kph.market.events failed: %s", exc)
        return pl.DataFrame()
    rows = []
    for item in (payload.get("data") or []):
        rows.append({
            "trade_date": trade_date,
            "event_time": item.get("event_time"),
            "tag_id": item.get("tag_id"),
            "tag_name": item.get("tag_name"),
            "tag_attribute": item.get("tag_attribute"),
            "plate_id": item.get("plate_id"),
            "plate_name": item.get("plate_name"),
            "detail": item.get("detail"),
            "raw_stock_list": item.get("raw_stock_list"),  # AxData 新增: 相关股票列表
            "_source": "kph.market_events",
        })
    return pl.DataFrame(rows) if rows else pl.DataFrame()


# ────────────────── 入口 ──────────────────


def run(
    trade_date: date,
    base_url: str = DEFAULT_BASE_URL,
    data_root: Path = Path("data/extensions/kph"),
) -> dict[str, Path]:
    """跑一次全量抓取. 返回 {snapshot_name: parquet_path}."""
    out_dir = data_root / trade_date.isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)

    client = KphClient(base_url=base_url)
    try:
        results = {
            "limit_pool": fetch_limit_pool(client, trade_date),
            "limit_down_pool": fetch_limit_down_pool(client, trade_date),
            "limit_ladder": fetch_limit_ladder(client, trade_date),
            "market_emotion": fetch_market_emotion(client, trade_date),
            "sector_ranking": fetch_sector_ranking(client, trade_date),
            "sector_constituents": fetch_sector_constituents(client, trade_date),
            "market_events": fetch_market_events(client, trade_date),
        }
    finally:
        client.close()

    out_paths: dict[str, Path] = {}
    for name, df in results.items():
        path = out_dir / f"{name}.parquet"
        if df.is_empty():
            LOG.info("kph.%s empty, skip", name)
            continue
        df.write_parquet(path)
        out_paths[name] = path
        LOG.info("kph.%s → %s (%d rows)", name, path, df.height)

    return out_paths


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="KPH 开盘红数据 fetcher")
    parser.add_argument("--date", required=True, help="交易日 YYYY-MM-DD")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--data-root", default="data/extensions/kph", type=Path)
    args = parser.parse_args(argv)

    trade_date = date.fromisoformat(args.date)
    paths = run(trade_date, base_url=args.base_url, data_root=args.data_root)
    LOG.info("done. %d files written", len(paths))
    return 0


if __name__ == "__main__":
    sys.exit(main())