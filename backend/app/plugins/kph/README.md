# 开盘啦 (KPH) Provider 插件

> **v0.2** — 从「扩展数据 fetcher」升级为**正式 Provider 插件**。
> v0.1 的 endpoint (`https://www.kphhq.com/api/...`) 是**猜的占位符**, 从未验证过,
> 已全部替换为真实值 (来源: AxData 开源实现 `axdata_core/adapters/kph/request.py`)。
>
> 规范依据: `docs/plugin-development.md` (vendor 规范) + `CONTRIBUTING §3.1` (内部数据契约)

---

## 1. 这是什么

**开盘啦 / 开盘红 (kaipanhong.com)** 的短线情绪数据接入, 封装为 TSP 的 Provider 插件。

覆盖 7 类数据:

| board_type | 内容 | AxData 接口名 |
|---|---|---|
| `up` | 涨停池 (**含涨停原因**) | `kph_limit_up_history` |
| `down` | 跌停池 | `kph_limit_down_history` |
| `wind_vane` | 风向标 | `kph_wind_vane_history` |
| `ladder` | 连板天梯 (含一字板/热门标记) | `kph_limit_ladder` |
| `emotion` | 市场情绪截面 (涨停家数/涨跌家数) | `kph_market_emotion` |
| `sector` | 板块排行 (按 selected/industry/region) | `kph_sector_ranking` |
| `resumption` | 涨停复盘 (板块 → 个股, 含详细涨停原因) | `kph_limit_resumption_history` |

**数据集声明** (`plugin.yaml`):
- ✅ `limit_board` — 框架**新增**的第 8 个标准数据集 (见 `framework-patch/`)
- ❌ `daily` / `adj_factor` / `realtime` / `minute` / `depth5` / `full_minute` / `financial`
  — KPH 无 OHLCV / 盘口 / 财务, **未声明**, 自动回退 tickflow

**API Key**: 无 (公开接口, 但必须节流)

---

## 2. ⚠️ 协议不是常规 REST (接入前必读)

KPH 的协议有 5 个反常规点, 全部在 `client.py` 里显式实现:

1. **POST + `application/x-www-form-urlencoded`**, 不是 GET
2. **双参数路由**: `a` = action (业务方法), `c` = controller (控制器), 二者组合随接口而变
   (涨停池 `a=HisDaBanList&c=HisHomeDingPan`; 天梯 `a=GetZhangTingTianTi&c=FuPanLa`)
3. **必须带设备指纹** (`PhoneOSNew` / `DeviceID` / `VerSion` / `Token` / `UserID` / `Red` / `apiv`),
   缺参数上游返回空 payload
4. **响应行是位置数组**, 字段靠下标定位 (不是具名 key)
5. **一次请求混合多种结构**: 板块排行是位置数组、市场情绪在 `payload["info"]` 下是 dict、
   复盘事件在 `payload["List"]` 下又是 dict 列表

**端点**:

| 用途 | URL |
|---|---|
| 实时 | `https://apphwshhq.kaipanhong.com/w1/api/index.php` |
| 历史 | `https://apphis.kaipanhong.com/w1/api/index.php` |

**这决定了它走不了 `custom-data-source.md` 的 YAML 路线** —— 详细论证见
[`data-source-config.md`](./data-source-config.md)。

---

## 3. 字段下标表 (**实测真实响应确认**, 不是照抄 AxData)

采集于 2026-10-16 真实响应。⚠️ AxData 公开实现的**板块排行**下标映射是错的,
本插件用 50/50 行交叉验证修正过。

### 3.1 涨停池 / 跌停池 / 风向标 (行长 34)

| idx | 含义 | 本插件列 | 备注 |
|---|---|---|---|
| 0 | 裸代码 | `symbol` | `301586` → `301586.SZ` |
| 1 | 名称 | `name` | |
| 6 | 涨停时间 | `limit_time` | unix 秒 |
| 8 | 封单量 | `seal_amount` | **股** → /100 → 手 |
| 9 | **连板描述文本** | `limit_count`(解析) / `total_board_count` | 真实形态：`"首板"` / `"3连板"` / `"4天3板"` |
| 10 | **区间总板数** | `total_board_count` | 跌停池无此语义 → None |
| 11 | 题材 | `themes` | `"飞行汽车、低空经济"` |
| 12 | 净流入 | `net_inflow` | 元 |
| 13 | 成交额 | `amount` | 元 |
| 14 | 换手率 | `turnover_rate` | **百分数** → /100 → 小数制 |
| 15 | 总市值 | `market_cap` | 元 |
| 16 | 涨停原因 | `reason` | 跌停池无此语义 → None |
| 23 | 封单额 | `seal_money` | 元 |
| **24** | **是否炸过板** | **`is_exploded`** | **布尔 0/1**；2026-10-20 晚用 ELTDX `kbcs` + 东财 `zbc` 双重标定，**逐只零分歧**。跌停池/风向标实测**恒 0** → 填 None |
| 26 | 行业代码 | (未入 schema) | |
| 27 | 行业内涨停家数 | (未入 schema) | |

### 3.2 连板天梯 (行长 11)

| idx | 含义 | 本插件列 |
|---|---|---|
| 0 / 1 | 代码 / 名称 | `symbol` / `name` |
| 2 | **当前连板天数** | `limit_count`（总板数列在 ladder 无对应 → `total_board_count`=None） |
| 3 | 涨停时间 | `limit_time` |
| 4 / 5 | 板块 id / 名 | `plate_id` / `plate_name` |
| 6 | 一字板 | `one_word` |
| 7 | 热门股 | `popular` |
| 8 | 板块内涨停家数 | (未入 schema) |
| 9 | 成交额 | `amount` |
| 10 | 板块成交额 | (未入 schema) |

### 3.3 板块排行 (行长 19) — ⚠️ 与 AxData 公开实现**不同**

| idx | 真实语义 | 交叉验证 |
|---|---|---|
| 0 / 1 | 板块 id / 名 | |
| 2 | **板块热度积分 (排行排序键)** | 全 50 行**严格降序** (15400 → 102) |
| 3 | 涨跌幅 (%) | `idx3 == idx18` 恒成立 (50/50) |
| 4 | 振幅 (%) | |
| 5 | **板块成交额 (元)** | 与成分股成交额合计**逐分吻合** (139,910,210,811 vs 891) |
| 8 | **净流入 (元)**, 负 = 净流出 | |
| 9 | 换手率 (%) | |
| 10 | 总市值 (元) | |
| 11 | 未确认 —— **可正可负的日度比率** | |
| 12 | 未确认 —— **可正可负的日度金额** | |
| 15 / 16 | 未确认 —— **跨日恒定的板块级静态常量** (与排行名次不单调; 部分新概念板块为 0) | |
| 17 | == idx 2 (重复) | 50/50 成立 |
| 18 | == idx 3 (重复) | 50/50 成立 |

> ⚠️ **idx 2 不是成分股数** —— 它逐日剧烈变化 (体育产业 10-13/10-14/10-16
> 分别为 5387 / 1090 / 425), 而成分股接口实测只返回 453 只 (低空经济),
> 且 15400 > 全市场股票总数 (~5400)。它是**板块热度/人气积分**, 即排行排序键。
> 本插件把它映射为 `plate_heat_score` (不再是 `stock_count`)。
>
> AxData 把 idx2 当 `amount`、idx5 当 `net_inflow` —— 实测证明是错的
> (idx2=15400 不可能是成交额)。

### 3.4 市场情绪 (dict, `payload["info"]`)

| 键 | 含义 | 本插件列 |
|---|---|---|
| `ZT` / `DT` | 全市场涨停 / 跌停家数 | `market_limit_up_count` / `market_limit_down_count` |
| `SZJS` / `XDJS` | 上涨 / 下跌家数 | `rise_count` / `fall_count` |
| `sign` | 情绪描述 | `reason` |
| `qscln` | 全市场成交额 | `amount` |
| `-10..-1` / `0` / `1..10` | 涨跌分布直方图 | (未入 schema) |

### 3.5 涨停复盘 (板块 → 个股)

行下标同 §3.1, 额外:
- idx 16 = 涨停原因简称, idx 17 = 涨停原因**详细** (本插件取 17, 缺失回退 16)

### 3.6 与 AxData catalog 的差异（重要）

AxData **有** KPH 的字段说明 —— 在 `libs/axdata_core/axdata_core/sources/kph/catalog.py`
的 `fields=` 声明里，每个字段都有**中文名 + 类型 + 单位**（`F(name, dtype, zh)`）。
但它是**按位置手测**的，实测发现它的**下标有 3 处错位**，且**漏掉了 6 个位置**。

| 接口 | 字段 | AxData catalog | AxData 实现下标 | 本插件实测下标 | 判定 |
|---|---|---|---|---|---|
| 天梯 | 11 个字段 | 全部有中文名 | 0,1,2,3,4,5,6,7,8,9,10 | 同 | ✅ **完全一致** |
| 涨停池 | `seal_amount` | "封单量"（**未给单位**） | 8 | 8 | ✅ 下标一致，本插件补上单位=**股** |
| 涨停池 | `seal_money` | "封单金额，单位：元" | 23 | 23 | ✅ |
| 涨停池 | `market_cap` | **"流通市值"**，单位：元 | 15 | 15 | ✅（本插件原标"总市值"，已按 AxData 修正） |
| 涨停池 | `turnover_rate` | "换手率，**百分比数值**" | 14 | 14 | ✅ |
| 板块 | `change_pct` | 涨跌幅 | 3 | 3 | ✅ |
| 板块 | `amplitude` | 振幅 | 4 | 4 | ✅ |
| 板块 | `turnover_rate` | 换手率 | 9 | 9 | ✅ |
| 板块 | `market_cap` | **"总市值"**，单位：元 | 10 | 10 | ✅ |
| 板块 | `amount` | 成交额，单位：元 | **2** ❌ | **5** | ❌ **AxData 错** |
| 板块 | `net_inflow` | 净流入，单位：元 | **5** ❌ | **8** | ❌ **AxData 错** |
| 板块 | `stock_count` | 成分股数量 | **17** ❌ | idx2/17 = **板块热度积分** | ❌ **AxData 错**（含命名错） |
| 板块 | *(未列)* | — | — | idx 6 / 7 / 11 / 12 / 15 / 16 | ⚠️ **AxData 完全没提**（行有 19 位，只声明 11 个） |

**为什么能确定 AxData 错**：

- `amount`：AxData 映射到 idx2 = **15400**（低空经济）—— 不可能是成交额；
  实测 idx5 = 139,910,210,891，与**成分股成交额合计逐分吻合** ✓
- `net_inflow`：AxData 映射到 idx5 = 成交额 → 净流入实际在 idx8（符号与板块涨跌一致）✓
- `stock_count`：AxData 映射到 idx17，而 **idx17 ≡ idx2**（50/50 行恒成立）——
  idx2 是**排行的排序键**（全 50 行严格降序 15400→102）、**逐日剧烈变化**
  （体育产业 5387→1090→425）、且 **15400 > 全市场股票总数(~5400)**，
  成分股接口实测只返回 453 只 → 它是**板块热度/人气积分**，不是股数

**本插件相对 AxData 的增量**：
1. 修正板块排行的 3 处下标（否则 `amount`/`net_inflow`/`stock_count` 三列全错）
2. 补上 `seal_amount` 的单位（股，AxData 未写）
3. 涨停池 `market_cap` 按 AxData 口径确认为**流通市值**（板块行才是总市值）
4. `popular` 译作「是否有人气」（AxData 用词），`one_word` 为「是否大单一字」
5. 补上 AxData 未声明的 6 个位置的**特征**（静态/动态、符号、量级），保持 `None` 不瞎填
6. 额外实现 AxData 没有的：`Is_st` 包含 ST 股、涨停池翻页、非交易日自动回退

---

## 4. 关键行为

| 行为 | 实现 |
|---|---|
| **默认日期** | 历史类接口 (up/down/wind_vane/resumption) 用**上一个工作日** (跳过周末); 实时类 (ladder/emotion/sector) 用今天 |
| **非交易日回退** | 上游对非交易日返回 `errcode=1020`; **未显式指定日期**时自动向前回退到最近有数据的交易日 (最多 10 次) |
| **显式日期不回退** | 调用方明确要某天时, 拿不到就抛错, 不静默换日期 |
| **涨停池翻页** | 上游 `st` 上限约 60 (`st=80+` 返回 0 行); 用 `st=50` 翻页, 满页继续 / 短页终止 |
| **分页上限** | 板块 40 页 / 涨停池 10 页, 防上游异常导致死循环 |
| **包含 ST 股** | `include_st=True` (默认) → 上游 `Is_st=0`。⚠️ `Is_st` 语义**反直觉**: `Is_st=1` 是**排除** ST, `Is_st=0` 才是**包含** (见下) |
| **节流** | 客户端默认 1s 间隔 (上游无公开 SLA) |
| **缺字段** | 一律 `None`, **禁止启发式补全** (`CONTRIBUTING §3.1`) |
| **异常语义** | 单类型 board_type 的错误**向上抛** (对齐 `get_daily`); 批量 `all` 才逐类型软失败 |
| **参数错误** | `KphValidationError` (KphError 子类) → fail-fast, 不被软失败吞掉 |

### ⚠️ `Is_st` 参数反直觉 —— 默认必须包含 ST

上游 `Is_st` 参数的语义与字面意思**相反**。实测 2026-10-13:

| `Is_st` | 返回 | 差的 3 只是谁 |
|---|---|---|
| `1` | 64 只 | 排除 `*ST威领`(2连板) / `*ST仕净`(2连板) / `*ST三房`(首板), 涨停原因均标 `ST板块` |
| `0` | **67 只** | — |

且 `Is_st=0` 的 67 只与「连板天梯」的 67 只**完全一致** (交叉验证)。
短线连板场景 ST 涨停是关键信号 → 本插件默认 `include_st=True` (发 `Is_st=0`)。
AxData 开源实现用的是 `Is_st=1`, **会漏掉 ST 股**。

### ⚠️ 涨停池与连板梯队是**互补**集合, 不是重复

实测 5 个交易日 (2026-10-12 ~ 10-16), 按 symbol 去重:

| 交易日 | 涨停池 | 连板梯队 | 交集 | **并集** | 情绪 ZT | ZT − 并集 |
|---|---|---|---|---|---|---|
| 10-12 | 42 | 46 | 41 | 47 | 48 | 1 |
| 10-13 | 67 | 67 | 64 | **70** | **70** | 0 |
| 10-14 | 33 | 36 | 30 | **39** | **39** | 0 |
| 10-15 | 35 | 36 | 35 | **36** | **36** | 0 |
| 10-16 | 55 | 59 | 53 | **61** | **61** | 0 |

**涨停池 ∪ 连板梯队 == 市场情绪的 ZT** (4/5 天完全相等) → 这两个接口是互补的两个集合,
**单取任何一个都会漏 1~6 只**, 必须同时取。
好消息: 本插件的 7 个 board_type **全跑**, 所以拿到的数据是完整闭合的。

字段上两者也互补: 池给 `题材` / `涨停原因` / `封单额`, 梯队给 `连板高度` / `一字板` / `热门股`。

---

## 5. 能喂给 IRP 短线连板的什么、不能喂什么

对照 `D:\AI-Data\投资工作台开发\docs\IRP-SHORT-TERM-SPEC-v2.0.md`（IRP 短线连板 v2.0）的指标：

| IRP v2.0 指标 | KPH 能喂吗 | 来源 |
|---|---|---|
| 涨停家数 / 连板家数 / 首板只数 / 最高连板 | ✅ | 涨停池 count + `limit_count` |
| 梯队结构 + 晋级率（1进2/2进3…） | ✅ | 连板天梯 + 涨停池 `limit_count` 跨日 |
| 概念热度榜 / 主线集中度 | ✅ | 板块排行（`plate_heat_score` 就是排行排序键）+ 涨停池 `plate_id` 分布 |
| 14 日情绪时间轴 | ✅ | 涨停池 + 情绪截面，可跨日累积 |
| `seal_amount_yi` 封板成交额 | ⚠️ 口径要定 | KPH 行里 **idx13 = 成交额**（IRP spec 写的 `items[].amount`）、**idx23 = 封单额**，两个值都在，别拿错 |
| 涨停原因 | ✅（**但 ELTDX 也有**） | 涨停池 idx16 |
| 跌停池 | ✅（**且含 ST 股**，ELTDX 的 F10 跌停池不含） | 跌停池 |
| **封板率 / 炸板率 / 连板梯队 / 市场概况** | ❌ | ⚠️ **ELTDX 早就有**：`f10.limit_up_down_list(start, end, include_summary=True)` 返回 `status ∈ {limit_up, broken, limit_down}` + `limit_reason` + `board_level` + 市场概况，**且支持历史日期区间查询** |
| `is_exploded` 当日开板痕迹 | ✅ **（2026-10-20 晚发现）** | 涨停池 **idx 24 = 是否炸过板**（布尔）。用 ELTDX `raw["kbcs"]` + 东财 `zbc` 双重标定，涨停池 55 只**逐只零分歧** → 插件已映射为 `is_exploded` 列（仅 `up` 板型填值） |
| **一字板补齐** | ✅ **（2026-10-20 晚，market_events）** | 涨停池缺的 6 只（`000573`/`000607`/`002889`/`600825`/`603636`/`603999`）**event 流 100% 覆盖**，其中 5 只标「大单一字」——一字板不在涨停池里（09:25 集合竞价封板） |

> ⚠️ **`limit_count` 与 `total_board_count` 是两个不同指标**（2026-10-20 拍板拆分）：
> | 列 | 语义 | up / resumption | ladder |
> |---|---|---|---|
> | `limit_count` | **当前连板天数** | 从 idx9 连板描述文本解析（跨板形态留 None） | idx2 直接给 |
> | `total_board_count` | **区间总板数** | idx10（"4天3板" → 3） | 无对应 → None |
>
> 跨板形态（"4天3板"）的当前连板天数**不可知**（实测 002917「4天3板」ELTDX lbts=1、
> 601811「6天5板」lbts=3）→ `limit_count` 留 `None`，不拿区间板数冒充。
> **算连板高度 / 晋级率用 ladder 板型**（它的 `limit_count` 就是当前连板天数）。

> ⚠️ **别用 KPH 的单个池子算涨停家数** —— 「涨停池」55 只与「连板天梯」59 只是**两个不同口径**，
> 而且**不是包含关系**（2026-10-20 晚逐只核对修正了旧结论）：
> ```
> KPH 涨停池 55 只  ⊄  连板天梯 59 只      ← 双向都有差
>   涨停池独有: 920006, 920690            ← 北交所 920xxx 不进天梯
>   天梯独有:   000573, 000607, 002889, 600825, 603636, 603999
> KPH 涨停池 55 ∪ KPH 连板天梯 59 = 61
> ELTDX limit_up = 61 ｜ 东财 getTopicZTPool = 61
> ```
> **KPH「涨停池 ∪ 天梯」与 ELTDX 61 只逐只完全一致**（差集双向为空）→ 数据可信，
> 但**必须取「池 ∪ 梯队」**，单取涨停池会少 6 只。

> ✅ **2026-10-20 晚修正：ELTDX 也有「当日开板次数」**（`raw["kbcs"]`，dataclass 字段名
> `broken_count`）→ **IRP 的 `is_exploded` / `re_seal_count` 不是缺口**。
> 与东财 `getTopicZTPool` 的 `zbc` 逐股对账：61 只涨停股中 **57 只精确一致**，
> `zbc>0` 与 `kbcs>0` 的集合**完全相同（30 只，差集 ∅）**。
> 证据见 `docs/sources/东财炸板与开板次数实测.md`。
>
> ⚠️ **KPH 的 `is_exploded` 只是布尔**（idx24 取值集合 = {0,1}），**给不出开板次数**。
> 要次数（`open_count`）只有 ELTDX `kbcs` / 东财 `zbc` 有。
>
> ⚠️ 之前写的「KPH 涨停池 `idx7` 恒 0 → KPH 给不了开板痕迹」**是找错了列**：
> `idx7` 确实恒 0（实测 434 行全 0，是垃圾列），但真正的标志是 **idx24**。
> 教训：位置数组源**不能只看一两个下标就下结论**，要用具名字段源当标尺把整行扫一遍
> （`scripts/verify_kph_crosscheck.py` 扫了全部 34 列，阳性对照 idx10↔连板数精确率 0.964）。
>
> 结论：**封板率/炸板率/连板梯队/市场概况/开板次数直接用 ELTDX 的 F10**，不必为炸板专门接东财
> （`BK1631` 降级为交叉校验 / 备援，见 `docs/sources/东财炸板板块实测.md`）。
> KPH 相对 ELTDX 的**真增量**只剩：**封单量**（ELTDX 只有额没有量）、涨停复盘长文本、板块热度积分、风向标、含 ST 的跌停池
> （⚠️ 2026-10-20 晚更正：ELTDX **是提供封单额**的 —— 涨停 61 只 + 跌停 10 只 `seal_amount` 全有值，
> 炸板 52 只为 0 合理。我上午写的「113 行全为 0」是统计脚本取错字段导致的误判）。

---

## 5.1 `market_events` —— 独立数据集（2026-10-20 决策）

盘面梳理事件流**单独声明为独立数据集**，不并入 `limit_board`：
event 返回的是 **dict 行**（不是位置数组），一条事件可涉及多只股票，语义上是"事件"而非"板"。

| 列 | 类型 | 含义 |
|---|---|---|
| `symbol` / `name` | Utf8 | 个股（从 `StockList` 展开；无关联个股的事件为 None） |
| `trade_date` | Date | 交易日 |
| `event_time` | Int64 | 事件时间（**unix 秒**；上游字段名 `TimeMin` 但值是秒不是分） |
| `tag_id` / `tag_name` | Int32 / Utf8 | 标签（大单一字 / 人气股杀跌 / 尾盘炸板回封 / 北交所 / …） |
| `plate_id` / `plate_name` | Utf8 | 板块 |
| `detail` | Utf8 | 事件描述全文 |

**实测（2026-10-16，`limit=100` → 31 条 / 9 列）**：

```
tag_name 分布: 人气股杀跌9 / 大单一字5 / 领先身位5 / 权重杀跌3 / 权重拉升3
               / 直线拉升2 / 北交所2 / 大长腿1 / 尾盘炸板回封1
事件时间范围: 2026-10-16 09:25:00 → 15:00:02
```

**💡 最有价值的用途：补一字板**。涨停池 55 只 vs ELTDX/东财 61 只，缺的 6 只
（`000573` `000607` `002889` `600825` `603636` `603999`）**event 流 100% 覆盖**，
其中 5 只标「大单一字」——**一字板不在涨停池里**（09:25 集合竞价封板，涨停池只收盘中板）。
「尾盘炸板回封」标签则是 `is_exploded` 的语义化版本。

### 5.2 ⚠️ 实时域名会忽略 `Date` 参数 → 客户端会**报错**

上游实时域名完全忽略 `Date`：2026-10-20 实测 5 个日期 × 2 次 = **10/10 次一律返回
2026-10-19 的数据**。以前这会**静默**拿旧数据，定时任务分辨不出来。

现在 client 层校验 `payload` 顶层日期（各板型字段名不同：`day` / `Date` / `date` / `Day[list]`），
**与请求日期不一致 → 抛 `KphError`**，错误信息里说明原因和怎么修。

真连验证（`scripts/verify_kph_date_guard.py`）：

```
用今天(2026-10-20)请求实时类板型:
  ladder  -> 已报错 (请求 2026-10-20, 实际 2026-10-19)
  event   -> 已报错
  emotion -> 未报错 (上游该端点今天已更新)
  sector  -> 未报错 (同上)
用历史日期(2026-10-16)请求: 5 个板型全部正常返回
```

> 副作用：`bridge.availability()` 探活**不能用今天**（会拿到日期不一致 → 误判插件不可用），
> 已改为依次试最多 3 个历史工作日。

---

## 6. 在 TSP 项目里启用

```powershell
# 1. 先应用框架补丁 (新增 limit_board 数据集)
cd D:\AI-Data\投资工作台开发\plugins\kph\framework-patch
powershell -ExecutionPolicy Bypass -File .\apply.ps1 -TargetBackend "D:\AI-Data\TickFlow-Desktop\backend"

# 2. 复制插件
$src = "D:\AI-Data\投资工作台开发\plugins\kph"
$dst = "D:\AI-Data\TickFlow-Desktop\backend\app\plugins\kph"
New-Item -ItemType Directory -Force $dst | Out-Null
Copy-Item "$src\plugin.yaml" $dst
Copy-Item "$src\__init__.py"  $dst
Copy-Item "$src\client.py"    $dst
Copy-Item "$src\provider.py"  $dst
Copy-Item "$src\bridge.py"    $dst
Copy-Item "$src\README.md"    $dst
# 测试
New-Item -ItemType Directory -Force "D:\AI-Data\TickFlow-Desktop\backend\tests" | Out-Null
Copy-Item "$src\tests\test_kph_provider.py" "D:\AI-Data\TickFlow-Desktop\backend\tests"
Copy-Item "$src\tests\test_fetcher.py"       "D:\AI-Data\TickFlow-Desktop\backend\tests"

# 3. 跑契约测试
cd D:\AI-Data\TickFlow-Desktop\backend
uv run --frozen pytest tests/test_kph_provider.py tests/test_fetcher.py -q
uv run --frozen ruff check app/plugins/kph/ tests/test_kph_provider.py

# 4. 启动 backend, 设置页 → 数据源 → 确认 kph 卡片可用 (check 函数会真探上游)
#    设置页 → 能力路由 → 涨停板 → 切到 kph
# 5. 数据页 → 立即同步 (或调 POST /api/data-providers/limit-board/sync)
```

### 不想装框架, 只想要落一份 parquet

```powershell
# 盘后批量落盘 (薄封装 Provider)
uv run python scripts/kph_fetcher.py --date 2026-08-14 --board-type all
# 输出: data/extensions/kph/2026-08-14/limit_board.parquet + .duckdb
```

---

## 7. 目录

```text
plugins/kph/
├── plugin.yaml              插件清单 (datasets: [limit_board, market_events])
├── provider.py              Provider 实现 (get_limit_board / get_market_events / test_dataset)
├── client.py                HTTP + 协议层 (双参数路由 / 设备指纹 / 位置数组解析)
├── bridge.py                availability() — 真探上游可达性
├── fetcher.py               盘后批量落盘 CLI (薄封装 Provider)
├── ruff.toml                vendor 规则集
├── data-source-config.md    为什么 YAML 路线走不通 (设计决策说明)
├── framework-patch/         框架新增 limit_board 数据集的补丁包 (见其 README)
└── tests/
    ├── test_kph_provider.py 34 个契约测试
    └── test_fetcher.py      CLI 落盘测试
```

---

## 8. 上线前必须核对 (已核对 ✅ / 待核对 ⚠️)

| 项 | 状态 |
|---|---|
| 端点 URL / a-c 路由 / 设备指纹 | ✅ 实测 2026-10-19 验证通过 |
| 涨停池 / 跌停池 / 天梯 / 情绪 / 板块 / 复盘 字段下标 | ✅ 真实响应逐列核对 |
| `turnover_rate` / `change_pct` / `amplitude` 是百分数 | ✅ 实测 (14.36 → 0.1436) |
| `amount` / `seal_money` / `net_inflow` / `market_cap` 单位是元 | ✅ 实测 |
| `idx13` = 成交额 (元) | ✅ **与财联社同股同日字节级一致** (佳力奇 234,615,415 / 英搏尔 593,115,163) |
| `seal_amount` (idx8) 单位是**股** (需 /100 → 手) | ✅ **已确认**: 与 CLS **成交量(股)** 同量级且 5/6 只 ≤ 成交量; 若单位是"手"则会比成交量大 100 倍 (物理不可能) |
| 板块行 idx 2 = 板块热度积分 (排序键) | ✅ 全 50 行严格降序验证; **不是**成分股数 |
| 板块行 idx 5 = 板块成交额合计 | ✅ 与成分股实算吻合到元级 |
| 板块行 idx 11 / 12 / 15 / 16 语义 | ⚠️ **未能确认**, 当前置 None (已确认特征见 §3.3) |
| `limit_time` 是 unix 秒 | ⚠️ **按量级推断** (1792127637 → 2026-10-16), 未核对到分钟级 |
| 涨停池 `ZT` vs 池子数量差 6 只 | ✅ **已解释**: 涨停池 ∪ 连板梯队 == ZT (4/5 天完全相等), 两接口互补 |
| **`idx24` = 是否炸过板** | ✅ **已确认** (2026-10-20 晚): 与 ELTDX `kbcs>0` / 东财 `zbc>0` 在 55 只涨停股上**逐只零分歧**（25 只 0 / 30 只 1）。跌停池 11 只 + 风向标 22 只实测**恒 0** → 填 None。⚠️ 只给**布尔**，次数要 ELTDX/东财 |
| 涨停池 vs 连板天梯的包含关系 | ✅ **已修正**: **不是包含关系**（涨停池独有 920006/920690，北交所不进天梯；天梯独有 6 只）。必须取「池 ∪ 梯队」 |
| `idx23` (封单额) / `idx8` (封单量) 的比值 | ⚠️ 只有 1/45 只 ≈ 涨停价 → **两者不是同口径/同时点**, 不能互推封单价 |

**核对方法**: 改 `client.py` 的下标映射 → 重跑 `tests/test_kph_provider.py` →
用 `fetcher.py --date <某日>` 拉一次真实数据 → 与 KPH 网页端该日数据逐字段比对。
**跨源交叉验证**是这次最有效的手段: 财联社的 `business_amount`/`business_balance`/
`last_px` 都是**具名字段**, 拿来当 KPH 位置数组的标尺, 一次就定位出 3 处映射错误。

---

## 9. 协议合规

KPH 无公开 API 文档, 本插件抓的是其**移动端网页的公开接口**。使用边界:
- 仅供个人研究 / 协议学习, **不做商业用途**、不转售、不高频压测 (默认 1s 节流)
- 遇到封禁/限流应立即降频, 不要绕过
- 数据版权归 KPH, 引用需注明来源
