# KPH (开盘红) 数据接入

> **实施前必读**: KPH 没有公开 API 文档, 本目录的 `kph_fetcher.py` 和
> 配置示例是**占位骨架**。真实启用前**必须**用浏览器开发者工具抓包确认
> endpoint URL 和响应结构。

## 1. 为什么 KPH 不做成 Provider

vendor `plugin-development.md` §"能力声明的粒度" 原文:

> 只声明真实提供的数据集; 未声明的数据集 `provider_has_dataset` 返回
> False, 自动回退 TickFlow。**不要声明做不了的数据集**。

vendor 的 7 个标准数据集 (`custom-data-source.md` §"支持范围") 要求
OHLCV/五档/财务等**交易字段**, 而 KPH 的 9 个接口是:

| KPH 接口 | 主要字段 | 适配 vendor 7 数据集? |
|---|---|---|
| `kph_limit_ladder` | `limit_count, limit_time, plate_id, seal_amount, themes, reason` | ❌ 无 OHLCV |
| `kph_limit_up_history` | 同上 + `amount, turnover_rate, market_cap` | ❌ |
| `kph_limit_down_history` | 同上 | ❌ |
| `kph_market_review_events` | `event_time, tag_name, plate_id, detail, raw_stock_list` | ❌ 事件流 |
| `kph_market_emotion` | `limit_up_count, rise_count, fall_count, market_sign` | ❌ 截面统计 |
| `kph_sector_constituents_history` | `last_price, change_pct, amount, main_net, limit_tag, limit_count` | ⚠️ 字段部分重叠, 但**没有标准 volume/amount 单位** |
| `kph_sector_ranking` | `plate_id, change_pct, amount, net_inflow` | ❌ 板块级非标的级 |
| `kph_wind_vane_history` | 同 `kph_limit_up_history` | ❌ |

**强行做成 Provider 会导致**:

1. 必须**伪造** `last_price=0 / volume=0` 等必填字段
2. 失去 KPH 最有价值的 `reason / limit_count / seal_amount` 等独有字段
3. 违反 vendor §"缺字段禁止启发式补全"

→ **正确做法是扩展数据 Snapshot** (vendor §22.4)。

## 2. 推荐路径: vendor 自带扩展数据机制

vendor 已自带 `data/ext_data/<id>/config.json` + `app.services.ext_pull`
拉取引擎, 用户在设置页添加 HTTP 拉取配置即可, **不需要**写 fetcher.py。

### 2.1 单点拉 (推荐, 4 个 dataset)

| dataset 名 | URL | 响应 path | 字段映射参考 |
|---|---|---|---|
| `kph_limit_pool` | `https://<host>/api/limit-up/history` | `data` | `code→symbol`, `reason`, `limit_count`, `seal_amount` |
| `kph_limit_down_pool` | `https://<host>/api/limit-down/history` | `data` | 同上 |
| `kph_limit_ladder` | `https://<host>/api/limit-ladder` | `data` | `code→symbol`, `limit_count` |
| `kph_market_emotion` | `https://<host>/api/market-emotion` | `data` | `limit_up_count`, `market_sign` |

每个 dataset 创建 `data/ext_data/<id>/config.json`, 字段参考
`data/ext_data/ext_gn_ths/config.json` (同款 schema, `pull.url` /
`pull.response_path` / `pull.field_map` / `pull.schedule_minutes`)。

### 2.2 扇出拉 (板块成分股)

`kph_sector_constituents` 需要"先取板块列表再逐板块拉成分股", vendor 单层
`pull` 引擎不直接支持 — 此时用 `kph_fetcher.py` 替代:

```powershell
cd backend
uv run python scripts/kph_fetcher.py --date 2024-01-15
```

或者参考 `data/ext_data/ext_gn_ths/config.json` 的 `fanout` 字段,
在 `data/ext_data/kph_sector_constituents/config.json` 里配 `fanout.url` /
`fanout.param` 等, vendor 引擎会自动扇出。

### 2.3 抓包工具

`fetch_*` 函数支持注入假 client (见 `tests/test_kph_fetcher.py`), 真实抓包
后**先**用 fake client 把响应结构对一遍, **再**写到 vendor config 或
fetcher.py 里。

## 3. 用 fetcher.py 跑一次性回填

```powershell
cd D:\AI-Stock\tickflow-desktop\backend

# 1. 校对 endpoint
#    浏览器开发者工具 / Fiddler / mitmproxy 抓 kphhq.com (或实际接入域名)
#    把真实 URL 写回 kph_fetcher.py 的 DEFAULT_BASE_URL / 各 fetch_* 函数

# 2. 跑契约测试 (用 fake client, 不依赖真实网络)
uv run --frozen pytest tests/test_kph_fetcher.py -q

# 3. 真实抓取
uv run python scripts/kph_fetcher.py --date 2024-01-15

# 4. 写出的 parquet:
#    data/extensions/kph/2024-01-15/limit_pool.parquet
#    data/extensions/kph/2024-01-15/limit_down_pool.parquet
#    ... 共 7 个文件
```

## 4. 输出 schema (跟 fetcher.py 一致)

| 文件 | 字段 (节选) |
|---|---|
| `limit_pool.parquet` | `symbol, name, trade_date, limit_count, limit_time, plate_id, plate_name, seal_amount, seal_money, themes, reason, amount, turnover_rate, market_cap` |
| `market_emotion.parquet` | `trade_date, limit_up_count, rise_count, fall_count, market_sign, total_amount` |
| `sector_constituents.parquet` | `trade_date, plate_id, plate_name, symbol, last_price, change_pct, main_net, limit_tag` |

## 5. 关键设计决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 不做 Provider, 做扩展数据 | ✅ | KPH 字段不符合 vendor 7 标准数据集, 强做 Provider 必伪造 |
| Snapshot 而非时序 | ✅ | 涨停池/板块排行每天收盘后全量覆写, 无需历史时序 |
| 优先 vendor ext_data 机制 | ✅ | 自带 HTTP 拉取引擎, 调度/重试/落盘都现成 |
| fetcher.py 作扇出兜底 | ✅ | vendor 单层 pull 不支持"先列表后扇出" |
| DuckDB 落 parquet | ✅ | 跟 vendor `data/core/` 一致, 后续 DuckDB 直接查 |
| 字段命名贴近 vendor §"上市公司维表" | ✅ | `symbol` 不用 `code`, `trade_date` 不用 `date` |

## 6. 与 CLS 插件的关系

- **CLS** 走真 Provider (daily + minute 两个标准数据集)
- **KPH** 走扩展数据 (9 个独有字段, 不符合标准数据集)

两个**完全不同**的接入路径, 不冲突。如果未来需要把 KPH `last_price` /
`change_pct` 等 partial 字段拼成 K 线辅助视图, 可以用 `data/extensions/kph/`
里的 parquet 作为**二级数据源**, 在 frontend ext_pages 里做 JOIN。