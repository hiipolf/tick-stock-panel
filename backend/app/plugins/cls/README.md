# 财联社 (CLS) Provider 插件

财联社 (cls.cn) 24 小时持牌财经通讯社 HTTP 数据接入，按 vendor `plugin-development.md` 规范封装为 TSP Provider。

**数据集声明** (`plugin.yaml`):
- ✅ `daily` — 来自 CLS `cls_stock_kline`（个股日 K）
- ✅ `minute` — 来自 CLS `cls_stock_timeline`（个股当日分时）
- ❌ `realtime` / `depth5` / `adj_factor` / `financial` / `full_minute` — CLS 无对应数据，**未声明**，自动回退 tickflow / tdxquant

**API Key**：无（cls.cn 公开页面接口，无鉴权）

## 启用前必做

`client.py` 里的 endpoint URL (`https://api3.cls.cn/quote/stock/kline` + `/timeline`) 与响应结构是依据 AxData README 推断, vendor 启用前**必须**用浏览器开发者工具 / 抓包核对:

```text
1. 浏览器打开 https://www.cls.cn
2. 在任意个股页触发一次 K 线 / 分时加载
3. 抓 Network 面板, 记录真实 URL / params / response 字段名
4. 把真实 URL / 字段名写回 client.py 与 _map_daily_bar / _map_minute_bar
```

当前实现把 `time / volume / amount` 等字段名都做了软兼容（缺字段置 0 或 None），但若真实字段名差异较大，建议替换 `_map_*` 函数。

## 启用流程

```powershell
# 1. 后端跑契约测试 (vendor §"测试要求" — 用假 Client 注入, 不依赖真实网络)
cd D:\AI-Stock\tickflow-desktop\backend
uv run --frozen pytest tests/test_cls_provider.py -q
uv run --frozen ruff check app/plugins/cls/ tests/test_cls_provider.py

# 2. 启动 backend → 设置 → 数据源 → cls 卡片可见 (无 Key 配置项)
#    设置 → 数据源 → 切换 daily/minute 到 cls 源

# 3. 数据页 → 立即同步 → 选择 A 股 → 启动
```

## 关键设计决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 只声明 `daily` + `minute` | ✅ | CLS 只提供这 2 类有标准字段的数据；其他走 tickflow / tdxquant |
| 零 API Key | ✅ | CLS 公开页面无鉴权 |
| `runtime: python` (httpx 依赖) | ✅ | 跟 fuyao/tdxquant 一致, httpx 是后端默认依赖 |
| 端点 URL 用 `api3.cls.cn` 起点 | ⏳ 启用前必须用抓包核对 | 公开文档缺失, 靠 AxData README 推断 |
| 节流 1.5s/请求 | ✅ | CLS 无公开 SLA, 保守 1.5s |
| volume 转手、change_pct 转小数 | ✅ | vendor §"单位制" 强约束 |

## 不做的范围（明确）

- ❌ **不做**财务（CLS 无标准财务三表）→ 走 tickflow expert 或补独立 eastmoney_financial 插件
- ❌ **不做**复权因子（CLS K 线无复权字段）→ 走 TDX (`market-data-tick-adapter`) 或 tickflow
- ❌ **不做**五档盘口（CLS 无）→ 走 TDX/tdxquant
- ❌ **不做**涨停池 + 涨停原因 / 电报快讯（CLS 有但**不**符合 vendor 7 个标准数据集）→ 走**扩展数据 Snapshot**（参考 `backend/scripts/kph_fetcher.py` 模式）

## 失败模式

| 场景 | 行为 |
|---|---|
| 单个标的 fail | 跳过该标的，warning 日志，继续拉其他 |
| 所有标的 fail | 返回空 DataFrame（schema 仍合法），service 层记失败 |
| 整页 schema 变化 | 解析失败 → 跳过, 不静默 return 空（vendor 强约束）|
| 缺字段 | 0 或 None（按字段类型），**不**启发式补全 |
| endpoint 404/500 | 抛 httpx.HTTPError → service 层捕获 |