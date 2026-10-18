"""IRP HTTP 路由 — P2 v2.0b。

P0 端点:
  GET  /api/custom/research/status
  GET  /api/custom/research/hello

P1.1 端点:
  POST /api/custom/research/limit-up-pool
  POST /api/custom/research/limit-up-ladder
  POST /api/custom/research/theme-index-kline
  POST /api/custom/research/theme-attribution
  POST /api/custom/research/theme-phase
  GET  /api/custom/research/tools

P2 v2.0b 新增 (聚合端点):
  POST /api/custom/research/short-term-overview
        入参: {trade_date, quotes, prev_pool, exploded_count, history_30d?, industry_map?}
        出参: {emotion_score, phase, secondary_indicators, deep_metrics,
               promotion_by_board, theme_heat, stats}
"""
from __future__ import annotations

import concurrent.futures
import os
import threading
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.custom.research import tools as irp_tools
from app.custom.research.short_term import Quote, LimitUpItem, aggregate_limit_up_pool, build_ladder
from app.custom.research.short_term_v2 import (
    compute_deep_metrics,
    compute_emotion_score,
    compute_promotion_by_board,
    compute_secondary_indicators,
    compute_theme_heat,
    judge_emotion_phase,
)
from app.custom.research.chain import load_theme_chain

# TSP 后端自调基址 (dev 默认 127.0.0.1:3018)。真实部署可被环境变量覆盖。
_BACKEND_SELF_BASE = os.environ.get("TSP_SELF_BASE", "http://127.0.0.1:3018")
_TIMEOUT = 20.0

# v2.0i: limit-ladder per-date cache.
# 历史交易日数据不变 → 24h TTL 足够. 单 uvicorn worker 进程内有效 (多 worker 各持一份).
# 实测 90 日首次 fetch ~25s+ 同 worker 串行递归, 加 cache 后再次调用 < 1s.
_LADDER_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_LADDER_CACHE_LOCK = threading.Lock()
_LADDER_CACHE_TTL_SEC = 24 * 3600
_LADDER_CACHE_MAX = 512  # LRU 软上限 (按 as_of 字符串 FIFO 裁剪)


# === Request/Response 模型 ===
class QuoteModel(BaseModel):
    symbol: str
    name: str
    trade_date: str
    prev_close: float
    last_close: float
    high: float = 0.0
    low: float = 0.0
    volume: float = 0.0
    amount: float = 0.0
    industry: str = ""
    theme_tags: list[str] = Field(default_factory=list)
    limit_time: str | None = None
    is_exploded: bool = False
    is_limit_down: bool = False


class PrevPoolItem(BaseModel):
    symbol: str
    board_count: int


class LimitUpPoolRequest(BaseModel):
    trade_date: str
    quotes: list[QuoteModel]
    prev_pool: list[PrevPoolItem] | None = None


class LimitUpItemModel(BaseModel):
    symbol: str
    name: str = ""
    trade_date: str = ""
    limit_time: str | None = None
    first_limit_price: float = 0.0
    last_close: float = 0.0
    turnover: float = 0.0
    amount: float = 0.0
    industry: str = ""
    theme_tags: list[str] = Field(default_factory=list)
    board_count: int


class LimitUpLadderRequest(BaseModel):
    items: list[LimitUpItemModel]


class StockBarModel(BaseModel):
    date: str
    close: float
    volume: float = 0.0
    amount: float = 0.0


class ThemeIndexKlineRequest(BaseModel):
    theme_id: str
    freq: str = "1d"  # 1d / 1w / 1m
    stocks: dict[str, list[StockBarModel]]


class ThemeAttributionRequest(BaseModel):
    theme_id: str
    change_pct_map: dict[str, float]


class ThemeSnapshotModel(BaseModel):
    theme_id: str
    theme_name: str
    trade_date: str
    change_pct: float
    width: float
    consecutive_days: int
    rank: int


class ThemePhaseRequest(BaseModel):
    today: ThemeSnapshotModel
    history_30d: list[ThemeSnapshotModel]


# === P2 v2.0b 聚合端点 ===
class ShortTermOverviewRequest(BaseModel):
    trade_date: str
    quotes: list[QuoteModel]
    prev_pool: list[PrevPoolItem] | None = None
    exploded_count: int = 0
    history_30d: list[dict] | None = None
    industry_map: dict[str, int] | None = None


# === 路由构造 ===
def build_router() -> APIRouter:
    """构造 FastAPI 路由。"""
    router = APIRouter(prefix="/api/custom/research", tags=["research"])

    @router.get("/status")
    def status() -> dict:
        """健康检查 + 当前 phase 进度。"""
        return {
            "status": "ok",
            "id": "research.studio",
            "phase": "P1.1-tools-and-tests",
            "version": "0.1.0",
            "tools": [t["function"]["name"] for t in irp_tools.list_tools()],
        }

    @router.get("/hello")
    def hello(name: str = "world") -> dict:
        """P0 测试端点 (保留)。"""
        return {"message": f"hello, {name}!", "from": "research.studio"}

    # === 4 工具 HTTP 端点 ===
    @router.post("/limit-up-pool")
    def limit_up_pool(req: LimitUpPoolRequest) -> dict[str, Any]:
        """涨停池聚合端点 (短路版 1.0: 调用方传 quotes, IRP 内部算)。"""
        args = {
            "trade_date": req.trade_date,
            "quotes": [q.model_dump() for q in req.quotes],
            "prev_pool": [p.model_dump() for p in req.prev_pool] if req.prev_pool else None,
        }
        result = irp_tools.execute_tool("get_limit_up_pool", args)
        if not result["ok"]:
            raise HTTPException(status_code=400, detail=result["error"])
        return result

    @router.post("/limit-up-ladder")
    def limit_up_ladder(req: LimitUpLadderRequest) -> dict[str, Any]:
        """连板梯队分组。"""
        args = {"items": [it.model_dump() for it in req.items]}
        result = irp_tools.execute_tool("get_limit_up_ladder", args)
        if not result["ok"]:
            raise HTTPException(status_code=400, detail=result["error"])
        return result

    @router.post("/theme-index-kline")
    def theme_index_kline(req: ThemeIndexKlineRequest) -> dict[str, Any]:
        """题材指数 K 线。"""
        args = {
            "theme_id": req.theme_id,
            "freq": req.freq,
            "stocks": {sym: [b.model_dump() for b in bars]
                       for sym, bars in req.stocks.items()},
        }
        result = irp_tools.execute_tool("get_theme_index_kline", args)
        if not result["ok"]:
            raise HTTPException(status_code=400, detail=result["error"])
        return result

    @router.post("/theme-attribution")
    def theme_attribution(req: ThemeAttributionRequest) -> dict[str, Any]:
        """产业链归因 (读 IRP data/theme_chain.json)。"""
        args = {
            "theme_id": req.theme_id,
            "change_pct_map": dict(req.change_pct_map),
        }
        result = irp_tools.execute_tool("get_theme_attribution", args)
        if not result["ok"]:
            raise HTTPException(status_code=400, detail=result["error"])
        return result

    @router.post("/theme-phase")
    def theme_phase(req: ThemePhaseRequest) -> dict[str, Any]:
        """5 阶段判定 (内部辅助, 也可消费)。"""
        try:
            phase = irp_tools.call_judge_phase(
                req.today.model_dump(),
                [h.model_dump() for h in req.history_30d],
            )
            return {"ok": True, "phase": phase}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"{type(exc).__name__}: {exc}") from exc

    # === P2 v2.0b 聚合端点 ===
    @router.post("/short-term-overview")
    def short_term_overview(req: ShortTermOverviewRequest) -> dict[str, Any]:
        """短线连板综合概览 — 一次返回 6 算法结果.

        净室重写: 字段名借鉴 easy-stock, 公式 IRP 自创.
        """
        try:
            # 1. 涨停池 (复用 v1 算法)
            quotes = [
                Quote(
                    symbol=q.symbol, name=q.name, trade_date=q.trade_date,
                    prev_close=q.prev_close, last_close=q.last_close,
                    high=q.high, low=q.low, volume=q.volume, amount=q.amount,
                    industry=q.industry, theme_tags=q.theme_tags,
                    limit_time=q.limit_time,
                    is_exploded=q.is_exploded, is_limit_down=q.is_limit_down,
                )
                for q in req.quotes
            ]
            # 2. prev_pool 转 dict (算法期望 list[dict])
            prev_pool_dicts = [p.model_dump() for p in req.prev_pool] if req.prev_pool else None

            items, stats = aggregate_limit_up_pool(quotes, prev_pool_dicts)
            ladder = build_ladder(items)

            # 3. 6 个 v2 算法
            emotion = compute_emotion_score(items, req.history_30d, req.industry_map)
            phase = judge_emotion_phase(emotion, req.history_30d)
            secondary = compute_secondary_indicators(items, prev_pool_dicts)
            deep = compute_deep_metrics(items, prev_pool_dicts, req.industry_map, req.exploded_count)
            promo = compute_promotion_by_board(items, prev_pool_dicts)

            # 3. theme_heat 需加载 theme_chain.json
            theme_heat = []
            chain_data = _load_theme_chains()
            if chain_data:
                theme_heat = compute_theme_heat(chain_data, items, prev_pool_dicts)

            return {
                "ok": True,
                "result": {
                    "trade_date": req.trade_date,
                    "emotion_score": emotion,
                    "phase": phase,
                    "secondary_indicators": {
                        "high_pos_avg": secondary.high_pos_avg,
                        "decline_coverage": secondary.decline_coverage,
                        "high_collapse": secondary.high_collapse,
                        "high_promotion": secondary.high_promotion,
                    },
                    "deep_metrics": {
                        "final_explode_rate": deep.final_explode_rate,
                        "prev_limit_up_feedback": deep.prev_limit_up_feedback,
                        "prev_consecutive_feedback": deep.prev_consecutive_feedback,
                        "promotion_by_board_overall": deep.promotion_by_board_overall,
                        "main_line_concentration": deep.main_line_concentration,
                        "high_risk_score": deep.high_risk_score,
                    },
                    "promotion_by_board": [
                        {
                            "from_board": s.from_board,
                            "to_label": s.to_label,
                            "promoted": s.promoted,
                            "total": s.total,
                            "ratio": s.ratio,
                        }
                        for s in promo
                    ],
                    "theme_heat": [
                        {
                            "theme_id": h.theme_id,
                            "theme_name": h.theme_name,
                            "max_board": h.max_board,
                            "limit_up_count": h.limit_up_count,
                            "consecutive_count": h.consecutive_count,
                            "heat_score": h.heat_score,
                        }
                        for h in theme_heat
                    ],
                    "items": [it.to_dict() for it in items],
                    "ladder": [l.to_dict() for l in ladder],
                    "stats": {
                        "limit_up_total": len(items),
                        "limit_down_total": stats["limit_down_total"],
                        "exploded_total": stats["exploded_total"],
                        "re_seal_count": sum(1 for it in items if it.last_close >= it.first_limit_price - 0.01),
                        "max_board": max((it.board_count for it in items), default=0),
                        "seal_rate": (sum(1 for it in items if it.limit_time) / len(items)) if items else 0.0,
                        "ladder_count": len(ladder),
                    },
                },
            }
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"{type(exc).__name__}: {exc}") from exc

    # === 工具发现端点 ===
    @router.get("/tools")
    def list_tools() -> dict[str, Any]:
        """列出所有可用工具的 JSON Schema。

        用途: 前端调试 / 后续 P4 AI 集成时让 LLM 看 schema 自己选工具。
        """
        return {"tools": irp_tools.list_tools()}

    # === P2 v2.0c 真实数据接入端点 ===
    @router.get("/short-term-real")
    def short_term_real(
        as_of: str | None = Query(
            default=None,
            description="指定日期 (YYYY-MM-DD); 不传则用 limit-ladder 默认 (最近一个交易日)",
        ),
        ext_columns: str | None = Query(
            default="ext_gn_ths.所属概念,ext_hy_ths.所属同花顺行业",
            description="透传给 limit-ladder 的 ext_columns 参数,用于个股带概念/行业 tag",
        ),
        include_ladder: bool = Query(
            default=True,
            description="是否在 result 里附带 limit-ladder 完整 tiers (含 status/is_one_word/sealed/ext 概念行业), 供前端卡片渲染",
        ),
    ) -> dict[str, Any]:
        """短线连板真实数据版 — 自调 TSP screener 拉涨停梯队 + 市场快照, 调 6 算法.

        数据流: TSP /api/screener/limit-ladder?as_of=... (连板梯队 + 个股)
              + TSP /api/screener/market-snapshot (prev_close/industry enrichment)
              → IRP Quote[] 构造 → aggregate_limit_up_pool + 6 v2 算法
              → 同 /short-term-overview 的 result schema + 可选 ladder 原始数据

        与短路版差异:
          - 不需要 caller 传 quotes, 完全由后端拉取
          - prev_pool 从 limit-ladder.consecutive_limit_ups 反推 (limit_up 且 boards≥2 → boards-1)
          - 默认带 ext_gn_ths/ext_hy_ths, 让个股带概念/行业 tag (limit-ladder 卡片风格)
          - include_ladder=true 时附 ladder_raw, 前端渲染卡片网格无需二次请求
          - as_of 透传给 limit-ladder, 支持查看历史交易日数据
        """
        try:
            ladder_params: dict[str, Any] = {}
            if as_of:
                ladder_params["as_of"] = as_of
            if ext_columns:
                ladder_params["ext_columns"] = ext_columns
            with httpx.Client(timeout=_TIMEOUT) as cli:
                # v2.0i: 走带 cache 的 helper (as_of 作为 key, 24h TTL)
                ladder = _fetch_limit_ladder_cached(
                    as_of=as_of, ext_columns=ext_columns, client=cli
                )
                sr = cli.get(
                    f"{_BACKEND_SELF_BASE}/api/screener/market-snapshot",
                    params={"limit": 6000, **({"as_of": as_of} if as_of else {})},
                )
                sr.raise_for_status()
                snapshot_data = sr.json()
            snapshot = {
                row.get("symbol"): row
                for row in (snapshot_data.get("rows") or [])
                if row.get("symbol")
            }
            trade_date = ladder.get("as_of") or "1970-01-01"
            quotes, prev_pool, industry_map = _build_quotes_from_ladder(ladder, snapshot)
            if not quotes:
                raise HTTPException(
                    status_code=404,
                    detail=f"no quotes returned by limit-ladder at {trade_date}",
                )
            result = _aggregate_overview(
                quotes, trade_date,
                prev_pool=prev_pool,
                industry_map=industry_map or None,  # 空 map 让函数兜底派生
            )
            if include_ladder:
                result["ladder_raw"] = {
                    "as_of": ladder.get("as_of"),
                    "counts": ladder.get("counts"),
                    "counts_raw": ladder.get("counts_raw"),
                    "sealed_ready": ladder.get("sealed_ready"),
                    "sealed_counts": ladder.get("sealed_counts"),
                    "sealed_counts_up": ladder.get("sealed_counts_up"),
                    "sealed_counts_down": ladder.get("sealed_counts_down"),
                    "tiers": ladder.get("tiers"),
                }
            return {
                "ok": True,
                "result": result,
                "source": {
                    "ladder_url": f"{_BACKEND_SELF_BASE}/api/screener/limit-ladder",
                    "snapshot_url": f"{_BACKEND_SELF_BASE}/api/screener/market-snapshot",
                    "quote_count": len(quotes),
                    "trade_date": trade_date,
                    "prev_pool_size": len(prev_pool),
                    "ext_columns": ext_columns,
                    "as_of_requested": as_of,
                },
            }
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}") from exc

    # === P2 v2.0f 20 日历史 (5 曲线时间轴, 自动跳过非交易日) ===
    @router.get("/short-term-history")
    def short_term_history(
        as_of: str | None = Query(
            default=None,
            description="结束日期 (YYYY-MM-DD); 不传则用今天",
        ),
        days: int = Query(
            default=90,
            ge=2,
            le=120,
            description="历史交易日数 (排除周末/节假日), 默认 90, 上限 120",
        ),
    ) -> dict[str, Any]:
        """过去 N 个交易日每日 stats — 用于 EmotionTimeline 5 曲线图.

        自动跳过非交易日: 倒推 cursor 时, limit-ladder 返空数据 (no stocks /
        counts=0) 视为休市, 继续往前推一天, 直到凑够 N 个有效交易日。

        实际可能跨 (N + N/3) 个日历日 (周末 + 节假日 + 数据缺失期)。

        数据流: 对 N 个交易日并发拉 limit-ladder, 聚合:
          - limit_up / limit_down: counts_raw.up / counts_raw.down
          - broken / failed / recovery / is_one_word: 从 tiers[].stocks[].status 统计
          - max_board: max(consecutive_limit_ups)
          - limit_up_main / star / chinext / bj: 分板统计

        v2.0i 性能优化:
          - 默认 20 → 90 (前端 EmotionTimeline 现在显示半年曲线)
          - 上限 60 → 120 (允许拉到约半年日历跨度, 等价 90+ 缓冲)
          - 共享 httpx.Client 在所有并发 worker 之间复用 TCP keep-alive 连接池
            (顺序 HTTP/1.1 + Connection: keep-alive 头; 不强制 HTTP/2)
          - ThreadPoolExecutor max_workers 8 → 16, 90 日请求量下并发吞吐临界点
        """
        try:
            end_date = date.fromisoformat(as_of) if as_of else date.today()

            # v2.0i: 共享 httpx.Client (含 HTTP/1.1 keep-alive) - 所有 worker 复用 TCP 连接池.
            _shared_cli = httpx.Client(
                timeout=_TIMEOUT,
                headers={"Connection": "keep-alive"},
            )

            def _fetch_one(d: date) -> tuple[date, dict[str, Any]]:
                """拉某天的 limit-ladder 数据; 返 (date, dict).

                v2.0i: cache 优先; 命中走字典读, 未命中才走 HTTP.
                """
                d_str = d.isoformat()
                # 先查 cache; 命中直接走后续解析 (跳过 HTTP 往返)
                cache_key = f"{d_str}|"
                now = time.monotonic()
                with _LADDER_CACHE_LOCK:
                    hit = _LADDER_CACHE.get(cache_key)
                body_cached = hit if hit and (now - hit[0]) < _LADDER_CACHE_TTL_SEC else None
                if body_cached is not None:
                    body = body_cached[1]
                    r_ok = True
                else:
                    r = _shared_cli.get(
                        f"{_BACKEND_SELF_BASE}/api/screener/limit-ladder",
                        params={"as_of": d_str},
                    )
                    if r.status_code != 200:
                        return d, {"date": d_str, "ok": False}
                    body = r.json()
                    r_ok = True
                    # 写 cache (后续同 date 直接命中)
                    with _LADDER_CACHE_LOCK:
                        if len(_LADDER_CACHE) >= _LADDER_CACHE_MAX:
                            drop_n = max(1, _LADDER_CACHE_MAX // 20)
                            for old_key in list(_LADDER_CACHE.keys())[:drop_n]:
                                _LADDER_CACHE.pop(old_key, None)
                        _LADDER_CACHE[cache_key] = (time.monotonic(), body)
                    if not r_ok:
                        return d, {"date": d_str, "ok": False}
                counts = body.get("counts") or {}
                counts_raw = body.get("counts_raw") or counts
                tiers = body.get("tiers") or []
                broken = failed = recovery = is_one_word = 0
                main_count = star_count = chinext_count = bj_count = 0
                max_board = 0
                for tier in tiers:
                    for st in (tier.get("stocks") or []):
                        status = str(st.get("status") or "")
                        sym = str(st.get("symbol") or "")
                        if st.get("is_one_word"):
                            is_one_word += 1
                        if status == "broken":
                            broken += 1
                        elif status == "failed":
                            failed += 1
                        elif status == "recovery":
                            recovery += 1
                        boards = int(st.get("boards") or st.get("consecutive_limit_ups") or 0)
                        if boards > max_board:
                            max_board = boards
                        if sym.endswith(".SH") and not sym.startswith("688") and sym.startswith(("600", "601", "603", "605", "900")):
                            if status == "limit_up" or not status:
                                main_count += 1
                        elif sym.endswith(".SZ") and sym.startswith(("000", "001", "002", "003")):
                            if status == "limit_up" or not status:
                                main_count += 1
                        elif sym.startswith(("300", "301")):
                            if status == "limit_up" or not status:
                                chinext_count += 1
                        elif sym.startswith("688"):
                            if status == "limit_up" or not status:
                                star_count += 1
                        elif sym.endswith(".BJ"):
                            if status == "limit_up" or not status:
                                bj_count += 1
                # 判定是否交易日: 任一涨跌停事件 > 0 (limit-ladder 对缺失日可能返 200
                # 但 counts 全 0 + stocks 全 status=broken/failed, 不算真实交易日)
                up = int(counts_raw.get("up", 0))
                down = int(counts_raw.get("down", 0))
                is_trading_day = (up + down + broken + failed + recovery) > 0
                return d, {
                    "date": d_str,
                    "is_trading_day": is_trading_day,
                    "limit_up": int(counts_raw.get("up", 0)),
                    "limit_down": int(counts_raw.get("down", 0)),
                    "broken": broken,
                    "failed": failed,
                    "recovery": recovery,
                    "is_one_word": is_one_word,
                    "max_board": max_board,
                    "limit_up_main": main_count,
                    "limit_up_star": star_count,
                    "limit_up_chinext": chinext_count,
                    "limit_up_bj": bj_count,
                    "ok": True,
                }

            # 倒推收集 N 个有效交易日 (跳过非交易日 + 数据缺失日, 按时间正序)
            # 数据缺失也算非交易日: TSP enriched 未覆盖到的早期日期 (返 200 但 limit_up=0)
            # 最多往前推 days*5 + 14 个日历日 (节流); 凑不够就返回能拿到的, 不硬往前凑
            day_results: list[dict[str, Any]] = []
            skipped_non_trading = 0
            max_lookback_days = days * 5 + 14
            segment_end = end_date
            for _segment in range(2):
                if len(day_results) >= days:
                    break
                # 本段拉 max_lookback_days 个候选 (倒序收集, 反转成时间正序)
                target_dates: list[date] = []
                cursor = segment_end
                for _ in range(max_lookback_days):
                    target_dates.append(cursor)
                    cursor = cursor - timedelta(days=1)
                target_dates.reverse()  # 早 → 晚

                # 并发拉 — v2.0i: 8 → 16 (90 日请求量下临界吞吐平衡点)
                with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
                    raw_results = list(ex.map(_fetch_one, target_dates))

                # 从最晚 (raw_results[-1]) 倒数, 收 N 个 trading day,
                # 最后再 reverse 回早→晚, 保证返回最新 N 个
                collected: list[dict[str, Any]] = []
                for _d, payload in reversed(raw_results):
                    if not (payload.get("ok") and payload.get("is_trading_day")):
                        skipped_non_trading += 1
                        continue
                    collected.append(payload)
                    if len(collected) >= days:
                        break
                collected.reverse()  # 早 → 晚
                day_results.extend(collected)

                # 下一段从本段最早 -1 开始 (凑不够时)
                earliest = min(target_dates)
                segment_end = earliest - timedelta(days=1)
                if (end_date - segment_end).days > max_lookback_days * 2:
                    break
            # v2.0i: 段循环结束后, 关闭共享 httpx.Client (异常路径由函数级 try/except
            #   转 HTTPException + 进程退出, client 也会被 GC; 这里显式 close 提升可读性)
            _shared_cli.close()

            # 仅取前 days 个 (数据缺失时可能不足 days)
            day_results = day_results[:days]

            series: dict[str, list[int]] = {k: [] for k in (
                "limit_up", "limit_down", "broken", "failed", "recovery",
                "is_one_word", "max_board",
                "limit_up_main", "limit_up_star", "limit_up_chinext", "limit_up_bj",
            )}
            for d in day_results:
                for k in series:
                    series[k].append(int(d.get(k, 0)))

            calendar_days = (
                (date.fromisoformat(day_results[-1]["date"]) - date.fromisoformat(day_results[0]["date"])).days + 1
                if day_results else 0
            )
            return {
                "ok": True,
                "result": {
                    "end_date": end_date.isoformat(),
                    "days": days,
                    "trading_days_returned": len(day_results),
                    "calendar_days_span": calendar_days,
                    "skipped_non_trading": skipped_non_trading,
                    "dates": [d["date"] for d in day_results],
                    "series": series,
                    "per_day": day_results,
                },
                "source": {
                    "ladder_url": f"{_BACKEND_SELF_BASE}/api/screener/limit-ladder",
                    "as_of_requested": as_of,
                    "note": "自动跳过非交易日; calendar_days_span 包含跳过的周末/节假日",
                },
            }
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}") from exc

    return router


def _load_theme_chains() -> dict:
    """加载 theme_chain.json (dev / install 兼容, 跟 tools.py 同算法)."""
    env_dir = os.environ.get("IRP_DATA_DIR")
    if env_dir:
        candidates = [Path(env_dir) / "theme_chain.json"]
    else:
        here = Path(__file__).resolve()
        data_root = None
        for ancestor in here.parents:
            if (ancestor / "app").is_dir() and ancestor.name != "app":
                data_root = ancestor
                break
        if data_root is None:
            data_root = here.parents[3]
        candidates = [
            data_root / "data" / "research" / "theme_chain.json",
            data_root / "data" / "theme_chain.json",
        ]
    chain_path = next((p for p in candidates if p.exists()), candidates[0])
    if not chain_path.exists():
        return {}
    return load_theme_chain(chain_path)


# === P2 v2.0c 真实数据接入 ===

def _fetch_limit_ladder_cached(
    as_of: str | None = None,
    ext_columns: str | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """自调 TSP `/api/screener/limit-ladder`, 带 in-memory cache.

    Args:
        as_of: 指定日期 (YYYY-MM-DD). None = 最新 (后端决定).
        ext_columns: 透传给 limit-ladder 的 ext_columns 参数.
            用于个股带概念/行业 tag, 渲染 limit-ladder 风格卡片用.
        client: 共享 httpx.Client (用于历史端点 keep-alive 优化).
            为 None 时, helper 自建短生命周期 client.

    Returns: limit-ladder 完整响应 dict (含 as_of, tiers, counts).

    Cache:
        - key = f"{as_of or 'latest'}|{ext_columns or ''}"
        - TTL 24h (历史交易日期数据不变, 24h 足够)
        - 命中: O(1) 字典读
        - 未命中: HTTP fetch, 写入; 缓存 FIFO 软上限 _LADDER_CACHE_MAX
        - 线程安全: _LADDER_CACHE_LOCK
    """
    key = f"{as_of or 'latest'}|{ext_columns or ''}"
    now = time.monotonic()
    with _LADDER_CACHE_LOCK:
        hit = _LADDER_CACHE.get(key)
        if hit and (now - hit[0]) < _LADDER_CACHE_TTL_SEC:
            return hit[1]

    params: dict[str, Any] = {}
    if as_of:
        params["as_of"] = as_of
    if ext_columns:
        params["ext_columns"] = ext_columns

    own_client = client is None
    cli = client or httpx.Client(
        timeout=_TIMEOUT,
        headers={"Connection": "keep-alive"},
    )
    try:
        r = cli.get(f"{_BACKEND_SELF_BASE}/api/screener/limit-ladder", params=params)
        r.raise_for_status()
        body = r.json()
    finally:
        if own_client:
            cli.close()

    with _LADDER_CACHE_LOCK:
        # 软上限: FIFO 裁剪老键 (避免内存爆)
        if len(_LADDER_CACHE) >= _LADDER_CACHE_MAX:
            # 删最早插入的 5% 键 (近似 LRU)
            drop_n = max(1, _LADDER_CACHE_MAX // 20)
            for old_key in list(_LADDER_CACHE.keys())[:drop_n]:
                _LADDER_CACHE.pop(old_key, None)
        _LADDER_CACHE[key] = (time.monotonic(), body)
    return body


def _fetch_limit_ladder(ext_columns: str | None = None) -> dict[str, Any]:
    """自调 TSP `/api/screener/limit-ladder` 拿连板梯队 + 个股行情（带 cache）。

    向后兼容入口; /short-term-real 等继续用此名调用。
    """
    return _fetch_limit_ladder_cached(as_of=None, ext_columns=ext_columns)


def _fetch_market_snapshot() -> dict[str, dict[str, Any]]:
    """自调 TSP `/api/screener/market-snapshot` 拿全市场详情。

    Returns: {symbol → row_dict} 索引，供连板梯队补全 prev_close/industry/theme_tags。
    """
    with httpx.Client(timeout=_TIMEOUT) as cli:
        r = cli.get(f"{_BACKEND_SELF_BASE}/api/screener/market-snapshot", params={"limit": 6000})
        r.raise_for_status()
    body = r.json()
    rows = body.get("rows") or []
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        sym = row.get("symbol")
        if sym:
            out[sym] = row
    return out


def _build_quotes_from_ladder(
    ladder: dict[str, Any], snapshot: dict[str, dict[str, Any]]
) -> tuple[list[Quote], list[dict], dict[str, int]]:
    """从 limit-ladder + market-snapshot 构造 IRP Quote[] + prev_pool + industry_map。

    Returns: (quotes, prev_pool, industry_map) 三元组.
      industry_map 来自 st 上 ext_hy_ths.所属同花顺行业 (限转 `__` 形式),
      用于 compute_emotion_score 的 main_line_concentration 维度.

    字段映射:
      - prev_close = close / (1 + change_pct)   # snapshot 无 prev_close, 反推
      - last_close = close
      - high/low/volume 从 snapshot 补 (snapshot 不含 high/low, 用 close/avg 当近似)
      - industry  从 st.ext_hy_ths__所属同花顺行业 (limit-ladder ext_columns 默认传),
                   fallback 到 snap.get("industry") (尚未提供)
      - theme_tags 从 st.ext_gn_ths__所属概念 拆分 (`;` 分隔的多概念), fallback snap.theme_tags
      - is_exploded  status == 'broken'
      - is_limit_down status == 'failed' 且 change_pct < -0.095
      - limit_time  None (limit-ladder 不提供, 已知涨停但无具体时刻)

    prev_pool 构造: 仅今日 status=limit_up 且 consecutive_limit_ups > 1 时加入,
      board_count = consecutive_limit_ups - 1 (昨日连板数)。这让
      aggregate_limit_up_pool 通过 prev_board_map 算出真实 board_count。
    """
    quotes: list[Quote] = []
    prev_pool: list[dict] = []
    industry_map: dict[str, int] = {}
    as_of = ladder.get("as_of") or "1970-01-01"
    for tier in ladder.get("tiers") or []:
        for st in tier.get("stocks") or []:
            symbol = st.get("symbol") or ""
            if not symbol:
                continue
            close = float(st.get("close") or 0.0)
            change_pct = float(st.get("change_pct") or 0.0)
            prev_close = close / (1.0 + change_pct) if (1.0 + change_pct) != 0 else close
            snap = snapshot.get(symbol) or {}
            volume = float(snap.get("volume") or 0.0)
            amount = float(snap.get("amount") or 0.0)
            turnover = float(snap.get("turnover_rate") or 0.0)  # 小数制, 0.030 = 3.0%
            # industry 从 ext_hy_ths__所属同花顺行业 抽 (limit-ladder ext 字段名已
            # 用 "__" 替换 "."); snapshot 暂未提供 industry 字段.
            industry = str(
                st.get("ext_hy_ths__所属同花顺行业")
                or snap.get("industry")
                or ""
            ).strip()
            # theme_tags 拆分 `;` 分隔的多概念字符串 (limit-ladder ext_gn_ths 字段)
            gn_raw = st.get("ext_gn_ths__所属概念") or snap.get("theme_tags") or []
            if isinstance(gn_raw, str):
                theme_tags = [t.strip() for t in gn_raw.split(";") if t.strip()]
            else:
                theme_tags = list(gn_raw)
            # 累计 industry_map (用于 main_line_concentration)
            if industry:
                industry_map[industry] = industry_map.get(industry, 0) + 1

            status = str(st.get("status") or "")
            boards = int(st.get("consecutive_limit_ups") or 0)
            is_exploded = status == "broken"
            is_limit_down = status == "failed" and change_pct < -0.095

            quotes.append(Quote(
                symbol=symbol,
                name=str(st.get("name") or ""),
                trade_date=as_of,
                prev_close=round(prev_close, 4),
                last_close=close,
                high=close,  # snapshot 不含 high, 用 close 近似 (涨停时 close ≈ high)
                low=close,
                volume=volume,
                amount=amount,
                industry=industry,
                theme_tags=theme_tags,
                limit_time=None,  # limit-ladder 不带封板时刻
                is_exploded=is_exploded,
                is_limit_down=is_limit_down,
            ))
            # prev_pool: 只填今日涨停且连板 ≥ 2 的; prev_board = boards - 1
            if status == "limit_up" and boards >= 2:
                prev_pool.append({"symbol": symbol, "board_count": boards - 1})
    return quotes, prev_pool, industry_map


def _aggregate_overview(
    quotes: list[Quote],
    trade_date: str,
    prev_pool: list[dict] | None = None,
    industry_map: dict[str, int] | None = None,
) -> dict[str, Any]:
    """复用短路版算法: 涨停池 + 6 v2 算法 + theme_heat 聚合。

    Args:
        quotes: 今日全市场 quote 池 (limit_up/broken/failed 都含)
        trade_date: YYYY-MM-DD
        prev_pool: 昨日涨停池, 用于算连板晋级率; P2 v2.0c 从
            limit-ladder.consecutive_limit_ups 反推 (limit_up 且 boards≥2 → boards-1)
        industry_map: 行业 → 涨停只数 统计. None 时从 quotes[i].industry 派生
            (聚合空字符串以外的值). 供 compute_emotion_score / compute_deep_metrics
            的 main_line_concentration 维度计算 — 之前 main_line_concentration
            总是 0% 是因为 industry_map 始终为空 (snapshot 不含 industry 字段,
            limit-ladder stock dict 默认也不带 — 直到 P2 v2.0j 接 ext_hy_ths).

    Returns: 与 /short-term-overview 同 schema 的 result dict。
    """
    items, stats = aggregate_limit_up_pool(quotes, prev_pool)
    ladder = build_ladder(items)

    # industry_map 兜底: 从 quotes[i].industry 聚合 (count 非空行业)
    if industry_map is None:
        industry_map = {}
        for q in quotes:
            if q.industry:
                industry_map[q.industry] = industry_map.get(q.industry, 0) + 1

    emotion = compute_emotion_score(items, None, industry_map)
    phase = judge_emotion_phase(emotion, None)
    secondary = compute_secondary_indicators(items, prev_pool)
    # v2.0i: 传 stats["exploded_total"] (status==broken 的只数) 给 compute_deep_metrics,
    #   否则 final_explode_rate 永远 = 0/total = 0% (易误读为"无炸板").
    # v2.0j: 同时传 industry_map, 让 main_line_concentration 不再永远 0%.
    deep = compute_deep_metrics(
        items, prev_pool, industry_map, int(stats.get("exploded_total", 0))
    )
    promo = compute_promotion_by_board(items, prev_pool)

    theme_heat = []
    chain_data = _load_theme_chains()
    if chain_data:
        theme_heat = compute_theme_heat(chain_data, items, prev_pool)

    return {
        "trade_date": trade_date,
        "emotion_score": emotion,
        "phase": phase,
        "secondary_indicators": {
            "high_pos_avg": secondary.high_pos_avg,
            "decline_coverage": secondary.decline_coverage,
            "high_collapse": secondary.high_collapse,
            "high_promotion": secondary.high_promotion,
        },
        "deep_metrics": {
            "final_explode_rate": deep.final_explode_rate,
            "prev_limit_up_feedback": deep.prev_limit_up_feedback,
            "prev_consecutive_feedback": deep.prev_consecutive_feedback,
            "promotion_by_board_overall": deep.promotion_by_board_overall,
            "main_line_concentration": deep.main_line_concentration,
            "high_risk_score": deep.high_risk_score,
        },
        "promotion_by_board": [
            {
                "from_board": s.from_board,
                "to_label": s.to_label,
                "promoted": s.promoted,
                "total": s.total,
                "ratio": s.ratio,
            }
            for s in promo
        ],
        "theme_heat": [
            {
                "theme_id": h.theme_id,
                "theme_name": h.theme_name,
                "max_board": h.max_board,
                "limit_up_count": h.limit_up_count,
                "consecutive_count": h.consecutive_count,
                "heat_score": h.heat_score,
            }
            for h in theme_heat
        ],
        "items": [it.to_dict() for it in items],
        "ladder": [l.to_dict() for l in ladder],
        "stats": {
            "limit_up_total": len(items),
            "limit_down_total": stats["limit_down_total"],
            "exploded_total": stats["exploded_total"],
            "re_seal_count": sum(1 for it in items if it.last_close >= it.first_limit_price - 0.01),
            "max_board": max((it.board_count for it in items), default=0),
            "seal_rate": (sum(1 for it in items if it.limit_time) / len(items)) if items else 0.0,
            "ladder_count": len(ladder),
        },
    }