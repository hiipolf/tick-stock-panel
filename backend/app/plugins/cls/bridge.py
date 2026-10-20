"""财联社 (CLS) Provider 可用性检测.

vendor 契约 (plugin-development.md §"check 函数"):
- 返回 (是否可用, 原因). 不抛异常.
- 可用 → 插件注册进路由表
- 不可用 → 设置页显示灰显卡片 + 原因

CLS 无 Key, 唯一失败模式是上游不可达。检测走**真实轻量请求**:
用市场情绪接口 (单次 GET, 返回一个 dict), 失败则不可用。
"""

from __future__ import annotations


def availability() -> tuple[bool, str]:
    """检测 CLS 上游是否可达。

    真实检测: 试拉一次市场情绪接口 (x-quote.cls.cn/v2/quote/a/stock/emotion)。
    失败则 (False, 原因), 绝不抛异常。
    """
    try:
        # 相对导入: vendor 里是 app.plugins.cls.bridge, 暂存区是 cls.bridge
        from .client import ClsClient, ClsError

        client = ClsClient(timeout=8.0)
        try:
            client.stock_emotion()
        except ClsError as exc:
            return False, f"财联社不可达: {exc}"
        finally:
            client.close()
    except Exception as exc:
        return False, f"财联社检测失败: {exc}"
    return True, "ok"
