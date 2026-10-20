"""开盘红 (KPH / kaipanhong) HTTP 客户端.

端点与参数来自 AxData `axdata_core/adapters/kph/request.py` (开源实现, 已核对源码):
- 实时: https://apphwshhq.kaipanhong.com/w1/api/index.php
- 历史: https://apphis.kaipanhong.com/w1/api/index.php

协议特征 (与常规 REST 源不同, 接入时注意):
1. **POST + application/x-www-form-urlencoded**, 不是 GET
2. 双参数路由: `a` = action (业务方法), `c` = controller (控制器)
3. 每次请求必须附带设备指纹 `_BASE_PARAMS` (PhoneOSNew / DeviceID / VerSion / Token / UserID / Red / apiv),
   缺参数上游会返回空 payload
4. **响应行是位置数组 (list), 不是 dict** —— 字段靠下标定位, 见各 `parse_*` 函数
5. 部分接口的 payload 是 dict (市场情绪在 `info` 下, 复盘事件在 `List` 下, 天梯在 `StockList` 下)
6. 错误码在 `errcode` 字段 (0 或缺失 = 成功)

本模块只负责 HTTP + 协议层解包, 不认识 TSP 的 provider 契约 (见 provider.py)。
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Mapping, Sequence
from datetime import date as date_type
from datetime import datetime, timedelta
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ────────────────── 端点 ──────────────────

KPH_REALTIME_URL = "https://apphwshhq.kaipanhong.com/w1/api/index.php"
KPH_HISTORY_URL = "https://apphis.kaipanhong.com/w1/api/index.php"

# 设备指纹: 每次请求必带, 否则上游返回空 payload (AxData 源码 _BASE_PARAMS)
BASE_PARAMS: dict[str, str] = {
    "PhoneOSNew": "1",
    "DeviceID": "1a609dd6-b2b8-3bf9-ac40-a77581551454",
    "VerSion": "6.0.6",
    "Token": "0",
    "UserID": "0",
    "Red": "1",
    "apiv": "w45",
}

# 板块类型 → 上游 ZSType (AxData 源码 _SECTOR_TYPE_MAP)
SECTOR_TYPE_MAP: dict[str, str] = {
    "selected": "7",
    "industry": "4",
    "region": "6",
}

# 涨停/跌停/风向标 → 上游 PidType (AxData 源码 _LIMIT_PID)
LIMIT_PID: dict[str, str] = {
    "up": "4",
    "down": "3",
    "wind_vane": "6",
}

# board_type → (a, c) 路由 + 额外参数
#   kind="history": 只在历史域名可用 (实时域名没有历史接口)
#   kind="realtime": 今日走实时域名, 历史走历史域名
BOARD_ROUTES: dict[str, dict[str, Any]] = {
    "up": {"a": "HisDaBanList", "c": "HisHomeDingPan", "kind": "history", "pid": "4"},
    "down": {"a": "HisDaBanList", "c": "HisHomeDingPan", "kind": "history", "pid": "3"},
    "wind_vane": {"a": "HisDaBanList", "c": "HisHomeDingPan", "kind": "history", "pid": "6"},
    "ladder": {"a": "GetZhangTingTianTi", "c": "FuPanLa", "kind": "realtime"},
    "emotion": {"a": "ZhangFuDetail", "c": "HomeDingPan", "kind": "realtime"},
    "sector": {"a": "RealRankingInfo", "c": "ZhiShuRanking", "kind": "realtime"},
    "event": {"a": "GetPMSL_PMLD", "c": "FuPanLa", "kind": "realtime"},
    "resumption": {"a": "GetPlateInfo_w38", "c": "HisLimitResumption", "kind": "history"},
}

# "all" 合并的 board_type 集合 (事件流与复盘是事件型, 不进合并)
ALL_BOARD_TYPES: tuple[str, ...] = ("up", "down", "wind_vane", "ladder", "emotion", "sector")

# ⚠️ 本常量是**协议层**的全部板型 (含 event), 比 provider 的
# `VALID_BOARD_TYPES` 多一个 event —— 这是**有意为之**:
# event (盘面梳理事件流) 返回的是 dict 行而不是位置数组, 语义上装不进
# limit_board 的宽联合 schema, 所以 provider 用**独立方法**
# `get_market_events()` 提供它, 不并入 get_limit_board()。
# 调用方不要拿 BOARD_TYPES 当作"能进 limit_board 的板型列表"。
BOARD_TYPES: tuple[str, ...] = (*BOARD_ROUTES.keys(), "all")

DEFAULT_TIMEOUT = 20.0
# 上游无公开 SLA, 保守节流. 板块排行要翻页, 每页间隔不能太短.
DEFAULT_MIN_INTERVAL = 1.0
_SECTOR_PAGE_SIZE = 50
_SECTOR_MAX_PAGES = 40
# 涨停/跌停池分页: 实测上游 st 上限约 60 (st=80+ 返回 0 行), 用 50 稳妥
_LIMIT_PAGE_SIZE = 50
_LIMIT_MAX_PAGES = 10


class KphError(Exception):
    """KPH 接口错误 (网络失败 / errcode != 0 / 响应结构异常)。

    `errcode` 是上游返回的**结构化**错误码 (可能为 None)。
    带上它是为了让调用方可以按错误码判断, 而不是靠匹配异常消息里的字符串
    (上游改一次文案, 字符串匹配就失效)。
    """

    def __init__(self, message: str, *, errcode: Any = None) -> None:
        super().__init__(message)
        self.errcode = errcode


class KphValidationError(KphError):
    """参数校验错误 (调用方传错)。

    与 KphError 的运行时错误区分: 校验错误是**调用方的错**, 应当 fail-fast 抛出去;
    运行时错误 (网络/上游) 才允许在批量场景下软失败跳过。
    """


class KphClient:
    """开盘红客户端。线程安全 (httpx.Client 可并发复用, 但本类自带节流锁语义是串行的)。"""

    def __init__(
        self,
        base_url: str = KPH_REALTIME_URL,
        history_url: str = KPH_HISTORY_URL,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.history_url = history_url.rstrip("/")
        self.min_interval = min_interval
        self._last_call = 0.0
        self._http = httpx.Client(
            timeout=timeout,
            headers={
                "User-Agent": "Dalvik/2.1.0 AxData/0.1",
                "Accept": "application/json,text/plain,*/*",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
        )

    def close(self) -> None:
        self._http.close()

    # ────────────────── 内部 ──────────────────

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_call = time.monotonic()

    def _post(self, url: str, params: Mapping[str, Any], *, context: str) -> Mapping[str, Any]:
        """POST + form-urlencoded + 设备指纹 + errcode 校验。"""
        self._throttle()
        body: dict[str, Any] = {**BASE_PARAMS}
        body.update({k: v for k, v in params.items() if v is not None})
        try:
            resp = self._http.post(url, data=body)
        except httpx.HTTPError as exc:
            raise KphError(f"{context} 网络请求失败: {exc}") from exc
        if resp.status_code != 200:
            raise KphError(f"{context} HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise KphError(f"{context} 响应不是 JSON: {resp.text[:200]}") from exc
        if not isinstance(payload, Mapping):
            raise KphError(f"{context} 响应结构异常 (不是对象)")
        errcode = payload.get("errcode")
        if errcode not in (None, "0", 0):
            raise KphError(f"{context} 上游返回 errcode={errcode}", errcode=errcode)
        return payload

    def _host(self, kind: str, trade_date: str) -> str:
        """决定用实时域名还是历史域名。

        kind="history"  → 恒定历史域名 (涨停池/跌停池/风向标/复盘 只有历史接口)
        kind="realtime" → 今日走实时域名, 历史日期走历史域名

        ⚠️ 实时域名**忽略 Date 参数**, 返回最近有数据的交易日 → 由 `_assert_date`
           检出并报错 (见模块级注释)。
        """
        if kind == "history":
            return self.history_url
        return self.base_url if trade_date == today_dash() else self.history_url

    def _assert_date(
        self, payload: Mapping[str, Any], requested: str, *, context: str
    ) -> None:
        """数据日期自证: payload 顶层日期与请求日期不一致 → 抛 KphError。

        上游实时域名忽略 `Date` 参数, 会静默返回上一个交易日的数据。
        这里把"日期对不上"变成**显式错误**, 调用方能立刻发现拿错数据。

        宽容点: payload 里取不到日期字段 → 不报错 (上游结构变了不该炸整个拉数)。
        """
        actual = extract_payload_date(payload)
        if actual is None or actual == requested:
            return
        raise KphError(
            f"{context} 数据日期与请求日期不一致: 请求 {requested}, 实际 {actual}。"
            f"实时域名会忽略 Date 参数并返回最近有数据的交易日 —— "
            f"请显式传入要取的历史日期 (YYYY-MM-DD)。"
        )

    # ────────────────── 各 board_type ──────────────────

    def limit_history(
        self, board_type: str, trade_date: str, *, include_st: bool = True
    ) -> list[Sequence[Any]]:
        """涨停池 / 跌停池 / 风向标 (历史接口, 位置数组行)。

        ⚠️ **必须翻页**: 实测 (2026-10-16) 当日涨停 55 只, 单页 st=50 只返回 50 只,
        会漏 5 只。st=60 一次能拿全 (55), 但 st=80/100/200 上游直接返回 0 行
        (页大小上限约 60)。所以用 st=50 翻页, 满页继续 / 短页终止。
        ⚠️ 且 st=60 反而不如 st=50 翻页全: 2026-10-13 st=60 单取 60 只,
        st=50 翻页到底 64 只 (st=60 的结果是子集) → 必须用 st=50 + 翻页。

        ⚠️ **include_st 必须为 True**: 上游 `Is_st` 参数语义反直觉 —— 实测
        `Is_st=1` 会**排除** ST 股, `Is_st=0` 才是**包含**。
        2026-10-13 实测: Is_st=1 → 64 只, Is_st=0 → 67 只, 差的 3 只是
        `*ST威领`(2连板) / `*ST仕净`(2连板) / `*ST三房`(首板), 涨停原因均标 "ST板块"。
        且 Is_st=0 的 67 只与「连板天梯」的 67 只**完全一致** (交叉验证)。
        短线连板场景 ST 涨停是关键信号, 漏了会误判梯队高度。

        口径备注: 涨停池实际 67 只 (含 ST), 而市场情绪接口的 ZT=70 —— 上游两个接口
        统计口径仍有 2~3 只差异 (疑似含一字板/未开板), 不是漏数。
        """
        route = BOARD_ROUTES[board_type]
        rows: list[Sequence[Any]] = []
        index = 0
        for _ in range(_LIMIT_MAX_PAGES):
            payload = self._post(
                self.history_url,
                {
                    "a": route["a"],
                    "c": route["c"],
                    "Order": "1",
                    "st": str(_LIMIT_PAGE_SIZE),
                    "Index": str(index),
                    # 反直觉: "0" = 包含 ST, "1" = 排除 ST (实测)
                    "Is_st": "0" if include_st else "1",
                    "PidType": route["pid"],
                    "Type": "6",
                    "FilterMotherboard": "0",
                    "Filter": "0",
                    "FilterTIB": "0",
                    "FilterGem": "0",
                    "Day": trade_date,
                },
                context=f"KPH {board_type}",
            )
            self._assert_date(payload, trade_date, context=f"KPH {board_type}")
            batch = payload_list(payload)
            rows.extend(batch)
            if len(batch) < _LIMIT_PAGE_SIZE:
                break
            index += _LIMIT_PAGE_SIZE
        return rows

    def limit_ladder(self, trade_date: str) -> list[Sequence[Any]]:
        """连板天梯。payload 在 StockList 下, 是二维数组 (按连板高度分组)。"""
        payload = self._post(
            self._host("realtime", trade_date),
            {"a": "GetZhangTingTianTi", "c": "FuPanLa", "Date": trade_date},
            context="KPH limit ladder",
        )
        self._assert_date(payload, trade_date, context="KPH ladder")
        stock_list = payload.get("StockList") or payload.get("stock_list") or []
        rows: list[Sequence[Any]] = []
        if not isinstance(stock_list, list):
            return rows
        for group in stock_list:
            if not isinstance(group, list) or not group:
                continue
            if isinstance(group[0], list):
                # 二维: [[row, row, ...], [row, ...]] —— 高度分组
                rows.extend(row for row in group if isinstance(row, list))
            else:
                rows.append(group)
        return rows

    def market_emotion(self, trade_date: str) -> Mapping[str, Any]:
        """市场情绪截面。返回 payload["info"] 字典 (无数据时返回空 dict)。"""
        is_today = trade_date == today_dash()
        payload = self._post(
            self._host("realtime", trade_date),
            {
                "a": "ZhangFuDetail" if is_today else "HisZhangFuDetail",
                "c": "HomeDingPan" if is_today else "HisHomeDingPan",
                **({} if is_today else {"Day": trade_date}),
            },
            context="KPH market emotion",
        )
        self._assert_date(payload, trade_date, context="KPH emotion")
        info = payload.get("info")
        return info if isinstance(info, Mapping) else {}

    def sector_ranking(
        self, trade_date: str, sector_type: str = "selected", *, fetch_all: bool = False
    ) -> list[Sequence[Any]]:
        """板块排行。分页 (st=50/页), fetch_all=False 时只拉第一页。"""
        zs_type = SECTOR_TYPE_MAP.get(sector_type)
        if zs_type is None:
            raise KphValidationError(
                f"sector_type 必须是 {sorted(SECTOR_TYPE_MAP)} 之一, 收到 {sector_type!r}"
            )
        type_param = "1" if zs_type == "7" else "2"
        host = self._host("realtime", trade_date)
        rows: list[Sequence[Any]] = []
        index = 0
        for _ in range(_SECTOR_MAX_PAGES):
            payload = self._post(
                host,
                {
                    "a": "RealRankingInfo",
                    "c": "ZhiShuRanking",
                    "Order": "1",
                    "st": str(_SECTOR_PAGE_SIZE),
                    "Index": str(index),
                    "Date": trade_date,
                    "Type": type_param,
                    "ZSType": zs_type,
                },
                context="KPH sector ranking",
            )
            self._assert_date(payload, trade_date, context="KPH sector")
            batch = payload_list(payload)
            rows.extend(batch)
            if not fetch_all or len(batch) < _SECTOR_PAGE_SIZE:
                break
            index += _SECTOR_PAGE_SIZE
        return rows

    def sector_constituents(self, trade_date: str, plate_id: str) -> list[Sequence[Any]]:
        """板块成分股 (单板块)。"""
        payload = self._post(
            self.history_url,
            {
                "a": "ZhiShuStockList_W8",
                "c": "ZhiShuRanking",
                "Order": "1",
                "st": "1000",
                "old": "1",
                "Index": "0",
                "Date": trade_date,
                "Type": "6",
                "PlateID": plate_id,
                "IsZZ": "0",
                "IsKZZType": "0",
                "TSZB": "0",
                "TSZB_Type": "0",
                "filterType": "0",
            },
            context="KPH sector constituents",
        )
        self._assert_date(payload, trade_date, context="KPH sector constituents")
        return payload_list(payload)

    def market_review_events(self, trade_date: str, limit: int = 30, offset: int = 0) -> list[Mapping[str, Any]]:
        """盘面梳理事件流。返回 dict 列表 (payload["List"])。"""
        payload = self._post(
            self._host("realtime", trade_date),
            {
                "a": "GetPMSL_PMLD",
                "c": "FuPanLa",
                "st": str(limit),
                "Index": str(offset),
                "Date": trade_date,
            },
            context="KPH market review events",
        )
        self._assert_date(payload, trade_date, context="KPH event")
        items = payload.get("List") or payload.get("list") or []
        return [item for item in items if isinstance(item, Mapping)] if isinstance(items, list) else []

    def limit_resumption(self, trade_date: str, limit: int = 100, offset: int = 0) -> list[Mapping[str, Any]]:
        """涨停复盘 (板块维度 → 展开成个股行)。

        payload["list"] 是板块列表, 每个板块含 StockList 个股数组。
        返回 {"plate_id", "plate_name", "row"} 三元组列表 (row 是位置数组)。
        """
        payload = self._post(
            self.history_url,
            {
                "a": "GetPlateInfo_w38",
                "c": "HisLimitResumption",
                "st": str(limit),
                "Index": str(offset),
                "Date": trade_date,
            },
            context="KPH limit resumption",
        )
        self._assert_date(payload, trade_date, context="KPH resumption")
        plates = payload.get("list")
        out: list[Mapping[str, Any]] = []
        if not isinstance(plates, list):
            return out
        for plate in plates:
            if not isinstance(plate, Mapping):
                continue
            stock_list = plate.get("StockList")
            if not isinstance(stock_list, list):
                continue
            for row in stock_list:
                if isinstance(row, list):
                    out.append({"plate_id": plate.get("ZSCode"), "plate_name": plate.get("ZSName"), "row": row})
        return out


# ────────────────── 协议层解析 (位置数组 → 命名字段) ──────────────────


# ────────────────── 数据日期自证 (2026-10-20 决策) ──────────────────
#
# ⚠️ 上游**实时域名完全忽略 `Date` 参数**: 2026-10-20 实测 5 个日期 x 2 次 = 10/10
#    一律返回 2026-10-19 的数据。而历史域名给的日期与请求一致。
#    → 调用方**无法自证**拿到的是哪一天的数据 (静默拿旧数据, 定时任务分辨不出来)。
#    → 决策: 数据日期与请求日期不一致时**直接报错**, 不静默返回。
#
# 各板型 payload 顶层的日期字段名不一样 (实测):
#   up / down / wind_vane → "day"      = "2026-10-16"
#   ladder                → "Date"     = "2026-10-16"
#   emotion               → "date"     = "2026-10-16"  (另有 "time" = unix 秒)
#   sector                → "Day"      = ["2026-10-16"]  ← **列表**
#   event                 → "date"     = "2026-10-16"  (另有 "Time" = unix 秒)
#   resumption            → "date"     = "2026-10-16"

_PAYLOAD_DATE_KEYS: tuple[str, ...] = ("day", "Day", "date", "Date")


def extract_payload_date(payload: Mapping[str, Any]) -> str | None:
    """从 payload 顶层提取数据日期 (归一成 YYYY-MM-DD)。取不到返回 None。

    容错: 字段名不认 / 值为空 / 格式不认识 → 一律返回 None (由调用方决定是否报错)。
    """
    for key in _PAYLOAD_DATE_KEYS:
        value = payload.get(key)
        if isinstance(value, (list, tuple)):
            value = value[0] if value else None
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        if len(text) == 8 and text.isdigit():  # 20261016
            return f"{text[:4]}-{text[4:6]}-{text[6:]}"
        if len(text) >= 10 and text[4] == "-" and text[7] == "-":  # 2026-10-16
            return text[:10]
    return None


def payload_list(payload: Mapping[str, Any]) -> list[Sequence[Any]]:
    """取 payload["list"] 里的位置数组行, 过滤掉非数组元素。"""
    rows = payload.get("list", [])
    if not isinstance(rows, list):
        return []
    return [
        row
        for row in rows
        if isinstance(row, Sequence) and not isinstance(row, (str, bytes, bytearray))
    ]


def at(row: Sequence[Any], index: int) -> Any:
    """安全取下标, 越界返回 None。"""
    return row[index] if index < len(row) else None


def clean_text(value: Any) -> str | None:
    """清洗文本: 压缩空白; '-' / '--' / 'null' 视为空。"""
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    if not text or text in {"-", "--", "null", "None"}:
        return None
    return text


def parse_float(value: Any) -> float | None:
    """解析浮点: 去千分位逗号和百分号, 失败返回 None (不抛异常)。"""
    text = clean_text(value)
    if text is None:
        return None
    try:
        return float(text.replace(",", "").replace("%", ""))
    except ValueError:
        return None


def parse_int(value: Any) -> int | None:
    parsed = parse_float(value)
    return int(parsed) if parsed is not None else None


def parse_bool_value(value: Any) -> bool | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip() in {"1", "true", "True"}


def normalize_date(value: Any, *, required: bool = False) -> str | None:
    """YYYYMMDD / YYYY-MM-DD → YYYY-MM-DD。"""
    if value in (None, ""):
        if required:
            raise KphValidationError("trade_date 不能为空")
        return None
    digits = re.sub(r"\D", "", str(value))
    if len(digits) != 8:
        raise KphValidationError(f"trade_date 必须是 YYYYMMDD 或 YYYY-MM-DD, 收到 {value!r}")
    try:
        datetime.strptime(digits, "%Y%m%d")
    except ValueError as exc:
        raise KphValidationError(f"trade_date 不是合法日期: {value!r}") from exc
    return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"


def today_dash() -> str:
    return date_type.today().strftime("%Y-%m-%d")


def previous_weekday(value: str) -> str:
    """上一个工作日 (跳过周六周日)。

    ⚠️ 这只是**启发式**回退: 无法处理法定节假日。
    真正的兜底是 provider 侧的「自动向前回退直到拿到数据」——
    上游对非交易日返回 errcode=1020, 由实测触发回退, 不依赖日历。
    """
    day = datetime.strptime(value, "%Y-%m-%d").date() - timedelta(days=1)
    while day.weekday() >= 5:  # 5=周六 6=周日
        day -= timedelta(days=1)
    return day.strftime("%Y-%m-%d")


def is_non_trading_error(exc: BaseException) -> bool:
    """判断异常是否为「该日期不是交易日」(上游 errcode=1020)。

    实测: 非交易日请求历史接口 → errcode=1020。

    优先用**结构化** `KphError.errcode`; 对没有该属性的异常 (第三方抛的,
    或历史代码路径) 回退到消息字符串匹配 —— 消息格式由本模块自己拼,
    上游改文案不会影响它, 但结构化判断仍然更可靠。
    """
    code = getattr(exc, "errcode", None)
    if code is not None:
        return str(code) == "1020"
    return "errcode=1020" in str(exc)


def historical_date(value: Any) -> str:
    """历史接口只支持过去日期 (含今天在部分接口也不行, 这里只拦 >= 今天)。"""
    trade_date = normalize_date(value, required=True)
    # ⚠️ 不能用 assert: Python -O 模式下 assert 被剥离, 这里会变成
    # `None >= today_dash()` → TypeError (而不是 KphValidationError), 破坏错误契约。
    if trade_date is None:
        raise KphValidationError(
            f"日期格式非法: {value!r} (需 YYYY-MM-DD 或 YYYYMMDD)"
        )
    if trade_date >= today_dash():
        raise KphValidationError("该 KPH 历史接口只接受早于今天的交易日")
    return trade_date


# 北交所判定: 二位前缀 {43, 83, 87, 92}
# 口径来源 backend/app/services/abnormal_moves.py:75 (board_of) —— 与项目内其它模块同源,
# 不能用简单的 ("4", "8") (会把 4xxxxx / 8xxxxx 全吞, 也不含 920xxx)
BJ_PREFIXES: frozenset[str] = frozenset({"43", "83", "87", "92"})


def normalize_symbol(code: Any) -> str | None:
    """裸代码 → 带交易所后缀的标准代码 (600519.SH / 000001.SZ / 430047.BJ)。

    口径来源 backend/app/services/abnormal_moves.py:75:
        if symbol.endswith(".BJ") or code[:2] in {"43", "83", "87", "92"}: → 北交所

    返回 None 表示代码无法识别 (交给调用方按"缺字段置 None"处理, 不抛异常)。
    """
    if code in (None, ""):
        return None
    clean = re.sub(r"\D", "", str(code))
    if len(clean) != 6:
        return None
    # 北交所判定必须**先于** 9 开头判定:
    #   920001 → "92" 命中 BJ 前缀 → BJ
    #   900001 → "90" 不命中 → 落到 9 开头 → SH (B 股段, 不是北交所)
    if clean[:2] in BJ_PREFIXES:
        return f"{clean}.BJ"
    if clean.startswith(("6", "5", "9")):
        return f"{clean}.SH"
    if clean.startswith(("0", "2", "3")):
        return f"{clean}.SZ"
    return None


def exchange_of(symbol: str | None) -> str | None:
    if not symbol or "." not in symbol:
        return None
    suffix = symbol.rsplit(".", 1)[1]
    return {"SH": "SSE", "SZ": "SZSE", "BJ": "BSE"}.get(suffix)
