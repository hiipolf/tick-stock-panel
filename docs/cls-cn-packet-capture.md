# 财联社 (cls.cn) / 开盘红 (kphhq.com) 真实启用指南

> **关键更新 (2026-10-18)**: 经仔细阅读 AxData 官方文档 (cls_stock_kline /
> cls_stock_timeline / kph_limit_up_history 等接口页), 字段名 / 单位
> **已完全明确**。本指南抓包范围从"猜字段 + 猜单位 + 猜 URL"大幅
> 收窄为**只确认 URL + 鉴权方式**。
>
> 详细字段定义见 vendor `app/plugins/cls/README.md` 和
> `scripts/kph_fetcher.py` 模块 docstring (代码注释里写明了 AxData 文档
> 对应字段).

## 1. 还需要抓包确认什么

| 维度 | 是否需抓包 | 备注 |
|---|---|---|
| 字段名 | ❌ 不需要 | AxData 文档已列全 14 字段, vendor 代码已对齐 |
| 字段类型 | ❌ 不需要 | 文档明列 string/number/date/integer |
| 单位 | ❌ 大部分不需要 | amount=元, change_pct=小数制, AxData 已标准化 |
| URL host | ✅ 需要 | AxData 隐藏, 真实 cls.cn / kphhq.com host 未公开 |
| URL path | ✅ 需要 | `/quote/stock/kline` 是 vendor 占位, 待确认 |
| 鉴权方式 | ✅ 需要 | 是否需要 cookies / Referer / 特定 UA / IP 白名单 |
| `volume` 真实单位 | ⚠️ 仍需确认 | 文档只说 "成交量" number, 不知道是手还是股, 当前 vendor 代码默认按"股 ÷100=手"处理 |

## 2. 抓包步骤 (Chrome DevTools)

```text
1. 打开 Chrome, 访问 https://www.cls.cn
2. F12 打开 DevTools → Network 面板
3. Filter 选 "Fetch/XHR" / "All"
4. 在 cls.cn 搜索任意个股 (如 600519)
5. 找到 K 线 / 分时请求 → 右键 Copy as cURL (bash)
6. 记录: host / path / method / query string / required headers
7. 在 K 线面板里 "切换到分时" → 同样抓一次 timeline 请求
8. 同样步骤访问 https://www.kphhq.com 抓 kph_limit_ladder /
   kph_market_emotion / kph_sector_ranking 等
```

备选工具:
- **Fiddler** (Windows GUI): 装好直接抓, 免配
- **mitmproxy** (CLI): `mitmproxy --set "block_global=false"`
- **Charles** (Mac GUI): 同 Fiddler

## 3. 填回真实数据 (只改 4 个常量)

### 3.1 `backend/app/plugins/cls/client.py`

```python
BASE_URL = "https://<真实 host>"  # 改这一行即可
```

### 3.2 `backend/app/plugins/cls/provider.py`

修改 `get_daily` 调用的 `path` 和 `params` (vendor 当前的占位是
`/quote/stock/kline` + `params={"code": bare, "type": "day"}`):

```python
payload = client.get(
    "<真实 path>",  # 例如 /api/quote/kline 或 /stock/kline
    params={
        # 按抓包真实参数名填, 常见字段: code / kline_type / limit
        "code": bare,
        "<其他真实参数>": "<value>",
    },
)
```

`get_minute` 同样改 path + params.

### 3.3 `backend/scripts/kph_fetcher.py`

修改 7 个 fetch_* 函数的 `client.get(path, params=...)` — 7 个 path 全部
是占位, 真实 KPH endpoint 待抓包确认.

### 3.4 `volume` 单位确认

抓包后看真实响应里 `volume` 是手还是股:
- 若量级跟行情软件 "成交量(手)" 一致 (如贵州茅台日 1 万手 = 10000) → 已经是手, vendor 代码需要去掉 `/ 100.0` 转换
- 若量级是 100 倍 (1 百万) → 是股, 保持当前 `/ 100.0` 不动

## 4. 验证契约测试

每次修改 BASE_URL / path / params 后, 跑契约测试:

```powershell
cd backend
uv run --frozen pytest tests/test_cls_provider.py -q
uv run --frozen pytest tests/test_kph_fetcher.py -q
```

测试用 fake client 注入, 不依赖真实网络. 如果响应结构真的跟 AxData
文档对齐, 测试 fixture 不需要改 (代码已用 AxData 字段名).

## 5. 端到端 mock 验证 (无真实网络)

在真实 endpoint 接入前, 用 mock server 验证流程:

```powershell
# 起 mock server
cd backend
uv run python scripts/mock_cls_server.py --port 9988
# 另一个 shell 跑 e2e
cd backend
uv run --frozen pytest tests/test_cls_e2e_mock.py -q
```

如果 e2e 通过, 说明 cls 插件端到端流程正确. mock server 的响应结构
已对齐 AxData 文档, 抓包回来后改 client.py BASE_URL 和 provider.py
的 path / params 即可上线, 不需要改响应结构.

## 6. 真实启用流程 (端到端)

```powershell
# 1. 抓包 (第 2 节) → 拿到真实 host / path / 鉴权
# 2. 改 BASE_URL + path + params (第 3 节)
# 3. 改 kph_fetcher 7 个 path (第 3.3 节)
# 4. 跑契约测试
cd backend
uv run --frozen pytest tests/test_cls_provider.py tests/test_kph_fetcher.py tests/test_cls_e2e_mock.py -q
# 5. 启动 backend, 设置 → 数据源 → cls 卡片可见 (无需 Key)
# 6. 设置 → 数据源 → 切换 daily / minute 到 cls 源
# 7. 数据页 → 立即同步 → 选择 A 股 → 启动
```

## 7. 与 AxData 文档的差异点 (vendor 当前占位 vs 真实接口)

vendor 当前占位 (`/quote/stock/kline`, `?type=day`, `?code=...`) 是基于
README 推断的. 真实 cls.cn 接口**可能**:
- 用 `/api/quote/kline` 或 `/quote/kline` 等不同 path
- 用 `kline_type=daily` (AxData 标准名) 而非 `type=day` (vendor 当前)
- 需要 cookies / Referer 校验
- 字段名虽与 AxData 对齐 (AxData 自己就是中转, 字段名跟上游一致) 但不保证 100%

任何不匹配, 改 vendor 代码 (BASE_URL / path / params) 而**不改**
字段名 (字段名按 AxData 文档已锁定).

## 8. 已知风险

- **反爬**: 财联社公开页面无显式鉴权, 但可能对高频请求限速. min_interval=1.5s
  保守值, 跑一段时间如触发限速, 再调高.
- **字段稳定性**: 财联社 / 开盘红 公开 API 字段名未公开承诺. 但因 AxData 文档已对齐,
  vendor 代码的字段名是上游公开标准的下游, 不会因 cls.cn / kphhq.com 自身
  变化而大改.
- **历史数据**: `cls_stock_timeline` 只返回当日分时, 历史分时需付费.
  `cls_stock_kline` 返回近期一段历史, 够做日 K, 不够 10 年回测.
- **KPH 鉴权**: 开盘红没公开 API, 大概率需要商务对接或浏览器模拟. 抓包可能
  看到 cookie / token 之类的鉴权头, 真实启用前必须与 KPH 商务确认.
