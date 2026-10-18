"""产业链映射加载 — P1.1。

净室重写自 easy-stock `internal/sector/mapper.go` 的"行业↔题材↔成分股"映射表。

数据来源：data/theme_chain.json（手工维护，v1.0 录入 20 个核心题材）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ChainNode:
    """产业链上的一个节点。"""
    node: str  # e.g. "上游-光模块"
    stocks: list[str]  # 代表股 code
    weight: float = 1.0


@dataclass
class ThemeChain:
    """一个题材的产业链映射。"""
    theme_id: str
    theme_name: str
    chain: list[ChainNode] = field(default_factory=list)
    industries: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "theme_id": self.theme_id,
            "theme_name": self.theme_name,
            "chain": [
                {"node": n.node, "stocks": n.stocks, "weight": n.weight}
                for n in self.chain
            ],
            "industries": self.industries,
        }


def load_theme_chain(path: str | Path) -> dict[str, ThemeChain]:
    """从 JSON 文件加载所有题材的产业链映射。"""
    p = Path(path)
    if not p.exists():
        return {}
    with p.open("r", encoding="utf-8") as f:
        data = json.load(f)

    result: dict[str, ThemeChain] = {}
    for theme_name, entry in data.items():
        theme_id = entry.get("theme_id") or _slugify(theme_name)
        chain = [
            ChainNode(
                node=node.get("node", ""),
                stocks=list(node.get("stocks", [])),
                weight=float(node.get("weight", 1.0)),
            )
            for node in entry.get("chain", [])
        ]
        result[theme_id] = ThemeChain(
            theme_id=theme_id,
            theme_name=theme_name,
            chain=chain,
            industries=list(entry.get("industries", [])),
        )
    return result


def get_theme_chain(chains: dict[str, ThemeChain], theme_id: str) -> ThemeChain | None:
    """按 theme_id 取一条映射。"""
    return chains.get(theme_id)


def attribute_chain(
    chain: ThemeChain,
    change_pct_map: dict[str, float],
) -> list[dict]:
    """产业链归因：每个节点按其代表股的涨跌幅汇总。

    Args:
        chain: 题材产业链
        change_pct_map: {symbol: change_pct（小数制）}

    Returns:
        [{node, weight, avg_change_pct, contribution, stocks: [{symbol, change_pct}]}]
    """
    result = []
    for node in chain.chain:
        stocks_with_change = []
        total_weight = 0.0
        weighted_change = 0.0
        for sym in node.stocks:
            if sym in change_pct_map:
                ch = change_pct_map[sym]
                w = node.weight
                stocks_with_change.append({"symbol": sym, "change_pct": ch})
                total_weight += w
                weighted_change += ch * w
        avg_change = weighted_change / total_weight if total_weight > 0 else 0.0
        result.append({
            "node": node.node,
            "weight": node.weight,
            "avg_change_pct": round(avg_change, 4),
            "contribution": round(avg_change * node.weight, 4),
            "stocks": stocks_with_change,
        })
    return result


def _slugify(s: str) -> str:
    """中文转拼音或简单 slug 备用。"""
    return s.replace(" ", "_").replace("（", "(").replace("）", ")")
