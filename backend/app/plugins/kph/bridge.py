"""开盘啦 (KPH) Provider 可用性检测.

vendor 契约 (plugin-development.md §"check 函数"):
- 返回 (是否可用, 原因). 不抛异常.
- 可用 → 插件注册进路由表
- 不可用 → 设置页显示灰显卡片 + 原因

KPH 无 Key, 唯一失败模式是上游不可达。检测走**真实轻量请求**:
用市场情绪接口 (单次 POST, 返回一个 info 字典), 失败则不可用。

⚠️ **探活必须用历史日期, 不能用今天** (2026-10-20 实测):
KPH 实时域名会**忽略 Date 参数**, 返回最近有数据的交易日 (实测 10/10 次
都返回上一交易日), 而 client 层现在会校验 "数据日期 == 请求日期" ->
用今天探活会拿到 "日期不一致" 错误 → **误判插件不可用**。

⚠️ 探活也不能只试一个日期: 上一个工作日可能是法定节假日 (上游 errcode=1020),
所以依次试最多 3 个候选日, 任一成功即视为可用。
"""

from __future__ import annotations

_MAX_PROBE_DAYS = 3


def _probe_dates() -> list[str]:
    """候选探活日期: 上一个工作日, 再往前最多 3 个工作日。"""
    from .client import previous_weekday, today_dash

    dates: list[str] = []
    cursor = today_dash()
    for _ in range(_MAX_PROBE_DAYS):
        cursor = previous_weekday(cursor)
        dates.append(cursor)
    return dates


def availability() -> tuple[bool, str]:
    """检测 KPH 上游是否可达。

    真实检测: 试拉一次市场情绪接口 (用历史日期, 避开实时域名的日期漂移)。
    失败则 (False, 原因)。
    """
    try:
        # 相对导入: vendor 里是 app.plugins.kph.bridge, 暂存区是 kph.bridge
        from .client import KphClient, KphError

        client = KphClient(timeout=8.0)
        try:
            last_error: Exception | None = None
            for day in _probe_dates():
                try:
                    client.market_emotion(day)
                except KphError as exc:
                    last_error = exc
                    continue
                return True, "ok"
            return False, f"开盘啦不可达: {last_error}"
        finally:
            client.close()
    except Exception as exc:
        return False, f"开盘啦检测失败: {exc}"
    return True, "ok"
