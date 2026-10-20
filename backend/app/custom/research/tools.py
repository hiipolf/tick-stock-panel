"""IRP 4 工具 — P1.1。

净室重写: 4 工具以 JSON Schema + 同步 handler 形式暴露, 供后续 P4 AI 集成使用。

本版本 P1.1 **不接入** TSP 助手（chat_service 不支持动态注册工具 —
按 docs/secondary-development.md「按需扩展」原则, 扩展 API 尚未实现）。
工具作为 IRP 自己的可发现 API, 后续接入方式:
  - 选项 A: 给 TSP 上游提 PR, 在 assistant/tools.py 加 register_external_tool
  - 选项 B: P4 给 IRP 自建独立 chat 端点 (/api/custom/research/chat)
  - 选项 C: 当前已通过 4 个 HTTP 端点直接暴露, 前端直接 fetch 调用
"""
from __future__ import annotations

from typing import Any, Callable

from app.custom.research.short_term import (
    Quote,
    aggregate_limit_up_pool,
    build_ladder,
    compute_promotion_rate,
    compute_stats,
)
from app.custom.research.theme_radar import ThemeSnapshot, judge_phase


# === 4 工具 JSON Schema ===
TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_limit_up_pool",
            "description": (
                "获取某交易日的涨停池: 涨停个股清单 + 综合统计 + 梯队分组。"
                "需要调用方提供今日所有股票行情 (prev_close, last_close, last_close 等)。"
                "可选 prev_pool 用于算连板数和晋级率。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "trade_date": {
                        "type": "string",
                        "description": "交易日 YYYY-MM-DD",
                    },
                    "quotes": {
                        "type": "array",
                        "description": "今日所有股票行情, 每条 Quote 见下方",
                        "items": {
                            "type": "object",
                            "properties": {
                                "symbol": {"type": "string"},
                                "name": {"type": "string"},
                                "trade_date": {"type": "string"},
                                "prev_close": {"type": "number"},
                                "last_close": {"type": "number"},
                                "high": {"type": "number"},
                                "low": {"type": "number"},
                                "volume": {"type": "number"},
                                "amount": {"type": "number"},
                                "industry": {"type": "string"},
                                "theme_tags": {"type": "array", "items": {"type": "string"}},
                                "limit_time": {"type": ["string", "null"]},
                                "is_exploded": {"type": "boolean"},
                                "is_limit_down": {"type": "boolean"},
                            },
                            "required": ["symbol", "name", "trade_date",
                                         "prev_close", "last_close"],
                        },
                    },
                    "prev_pool": {
                        "type": "array",
                        "description": "昨日涨停池, 用于算连板数和晋级率, 可选",
                        "items": {
                            "type": "object",
                            "properties": {
                                "symbol": {"type": "string"},
                                "board_count": {"type": "integer"},
                            },
                        },
                    },
                },
                "required": ["trade_date", "quotes"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_limit_up_ladder",
            "description": (
                "获取连板梯队: 1/2/3/4/5+ 各梯队的股票数量、首末封板时间。"
                "输入已聚合的涨停池 (上游 get_limit_up_pool 的 items)。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "description": "涨停池 items (同 get_limit_up_pool 输出格式)",
                        "items": {
                            "type": "object",
                            "properties": {
                                "symbol": {"type": "string"},
                                "board_count": {"type": "integer"},
                                "limit_time": {"type": ["string", "null"]},
                            },
                            "required": ["symbol", "board_count"],
                        },
                    },
                },
                "required": ["items"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_theme_index_kline",
            "description": (
                "获取某题材的等权参考指数 K 线: 成分股按等权重聚合, "
                "支持 1d / 1w / 1m 频率。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "theme_id": {"type": "string"},
                    "freq": {
                        "type": "string",
                        "enum": ["1d", "1w", "1m"],
                        "description": "频率",
                    },
                    "stocks": {
                        "type": "object",
                        "description": "{symbol: [Bar...]} 成分股 K 线",
                        "additionalProperties": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "date": {"type": "string"},
                                    "close": {"type": "number"},
                                    "volume": {"type": "number"},
                                    "amount": {"type": "number"},
                                },
                            },
                        },
                    },
                },
                "required": ["theme_id", "freq", "stocks"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_theme_attribution",
            "description": (
                "获取某题材的产业链归因: 各节点的代表股涨跌幅 + 加权贡献度。"
                "需要调用方提供产业链映射 (或使用 IRP 内置 data/theme_chain.json) "
                "和当日成分股涨跌幅。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "theme_id": {"type": "string"},
                    "change_pct_map": {
                        "type": "object",
                        "description": "{symbol: change_pct} 当日成分股涨跌幅",
                        "additionalProperties": {"type": "number"},
                    },
                },
                "required": ["theme_id", "change_pct_map"],
            },
        },
    },
]


# === Handler 注册表（name → callable） ===
def _tool_get_limit_up_pool(args: dict[str, Any]) -> dict[str, Any]:
    """涨停池聚合。"""
    quotes_raw = args.get("quotes", [])
    quotes = [
        Quote(
            symbol=q["symbol"], name=q["name"], trade_date=q["trade_date"],
            prev_close=float(q["prev_close"]), last_close=float(q["last_close"]),
            high=float(q.get("high", q["last_close"])),
            low=float(q.get("low", q["last_close"])),
            volume=float(q.get("volume", 0)),
            amount=float(q.get("amount", 0)),
            industry=q.get("industry", ""),
            theme_tags=list(q.get("theme_tags", [])),
            limit_time=q.get("limit_time"),
            is_exploded=bool(q.get("is_exploded", False)),
            is_limit_down=bool(q.get("is_limit_down", False)),
        )
        for q in quotes_raw
    ]
    prev_pool = args.get("prev_pool")
    items, stats = aggregate_limit_up_pool(quotes, prev_pool)
    ladder = build_ladder(items)
    promo = compute_promotion_rate(items, prev_pool)
    summary = compute_stats(items, ladder, total_quotes=len(quotes))

    return {
        "ok": True,
        "result": {
            "trade_date": args.get("trade_date"),
            "items": [it.to_dict() for it in items],
            "ladder": [l.to_dict() for l in ladder],
            "stats": {**stats, **summary, "promotion_rate": promo},
        },
    }


def _tool_get_limit_up_ladder(args: dict[str, Any]) -> dict[str, Any]:
    """连板梯队（独立调用时 — 输入已聚合 items）。"""
    from app.custom.research.short_term import LimitUpItem

    raw = args.get("items", [])
    items = [
        LimitUpItem(
            symbol=it["symbol"], name=it.get("name", it["symbol"]),
            trade_date=it.get("trade_date", ""),
            limit_time=it.get("limit_time"),
            first_limit_price=float(it.get("first_limit_price", 0)),
            last_close=float(it.get("last_close", 0)),
            turnover=float(it.get("turnover", 0)),
            amount=float(it.get("amount", 0)),
            industry=it.get("industry", ""),
            theme_tags=list(it.get("theme_tags", [])),
            board_count=int(it["board_count"]),
        )
        for it in raw
    ]
    ladders = build_ladder(items)
    return {
        "ok": True,
        "result": {"ladder": [l.to_dict() for l in ladders]},
    }


def _tool_get_theme_index_kline(args: dict[str, Any]) -> dict[str, Any]:
    """题材指数 K 线。"""
    from app.custom.research.theme_index import StockBar, aggregate_freq, compute_ew_index

    freq = args.get("freq", "1d")
    raw_stocks = args.get("stocks", {})
    stocks: dict[str, list[StockBar]] = {}
    for sym, bars in raw_stocks.items():
        stocks[sym] = [
            StockBar(
                date=b["date"], close=float(b["close"]),
                volume=float(b.get("volume", 0)),
                amount=float(b.get("amount", 0)),
            )
            for b in bars
        ]
    daily = compute_ew_index(stocks)
    out = aggregate_freq(daily, freq)
    return {
        "ok": True,
        "result": {
            "theme_id": args.get("theme_id"),
            "freq": freq,
            "bars": [b.to_dict() for b in out],
        },
    }


def _tool_get_theme_attribution(args: dict[str, Any]) -> dict[str, Any]:
    """产业链归因。

    简化 v1.0: 直接返回 IRP 维护的 data/theme_chain.json 中匹配 theme_id 的链,
    调用方负责传 change_pct_map。后续 P2/P3 接入真实数据时, 这里再改成
    从 app.state.repo / data_dir 读。
    """
    from pathlib import Path

    from app.custom.research.chain import (
        attribute_chain,
        load_theme_chain,
    )

    # 默认数据目录 (priority: env IRP_DATA_DIR → 当前包所在 backend 的 data/)
    # tools.py 路径:
    #   开发模式: backend_pack/app/custom/research/tools.py → parents[3] = backend_pack/
    #   安装模式: <任意>/backend/app/custom/research/tools.py → parents[2] = <任意>/backend/
    # 兼容两种模式 + 兼容 data/research/ (安装脚本默认) 与 data/ (直接放)
    env_dir = __import__("os").environ.get("IRP_DATA_DIR")
    if env_dir:
        candidate_paths = [Path(env_dir) / "theme_chain.json"]
        data_root = None
    else:
        here = Path(__file__).resolve()
        # 找到包含 app/ 的最近祖先 (= 仓库根: backend/ 或 backend_pack/)
        data_root = None
        for ancestor in here.parents:
            if (ancestor / "app").is_dir() and ancestor.name != "app":
                data_root = ancestor
                break
        if data_root is None:
            data_root = here.parents[3]
        # 优先 data/research/theme_chain.json (安装默认), fallback data/theme_chain.json
        candidate_paths = [
            data_root / "data" / "research" / "theme_chain.json",
            data_root / "data" / "theme_chain.json",
        ]
    chain_path = next((p for p in candidate_paths if p.exists()), candidate_paths[0])

    if not chain_path.exists():
        return {
            "ok": False,
            "error": f"产业链映射文件不存在: {chain_path}",
        }

    chains = load_theme_chain(chain_path)
    theme_id = args.get("theme_id", "")
    chain = chains.get(theme_id)
    if chain is None:
        return {"ok": False, "error": f"theme_id 不存在: {theme_id}"}

    cp_map = {str(k): float(v) for k, v in args.get("change_pct_map", {}).items()}
    attribution = attribute_chain(chain, cp_map)

    return {
        "ok": True,
        "result": {
            "theme_id": theme_id,
            "theme_name": chain.theme_name,
            "industries": chain.industries,
            "attribution": attribution,
        },
    }


HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "get_limit_up_pool": _tool_get_limit_up_pool,
    "get_limit_up_ladder": _tool_get_limit_up_ladder,
    "get_theme_index_kline": _tool_get_theme_index_kline,
    "get_theme_attribution": _tool_get_theme_attribution,
}


def list_tools() -> list[dict[str, Any]]:
    """列出所有工具 schema（供前端展示 / 调试用）。"""
    return TOOL_SCHEMAS


def execute_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """同步执行工具 — 失败不抛, 返 ok=False。"""
    handler = HANDLERS.get(name)
    if handler is None:
        return {"ok": False, "error": f"未知工具: {name}"}
    try:
        return handler(args)
    except Exception as exc:  # 边界隔离, 工具失败不影响主流程
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# === judge_phase 也暴露为可调用（不做工具, 供内部辅助） ===
def call_judge_phase(today: dict[str, Any], history: list[dict[str, Any]]) -> str:
    """辅助: 5 阶段判定（HTTP 层调用）。"""
    today_obj = ThemeSnapshot(**today)
    hist_objs = [ThemeSnapshot(**h) for h in history]
    return judge_phase(today_obj, hist_objs)