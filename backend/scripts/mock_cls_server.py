"""CLS (财联社) 端到端 mock server。

目的: 验证 vendor `app.plugins.cls` Provider 端到端流程(httpx 真实请求 →
节流 → JSON 解析 → 单位转换 → Polars DataFrame)能跑通, 不依赖真实 cls.cn
或真实 network 抓包。

启动方式:
    cd backend
    uv run python scripts/mock_cls_server.py              # 默认 127.0.0.1:9988
    uv run python scripts/mock_cls_server.py --port 9999  # 自定义端口

或在测试里 (in-process):
    from scripts.mock_cls_server import start_server, MOCK_BASE_URL
    server = start_server(port=0)  # 0 = 随机可用端口
    base_url = server.base_url
    ...
    server.stop()

mock 响应结构与真实 cls.cn 同款 (基于 AxData README 推断):
  GET /quote/stock/kline?code=600519&type=day&limit=200
  GET /quote/stock/timeline?code=600519

支持标的 (跟团队常用测试集对齐):
  600519 (SH) / 000001 (SZ) / 002664 (SZ) / 300750 (SZ) /
  688981 (SH) / 830799 (BJ) / 920001 (BJ)
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import threading
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

LOG = logging.getLogger("mock_cls_server")

# 默认端口
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9988


# ===== 测试标的: 静态 OHLCV 模板, 跟真实股票"长相"对齐 =====
# (last_close, vol_daily_shares, daily_change_pct, name)
SAMPLE_STOCKS: dict[str, dict[str, Any]] = {
    "600519": {"name": "贵州茅台",  "last_close": 1480.0, "avg_vol_shares": 1_200_000,  "daily_chg_pct": 1.5},
    "000001": {"name": "平安银行",  "last_close": 12.50,  "avg_vol_shares": 80_000_000, "daily_chg_pct": 0.8},
    "002664": {"name": "长城汽车",  "last_close": 25.30,  "avg_vol_shares": 35_000_000, "daily_chg_pct": -0.5},
    "300750": {"name": "宁德时代",  "last_close": 215.0,  "avg_vol_shares": 12_000_000, "daily_chg_pct": 2.3},
    "688981": {"name": "中芯国际",  "last_close": 58.40,  "avg_vol_shares": 18_000_000, "daily_chg_pct": -1.2},
    "830799": {"name": "北证测试",  "last_close": 18.20,  "avg_vol_shares": 5_000_000,  "daily_chg_pct": 0.0},
    "920001": {"name": "北证新段",  "last_close": 22.10,  "avg_vol_shares": 3_000_000,  "daily_chg_pct": 0.3},
}


def _gen_kline(code: str, days: int = 30) -> list[dict[str, Any]]:
    """生成 days 根日 K, 起点 (今天 - days, 终点 今天)。"""
    sample = SAMPLE_STOCKS.get(code, {
        "name": f"测试{code}", "last_close": 10.0, "avg_vol_shares": 5_000_000, "daily_chg_pct": 0.5,
    })
    rng = random.Random(code)  # 同一只股票序列稳定
    bars = []
    today = date.today()
    close = sample["last_close"] / (1 + sample["daily_chg_pct"] / 100)  # 倒数前一天收盘
    for i in range(days):
        d = today - timedelta(days=days - i)
        # 跳周末 (粗略: 周六周日不交易)
        if d.weekday() >= 5:
            continue
        chg = rng.uniform(-0.025, 0.030)  # ±2.5%/+3%
        open_p = close * (1 + rng.uniform(-0.005, 0.005))
        close = round(close * (1 + chg), 2)
        high = round(max(open_p, close) * (1 + rng.uniform(0, 0.008)), 2)
        low = round(min(open_p, close) * (1 - rng.uniform(0, 0.008)), 2)
        vol = int(sample["avg_vol_shares"] * rng.uniform(0.6, 1.4))
        amt = round(vol * (open_p + close) / 2, 2)
        bars.append({
            "time": d.isoformat(),
            "open": open_p, "high": high, "low": low, "close": close,
            "volume": vol, "amount": amt,
            "changePercent": round(chg * 100, 2),  # 百分数, vendor 测试断言透传
        })
    return bars


def _gen_timeline(code: str) -> list[dict[str, Any]]:
    """生成今日分时 (240 根, 9:30-11:30 + 13:00-15:00)。"""
    sample = SAMPLE_STOCKS.get(code, {"last_close": 10.0, "avg_vol_shares": 1_000_000})
    rng = random.Random(f"{code}-timeline")
    price = sample["last_close"] * 0.998
    out = []
    # 上午: 9:30 - 11:30 (120 根 1m)
    for i in range(120):
        h, m = 9 + (i + 30) // 60, (i + 30) % 60
        if h >= 11 and m > 30:
            break
        price *= 1 + rng.uniform(-0.001, 0.0012)
        minute = f"{h:02d}:{m:02d}"
        out.append({
            "minute": minute,
            "last_price": round(price, 2),
            "open": round(price * 0.999, 2),
            "high": round(price * 1.001, 2),
            "low": round(price * 0.998, 2),
            "volume": int(sample["avg_vol_shares"] / 240 * rng.uniform(0.5, 1.5)),
            "amount": round(price * sample["avg_vol_shares"] / 240 * rng.uniform(0.5, 1.5), 2),
        })
    # 下午: 13:00 - 15:00 (120 根 1m)
    for i in range(120):
        h, m = 13 + i // 60, i % 60
        price *= 1 + rng.uniform(-0.0008, 0.001)
        minute = f"{h:02d}:{m:02d}"
        out.append({
            "minute": minute,
            "last_price": round(price, 2),
            "open": round(price * 0.999, 2),
            "high": round(price * 1.001, 2),
            "low": round(price * 0.998, 2),
            "volume": int(sample["avg_vol_shares"] / 240 * rng.uniform(0.5, 1.5)),
            "amount": round(price * sample["avg_vol_shares"] / 240 * rng.uniform(0.5, 1.5), 2),
        })
    return out


# ===== HTTP handler =====

class _MockClsHandler(BaseHTTPRequestHandler):
    """按 vendor cls 接口 shape mock 响应。"""

    # 减少测试输出噪音
    def log_message(self, format: str, *args: Any) -> None:
        LOG.debug(format, *args)

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        u = urlparse(self.path)
        params = {k: v[0] for k, v in parse_qs(u.query).items()}
        # 支持测试时手动注入错误
        if "code" in params and params["code"].startswith("ERR"):
            self._send_json(500, {"error": "simulated server error", "code": params["code"]})
            return
        if "code" in params and params["code"] == "000000":
            self._send_json(200, {"data": []})  # 空数据, 跟真实响应结构一致
            return

        if u.path == "/quote/stock/kline":
            code = params.get("code", "600519")
            limit = int(params.get("limit", 200))
            bars = _gen_kline(code, days=min(limit, 250))
            self._send_json(200, {"data": bars})
            return
        if u.path == "/quote/stock/timeline":
            code = params.get("code", "600519")
            bars = _gen_timeline(code)
            self._send_json(200, {"data": bars})
            return
        if u.path == "/health":
            self._send_json(200, {"ok": True, "ts": datetime.now().isoformat()})
            return

        # 未知 endpoint
        self._send_json(404, {"error": f"unknown path: {u.path}"})


# ===== 服务管理 =====

class MockClsServer:
    """轻量包装, 供测试 inline 起。"""

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
        self.host = host
        self.port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        self._server = ThreadingHTTPServer((self.host, self.port), _MockClsHandler)
        self.port = self._server.server_address[1]  # 实际绑定端口 (port=0 时)
        self._thread = threading.Thread(target=self._server.serve_forever, name="mock-cls", daemon=True)
        self._thread.start()
        LOG.info("mock cls server started at %s", self.base_url)

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if self._thread:
            self._thread.join(timeout=5)
        LOG.info("mock cls server stopped")


def start_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> MockClsServer:
    """便捷函数: 起 server, 返回 wrapper. port=0 让 OS 分配可用端口."""
    server = MockClsServer(host=host, port=port)
    server.start()
    return server


# ===== CLI =====

def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="CLS (财联社) 端到端 mock server")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)

    server = start_server(host=args.host, port=args.port)
    print(f"✓ mock cls server listening on {server.base_url}")
    print("  endpoints:")
    print(f"    GET {server.base_url}/quote/stock/kline?code=600519&type=day&limit=200")
    print(f"    GET {server.base_url}/quote/stock/timeline?code=600519")
    print(f"    GET {server.base_url}/health")
    print(f"  test codes: {', '.join(SAMPLE_STOCKS.keys())}")
    print("  error trigger: code=ERRxxx  → HTTP 500")
    print("  empty trigger: code=000000  → HTTP 200 with empty data")
    print("  press Ctrl+C to stop")
    try:
        server._thread.join()  # type: ignore[union-attr]
    except KeyboardInterrupt:
        print("\nshutting down...")
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())