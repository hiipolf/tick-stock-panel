"""数据源能力 API — 涨停板/盘面事件等「新增标准数据集」的只读查询与同步。

限价板 (limit_board) 是本仓新增的第 8 个标准数据集, 与其他 7 个不同:
它不是一个按 symbol 拉取的时间序列, 而是全市场截面的「板」——
涨停池 / 跌停池 / 连板梯队 / 涨停原因 / 板块排行 / 市场情绪。
盘面事件 (market_events) 是第 9 个标准数据集, 同样不是时间序列:
它是盘中异动事件流 (涨停复盘 / 一字板 / 炸板回封等标签),
一条事件可关联多只个股, 语义上是「事件」而不是「板」。
因此单独开一个 router (前缀 /api/data-providers), 不混进 /api/settings。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query

from app.services import limit_board_sync, market_events_sync

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/data-providers", tags=["data-providers"])

# 预览行数 (与 provider.test_dataset 的回显口径一致)
_PREVIEW_ROWS = 5


def _payload(df, **extra) -> dict:
    """统一响应体: rows (总数) / columns (列名) / preview (前几行) / data (全部行)。"""
    payload = {
        "rows": df.height,
        "columns": list(df.columns),
        "preview": df.head(_PREVIEW_ROWS).to_dicts(),
        "data": df.to_dicts(),
    }
    payload.update(extra)
    return payload


@router.get("/limit-board")
def get_limit_board(
    trade_date: str | None = Query(None, description="交易日 YYYY-MM-DD, 缺省为全部已落盘交易日"),
    board_type: str | None = Query(None, description="板型 up/down/wind_vane/ladder/emotion/sector/resumption, 缺省为全部"),
) -> dict:
    """读取已落盘的涨停板数据 (data/core/limit_board.parquet 是事实源)。

    只读, 不触发上游拉取。尚未同步过时返回 {error}。
    """
    try:
        df = limit_board_sync.read_limit_board(trade_date=trade_date, board_type=board_type)
    except ValueError as e:
        return {"error": f"涨停板查询参数非法: {e}"}
    except Exception as e:
        logger.warning("涨停板读取失败: %s", e)
        return {"error": f"涨停板数据读取失败: {e}"}
    if df.is_empty():
        return {
            "error": "涨停板数据尚未同步, 请先调用 POST /api/data-providers/limit-board/sync",
            "rows": 0,
            "columns": [],
            "preview": [],
        }
    return _payload(df, trade_date=trade_date, board_type=board_type)


@router.post("/limit-board/sync")
def sync_limit_board(
    trade_date: str | None = Query(None, description="交易日 YYYY-MM-DD, 缺省由上游取最新"),
    board_type: str = Query("up", description="板型 up/down/wind_vane/ladder/emotion/sector/resumption/all"),
    provider: str | None = Query(None, description="指定数据源名, 缺省用偏好里的生效源"),
) -> dict:
    """拉取涨停板数据并落盘 (同 (trade_date, board_type) 重跑覆盖)。

    失败按框架惯例抛 HTTPException (400), 由调用方处理。
    """
    try:
        result = limit_board_sync.sync_limit_board(
            trade_date=trade_date,
            board_type=board_type,
            provider_name=provider,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.warning("涨停板同步失败: %s", e)
        raise HTTPException(status_code=502, detail=f"涨停板同步失败: {e}") from e
    return result


@router.get("/market-events")
def get_market_events(
    trade_date: str | None = Query(None, description="交易日 YYYY-MM-DD, 缺省为全部已落盘交易日"),
) -> dict:
    """读取已落盘的盘面事件数据 (data/core/market_events.parquet 是事实源)。

    只读, 不触发上游拉取。尚未同步过时返回 {error}。
    """
    try:
        df = market_events_sync.read_market_events(trade_date=trade_date)
    except ValueError as e:
        return {"error": f"盘面事件查询参数非法: {e}"}
    except Exception as e:
        logger.warning("盘面事件读取失败: %s", e)
        return {"error": f"盘面事件数据读取失败: {e}"}
    if df.is_empty():
        return {
            "error": "盘面事件数据尚未同步, 请先调用 POST /api/data-providers/market-events/sync",
            "rows": 0,
            "columns": [],
            "preview": [],
        }
    return _payload(df, trade_date=trade_date)


@router.post("/market-events/sync")
def sync_market_events(
    trade_date: str | None = Query(None, description="交易日 YYYY-MM-DD, 缺省由上游取最新"),
    limit: int = Query(100, ge=1, le=2000, description="单次拉取的事件条数"),
    offset: int = Query(0, ge=0, description="分页偏移 (从第几条开始)"),
    provider: str | None = Query(None, description="指定数据源名, 缺省用偏好里的生效源"),
) -> dict:
    """拉取盘面事件数据并落盘 (同 trade_date 重跑覆盖)。

    失败按框架惯例抛 HTTPException (400), 由调用方处理。
    """
    try:
        result = market_events_sync.sync_market_events(
            trade_date=trade_date,
            limit=limit,
            offset=offset,
            provider_name=provider,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.warning("盘面事件同步失败: %s", e)
        raise HTTPException(status_code=502, detail=f"盘面事件同步失败: {e}") from e
    return result
