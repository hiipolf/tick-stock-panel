"""涨停板数据同步服务。

从当前生效的涨停板数据源 (插件 / 自定义源, 声明了 limit_board 数据集) 拉取
涨停池 / 跌停池 / 连板梯队 / 涨停原因 / 板块排行 / 市场情绪, 落盘到
data/core/limit_board.parquet (Parquet 是事实源)。

路由: 走 app.data_providers.custom.loader 的 get_provider / provider_has_dataset,
与 services/financial_sync 的自定义源分流同款 (声明了 limit_board 就接管)。
TickFlow 侧没有这个能力 (能力注册表 tf_provides=False), 路由到 tickflow 直接报错,
不做跨源回退 —— 换源是用户的选择, 静默换源会掩盖配置错误。

落盘: 单文件存全部 (trade_date, board_type) 组合。重跑同一组合先删旧行再写入
(幂等), 其他组合的历史数据保留。

失败语义: 异常向上抛给调用方 (与 get_daily 一致), 不静默吞掉;
上游返回空数据时返回空结果, 且不覆盖已落盘的历史数据 (空响应与源故障不可区分)。
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import polars as pl

from app.data_providers.base import LIMIT_BOARD_SCHEMA, cast_limit_board
from app.services.fs_utils import atomic_write_parquet

logger = logging.getLogger(__name__)

# 判别列 board_type 的合法取值 (与能力注册表同源)
BOARD_TYPES = ("up", "down", "wind_vane", "ladder", "emotion", "sector", "resumption")
# 查询参数 "all" = 合并全部类型 (不含 resumption)
ALL_BOARD_TYPES = ("up", "down", "wind_vane", "ladder", "emotion", "sector")


def limit_board_path() -> Path:
    """涨停板 parquet 落盘路径: data/core/limit_board.parquet。"""
    from app.config import settings

    return settings.data_dir / "core" / "limit_board.parquet"


def _to_date(value: str | date | None) -> date | None:
    """把 YYYY-MM-DD 字符串转成 date; 非法格式抛 ValueError (不静默忽略)。"""
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value).strip())


def _empty_frame() -> pl.DataFrame:
    return pl.DataFrame(schema=LIMIT_BOARD_SCHEMA)


def read_limit_board(
    trade_date: str | date | None = None,
    board_type: str | None = None,
) -> pl.DataFrame:
    """读取已落盘的涨停板数据 (事实源)。文件不存在返回空表。"""
    path = limit_board_path()
    if not path.exists():
        return _empty_frame()
    try:
        df = pl.read_parquet(path)
    except Exception as e:
        logger.warning("涨停板 parquet 读取失败: %s", e)
        return _empty_frame()
    if df.is_empty():
        return _empty_frame()
    # 落盘文件是本服务写的, 形状已是 canonical; 这里再过一次 cast 兜底历史文件
    df = cast_limit_board(df, "up")
    day = _to_date(trade_date)
    if day is not None:
        df = df.filter(pl.col("trade_date") == day)
    if board_type:
        df = df.filter(pl.col("board_type") == str(board_type).strip().lower())
    return df


def _limit_board_sources(custom_sources) -> list[str]:
    """列出当前已注册且声明了 limit_board 数据集的源 (按名字排序)。"""
    out: list[str] = []
    for n in sorted(custom_sources.names()):
        try:
            if custom_sources.provider_has_dataset(n, "limit_board"):
                out.append(n)
        except Exception:
            continue
    return out


def _resolve_provider(provider_name: str | None):
    """定位生效的涨停板数据源; 不可用时抛 ValueError (说明怎么修)。"""
    from app.data_providers import custom as custom_sources
    from app.services import preferences

    name = str(provider_name or preferences.get_limit_board_data_provider() or "").strip().lower()
    if not name or name == "tickflow":
        # TickFlow 不提供 limit_board (能力注册表里 tf_provides=False), 所以偏好默认值
        # 永远指向一个"不提供该能力"的源。若用户在设置页显式切过 (name != tickflow)
        # 就按用户的选择走; 只有"没切过"时才自动回退。
        candidates = _limit_board_sources(custom_sources)
        if len(candidates) == 1:
            name = candidates[0]
            logger.info("涨停板数据源未显式指定, 自动回退到唯一可用源: %s", name)
        elif len(candidates) > 1:
            raise ValueError(
                "涨停板数据源未指定, 且有多个源声明了 limit_board: "
                f"{', '.join(candidates)} —— 请在设置页「数据源」选择"
            )
        else:
            raise ValueError(
                "涨停板数据源未生效: TickFlow 不提供 limit_board 能力, "
                "请在设置页「数据源」把「涨停板」路由到声明了 limit_board 的源 (如 KPH 开盘啦)"
            )
    if not custom_sources.provider_has_dataset(name, "limit_board"):
        raise ValueError(f"数据源 '{name}' 未声明 limit_board 数据集")
    provider = custom_sources.get_provider(name)
    fetch = getattr(provider, "get_limit_board", None)
    if not callable(fetch):
        raise ValueError(f"数据源 '{name}' 未实现 get_limit_board")
    return name, provider


def _merge_and_write(df: pl.DataFrame) -> Path:
    """把新数据并进已有文件: 同 (trade_date, board_type) 的旧行先删, 再整体落盘。"""
    path = limit_board_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _empty_frame()
    if path.exists():
        try:
            existing = cast_limit_board(pl.read_parquet(path), "up")
        except Exception as e:
            logger.warning("涨停板 parquet 读取失败, 将重建: %s", e)
            existing = _empty_frame()
    if not existing.is_empty():
        keys = df.select(["trade_date", "board_type"]).unique()
        existing = existing.join(keys, on=["trade_date", "board_type"], how="anti")
    merged = pl.concat([existing, df], how="diagonal_relaxed") if not existing.is_empty() else df
    atomic_write_parquet(merged, path)
    return path


def sync_limit_board(
    trade_date: str | None = None,
    board_type: str = "up",
    provider_name: str | None = None,
) -> dict:
    """拉取涨停板数据并落盘, 返回本次同步的结果摘要。

    参数:
      - trade_date: 交易日 YYYY-MM-DD, 缺省由上游取最新;
      - board_type: up/down/wind_vane/ladder/emotion/sector/resumption/all;
      - provider_name: 指定源名, 缺省用偏好里的生效源。

    失败语义: 异常向上抛给调用方 (与 get_daily 一致)。
    """
    name, provider = _resolve_provider(provider_name)
    wanted = str(board_type or "up").strip().lower()
    if wanted != "all" and wanted not in BOARD_TYPES:
        raise ValueError(
            f"board_type '{board_type}' 非法, 可选: {'/'.join(BOARD_TYPES)}/all"
        )

    df = provider.get_limit_board(trade_date=trade_date, board_type=wanted)
    # provider 侧已按契约返回 canonical 形状, 这里再过一次 cast 兜底 (插件自实现)
    day = _to_date(trade_date)
    df = cast_limit_board(df, wanted, day.isoformat() if day else None)
    if df.is_empty():
        logger.warning("涨停板同步: 源 '%s' 返回空数据 (board_type=%s), 保留已落盘数据", name, wanted)
        return {
            "provider": name,
            "trade_date": trade_date,
            "board_type": wanted,
            "rows": 0,
            "columns": list(LIMIT_BOARD_SCHEMA),
            "preview": [],
            "path": str(limit_board_path()),
            "written": False,
        }

    path = _merge_and_write(df)
    logger.info(
        "涨停板同步完成: 源=%s board_type=%s 新增 %d 行 -> %s",
        name, wanted, df.height, path,
    )
    return {
        "provider": name,
        "trade_date": trade_date,
        "board_type": wanted,
        "rows": df.height,
        "columns": list(df.columns),
        "preview": df.head(5).to_dicts(),
        "path": str(path),
        "written": True,
    }
