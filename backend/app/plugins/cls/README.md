# 财联社 (CLS) Provider 插件

> **v1.0** — 按 2026-10-19 **真连上游实测**（12/12 接口返回 200）重写。
> 上一版 v0.1 的 URL / 参数 / 字段名 / symbol 格式全是猜的，已全部作废。

## 1. 这是什么

财联社（cls.cn）24 小时持牌财经通讯社的 HTTP 接入，按 vendor 插件规范封装为 TSP 的 Provider 插件。

**数据集声明**（`plugin.yaml`）：

- ✅ `minute` — CLS `x-quote.cls.cn/quote/stock/tline`（个股**当日**分时，北京墙钟）
- ❌ `daily` — **故意不声明**，见下方 ⚠️
- ❌ `realtime` / `depth5` / `adj_factor` / `financial` / `full_minute` — CLS 无对应数据，**未声明**，自动回退 tickflow

### ⚠️ 为什么 `daily` 故意不声明（2026-10-19 实测，重要）

CLS 的 kline **价格字段是「减法式前复权价」**：`真实价 − 上市至今累计每股分红`，
**不是** vendor 契约要求的「不复权原始价」。

实测（sh600519，offset 翻页取 2500 根，2016-06 ~ 2026-10）：

| 检验 | 结果 |
|---|---|
| `close_px` 与自身 `change` 字段的自洽性 | **100% 自洽**（2500/2500）→ 序列本身一致，不是随机脏数据 |
| `close_px` ÷ 真实均价（成交额÷成交量） | 2016-06 ≈ **-0.02**（负价！）→ 2026-09 ≈ **1.000**，随时间单调上升 |
| 差值（真实均价 − close_px） | 2016-06 = 299.40 → 2018-01 = 283.12 → 2024-01 = 137.63 → 2026-09 ≈ 0 |

→ 差值就是**累计每股分红**，2016 年累计分红已超过股价本身，减法式前复权后 `close_px` 成负数。

**后果与决策**：

- 涨跌幅 / 成交量 / 成交额 **全区间可信**
- 绝对价位**深历史不可用**（偏差 8% ~ 40%+），最近约 1 年内可用
- **若声明 `daily`**，框架的 `adj_factor` 管道会在前复权价上再叠一次前复权 →
  **双重复权 → 静默产出错误价格**（最坏的一类 bug：页面照样渲染，数字全错）
- 所以：**不声明 `daily`**，宁可少一个数据集也不污染 `core/daily`
- `get_daily()` 方法**仍然保留**，供显式调用；provider 会在检测到前复权偏移时打
  WARNING（`adjusted_price_warn_threshold`，默认 5%）

**想要 CLS 的不复权 K 线**：
`真实价 ≈ close_px + 累计分红`，其中累计分红可在每个除权区间用
`(成交额 ÷ 成交量 − close_px)` 的中位数还原（区间内是常数，噪声来自 VWAP 与收盘的差异）。
或者直接用 tickflow / ELTDX 拿不复权日 K。

**API Key**：无（cls.cn 公开页面接口，无鉴权）

## 2. 协议（实测，非推测）

### 2.1 两套签名体系

| | Web 端 | 移动端 |
|---|---|---|
| 端点 | `stock/emotion` / `plate_list` / `up_down_analysis` / `tline` / `kline` | `todayTuyere` / `dingPan/mainline` / `plate_heat_list` / `plate/rotation` / `plate/tuyere/stocks` / `plate/popular_stocks` / `roll/get_roll_list` |
| base | `{app: CailianpressWeb, os: web, sv: 8.4.6}` | `{app: cailianpress, sv: 8.7.4, os: android, mb: Xiaomi-2206123SC, ov: 32, channel: 8, motif: 0, net: "", province_code: 3205, token: "", uid: ""}` |
| `sign` | **硬编码常量**（见 `client.WEB_SIGNS`），换标的 / 换 limit / 换 offset 都实测照常 200 | **动态计算** `md5(sha1("&".join(sorted(k=v))))` |
| UA | `Mozilla/5.0 AxData/0.1` | `okhttp/4.9.0` |

所有请求都带 `Referer: https://www.cls.cn/` 与对应域名的 `Host` 头。

### 2.2 实测补充（与最初口径说明有出入的地方）

| 项 | 最初的说法 | **实测** |
|---|---|---|
| 响应信封 | web `code` / 移动端 `errno` | **按域名分**：`api3.cls.cn` → `{"errno": 0}`；`x-quote.cls.cn` → `{"code": 200}`。client 两种都认 |
| tline `fields` | `+ fields` | **必须传字段名**（`minute,last_px,...`）。传数字下标（`1,2,3`）上游**不报错但每格都是 null** —— 静默失败 |
| tline `data.date` | int `20261019` | **是 list** `[20261019]`。每根 line 项里另带自己的 `date` |
| `plate/rotation` 的 `days` | 4 | **只支持 4 / 30**。传 6 上游把 `data` 换成 `{"days": "支持 4/30"}`（dict 不是 list） |
| `tuyere/stocks` / `popular/stocks` 的 `change` | 小数制 | **是带百分号的字符串** `"+20.00%"`；`last_px` 也是字符串 |
| kline `offset` | 翻页 | **从最新一根往回数**：实测 `limit=2, offset=0` → 最近 2 根；`offset=2` → 再往前 2 根 |

### 2.3 `secu_code` 格式

`sh600519` / `sz000001` / `bj430047`（小写前缀 + 6 位）。

- 入参 `600519.SH` → `sh600519`
- 出参 `sh600519` → `600519.SH`（**必须带后缀**，vendor 契约要求）
- 北交所判定用**二位前缀** `{43, 83, 87, 92}`（口径同 `backend/app/services/abnormal_moves.py:75`），**不是** `("4", "8")`

## 3. 字段映射与单位（实测核对）

### kline → canonical daily

| CLS 字段 | → | 单位 |
|---|---|---|
| `date` (int YYYYMMDD) | `date` | Date |
| `open_px` / `high_px` / `low_px` / `close_px` | `open` / `high` / `low` / `close` | 元，**减法式前复权价** ⚠️ 见 §1 |
| `business_amount` | `volume` | **股** → ÷100 → 手 |
| `business_balance` | `amount` | 元 |
| `change` | `change_pct` | **已经是小数制**（0.021 = 2.1%）→ 直接用，**不再 ÷100** |
| `tr` / `amp` | 换手率 / 振幅 | 小数制 |

> **单位已被上游数据自证**：`close/preclose - 1` 与 `change` 完全吻合（`-0.010179` vs `-0.0102`）；
> `business_balance / business_amount` ≈ 当日均价（`5767722` 股 → `7537706224` 元，均价 1306.88 ≈ 收盘 1312.97）。
> 所以 `business_amount` 是**股**、`business_balance` 是**元**、`change` 是**小数制**，三条互相印证。
>
> ⚠️ **但价格字段本身是前复权价**（`真实价 − 累计每股分红`），深历史绝对价位不可用 ——
> 详见 §1 的完整实测。**这也是 `daily` 不作为标准数据集声明的原因。**

`type` 参数映射：`daily→fd1`、`weekly→fw`、`monthly→fm`、`yearly→fy`；`limit` 上限 500，`offset` 往更早翻。
（`limit=500` 只覆盖**最近 500 个交易日**，2026-10 实测 = 2024-09-20 起；更深历史要靠 `offset` 翻页。）

### tline → canonical minute

| CLS 字段 | → | 单位 |
|---|---|---|
| `minute` (int HHMM) + `date` (int YYYYMMDD) | `datetime` | **北京墙钟 naive**（930 → 09:30:00） |
| `last_px` | `close` | 元（该分钟**收盘**价） |
| `open_px` | `open` | 元（该分钟**开盘**价） |
| `business_amount` | `volume` | **股** → ÷100 → 手 |
| `business_balance` | `amount` | 元 |
| `av_px` | 均价 | 元（附加信息，不进 canonical 8 列） |

⚠️ CLS 分时**不提供分时 high/low** → `high` / `low` 置 `None`（不拿 `last_px` 顶替，不启发式补全）。

> **tline 字段语义已逐根验证**（2026-10-19，sh600519 241 根）：
> ```
> 930  last=1322.5   open=1325.0   preclose=1325.0   av=1322.5
> 931  last=1329.0   open=1322.99  preclose=1322.5   av=1324.8768
> 932  last=1334.3   open=1330.99  preclose=1329.0   av=1327.7114
> ```
> → `preclose_px` = **上一分钟的收盘价**（标准 K 线语义），`open_px` = 该分钟开盘价，
> `av_px` = **累计** VWAP（单调变化，239 个不同取值）。
>
> **数据可信度交叉验证**（3 只股 × 241 根）：
> - 241 根 `last_px` **全部落在** kline 当根的 `[low, high]` 区间内 → **0 根越界**
> - tline 累计成交额/成交量 vs kline 当根 → 偏差 **-0.39% / -0.46% / 0.00%**
>   （差额是尾盘集合竞价，不在 241 根里，正常）
>
> ⚠️ **tline 与 kline 的「昨收」口径不同**（茅台 tline 1325.0 vs kline 1329.0）——
> kline 用未除权原始前收，tline 用当日基准价。canonical 8 列**不含 preclose**，
> 所以不受影响；但**不要拿 tline 的 preclose 去对 kline 的前收**。

## 4. CLS 独有短线数据（**不声明为数据集**）

以下 10 个接口是 CLS 独有的短线数据，**不符合 vendor 任何标准数据集**，所以
**不在 `plugin.yaml` 的 `datasets` 里声明**（声明了路由层会去找不存在的 schema）。
它们作为 Provider 的独立公开方法暴露，由上层 API / 前端直接调用：

| 方法 | 上游端点 | 返回 |
|---|---|---|
| `get_market_emotion()` | `stock/emotion` | **dict**（截面）：`up_down_dis` 涨跌家数分布 + **`limit_up_board` 连板梯队**（row1=梯队名 / row2=家数 / row3=连板率） |
| `get_limit_up_pool(pool_type="up_pool")` | `quote/index/up_down_analysis` | `list[dict]`：`up_reason` 按 `\|` 拆成 `theme` + `reason` |
| `get_telegraph(date=None, category="important", limit=20)` | `v1/roll/get_roll_list` | `list[dict]`：按 `ctime`（unix 秒）落在北京日过滤；`level=="C"` 视为重要 |
| `get_sector_heat(plate_type="concept")` | `plate/plate_heat_list` | `list[dict]`：板块热度 |
| `get_sector_industry(page=1)` | `web_quote/plate/plate_list` | `list[dict]`：行业板块（涨停家数 / 主力净流入 / 领涨股） |
| `get_sector_rotation(days=4)` | `plate/rotation` | `list[dict]`：`{trade_date, plates[]}`；**逐项 isinstance 判断**，非 dict 跳过 |
| `get_market_wind()` | `v2/todayTuyere` | `list[dict]`：今日题材 / 风向标 |
| `get_market_mainline()` | `v2/dingPan/mainline` | `list[dict]`：摊平成 `section=chance/wind/style`；上游 `styles` 是 **dict** 而 `chances`/`winds` 是 list |
| `get_plate_stocks(plate_code)` | `plate/tuyere/stocks` | `list[dict]`：题材成分股 |
| `get_popular_stocks(plate_code)` | `plate/popular_stocks` | `list[dict]`：板块人气股 |

### 4.1 ⚠️ 电报的历史日期怎么取 (三个坑, 缺一个就永远拿不到历史)

上游 `v1/roll/get_roll_list` **默认只返回最新一批**。要取某个历史日必须同时满足:

| # | 要求 | 不满足的后果 |
|---|---|---|
| 1 | **必须带 `refresh_type=1`** | 上游**静默忽略** `last_time`, 永远返回最新一批 (AxData 开源实现就是漏了这个参数 → 历史日期永远 0 条) |
| 2 | `last_time` 必须是 **epoch 秒的字符串** | 传 `"2026-10-13 23:59:59"` 这种格式化字符串 → 返回 0 条, **不报错** |
| 3 | `rn` **≤ 50** | `rn=60/80/100` → 返回 0 条 (不是截断成 50, 是直接空) |

`last_time` 是**向前翻页的锚点** (返回 `ctime <= last_time` 的最近 N 条)。
要覆盖一整天必须连续翻页: 锚点 = 当天 23:59:59 → 翻到本页最小 `ctime - 1` → 继续,
直到覆盖当天 00:00。实测:

| 交易日 | 页数 | 当天条数 | 其中 level=C |
|---|---|---|---|
| 2026-10-13 | 11 页 | 549 | 489 |
| 2026-10-14 | 12 页 | 580 | 515 |
| 2026-10-16 | 12 页 | 551 | 475 |

provider 的 `get_telegraph(date=...)` 已经封装了这个翻页循环 (上限 30 页, 锚点不前进时安全退出)。

### 4.2 ⚠️ 涨停池只有实时版, 没有历史版

`quote/index/up_down_analysis` **不认任何日期参数**。实测传 `date` / `trade_date` / `day` /
`Date` 四种, 返回结果完全一样 (都是当天的池子); `type=up_pool_history` 直接返回 0 条。

所以 `get_limit_up_pool(trade_date=...)` 传日期时只会打一条 warning, 拿到的**永远是当天**的池子。
**历史涨停池请用 KPH 插件** (它有 `HisDaBanList` 历史接口, 见 `plugins/kph/`)。

> ⚠️ **盘前调用会静默返回空**（实测 2026-10-20 09:19 盘前 → 0 行；
> 10-19 22:00 盘后 → 49 行）。`market_emotion.limit_up_board` 同理
> （盘前 `row2`/`row3` 全是 `--`）。**不是故障，是上游还没填当日数据**——
> 跑批要安排在开盘之后。

## 5. 在真实 TSP 项目里启用

```powershell
# 1. 复制到 vendor 项目
$src = "D:\AI-Data\投资工作台开发\plugins\cls"
$dst = "D:\AI-Data\TickFlow-Desktop\backend\app\plugins\cls"
Copy-Item -Recurse -Force $src\* $dst\

# 2. 跑契约测试 (不需要网络)
cd D:\AI-Data\TickFlow-Desktop\backend
uv run --frozen pytest tests/test_cls_provider.py -q
uv run --frozen ruff check app/plugins/cls/ tests/test_cls_provider.py

# 3. 真连上游冒烟 (联网)
..\..\.venv\Scripts\python tests\smoke_cls_live.py

# 4. 启动 backend, 设置页 → 数据源 Beta → 启用 cls 插件
# 5. 数据页 → 立即同步 → 选择 A 股 → 启动
```

## 6. 关键设计决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 只声明 `daily` + `minute` | ✅ | 其余 10 个 CLS 接口不符合任何标准数据集，作为独立方法暴露 |
| 零 API Key (`api_key_env: null`) | ✅ | CLS 公开页面无鉴权 |
| web 端 sign 用硬编码常量 | ✅ | 实测上游不校验 sign 与参数的绑定，换标的/换分页都照常 200 |
| 节流 1.0s/请求 | ✅ | CLS 无公开 SLA；实测 12 接口一次跑完未限流 |
| volume 股 → 手 (÷100) | ✅ | 被 `business_balance / business_amount` ≈ 均价自证 |
| change 不再 ÷100 | ✅ | 被 `close/preclose - 1` 自证 |
| 缺字段置 `None` | ✅ | vendor §"缺字段与空数据" 禁止启发式补全 |
| 分时 high/low 置 `None` | ✅ | CLS 不提供，不用 `last_px` 顶替 |

## 7. 失败模式与降级

| 场景 | 行为 |
|---|---|
| 单个标的 fail | 跳过该标的，warning 日志，继续拉其他（不阻断整批） |
| 所有标的 fail | 返回空 DataFrame（schema 仍合法） |
| 参数校验错（`freq` / `days` / `limit`） | **fail-fast 抛 `ClsValidationError`**，不静默吞掉 |
| 缺字段 | 置 `None`（**不**填 0，**不**启发式推算） |
| 上游网络 / HTTP / 非 200 信封 | 抛 `ClsError`，由上层捕获记录 |
| 上游脏数据（价格 ≤ 0） | 丢弃该 bar 并打 **WARNING**（条数 + 涉及日期），见 §8 |
| `on_chunk_done` | 空批次也会回调，最终 `cur == total` |

## 8. ⚠️ 已发现的上游数据问题（照实记录）

### 8.1 CLS kline 深历史出现**负价格**（个股特有）

实测（2026-10-19，`sh600519`）：

| offset | 区间 | 坏 bar 数 |
|---|---|---|
| 0 | 2024-09-20 .. 2026-10-19 | 0 |
| 500 | 2022-08-29 .. 2024-09-19 | 0 |
| 1000 | 2020-08-07 .. 2022-08-26 | 0 |
| 1500 | 2018-07-18 .. 2020-08-06 | 0 |
| 2000 | 2016-06-30 .. 2018-07-17 | 9 |
| 2500 / 3000 / 3500 | 2016-06 及更早 | **500 / 500 / 500（全负）** |

同一接口同一时段对 `sz000001` **完全正常**（0 根坏）→ 这是**个股特有的上游脏数据**，不是参数问题。

**处理**：provider 层丢弃 OHLC ≤ 0 的 bar，并打 WARNING（不静默）。
vendor 自己的 `indicators/pipeline.filter_halt_days` 只过滤 `open==0 & high==0`（停牌日），拦不住负价；
负价进 MA/ATR 会污染指标，所以在 provider 层拦。

⚠️ 另外提醒：CLS kline 的**深历史价位与现实价位量级不符**（600519 在 2018-07 的收盘是 463.64，
而当日真实均价是 721.52）。

**2026-10-19 已定位根因**：这不是随机脏数据，而是**减法式前复权**（`真实价 − 上市至今累计每股分红`）——
`close_px` 与自身 `change` 字段 100% 自洽，但与真实均价（成交额÷成交量）的比值随时间单调上升
（2016-06 ≈ -0.02 → 2026-09 ≈ 1.000），差值即累计分红，2016 年累计分红已超过股价本身。
完整实测与决策见 **§1**。

**处理**：
1. provider 层丢弃 OHLC ≤ 0 的 bar，并打 WARNING（不静默）
2. provider 层新增**前复权偏移检测**（`close_px` vs 真实均价偏离 > 5% 即 WARNING），
   提醒调用方"该区间绝对价位不可用，涨跌幅/量/额仍可信"
3. **`daily` 不作为标准数据集声明** —— 避免框架 `adj_factor` 管道双重复权

### 8.2 CLS 行情**不覆盖北交所**

`bj430047` / `bj830799` / `bj871981` / `bj920001` 在 kline / tline 上均返回 `code=200` 但 **0 行数据**。
symbol 归一仍按项目口径 `{43,83,87,92}` → `.BJ`（保证不与其他模块不一致），
但北交所数据要另找源。

### 8.3 分时只有最近一个交易日

tline 固定返回最近一个交易日的 241 根（09:30~11:30 + 13:00~15:00），上游不提供历史分时。
