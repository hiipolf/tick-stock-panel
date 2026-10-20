"""盘面事件数据同步服务。

从当前生效的盘面事件数据源 (插件 / 自定义源, 声明了 market_events 数据集)
拉取盘中异动事件流 (涨停复盘 / 一字板 / 炸板回封等标签), 落盘到
data/core/market_events.parquet (Parquet 是事实源)。

与 limit_board 的区别: market_events 只有一块板 (不分 board_type —— 那是
limit_board 的概念), 所以合并键只有 trade_date 一个, 事件本身靠 tag_id +
event_time 区分。

路由: 走 app.data_providers.custom.loader 的 get_provider / provider_has_dataset,
与 services/limit_board_sync 同款 (声明了 market_events 就接管)。
TickFlow 侧没有这个能力 (能力注册表 tf_provides=False), 路由到 tickflow 直接报错,
不做跨源回退 —— 换源是用户的选择, 静默换源会掩盖配置错误。

落盘: 单文件存全部交易日。重跑同一交易日先删旧行再写入 (幂等),
其它交易日的历史数据保留。

失败语义: 异常向上抛给调用方 (与 get_daily 一致), 不静默吞掉;
上游返回空数据时返回空结果, 且不覆盖已落盘的历史数据 (空响应与源故障不可区分)。
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import polars as pl

from app.data_providers.base import MARKET_EVENTS_SCHEMA, cast_market_events
from app.services.fs_utils import atomic_write_parquet

logger = logging.getLogger(__name__)

# 单次拉取的事件条数上限 (与 provider.get_market_events 的默认分页一致)
DEFAULT_EVENT_LIMIT = 100


def market_events_path() -> Path:
    """盘面事件 parquet 落盘路径: data/core/market_events.parquet。"""
    from app.config import settings

    return settings.data_dir / "core" / "market_events.parquet"


def _to_date(value: str | date | None) -> date | None:
    """把 YYYY-MM-DD 字符串转成 date; 非法格式抛 ValueError (不静默忽略)。"""
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value).strip())


def _empty_frame() -> pl.DataFrame:
    return pl.DataFrame(schema=MARKET_EVENTS_SCHEMA)


def read_market_events(
    trade_date: str | date | None = None,
) -> pl.DataFrame:
    """读取已落盘的盘面事件数据 (事实源)。文件不存在返回空表。"""
    path = market_events_path()
    if not path.exists():
        return _empty_frame()
    try:
        df = pl.read_parquet(path)
    except Exception as e:
        logger.warning("盘面事件 parquet 读取失败: %s", e)
        return _empty_frame()
    if df.is_empty():
        return _empty_frame()
    # 落盘文件是本服务写的, 形状已是 canonical; 这里再过一次 cast 兜底历史文件
    df = cast_market_events(df)
    day = _to_date(trade_date)
    if day is not None:
        df = df.filter(pl.col("trade_date") == day)
    return df


def _market_events_sources(custom_sources) -> list[str]:
    """列出当前已注册且声明了 market_events 数据集的源 (按名字排序)。"""
    out: list[str] = []
    for n in sorted(custom_sources.names()):
        try:
            if custom_sources.provider_has_dataset(n, "market_events"):
                out.append(n)
        except Exception:  # 单个源探测失败不影响其他源
            continue
    return out


def _resolve_provider(provider_name: str | None):
    """定位生效的盘面事件数据源; 不可用时抛 ValueError (说明怎么修)。"""
    from app.data_providers import custom as custom_sources
    from app.services import preferences

    name = str(provider_name or preferences.get_market_events_data_provider() or "").strip().lower()
    if not name or name == "tickflow":
        # TickFlow 不提供 market_events (能力注册表里 tf_provides=False), 所以偏好默认值
        # 永远指向一个"不提供该能力"的源。若用户在设置页显式切过 (name != tickflow)
        # 就按用户的选择走; 只有"没切过"时才自动回退。
        candidates = _market_events_sources(custom_sources)
        if len(candidates) == 1:
            name = candidates[0]
            logger.info("盘面事件数据源未显式指定, 自动回退到唯一可用源: %s", name)
        elif len(candidates) > 1:
            raise ValueError(
                "盘面事件数据源未指定, 且有多个源声明了 market_events: "
                f"{', '.join(candidates)} —— 请在设置页「数据源」选择"
            )
        else:
            raise ValueError(
                "盘面事件数据源未生效: TickFlow 不提供 market_events 能力, "
                "请在设置页「数据源」把「盘面事件」路由到声明了 market_events 的源 (如 KPH 开盘啦)"
            )
    if not custom_sources.provider_has_dataset(name, "market_events"):
        raise ValueError(f"数据源 '{name}' 未声明 market_events 数据集")
    provider = custom_sources.get_provider(name)
    fetch = getattr(provider, "get_market_events", None)
    if not callable(fetch):
        raise ValueError(f"数据源 '{name}' 未实现 get_market_events")
    return name, provider


def _merge_and_write(df: pl.DataFrame) -> Path:
    """把新数据并进已有文件: 同 trade_date 的旧行先删, 再整体落盘。"""
    path = market_events_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _empty_frame()
    if path.exists():
        try:
            existing = cast_market_events(pl.read_parquet(path))
        except Exception as e:
            logger.warning("盘面事件 parquet 读取失败, 将重建: %s", e)
            existing = _empty_frame()
    if not existing.is_empty():
        keys = df.select(["trade_date"]).unique()
        existing = existing.join(keys, on=["trade_date"], how="anti")
    merged = pl.concat([existing, df], how="diagonal_relaxed") if not existing.is_empty() else df
    atomic_write_parquet(merged, path)
    return path


def sync_market_events(
    trade_date: str | None = None,
    limit: int = DEFAULT_EVENT_LIMIT,
    offset: int = 0,
    provider_name: str | None = None,
) -> dict:
    """拉取盘面事件数据并落盘, 返回本次同步的结果摘要。

    参数:
      - trade_date: 交易日 YYYY-MM-DD, 缺省由上游取最新;
      - limit/offset: 分页 (一次取多少条 / 从第几条开始);
      - provider_name: 指定源名, 缺省用偏好里的生效源。

    失败语义: 异常向上抛给调用方 (与 get_daily 一致)。
    """
    name, provider = _resolve_provider(provider_name)
    df = provider.get_market_events(
        trade_date=trade_date,
        limit=limit,
        offset=offset,
    )
    # provider 侧已按契约返回 canonical 形状, 这里再过一次 cast 兜底 (插件自实现)
    day = _to_date(trade_date)
    df = cast_market_events(df, day.isoformat() if day else None)
    if df.is_empty():
        logger.warning("盘面事件同步: 源 '%s' 返回空数据, 保留已落盘数据", name)
        return {
            "provider": name,
            "trade_date": trade_date,
            "limit": limit,
            "offset": offset,
            "rows": 0,
            "columns": list(MARKET_EVENTS_SCHEMA),
            "preview": [],
            "path": str(market_events_path()),
            "written": False,
        }

    path = _merge_and_write(df)
    logger.info(
        "盘面事件同步完成: 源=%s 新增 %d 行 -> %s",
        name, df.height, path,
    )
    return {
        "provider": name,
        "trade_date": trade_date,
        "limit": limit,
        "offset": offset,
        "rows": df.height,
        "columns": list(df.columns),
        "preview": df.head(5).to_dicts(),
        "path": str(path),
        "written": True,
    }
