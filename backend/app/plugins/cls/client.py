"""财联社 (CLS / cls.cn) HTTP 客户端.

协议全部来自 2026-10-19 **真实连跑** 实测 (12/12 接口返回 200), 不是猜的。

## 两类接口, 两套签名

CLS 有两套互不相同的调用风格, 混用会 400/空包:

1. **Web 端** (`stock/emotion` / `plate_list` / `up_down_analysis` / `tline` / `kline`)
   - 固定 base `{app: CailianpressWeb, os: web, sv: 8.4.6}`
   - `sign` 是**硬编码常量** (不是算出来的), 见 `WEB_SIGNS`
   - 请求头 `User-Agent: Mozilla/5.0 AxData/0.1`
2. **移动端** (`todayTuyere` / `dingPan/mainline` / `plate_heat_list` / `plate/rotation` /
   `plate/tuyere/stocks` / `plate/popular_stocks` / `roll/get_roll_list`)
   - 固定 base `{app: cailianpress, sv: 8.7.4, os: android, ...}`
   - `sign` **动态计算**: `md5(sha1("&".join(sorted(k=v))))`
   - 请求头 `User-Agent: okhttp/4.9.0`

实测补充 (与最初的口径说明有出入, 以实测为准):
- **信封按域名分**: `api3.cls.cn` 返回 `{"errno": 0, "data": ...}`;
  `x-quote.cls.cn` 返回 `{"code": 200, "data": ...}`。本模块两种都认 (见 `_unwrap`)。
- **tline 的 `fields` 必须是字段名** (`minute,last_px,...`)。传数字下标 (`1,2,3`)
  上游不报错, 但**每格都返回 null** —— 静默失败, 极易误判成"没数据"。
- **tline 的 `data.date` 是 list** (`[20261019]`), 不是 int; 每一行 line 项里另带自己的 `date`。
- **`plate/rotation` 的 `days` 只支持 4 / 30**; 传 6 上游返回
  `{"code":200,"data":{"days":"支持 4/30"}}` —— data 是 dict 不是 list, 解析必须逐项 isinstance 判断。
- **`tuyere/stocks` 与 `popular/stocks` 的 `change` 是带百分号的字符串** (`"+20.00%"`),
  `last_px` 也是字符串; 与 kline/tline 的小数制不是一回事。

本模块只负责 HTTP + 协议层解包, 不认识 TSP 的 provider 契约 (见 provider.py)。
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# ────────────────── 端点 (全部实测 200) ──────────────────

CLS_EMOTION_URL = "https://x-quote.cls.cn/v2/quote/a/stock/emotion"
CLS_TUYERE_URL = "https://api3.cls.cn/v2/todayTuyere"
CLS_TUYERE_STOCKS_URL = "https://x-quote.cls.cn/v2/quote/a/plate/tuyere/stocks"
CLS_MAINLINE_URL = "https://api3.cls.cn/v2/dingPan/mainline"
CLS_PLATE_LIST_URL = "https://x-quote.cls.cn/web_quote/plate/plate_list"
CLS_PLATE_HEAT_URL = "https://x-quote.cls.cn/v2/quote/a/plate/plate_heat_list"
CLS_PLATE_POPULAR_URL = "https://x-quote.cls.cn/v2/quote/a/plate/popular_stocks"
CLS_PLATE_ROTATION_URL = "https://x-quote.cls.cn/v2/quote/a/plate/rotation"
CLS_LIMIT_UP_URL = "https://x-quote.cls.cn/quote/index/up_down_analysis"
CLS_TIMELINE_URL = "https://x-quote.cls.cn/quote/stock/tline"
CLS_KLINE_URL = "https://x-quote.cls.cn/quote/stock/kline"
CLS_TELEGRAPH_URL = "https://api3.cls.cn/v1/roll/get_roll_list"

# 电报单页条数上限: 实测 rn>50 上游静默返回空列表 (不是截断)
TELEGRAPH_MAX_RN = 50

# ────────────────── 签名基座 ──────────────────

# 移动端固定 base (实测可用值)
MOBILE_BASE: dict[str, str] = {
    "app": "cailianpress",
    "sv": "8.7.4",
    "os": "android",
    "mb": "Xiaomi-2206123SC",
    "ov": "32",
    "channel": "8",
    "motif": "0",
    "net": "",
    "province_code": "3205",
    "token": "",
    "uid": "",
}

# Web 端固定 base
WEB_BASE: dict[str, str] = {
    "app": "CailianpressWeb",
    "os": "web",
    "sv": "8.4.6",
}

# Web 端硬编码 sign (上游只校验这组固定参数; 换标的/换 limit/换 offset 都实测照常返回 200)
WEB_SIGNS: dict[str, str] = {
    "emotion": "9f8797a1f4de66c2370f7a03990d2737",
    "plate_list": "ef1ec7886be706a0b722d7e7bf3c0054",
    "limit_up": "a6ab28604a6dbe891cdbd7764799eda1",
    "timeline": "afad7ec0475a1b9854313502389f3346",
    "kline": "d2656d0d3fdc1d489f6f316ea820cc17",
}

MOBILE_UA = "okhttp/4.9.0"
WEB_UA = "Mozilla/5.0 AxData/0.1"
REFERER = "https://www.cls.cn/"

# tline 需要的字段 (必须是**字段名**, 传数字下标上游静默返回 null)
TLINE_FIELDS = (
    "date,minute,last_px,av_px,open_px,preclose_px,change,amp,change_px,"
    "purchases,sales,business_amount,business_balance"
)

# kline 的周期 type 取值
KLINE_TYPES: frozenset[str] = frozenset({"fd1", "fw", "fm", "fy"})

# plate/rotation 上游只支持这两个天数
ROTATION_DAYS: frozenset[int] = frozenset({4, 30})

DEFAULT_TIMEOUT = 15.0
# CLS 无公开 SLA, 保守节流. 实测 12 个接口一次跑完没被限流, 1.0s 足够。
DEFAULT_MIN_INTERVAL = 1.0

# kline 单次 limit 上限
KLINE_MAX_LIMIT = 500


class ClsError(Exception):
    """财联社接口错误 (网络失败 / 信封非 0 / 响应结构异常)。"""


class ClsValidationError(ClsError):
    """参数校验错误 (调用方传错)。

    与 ClsError 的运行时错误区分: 校验错误是**调用方的错**, 应当 fail-fast 抛出去;
    运行时错误 (网络/上游) 才允许在批量场景下软失败跳过。
    """


# ────────────────── 签名 ──────────────────


def make_sign(params: Mapping[str, Any]) -> str:
    """动态 sign: `md5(sha1("&".join(f"{k}={v}" for k,v in sorted(params.items()))))`。

    ⚠️ 入参**不能**包含 sign 本身 (否则会算进待签串里)。

    实测样例 (固定输入 → 确定值, 见 tests/test_cls_provider.py):
        make_sign({"a": "1", "b": "2"}) == "4e13ecb1934db939e390065a9b63ecd8"
    """
    text = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    sha1 = hashlib.sha1(text.encode()).hexdigest()
    return hashlib.md5(sha1.encode()).hexdigest()


def signed_params(params: Mapping[str, Any], base: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """移动端参数: 固定 base + 业务参数 + 动态 sign。"""
    merged = {**(base if base is not None else MOBILE_BASE), **params}
    merged["sign"] = make_sign(merged)
    return merged


def web_params(params: Mapping[str, Any], sign: str) -> dict[str, Any]:
    """Web 端参数: 固定 base + 业务参数 + **硬编码** sign。"""
    return {**WEB_BASE, **params, "sign": sign}


# ────────────────── 客户端 ──────────────────


class ClsClient:
    """财联社 HTTP 客户端 + 节流。

    节流语义与 plugins/kph 一致: 串行, 两次请求之间至少间隔 `min_interval` 秒。
    """

    def __init__(
        self,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        timeout: float = DEFAULT_TIMEOUT,
        web_user_agent: str = WEB_UA,
        mobile_user_agent: str = MOBILE_UA,
    ) -> None:
        self.min_interval = min_interval
        self.timeout = timeout
        self.web_user_agent = web_user_agent
        self.mobile_user_agent = mobile_user_agent
        self._last_call = 0.0
        self._http = httpx.Client(
            timeout=timeout,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> ClsClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ────────────────── 内部 ──────────────────

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_call = time.monotonic()

    def _request(
        self,
        url: str,
        params: Mapping[str, Any],
        *,
        user_agent: str,
        context: str,
    ) -> Any:
        """GET + 信封解包。返回上游 `data` 字段 (原样, 不做结构假设)。"""
        self._throttle()
        host = urlparse(url).netloc
        headers = {
            "User-Agent": user_agent,
            "Referer": REFERER,
            "Host": host,
            "Accept": "application/json, text/plain, */*",
        }
        try:
            resp = self._http.get(url, params=dict(params), headers=headers)
        except httpx.HTTPError as exc:
            raise ClsError(f"{context} 网络请求失败: {exc}") from exc
        if resp.status_code != 200:
            raise ClsError(f"{context} HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise ClsError(f"{context} 响应不是 JSON: {resp.text[:200]}") from exc
        return self._unwrap(payload, context)

    @staticmethod
    def _unwrap(payload: Any, context: str) -> Any:
        """拆信封。

        实测两种信封: `api3.cls.cn` → `{"errno": 0, "data": ...}`;
        `x-quote.cls.cn` → `{"code": 200, "data": ...}`。两种都认, 认不出就报错。
        """
        if not isinstance(payload, Mapping):
            raise ClsError(f"{context} 响应结构异常 (不是对象)")
        errno = payload.get("errno")
        code = payload.get("code")
        if errno is not None and errno not in (0, "0"):
            raise ClsError(f"{context} 上游返回 errno={errno} msg={payload.get('msg')!r}")
        if code is not None and code not in (200, "200"):
            raise ClsError(f"{context} 上游返回 code={code} msg={payload.get('msg')!r}")
        if errno is None and code is None:
            raise ClsError(f"{context} 响应缺少 code/errno 信封")
        return payload.get("data")

    def _web(self, url: str, params: Mapping[str, Any], sign: str, context: str) -> Any:
        return self._request(
            url, web_params(params, sign), user_agent=self.web_user_agent, context=context
        )

    def _mobile(self, url: str, params: Mapping[str, Any], context: str) -> Any:
        return self._request(
            url,
            signed_params(params),
            user_agent=self.mobile_user_agent,
            context=context,
        )

    # ────────────────── Web 端接口 (硬编码 sign) ──────────────────

    def kline(
        self,
        secu_code: str,
        *,
        kline_type: str = "fd1",
        limit: int = KLINE_MAX_LIMIT,
        offset: int = 0,
    ) -> Any:
        """个股 K 线。`kline_type`: fd1=日 / fw=周 / fm=月 / fy=年。

        data 是 list of dict, 字段: date(int YYYYMMDD) / open_px / high_px / low_px /
        close_px / preclose_px / change(小数制) / business_amount(股) /
        business_balance(元) / tr / amp / ma5 / ma10 / ma20。
        """
        if kline_type not in KLINE_TYPES:
            raise ClsValidationError(
                f"kline_type 必须是 {sorted(KLINE_TYPES)} 之一, 收到 {kline_type!r}"
            )
        if limit < 1 or limit > KLINE_MAX_LIMIT:
            raise ClsValidationError(f"limit 必须在 1..{KLINE_MAX_LIMIT}, 收到 {limit}")
        if offset < 0:
            raise ClsValidationError(f"offset 不能为负, 收到 {offset}")
        return self._web(
            CLS_KLINE_URL,
            {"secu_code": secu_code, "type": kline_type, "limit": limit, "offset": offset},
            WEB_SIGNS["kline"],
            "CLS kline",
        )

    def timeline(self, secu_code: str, *, fields: str = TLINE_FIELDS) -> Any:
        """个股当日分时。

        ⚠️ `fields` 必须是**字段名**; 传数字下标上游不报错但每格都是 null (静默失败)。
        data 是 dict: `{"date": [YYYYMMDD, ...], "line": [{...}, ...]}`。
        line 每项: date / minute(int HHMM) / last_px / av_px / open_px / preclose_px /
        change / amp / change_px / purchases / sales / business_amount(股) /
        business_balance(元)。
        """
        return self._web(
            CLS_TIMELINE_URL,
            {"secu_code": secu_code, "fields": fields},
            WEB_SIGNS["timeline"],
            "CLS timeline",
        )

    def stock_emotion(self) -> Any:
        """市场情绪截面。data 是 dict。

        含 `up_down_dis` (涨跌家数分布) 与 `limit_up_board` (**连板梯队**:
        row1=梯队名 / row2=家数 / row3=连板率) —— 这是 CLS 最有价值的短线数据。
        """
        return self._web(CLS_EMOTION_URL, {}, WEB_SIGNS["emotion"], "CLS emotion")

    def limit_up_pool(self, pool_type: str = "up_pool") -> Any:
        """涨停/跌停池。data 是 list of dict。

        每项: secu_code / secu_name / change(小数制) / last_px / time /
        up_reason(题材|原因, `|` 分隔) / num_stocks / plate(所属题材列表)。
        """
        return self._web(
            CLS_LIMIT_UP_URL,
            {"rever": 1, "type": pool_type, "way": "last_px"},
            WEB_SIGNS["limit_up"],
            "CLS limit_up_pool",
        )

    def plate_list(self, page: int = 1) -> Any:
        """行业板块列表。data 是 dict: `{"is_all": 1, "plate_data": [...]}`。

        plate_data 每项: secu_code / secu_name(板块名) / change(小数制) /
        main_fund_diff / limit_up / limit_down / limit_up_num / limit_down_num /
        trade_status / first_stock(领涨股 dict)。
        """
        return self._web(
            CLS_PLATE_LIST_URL,
            {"page": page, "rever": 1, "type": "industry", "way": "change"},
            WEB_SIGNS["plate_list"],
            "CLS plate_list",
        )

    # ────────────────── 移动端接口 (动态 sign) ──────────────────

    def today_tuyere(self) -> Any:
        """今日题材 (市场风向标)。data 是 dict, `today_tuyere` 是 list。

        每项: id / title / plate_code / subject_id / interpret。
        """
        return self._mobile(CLS_TUYERE_URL, {}, "CLS todayTuyere")

    def market_mainline(self) -> Any:
        """主线机会。data 是 dict。

        ⚠️ 三个键的值**类型不同**: `chances` / `winds` 是 list, `styles` 是 **dict**。
        解析必须逐项 isinstance 判断 (见 provider.get_market_mainline)。
        """
        return self._mobile(CLS_MAINLINE_URL, {}, "CLS mainline")

    def plate_heat(
        self, plate_type: str = "concept", page: int = 1, page_size: int = 20
    ) -> Any:
        """板块热度排行。data 是 list of dict。

        每项: plate_code / rank / cur_heat / rank_change / is_new / plate_name。
        """
        return self._mobile(
            CLS_PLATE_HEAT_URL,
            {"type": plate_type, "page": page, "page_size": page_size},
            "CLS plate_heat",
        )

    def plate_rotation(self, days: int = 4) -> Any:
        """板块轮动。data 是 list of `{"trade_date": ..., "plates": [...]}`。

        ⚠️ 上游只支持 days=4 / days=30; 其它值返回 `{"days": "支持 4/30"}` (dict, 非 list)。
        """
        if days not in ROTATION_DAYS:
            raise ClsValidationError(
                f"days 必须是 {sorted(ROTATION_DAYS)} 之一 (上游只支持这两个), 收到 {days}"
            )
        return self._mobile(
            CLS_PLATE_ROTATION_URL, {"days": days}, "CLS plate_rotation"
        )

    def tuyere_stocks(
        self, plate_code: str, page: int = 1, page_size: int = 20
    ) -> Any:
        """题材成分股。data 是 list of dict。

        ⚠️ `change` 是**带百分号的字符串** (`"+20.00%"`), `last_px` 也是字符串。
        每项: secu_code / secu_name / ori_code / change / change_px / last_px /
        trade_status / limit_up_days(连板数)。
        """
        return self._mobile(
            CLS_TUYERE_STOCKS_URL,
            {"plate_code": plate_code, "page": page, "page_size": page_size},
            "CLS tuyere_stocks",
        )

    def popular_stocks(self, plate_code: str, page: int = 1, page_size: int = 20) -> Any:
        """板块人气股。data 是 list of dict。

        ⚠️ `change` 是带百分号的字符串。每项: secu_code / secu_name / change /
        change_px / last_px / tbm / head_num(讨论热度)。
        """
        return self._mobile(
            CLS_PLATE_POPULAR_URL,
            {"plate_code": plate_code, "page": page, "page_size": page_size},
            "CLS popular_stocks",
        )

    def telegraph(self, rn: int = 20, last_time: str | int = "") -> Any:
        """电报快讯。data 是 dict, `roll_data` 是 list。

        每项: id / title / content / brief / ctime(unix 秒) / level("C"=重要) /
        reading_num。

        ⚠️ 实测要点 (2026-10-19 逐项验证):
        - **必须带 `refresh_type=1`** —— 不带时上游**忽略** last_time, 永远返回最新一批
          (AxData 开源实现就是漏了这个参数, 导致历史日期永远取不到)
        - `last_time` 必须是 **epoch 秒的字符串**; 传 "2026-10-13 23:59:59" 这种
          格式化字符串 → 上游返回 0 条 (不报错, 静默空)
        - `rn` **上限 50** —— rn=60/80/100 上游直接返回 0 条 (不是截断成 50)
        - `last_time` 是**向前翻页的锚点**: 返回 ctime <= last_time 的最近 rn 条;
          要往前翻就用本页最小 ctime - 1 作为下一个锚点
        - 上游**不认** category / level 参数 (传了返回结果一样), 按日期/级别过滤只能在客户端做
        """
        if rn < 1:
            raise ClsValidationError(f"rn 必须 >= 1, 收到 {rn}")
        if rn > TELEGRAPH_MAX_RN:
            raise ClsValidationError(
                f"rn 不能超过 {TELEGRAPH_MAX_RN} (上游 rn>50 静默返回空列表), 收到 {rn}"
            )
        params: dict[str, Any] = {"rn": rn, "refresh_type": "1"}
        if last_time not in (None, ""):
            try:
                params["last_time"] = str(int(last_time))
            except (TypeError, ValueError) as exc:
                raise ClsValidationError(f"last_time 必须是 epoch 秒, 收到 {last_time!r}") from exc
        return self._mobile(
            CLS_TELEGRAPH_URL,
            params,
            "CLS telegraph",
        )
