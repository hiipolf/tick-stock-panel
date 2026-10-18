"""财联社 httpx 客户端。

职责: 节流 + 通用 GET, 不知道 provider/services 层。
endpoint URL 是基于 AxData README + vendor 已知 subclass 推断, vendor 启用前
需要用浏览器开发者工具 / 抓包核对真实路径与响应结构。
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://api3.cls.cn"
DEFAULT_TIMEOUT = 10.0
DEFAULT_MIN_INTERVAL = 1.5  # 财联社无公开 SLA, 保守 1.5s/请求


class ClsClient:
    """财联社 REST 客户端 + 单调用节流。"""

    def __init__(
        self,
        base_url: str = BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        min_interval: float = DEFAULT_MIN_INTERVAL,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.min_interval = min_interval
        self._last_call = 0.0
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
                ),
                "Referer": "https://www.cls.cn/",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
        )

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_call = time.monotonic()

    def get(self, path: str, params: dict | None = None) -> dict[str, Any]:
        """GET + 节流。响应 JSON 解析失败抛 ValueError, 非 2xx 抛 httpx.HTTPStatusError。"""
        self._throttle()
        resp = self._http.get(path, params=params or {})
        resp.raise_for_status()
        return resp.json()

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> ClsClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()