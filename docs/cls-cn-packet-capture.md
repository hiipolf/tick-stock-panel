# 财联社 (cls.cn) 端点抓包与真实化指南

> 本文档面向 vendor `app/plugins/cls` 插件的**真实启用**步骤。当前的
> `client.py` 里的 `https://api3.cls.cn/quote/stock/{kline,timeline}` 是
> 基于 AxData README 推断的占位,真实启用前**必须**用浏览器开发者工具 / 抓包
> 工具核对 endpoint URL 和响应结构。

## 1. 为什么需要抓包

vendor §plugin-development.md 的"内部数据契约" 强约束:
- symbol 必带交易所后缀
- change_pct 小数制
- volume 手
- amount 元
- 缺字段不启发式补全

当前 `_map_daily_bar` / `_map_minute_bar` 按 AxData 推断的字段名实现。如果
真实 cls.cn 接口字段名差异较大 (如返回 `openPrice` 而非 `open`),会导致
数据全为 0 或 None。抓包就是为了拿到真实字段名。

## 2. 抓包步骤 (Chrome DevTools)

```text
1. 打开 Chrome,访问 https://www.cls.cn
2. F12 打开 DevTools → Network 面板
3. Filter 选 "Fetch/XHR"
4. 在 cls.cn 搜索任意个股 (如 600519)
5. 找到 "kline" / "timeline" / "stock" 等请求
6. 右键 → Copy → Copy as cURL (bash)
7. 把 cURL 里的 URL + headers + params 填入第 3 节
```

备选工具:
- **Fiddler** (Windows GUI): 装好直接抓,免配
- **mitmproxy** (命令行): `mitmproxy --set "block_global=false"`
- **Charles** (Mac GUI): 同 Fiddler

## 3. 需要填回的真实数据

打开 `backend/app/plugins/cls/client.py`,替换 `BASE_URL`:

```python
# 改成真实抓到的 endpoint (没有路径前缀,只有 host)
BASE_URL = "https://<真实 host, 如 quote.cls.cn>"
```

打开 `backend/app/plugins/cls/provider.py`,按真实响应字段名更新
`_map_daily_bar` / `_map_minute_bar`:

```python
# 示例: 真实字段名是 openPrice/highPrice/lowPrice 而不是 open/high/low
{
    "time": bar.get("time") or bar.get("date"),
    "open": float(bar.get("openPrice") or bar.get("open") or 0),
    "high": float(bar.get("highPrice") or bar.get("high") or 0),
    # ... 同理
}
```

## 4. 更新契约测试 fixture

打开 `backend/tests/test_cls_provider.py`,把 `SAMPLE_KLINE` / `SAMPLE_TIMELINE`
的响应结构换成**真实抓包的一次响应**(脱敏后):

```python
# 拿一次 curl 命令的 JSON 输出,粘到 SAMPLE_KLINE 里
SAMPLE_KLINE = {
    ("/quote/stock/kline", "002664"): {
        "data": [
            {
                "time": "2024-01-15",
                "openPrice": 25.00,    # ← 真实字段名
                "highPrice": 25.50,
                # ...
            },
        ]
    },
}
```

然后跑契约测试 (vendor §"测试要求"):

```powershell
cd backend
uv run --frozen pytest tests/test_cls_provider.py -q
uv run --frozen ruff check app/plugins/cls/ tests/test_cls_provider.py
```

## 5. 端到端 mock 验证 (无真实网络)

在真实 endpoint 接入前,可以用 vendor 提供的 mock server 验证流程:

```powershell
# 起 mock server
cd backend
uv run python scripts/mock_cls_server.py --port 9988
# 另一个 shell 跑 e2e
cd backend
uv run --frozen pytest tests/test_cls_e2e_mock.py -q
```

如果 e2e 通过,说明 cls 插件端到端流程 (httpx → JSON → 单位转换 → Polars) 正确。
真实 endpoint 接入只需要改 BASE_URL 和响应字段名。

## 6. 真实启用流程

```powershell
# 1. 抓包 (第 2 节)
# 2. 改 BASE_URL (第 3 节)
# 3. 改 _map_daily_bar / _map_minute_bar (第 3 节)
# 4. 更新契约测试 fixture (第 4 节)
# 5. 跑测试
cd backend
uv run --frozen pytest tests/test_cls_provider.py tests/test_cls_e2e_mock.py -q
# 6. 启动 backend, 设置 → 数据源 → cls 卡片可见 (无需 Key)
# 7. 设置 → 数据源 → 切换 daily / minute 到 cls 源
# 8. 数据页 → 立即同步 → 选择 A 股 → 启动
```

## 7. 已知风险 (按 vendor 公开约束)

- **反爬**: 财联社公开页面无显式鉴权,但可能对高频请求限速。当前 min_interval=1.5s
  保守值,跑一段时间如触发限速,再调高。
- **字段稳定性**: 财联社公开 API 字段名未公开承诺,大版本升级时可能变化。`kph_readme.md`
  提到的"启用前必做抓包"对 cls 同样适用。
- **历史数据**: `cls_stock_timeline` 只返回当日分时,历史分时需付费。`cls_stock_kline`
  返回近期一段历史,够做日 K,但不够 10 年回测。
