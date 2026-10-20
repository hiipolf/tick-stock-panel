# PATH-SURVEY — Path A-6 候选调研

> Date: 2026-10-20
> Status: **Draft for grill** — 本文是选型证据, 不是决策。决策需走 ADR / Spec 流程。
> Scope: `backend/app/` 内仍为纯 Python 的 service-level compute hot spot。
> Method: 全部结论基于本仓库代码的 `file:line` 引用; 无外部资料。
> Measurement: Python AST 解析(函数 LOC、Polars 表达式 token 计数、call site 计数)+
> 真实数据目录 `D:/AI-Stock/Stock-Data/` 的 parquet schema 抽样。
> Related: [ADR-0001](adr/0001-rust-kernel-path-a-scope.md) / [ADR-0006](adr/0006-service-level-rust-migration.md) /
> [Spec 0001-0005](specs/) / [CONTEXT.md](../CONTEXT.md)

---

## 1. 摘要 / TL;DR

**推荐 Path A-6 目标:`backend/app/services/market_mainline.py::compute_mainline_range`(L124-250, 127 LOC)**
—— "每日主线排行" 的全市场 daily-grain 聚合, 2 个 Rust kernel, 走 Path A-5 的 multi-batch daily-grain 模板。

三个驱动数字 + 一条勘误状态:

1. **它是本仓库现存最大的、仍在 Python 侧的 (a) 类全市场 daily-grain 批聚合**:
   2 次 `group_by` + 2 次 `join` + 3 次 `.over("date")` + 2 次 `rank`,单函数 127 LOC。
2. **它和已经迁完的 Path A-5 是同一个 HTTP 入口的后半段**:
   `POST /api/regime/recompute` 先调 Rust 化的 `run_regime_batch`(`api/regime.py:159`),
   再调仍是纯 Python 的 `compute_mainline_range`(`api/regime.py:168` / `:305`)。
   同一按钮、同一次全量重算,一半已迁一半没迁 —— 迁移边界无需新设计。
3. **ADR-0001 点名的两个 hot zone 都不是 Polars,当前 seam 迁不了**:
   `因子矩阵`(`backend/app/backtest/matrix.py`)= `np.` 859 处 / `group_by` 0 处;
   `信号合并`(`backend/app/strategy/composite.py`)= `np.` 33 处 / `pl.` 0 处。
   两者都是 NumPy dense ndarray,迁到 `PyDataFrame` seam 需要引入 `numpy` Rust crate(新依赖)并新增一类 seam。
4. **Spec 0005 的 `mining_candidates._scan_*` 勘误已进工作树(commit `bad6c78`),但换上的新候选仍不合格**:
   该勘误把候选名换成 `repository._scan_daily_batch` / `matrix._scan_matrix_values` ——
   前者 16 LOC、**Python 算术 0 处**(纯 lazy scan,搬了没东西搬),
   后者是 **numpy 路径**(pyarrow batch → dense 数组 scatter)。
   **ADR-0006 L265 / L360 仍写着 `mining_candidates._scan_*`,尚未勘误。**
   完整复核见 §6。

> **与计划隐含选择的冲突(如实陈述)**:ADR-0001 §上下文 点名的是"支撑位 / 因子矩阵 / 信号合并";
> `crates/factors` / `crates/signals` 的占位注释也把 factor / signal merge 列为第 2、第 3 顺位
> (`crates/factors/src/lib.rs:3-5`, `crates/signals/src/lib.rs:3-4`)。
> **本调研的实测结论是:那两个 hot zone 在当前 Path A seam 下不可迁**(见 §5 排名表 R-8 / R-9),
> 因此 Path A-6 若继续在 Path A seam 内做,就不应再从那两个 hot zone 里选。

---

## 2. 选型标准 (Selection criteria)

标准从三处推导:ADR-0001(热区来源 + 硬约束)、ADR-0006 §"5 段"(seam 契约)、CONTEXT.md(术语与工具链 pin)。

### 2.1 硬约束 (违反即出局,不可谈判)

| # | 约束 | 出处 | 判据 |
|---|---|---|---|
| H1 | 不新增依赖 | ADR-0001 决策 5 / Spec 0005 Q9 | kernel 只能用已 vendored 的 `polars 0.55.2` crate + `pyo3` + `pyo3-polars`;**引入 `numpy` crate 即出局** |
| H2 | 不破坏 vendor 升级链路 | ADR-0001 决策 5 / ADR-0003 (TF-RS-1) | 不得要求 vendor 侧新增 polars 特性 / 触发 patch cascade |
| H3 | 单 wheel abi3-py310 | CONTEXT.md §"版本与工具链" L74 | 不得引入需要 per-version wheel 的输入类型(典型: numpy buffer ABI) |
| H4 | Python 侧 `polars 1.44` / Rust 侧 `polars 0.55.2` 双版本锁齐 | CONTEXT.md L73 | kernel 不得使用 Python 侧 1.44 独有或 Rust 侧 0.55.2 缺失的语义 |

### 2.2 Seam 契约 (ADR-0006 §"5 段", L44-L77)

| # | 要求 | 本调研的判定问题 |
|---|---|---|
| S1 | Pre-aggregate 在 Python adapter | IO(读 parquet / yml / ext parquet)能否完全切到 adapter 侧? |
| S2 | Kernel signature 多参数 | 能否用 2-4 个类型化参数精确表达输入(而非单一 df)? |
| S3 | Kernel 只接受 structured pure-compute input | kernel body 内有无 `Path::read` / 网络 / cache 读? |
| S4 | Kernel 返回 `PyDataFrame` | 输出能否落成一张**扁平、dtype 稳定**的表? |
| S5 | Cache 归属 Python | 模块级 cache 是否留在 Python 侧? |

> Path A-4 已把 S1/S3 放宽到"接受 `PathBuf` + `PlParquetReader`"(ADR-0006 L94-L104),
> 因此 IO kernel 是合法的;但代价是 kernel 必须**逐字复刻** Python 侧的 scan 语义
> (例:`repository.py:1734` 的 `pl.ScanCastOptions(integer_cast="allow-float")`)。

### 2.3 由 ADR-0001 热区来源导出的标准

| # | 要求 | 说明 |
|---|---|---|
| O1 | 落在 ADR-0001 L75 点名的三个热区(支撑位 / 因子矩阵 / 信号合并)或其**同族**(按概念/行业的横截面因子打分) | 支撑位已完成(Path A-1/2) |
| O2 | 与 `crates/{factors,signals}` 预留 crate 的归属一致 | `crates/factors` → 因子族;`crates/signals` → 信号族 |

### 2.4 派生标准(本调研新增,也是真正决定收益的一条)

**S6 — Python 层迭代密度 (Python-level iteration density)**

Polars 的表达式链(group_by / join / over / rolling)**本身已经由 Polars 的 Rust 引擎在 Python 进程内执行**。
把一条已经是 `group_by + agg` 的表达式链原样翻成 Rust kernel,搬的是同一份算力,**收益接近 0**。

Path A-1..A-5 的真实收益来源是另外三件事,本标准据此定义:

| 收益来源 | 判据 | 本仓库实例 |
|---|---|---|
| (i) Python 层逐行/逐期迭代 | 函数内有 `for` 遍历市场行 / `iter_rows` / `iter_rows(named=True)` | `composite.py:213` `for t in range(n_times)`;`factor.py:1158` `iter_rows` over 全市场 top 分位 |
| (ii) Python UDF | `pl.map_elements` / `apply` 逐行调 Python | `market_mainline.py:193` `map_elements` over 全部 join 后行 |
| (iii) Python 侧 frame / dict 装配 | 结果要逐行 build dict / list | `market_overview_builder` 的 `_dimension_rank`(Path A-3 的原对象) |

**推论:一个候选的"seam 契合度高"并不等于"收益高"。** §5 排名表里 R-4(`compute_rank_correlation`)
就是一个 seam 完美契合、但收益近 0 的反例 —— 它整段计算就是一条 `group_by + agg`。

---

## 3. 已迁移基线 (Completed baseline)

Path A-1..A-5 的实测形态,用于校准"一个合格候选长什么样"。

| 轮次 | 目标 | Kernel 数 | Kernel 位置 | Rust LOC | Fixture 形态 | 换来了什么 |
|---|---|---|---|---|---|---|
| **A-1** | `backend/app/indicators/levels.py::_support_resistance` | 1 | `crates/indicators/src/levels.rs` | 204 | 240-day frozen parquet | 锁住 indicator-level seam(`&PyDataFrame` 零拷贝) |
| **A-2** | `backend/app/indicators/levels.py::_extreme_levels` | 1 | `crates/indicators/src/extreme_levels.rs` | 253 | 240-day frozen parquet,多窗口 `[60, 250]` | 参数化 kernel |
| **A-3** | `backend/app/services/market_overview_builder.py::_dimension_rank` + `_top_rows` + `_pct_band_rows` | 3 | `crates/indicators/src/services_market_overview/` | 335 + 222 + 160 | frozen parquet + yaml + ext 子集(临时 `data_dir`) | **首次 service-level seam**,5 段契约冻结(ADR-0006) |
| **A-4** | `backend/app/services/sector_rotation.py::build_sector_rotation` + 5 helpers | 8 | `crates/indicators/src/services_sector_rotation/` | 67+205+361+240+337+350+201+338 | 5 天 × 30 标的(空 minute 短路路径) | **首次 multi-kernel**;§"5 段"§1+§3 原地修订(允许 `PathBuf`) |
| **A-5** | `backend/app/services/regime_builder.py::run_regime_batch` + `_compute_batch` + `_aggregate_daily` | 2 | `crates/indicators/src/services_regime/` | 368 + 745 | 5 天 × 30 标的 daily(真实 enriched 数据) | **首次 multi-batch daily-grain**;243 交易日 × 30 列 0 cell mismatch,1.85×;HTTP 层产物 byte-identical |

**从基线读出的 3 条校准线:**

1. **Fixture 模板已经定型**:5 天 × 30 标的 + frozen `data_dir` + `expected.json` + `gen_*_fixture.py` regenerator
   (Path A-3 引入,Path A-4/5 直接复用,见 Spec 0005 L194)。
2. **Kernel 落位惯例**:新增子目录 `crates/indicators/src/services_<domain>/`,不新建 workspace member
   (Spec 0005 L88 明确"避免 `crates/{factors,signals,shared}/` 占位 crate")。
3. **收益口径**:Path A-5 的 1.85× 主要来自 **kernel 只算需要的列** + 避开 Python 侧 70 列指标链,
   而不是来自"把 `group_by` 翻成 Rust"(ADR-0006 Path A-5 footnote L190-L210)。

**当前 cdylib 状态**:`tf_rs_indicators` 暴露 **16 个函数**(1 hello-world + 2 Path A-1/2 + 3 Path A-3 + 8 Path A-4 + 2 Path A-5),
Spec 0005 L21-L22。`crates/{factors,signals}` 仍是 `pub fn placeholder() {}` 空占位
(`crates/factors/src/lib.rs:9`, `crates/signals/src/lib.rs:9`)。

---

## 4. 候选清单 (Candidate inventory)

### 4.0 测量方法与口径

- **LOC**:函数 = `def` 行到 `end_lineno`(含);模块 = 文件总行数,括号内为非空行数。
  > 注:任务简报里 `matrix.py` 的 "3883 lines" 是**非空行**;文件总行数 4251。
- **Polars 表达式密度**:**函数体内**的 `group_by` / `.join(` / `pl.concat` / `rolling_` / `.over(` 出现次数(不是整文件)。
- **Callers**:按函数名在 `backend/` 全树 AST 统计,`app/` 与 `tests/` 分列;`app` 列 = 生产调用点。
- **Compute nature**:
  (a) 全市场 batch aggregate / (b) 逐行 scalar-rule / (c) IO-bound(网络/文件系统) / (d) 校验与控制流。
  **只有 (a) 和 (b) 的一部分是 kernel 候选。**

### 4.1 汇总表(按 ADR-0001 热区归属分组)

| ID | 候选 | 位置 | 函数 LOC / 模块 LOC | Polars 密度(group_by/join/concat/over) | Python 层迭代 | 生产调用点 | Nature | Seam 契合 | 结论 |
|---|---|---|---|---|---|---|---|---|---|
| R-1 | **主线排行** `compute_mainline_range` | `backend/app/services/market_mainline.py:124-250` | 127 / 296 (254) | **2 / 2 / 0 / 3** + 2 `rank` + 1 `map_elements` | 1 `map_elements` over 全部 join 后行 | **3**(`api/regime.py:168`, `:305`, `market_mainline.py:293`) | **(a)** | ✅ 5/5 | **推荐** |
| R-2 | **因子指标族** `_evaluate_panel` + 9 helpers | `backend/app/backtest/factor.py:561-663` + `:475-1303` | 103 + (85+13+26+31+90+90+34+52+43+55+69) / 1337 (1226) | 合计 5 `group_by` / 5 `join` / 0 / 3 | 2 处 `iter_rows`(全市场) | 2(`factor.py:210`, `:309`) | **(a)** | ⚠️ 输出 7 种异构 list[dict] | 亚军,太宽 |
| R-2b | **维度轮动** `build_rps_rotation` | `backend/app/services/rps_rotation.py:115-217` | 103 / 230 (189) | 3 / 1 / 0 / 0 | 1 `iter_rows`(按日) | 2(`api/rps.py:33`, `concept_rotation_analyzer.py:339`) | (a) | ✅ cache 模式完全契合 §5 | 备选 |
| R-3 | **板块分时** `_dimension_intraday_compute` | `backend/app/api/ext_data.py:613-722` | 110 / 1316 (1105) | 3 / 5 / 0 / 1 + 1 cross-join | 1 `iter_rows`(240 行) | 1(`ext_data.py:744`) | (a) **单日 grain** | ✅ 但输出是 dict 非扁平表 | 备选 |
| R-4 | **秩相关** `compute_rank_correlation` | `backend/app/backtest/mining.py:446-549` | 104 / 1660 (1509) | 1 / 0 / 0 / 0 | 0(仅遍历因子对) | 4(`mining.py:935`,`:1006`,`:1112`,`mining_runtime.py:1063`) | (a) | ✅ **完美** | **收益近 0,见 S6** |
| R-5 | **挖掘面板装载** `_load_compact_factor_panel` | `backend/app/backtest/mining_runtime.py:313-434` | 122 / 1556 (1459) | 1 / 0 / 2 / 0 | 按日期 `partition_by` + 逐日 `pl.concat`(L424-434) | 1(`mining_runtime.py:504`) | (a)+(c) | ✅ | 依赖 R-2 的 fixture |
| R-6 | **市场阶段** `classify_phase_series` | `backend/app/services/market_phase.py:172-255` | 84 | 1 / 0 / 0 / 1 | 逐日 `for i in range(n)`(L227) | 1(`regime_builder.py:796`) | (b) | ✅ | 日频 ~1700 行,收益≈0 |
| R-7 | **环境规则** `classify_state` + `_compute_subscores` | `backend/app/services/regime_builder.py:118-145` / `:65-115` | 28 + 51 / 968 (850) | 0 / 0 / 0 / 0 | 0 | 1 + 2 | (b) | ✅ | 逐日 scalar,非市场维 |
| R-8 | **信号合并** `merge_signal_matrices` + `merge_results` | `backend/app/strategy/composite.py:162-267` / `:46-134` | 106 + 89 / 267 (233) | **0 / 0 / 0 / 0**(`np.` 33,`pl.` 0) | `for t in range(n_times)`(L213)+ `np.argsort` | 1(`backtest/strategy.py:866`)+ 1(`strategy/engine.py:1485`) | (b) | ❌ **ndarray** | **H1/H3 出局** |
| R-9 | **因子矩阵** `_compute_matrix_feature` 等 | `backend/app/backtest/matrix.py:3803-4100` / `:571-677` | 298 + 107 / **4251 (3883)** | **0 / 0 / 0 / 0**(`np.` 859,`pl.` 35) | 0(numpy 向量化) | 6(`build_market_data_matrix`) | (b) | ❌ **ndarray** | **H1/H3 出局** |
| R-10 | **选股** `ScreenerService.run` | `backend/app/services/screener.py:363-429` | 67 / 550 (476) | 0 / 1 / 0 / 0 | 0 | 1(`api/screener.py:294`) | (c)+(d) | ❌ 计算在 DuckDB | 出局(L398-409) |
| R-11 | **挖掘候选** `mining_candidates.py` | `backend/app/services/mining_candidates.py` | — / 793 (754) | — | — | — | (c)+(d) | ❌ | **Spec 0005 引用错误,见 §6** |
| R-12 | **深度行情** `depth_service` | `backend/app/services/depth_service.py:294-346` | 53 / 662 (574) | 0 / 0 / 0 / 0 | 0 | 6 | (c) 网络 | ❌ | 出局 |
| R-13 | **板块监控** `sector_monitor` | `backend/app/services/sector_monitor.py:123-202` / `:79-121` | 80 + 43 / 349 (313) | 0 / 0 / 0 / 0 | 0 | 4 | (c)+(d) | ❌ | 出局 |
| R-14 | **扩展数据** `ext_data` | `backend/app/services/ext_data.py:621-696` / `:441-468` | 76 + 28 / 802 (688) | 0 / 0 / 3 / 0 | 0 | 2 + 6 | (c)+(b) | ❌ | parquet 写 + 逐行字符串规则 |
| R-15 | **日 K 批量扫描** `_scan_daily_batch` 等 4 个 | `backend/app/tickflow/repository.py:1748-1763` / `:1731-1746` / `:1765-1780` / `:1782-1797` | 各 16 / 2392 (2163) | **0 / 0 / 0 / 0** | 0 | — | (c) | ❌ 纯 lazy scan | 出局,见 §4.3 |
| R-15b | **矩阵值扫描** `_scan_matrix_values` | `backend/app/backtest/matrix.py:1338-1392` | 55 / 4251 (3883) | **0 / 0 / 0 / 0** | 0(numpy scatter) | 2(`matrix.py:968`, `:1122`) | (c)+(b) | ❌ pyarrow → numpy | 出局,见 §4.3 |
| R-16 | **目录统计** `_scan_dir_stats` / `_scan_dir_recursive` | `backend/app/api/data.py:439-456` / `:459-477` | 18 + 19 / 877 (767) | 0 / 0 / 0 / 0 | 0 | — | (c) 文件系统 | ❌ | 出局 |
| R-17 | **涨停梯队** `limit_ladder` | `backend/app/api/screener.py:808` | 266 / 1316 (1105) | 0 / 4 / 0 / 0 | — | API endpoint | (a)+(c) | ❌ `Request` 依赖 | 出局(见下) |

### 4.2 逐个候选要点(只写需要展开的)

**R-1 `compute_mainline_range` —— 推荐**

```
market_mainline.py:163-171  scan_parquet(enriched_dir/"**"/"*.parquet").select(4 列).filter(连板>=1).collect()
market_mainline.py:175-187  symbol 大写化 + ST 剔除
market_mainline.py:189      join 概念映射 (inner)
market_mainline.py:193-197  pl.col(kind).map_elements(_industry_member)   ← Python UDF
market_mainline.py:199-218  group_by([date, member]).agg(6 个聚合, 含 sort_by().first() 取龙头)
market_mainline.py:221-238  4 权重截面 rank 归一 → score → ordinal rank
market_mainline.py:239-249  取 rank<=30, 投影 11 列, 排序
```

- **IO 可分离性(S1)**:L163-171 的 parquet 扫描、L146 的 `_load_concept_map_df`、
  L185 的 `load_risk_warning_symbols`、L150 的 `_resolve_filter_config` 全部是 IO / 偏好读取,都在 adapter 侧。
- **输出 dtype 稳定(S4)**:L243-247 固定投影 11 列(`date` / `kind` / `member` / `limit_up_count` /
  `ge2_count` / `max_boards` / `boards_sum` / `rungs_filled` / `leader_symbol` / `score` / `rank`),
  无嵌套结构 → 单张扁平 `PyDataFrame` 即可承载。
- **Cache 归属(S5)**:函数内无模块级 cache;`_ST_SYMBOLS_CACHE`(L73, 600 s)与
  `rps_rotation._map_cache`(`rps_rotation.py:110-111`)均留在 Python。
- **真实规模**:`D:/AI-Stock/Stock-Data/kline_daily_enriched/` 实测 **1696 个分区**,
  末分区 5567 行 × 15 列(含 `consecutive_limit_ups` / `amount`)。
  模块自述全历史回填"只窄扫 enriched 的 4 列并先过滤连板 >=1(全历史 ~10 万行)"
  (`market_mainline.py:12-13`);`api/regime.py:291` 描述该操作"窄扫描, 秒级"。
- **Fixture 数据源(具体)**:

  | 用途 | 路径 | 实测 shape |
  |---|---|---|
  | 日线 enriched | `D:/AI-Stock/Stock-Data/kline_daily_enriched/date=<5 日>/part.parquet` | 15 列;末分区 5567 行 |
  | 概念映射 | `D:/AI-Stock/Stock-Data/ext_data/ext_gn_ths/part.parquet` | 5581 行 × 5 列(`股票代码`/`股票简称`/`所属概念`/`symbol`/`code`) |
  | 行业映射 | `D:/AI-Stock/Stock-Data/ext_data/ext_hy_ths/part.parquet` | 5575 行 × 5 列(`所属同花顺行业`) |
  | ST 名单 | `D:/AI-Stock/Stock-Data/instruments/instruments.parquet`(`symbol`,`name`) | `load_risk_warning_symbols` L93 读取 |
  | 期望产物(参考) | `D:/AI-Stock/Stock-Data/mainline_history/part.parquet` | 378.3 KB,`upsert_mainline_history` L253 写入 |

  → 4 个 frozen 输入 + 1 个 `data_dir`,与 Path A-3/4/5 的 fixture 模板完全同构。

- **Kernel 数推测**:**2 个**,走 Path A-5 multi-batch daily-grain 模板
  - kernel 1(IO helper):`PathBuf` + 起止日期 → 扫出 `limit_rows`(`date`/`symbol`/`consecutive_limit_ups`/`amount`)
  - kernel 2(reduce):`limit_rows` + 成员映射表 + ST 集合 → join + 6 聚合 + 4 权重归一 + ordinal rank → 扁平 `PyDataFrame`
  - 跨批 `pl.concat` 留 Python(ADR-0006 Path A-5 footnote L133-L135)
  - 备选 1-kernel 形态(Path A-3 模板)也可,但那样 `map_elements` 仍在 Python,S6 的 (ii) 收益拿不到。

**R-2 因子指标族 —— 亚军,但一个周期装不下**

- 9 个 helper 全部是 (a) 类,合计约 500 LOC;`factor.py` 整模块 `pl.` 109 处 / `group_by` 5 / `join` 5 / `over` 3。
- 真正值得迁的 Python 层部分只有两处:`_calc_turnover` 的 `iter_rows(named=True)` 全市场 top 分位逐行装配
  (`factor.py:1158-1159`)与 `_calc_regime_stats` 的 `iter_rows`(`factor.py:870-873`)。
- **卡点**:`FactorResult` 含 **7 种异构 list[dict]** 子结构(ic_series / group_stats / group_nav /
  long_short / yearly_ic / ic_decay / regime_stats),每种 schema 不同
  → 需要 8-9 个 kernel(接近 Path A-4 的 8-kernel 规模),adapter 编排负担重。
- **Fixture 卡点**:需要一个已物化的因子面板(symbol/date/因子值/前瞻收益),
  `D:/AI-Stock/Stock-Data/` 下**没有**这样的现成 parquet;fixture generator 必须先跑
  `compute_indicators` + `compute_limit_signals`(即 Path A-5 fixture 干过的事),成本明显高于 R-1。

**R-2b 维度轮动 `build_rps_rotation` —— seam 契合度最高的"cache 模式"样本**

- `rps_rotation.py:115-217`(103 LOC):IO 段是 `_load_concept_map_df`(L153,带 `_map_cache` / `_map_ts` / `_cache_days` 三层模块级 cache)
  + `repo.get_enriched_range`(L160,命中内存历史 cache);compute 段是 1 次 join(L169)
  + 1 次 `group_by(["date", kind]).agg(mean)`(L184-186)+ 1 次排序 + 1 次 `group_by` 收集成 list(L193-195)。
- **cache 模式与 ADR-0006 §5 完全同构**(cache 命中直接 return,不过 kernel),是 Path A-3 `_dimension_rank` 的近亲。
- 但**算力几乎全在 Polars 内**(S6)—— join + group_by + agg 都是 Polars 表达式,Python 侧只有 1 次按日的 `iter_rows`(L201-204)。
  **收益低**,排 R-1 之后。
- 输出是嵌套 dict(`dates` / `columns: {date: [[member, pct], ...]}` / `concept_count`,L206-210),不是扁平表 →
  kernel 仍可返回扁平 `PyDataFrame`,由 adapter 装配成 dict(S4 满足但多一层装配)。

**R-3 板块分时 —— 备选,单日 grain 限制了杠杆**

- `api/ext_data.py:613-722`:5 次 join + 3 次 group_by + `join(how="cross")` 网格化(L693)
  + `forward_fill().over("_bare")`(L698)。计算量在"单日全市场分钟 bar"(5567 标的 × 240 分钟)。
- **两处不利**:① grain 是**单日**不是日序列,不能像 Path A-5 那样批处理;
  ② 输出是 `{"status","date","basis","member_count","points":[…]}` 字典,不是扁平表,S4 只能靠 adapter 装配。

**R-4 秩相关 —— seam 完美契合,收益近 0(反面教材)**

- `mining.py:446-549`:`group_by(date).agg([... N(N-1)/2 个 pl.corr(spearman) ...])`(L497-503)
  + N 个 count 聚合,最后用 numpy 收尾成 `width × width` 矩阵(L507-521)。
- seam 上无可挑剔:1 个 kernel,Path A-3 单 kernel 模板,输入 `pl.DataFrame` + 因子名列表 + 日期范围。
- **但整段算力已经在 Polars 的 Rust 引擎里跑完了**(S6)。把它 1:1 翻成 Rust kernel,
  搬的是同一份 `pl.corr` 实现,收益接近 0;唯一理论收益是省掉 Python 侧构造 N(N-1)/2 个表达式的开销
  (20 因子 = 190 个 `pl.corr`,可忽略)。
- **结论:seam 契合度高 ≠ 收益高。** 这条要写进 grill,免得 Path A-6 掉进这个坑。

**R-5 / R-6 / R-7 —— 依赖关系或粒度不对**

- R-5 `_load_compact_factor_panel`(`mining_runtime.py:313-434`):按日期 `partition_by` + 逐日 `pl.concat`
  (L424-434)是真实的 Python 层循环,但它是**面板物化**步骤,应随挖掘链一起迁,不适合独立成轮;且 fixture 同 R-2。
- R-6 `classify_phase_series`(`market_phase.py:172-255`):逐日 `for i in range(n)`(L227)只有 ~1700 次迭代。
- R-7 `classify_state` / `_compute_subscores`(`regime_builder.py:118-145` / `:65-115`):
  逐日 scalar 规则,**无市场维**,且 Spec 0005 L251 / L254 已明确列为 out of scope —— 本调研复核后**确认该判断正确**。

**R-8 / R-9 —— ADR-0001 点名的两个热区,当前 seam 迁不了**

- R-8 `composite.py`:`import numpy as np`(L27),`pl.` 出现 **0** 次,`np.` 33 次。
  `merge_signal_matrices` 的 `for t in range(n_times)`(L213)内层是 `np.argsort`(L231)——
  **确实存在真实的 Python 层热点**,但输入是 `SignalMatrix`(numpy 支撑,`matrix.py:126-127`),
  输出也是 `SignalMatrix`,不是 `PyDataFrame`。
- R-9 `matrix.py`:`import numpy as np`(L23),`np.` **859** 次,`pl.` 35 次,`group_by`/`join`/`concat`/`over` **全为 0**。
  `_compute_matrix_feature`(L3803-4100, 298 LOC)是按特征名分派的 dense ndarray 运算
  (38 处 `valid_rolling_*` 调用,全是 numpy helper)。
- **两者迁到 Path A seam 需要**:
  1. `numpy` Rust crate 或 numpy C-API 互操作 → **违反 H1(不新增依赖)与 H3(abi3 单 wheel 对 numpy buffer ABI 不友好)**;
  2. 一类 ADR-0006 5 段契约**未覆盖**的新 seam(ndarray ↔ DataFrame)→ 需要新 ADR,不是 Path A-6。
- **这不是"不值得迁",而是"不在 Path A 序列里"。** 若维护者真正的痛点是回测路径,
  正确动作是另立一份 "Path A-Numpy" seam 提案(新 ADR),而不是硬塞进 Path A-6。

**R-10 / R-11 / R-12 / R-13 / R-14 / R-15 / R-16 / R-17 —— 非 (a) 类,直接出局**

- R-10 `screener.run`(L363-429):真正的筛选在 **DuckDB** 里(L398-409 `con.register` + `SELECT * FROM enriched WHERE …`),
  Python 侧只做拼 SQL 和取结果。计算不在 Python,迁不了。
- R-12 `depth_service._call_depth_batch`(L294-346):网络批量报价,(c) 类。
- R-13 `sector_monitor`:目录/快照装配,单函数 43-80 LOC,2 次 join 无 group_by。
- R-14 `ext_data.write_ext_parquet`(L621-696):parquet 原子写;`normalize_symbol`(L441-468):逐行字符串规则
  (北交所代码段口径就在这里),适合做微 kernel,不值得单开一轮。
- R-17 `limit_ladder`(`api/screener.py:808`, 266 LOC):有 `Request` 依赖 + 读 `depth_service` 全局状态,
  违反 CONTEXT.md L33 "service-level function = 无 Request 依赖的纯函数"。

### 4.3 R-15 / R-15b:任务简报的前提需要修正

任务简报把 `_scan_daily_batch` 等 4 个函数描述为"与 Path A-5 kernel 1 同形"。实测:

**R-15 —— repository 的 4 个日 K 扫描**

- 4 个函数各 **16 LOC**,函数体只有 `scan_enriched_parquet(...)` + `.filter()` + `.sort()` + `.select()` + `guarded_collect()`
  (`repository.py:1748-1763`)。**函数体内 Python 算术为 0** —— 没有可搬的计算。
- Path A-5 的 `compute_batch_kernel`(`crates/indicators/src/services_regime/compute_batch.rs`, 368 LOC Rust)
  之所以有收益,是因为它在 Rust 侧**重算**了指标子集并只取需要的列
  (ADR-0006 Path A-5 footnote L190-L200),而不是"把一个 lazy scan 翻成 Rust"。
- **结论:R-15 的 4 个函数是 (c) IO-bound,不是 (a) 批聚合,不在候选集内。**

**R-15b —— matrix 的 `_scan_matrix_values`**

- `matrix.py:1338-1392`:读 `pads.Dataset` 的 pyarrow scanner,逐 batch 把 Arrow 列
  转成 numpy 并 scatter 进 dense 数组 `target[time_ids, asset_ids] = values`(L1392)。
- 全部是 `np.ndarray` 参数 + `np.unique`(L1384)+ `np.int64`(L1383)—— **numpy 路径**,和 R-9 同病(H1/H3 出局)。

**`_scan_` 前缀在本仓库一共只有 7 个函数**:

| 位置 | 函数 | 类别 |
|---|---|---|
| `backend/scripts/cleanup_halt_days.py:29` | `_scan_dirty` | 脚本,非 `backend/app/` |
| `backend/app/backtest/matrix.py:1338` | `_scan_matrix_values` | (c)+(b) numpy |
| `backend/app/api/data.py:439` / `:459` | `_scan_dir_stats` / `_scan_dir_recursive` | (c) 文件系统 |
| `backend/app/tickflow/repository.py:1731` / `:1748` / `:1765` / `:1782` | `_scan_daily_symbol` / `_scan_daily_batch` / `_scan_index_daily_symbol` / `_scan_etf_daily_symbol` | (c) lazy scan |
| `backend/app/services/regime_builder.py:436` | `_scan_enriched_fallback` | Path A-5 已迁(IO helper) |

**`backend/app/services/mining_candidates.py` 一个都没有**(见 §6)。

> **给 Spec 0005 勘误的补充**:该勘误(commit `bad6c78`)把 Path A-6 的候选名换成了
> `repository._scan_daily_batch` / `matrix._scan_matrix_values`。**事实层面这是对的**
> (这两个函数确实存在),**但按本节实测,这两个都不是合格的 Path A-6 目标**:
> 前者 16 LOC、0 处 Python 算术(搬了也没东西搬),后者是 numpy 路径(撞 H1/H3)。
> 详见 §6.4。

---

## 5. 排序与推荐 (Ranking + recommendation)

### 5.1 排名表

评分:✅ 满足 / ⚠️ 部分 / ❌ 不满足。

| 排名 | 候选 | (a) 类? | Seam 契合 | S6 Python 层成本 | Fixture 可得性 | Kernel 数 | 风险 | 结论 |
|---|---|---|---|---|---|---|---|---|
| **1** | **R-1 `compute_mainline_range`** | ✅ | ✅ 5/5 | ✅ `map_elements` over 全量 join 行 | ✅ 4 个现成 parquet | 2 | 低 | **推荐 Path A-6** |
| 2 | R-2 因子指标族 | ✅ | ⚠️ 7 种输出 schema | ⚠️ 仅 2 处 `iter_rows` | ❌ 需先物化因子面板 | 8-9 | 中 | 留作 Path A-7 候选,可能拆成 2-3 轮 |
| 3 | R-3 `_dimension_intraday_compute` | ✅ 单日 | ⚠️ dict 输出 | ⚠️ cross-join 网格 | ✅ 分钟分区 + ext | 2-3 | 中 | 备选 |
| 4 | R-2b `build_rps_rotation` | ✅ | ✅ §5 cache 模式完全契合 | ⚠️ 低(算力全在 Polars 内) | ✅ 概念映射 + 日线 | 1-2 | 低 | 备选,收益低 |
| 5 | R-4 `compute_rank_correlation` | ✅ | ✅ 5/5 | ❌ 全在 Polars 内 | ❌ 同 R-2 | 1 | 低 | **收益近 0,不做** |
| 6 | R-5 `_load_compact_factor_panel` | ✅+c | ✅ | ✅ 逐日 partition+concat | ❌ 同 R-2 | 1-2 | 中 | 随挖掘链一起迁 |
| 7 | R-6 `classify_phase_series` | (b) 日频 | ✅ | ⚠️ 1700 次迭代 | ✅ | 1 | 低 | 收益≈0,不做 |
| 8 | R-7 `classify_state` / `_compute_subscores` | (b) 日频 | ✅ | ❌ | ✅ | 1 | 低 | Spec 0005 已排除,复核确认 |
| 9 | R-8 `merge_signal_matrices` | (b) | ❌ ndarray | ✅ `for t in range(n_times)` | ❌ 需 npz | ? | **高** | **出 Path A 序列**;另立 ADR |
| 10 | R-9 `matrix.py` 因子矩阵 | (b) | ❌ ndarray | ❌ numpy 已向量化 | ❌ 需 ndarray fixture | ? | **高** | **出 Path A 序列**;另立 ADR |
| 11-18 | R-10..R-17(含 R-15b) | (c)/(d) | — | — | — | — | — | 出局(见 §4.2) |

### 5.2 推荐:Path A-6 = `backend/app/services/market_mainline.py::compute_mainline_range`

**理由(按证据强度排序):**

1. **它是唯一同时满足"规模 + seam + fixture"三线、且收益不为 0 的 (a) 类候选。**
   - 规模:1696 个 enriched 分区(实测),全历史回填 ~10 万行涨停行(module docstring L12-13);
   - seam:5 段全中 —— IO 可完全切到 adapter(L146 / L163-171 / L185 / L150),
     输出是固定 11 列的扁平表(L243-247),cache 全在 Python(L73 / `rps_rotation.py:110`);
   - fixture:4 个现成 parquet(§4.2 表),与 Path A-3/4/5 模板同构,**不需要新物化流程**。

2. **它是 Path A-5 已验证路径的同构延伸,不是新发明。**
   `POST /api/regime/recompute`(`api/regime.py:159-171`)先跑 Rust 化的 `run_regime_batch`,
   再跑纯 Python 的 `compute_mainline_range`(两种 kind,`:167`)。
   Path A-5 已经证明"daily-grain 多 batch + 跨批 concat 留 Python + kernel-empty 回退"这一整套在真实数据上可用
   (ADR-0006 L149-L168)。Path A-6 只是把同一入口的另一半接上。

3. **它拿得到 S6 的 (ii) Python UDF 收益,而 R-4 完全拿不到。**
   `pl.col(kind).map_elements(...)`(`market_mainline.py:193-197`)是逐行 Python UDF,
   作用在**全部 join 后的行**上 —— 这是真实的 Python 层成本,迁到 Rust 后是字符串切片。
   此外 L164 的 `scan_parquet("**")` 会 glob 全部 1696 个分区再靠谓词下推,
   kernel 可只枚举日期区间内的分区。
   > **如实说明**:它只拿到 S6 的 (ii);(i) Python 层逐行迭代它没有(函数内唯一的 `for` 是
   > L223 遍历 4 个固定权重列,不是市场行),(iii) dict 装配也没有(输出是 DataFrame)。
   > 收益主要来自 (ii) + 分区 glob 收窄,**不是** 来自把 group_by 翻成 Rust。

4. **它落在 ADR-0001 的热区家族内,且归属 `crates/factors`。**
   主线分 = 4 个涨停梯队统计量(`_SCORE_WEIGHTS`,L40-45)的**横截面加权因子分**,
   与"因子矩阵 / 因子"同族;`crates/factors` 占位 crate 正是为此预留(`crates/factors/src/lib.rs:3-5`)。
   落位建议:`crates/indicators/src/services_mainline/`(沿用 Path A-3/4/5 的子目录惯例,
   Spec 0005 L88 明确不新建 workspace member)。

**预计收益(估算,未实测 —— 本次调研未跑 benchmark):**

- 现状 `api/regime.py:291` 自述"窄扫描, 秒级"。全量重算要把 concept + industry 两种 kind 各跑一次
  (L167 / L304)。预计 kernel 化后能把**全量双 kind 重算压到亚秒级**,即秒级操作上的 2-5×。
  **这是估算不是实测**;Path A-6 的 T01 research 应给出真实数字。
- 收益**不是** 100× 级别 —— 最大的两块算力(join / group_by+rank)本来就由 Polars 的 Rust 引擎承担。

**风险与边界(必须写进 Spec):**

| 风险 | 缓解 |
|---|---|
| 改动 `market_mainline` 的输出语义 = 改前端"什么阶段走什么主升"的判据 | parity 门槛照 Path A-1..A-5:bit-identical;`upsert_mainline_history` 产物 byte-equal |
| `leader_symbol` 来自 `sort_by(...).first()` 聚合(`market_mainline.py:210-216`),平局时顺序依赖 Polars 的稳定排序 | parity fixture 必须挑**存在并列**的交易日,否则测不出差异(见 §7 Q3) |
| `kind="industry"` 走 `_industry_member` 截断到 2 级(`L117-121`, `_INDUSTRY_LEVEL=2`),`kind="concept"` 原样返回 | fixture 必须**同时**覆盖两种 kind,否则只证明了一半 |
| 改了 `compute_mainline_range` 的调用签名会波及 3 个生产调用点 | 保持 public 签名不变(Path A-3 Q5 的既定做法) |

### 5.3 本推荐可能错在哪(如实列出反证条件)

- **若维护者的真实痛点是回测路径**(`matrix.py` / `composite.py`),
  那么本推荐就是错的 —— 正确的动作不是挑一个 Path A seam 内凑得过去的候选,
  而是先开一份 "numpy interop seam" 的新 ADR(新依赖 + 新 seam 类别,都超出 Path A 的既定边界)。
  **本文档不替这个决定下结论**,它只说明:在 Path A 现有边界内,回测路径**没有**合格候选。
- **若 R-2 因子指标族被接受为"一个周期"**,则 Path A-6 应改为 R-2,但需接受 8-9 个 kernel 的
  adapter 复杂度 + 一个需要自建物化流程的 fixture。本文不推荐,理由如上(输出 schema 异构 + fixture 成本)。

---

## 6. 对 Spec 0005 的勘误 (Correction to Spec 0005)

### 6.1 状态:Spec 0005 已在工作树中被勘误,ADR-0006 尚未

本调研开始时,`docs/specs/0005-...md` 与 `docs/adr/0006-...md` 中共 5 处把
`mining_candidates._scan_*` 当作 Path A-6 候选。**调研期间该勘误已由上游提交进工作树**:

| 位置 | 状态 | 内容 |
|---|---|---|
| `docs/specs/0005-...md:86` | ✅ 已勘误(commit `bad6c78`) | 现为 "(`repository._scan_daily_batch` / `matrix._scan_matrix_values` etc.)" |
| `docs/specs/0005-...md:255-257` | ✅ 已勘误(commit `bad6c78`) | 现为 "**勘误 2026-10-20**: 该函数名不存在 … 真实的 `_scan_*` 家族是: …" |
| `docs/adr/0006-...md:265` | ❌ **仍未勘误** | 仍写 "`mining_candidates._scan_*)` 等" |
| `docs/adr/0006-...md:360` | ❌ **仍未勘误** | 仍写 "`services/mining_candidates._scan_*` — 多因子打分,per-symbol loop,callers 多。" |

> 本节只做**记录与独立复核**,不代改 Spec 0005 / ADR-0006(按任务约束)。

### 6.2 独立复核:事实层面,勘误是对的

**`backend/app/services/mining_candidates.py` 中不存在任何名为 `_scan_*` 的函数。**

AST 函数清单(该模块共 793 行 / 非空 754 行,共 28 个函数):
`promote`(L83)、`publish`(L112)、`_load_candidate`(L184)、`_registered_candidates_path`(L216)、
`_read_artifact`(L233)、`_validate_definition`(L273)、`_request`(L331)、`_validate_factor_definition`(L337)、
`_validate_existing_definition`(L381)、`_promotion_config`(L398)、`_candidate_metrics`(L446)、
`_validated_summary`(L475)、`_asset_type`(L485)、`_verify_public_strategy`(L491)、
`_validate_published_id`(L524)、`_validate_publication_backlink`(L532)、`_custom_strategy_path`(L540)、
`_render_factor_strategy`(L553)、`_publish_or_verify_source`(L628)、`_verify_existing_source`(L688)、
`_rollback_publication`(L723)、`_rollback_created_source`(L733)、`_fsync_directory`(L746)、
`_write_backlink`(L756)、`_published_strategy_id`(L786)、`_optional_string`(L792)。

全仓库 `backend/` 下 `_scan_` 前缀的函数只有 7 个(见 §4.3 表)——
**`backend/app/services/mining_candidates.py` 一个都没有**。→ **Spec 0005 的勘误结论成立。**

### 6.3 `mining_candidates.py` 本身:不是 kernel 候选

该模块是**挖掘候选的晋级 / 发布服务**:读 artifact parquet(`_read_artifact` L233)、
校验因子定义(`_validate_factor_definition` L337)、渲染策略源码(`_render_factor_strategy` L553)、
写策略文件 + backlink(`_write_backlink` L756 / `_publish_or_verify_source` L628)、失败回滚
(`_rollback_publication` L723 / `_rollback_created_source` L733)。

按 §2.4 的分类,它整体是 **(c) IO-bound + (d) 校验与控制流** —— 没有任何全市场 batch aggregate,
**不构成 Path A kernel 候选**。→ **应从任何 Path A 候选清单中整体移除。**

### 6.4 但勘误换上的两个新候选也过不了 Path A-6 的门槛(重要)

Spec 0005 勘误把候选名换成了 `repository._scan_daily_batch` / `matrix._scan_matrix_values`。
**事实层面这两个函数确实存在,勘误没有编造**;但按本调研实测,它们都**不是合格的 Path A-6 目标**:

| 新候选 | 实测 | 为什么不合格 |
|---|---|---|
| `repository._scan_daily_batch` 等 4 个 | 各 **16 LOC**;函数体只有 `scan_enriched_parquet` + `.filter` + `.sort` + `.select` + `guarded_collect`;**Python 算术 0 处** | (c) IO-bound。**没有可搬的计算** —— lazy scan 的算力本来就在 Polars 的 Rust 引擎里。1:1 翻成 Rust kernel 收益 = 0(见 §2.4 S6 / R-4) |
| `matrix._scan_matrix_values` | `matrix.py:1338-1392`,55 LOC;pyarrow batch → `target[time_ids, asset_ids] = values`(L1392)写 numpy dense 数组;`np.unique`(L1384)/`np.int64`(L1383) | (c)+(b) **numpy 路径**。要迁需 numpy 互操作 → 撞 H1(不新增依赖)/ H3(abi3 单 wheel) |

**所以"勘误后的 Path A-6 候选名"目前指向两个都不合格的函数。** 真正的处置有两条路:

- **路线甲(推荐)**:把 Path A-6 的目标定在本文 §5.2 推荐的 `compute_mainline_range`,
  并把 Spec 0005 / ADR-0006 里的候选名**再勘误一次**(指向 `market_mainline.compute_mainline_range`)。
- **路线乙**:若维护者确实想沿用"扫描类"目标,则需要先把 ADR-0006 §"5 段"扩展成 numpy interop seam
  —— 那是新 ADR 的范畴,不是 Path A-6。

### 6.5 ADR-0006 L360 那句描述实际指向谁

ADR-0006 L360 写的语义是"**多因子打分, per-symbol loop, callers 多**"。
这与 `backend/app/services/mining_candidates.py` 完全不符,
而与 **`backend/app/backtest/mining.py`** 一族吻合:

| 真实候选 | 位置 | LOC | 说明 |
|---|---|---|---|
| 挖掘编排 | `backend/app/backtest/mining.py::run` | 474(L824-1297) | 多因子候选生成 + 束搜索 + 剪枝 |
| 束搜索 | `backend/app/backtest/mining.py::beam_search_factor_combinations` | 140(L610-749) | 因子组合搜索 |
| 秩相关 | `backend/app/backtest/mining.py::compute_rank_correlation` | 104(L446-549) | 见 R-4 |
| 挖掘运行时 | `backend/app/backtest/mining_runtime.py::run_mining_runtime` | 247(L437-683) | worker 进程内运行时 |
| 因子指标 | `backend/app/backtest/factor.py::_evaluate_panel` + 9 helpers | 103 + ~590 | 见 R-2 |

**勘误结论汇总:**

1. `mining_candidates._scan_*` **不存在** —— 事实错误成立(Spec 0005 已修,ADR-0006 L265/L360 **待修**)。
2. 该描述想表达的"Path A-6 候选"应指向 `backend/app/backtest/mining.py` / `mining_runtime.py` / `factor.py`
   一族(即本文 R-2 / R-4 / R-5),**不是** `mining_candidates.py`。
3. `mining_candidates.py` 应从任何 Path A 候选清单中**整体移除** —— 它是 (c)+(d) 类。
4. Spec 0005 勘误换上的 `repository._scan_daily_batch` / `matrix._scan_matrix_values` 同样不合格(§6.4),
   需要再勘误一次,或改用本文 §5.2 的推荐目标。

---

## 7. 未决问题 (Open questions)

以下是 Path A-6 的 grill **仍需拍板**的问题。本调研刻意不回答。

### Q1 — Kernel 边界:1 个还是 2 个?IO 归谁?

- **选项 A(2 kernel,Path A-5 模板)**:kernel 1 = IO helper(`PathBuf` + 日期 → `limit_rows`),
  kernel 2 = reduce(join + 聚合 + 归一 + rank)。
  - 好处:kernel 2 独占 `map_elements` 的 Rust 化;kernel 1 可只枚举日期区间内的分区,避开 1696 分区 glob。
  - 代价:kernel 1 必须逐字复刻 `scan_enriched_parquet` 的 cast 语义
    (对比 `repository.py:1734` 的 `pl.ScanCastOptions(integer_cast="allow-float")`),
    否则 dtype 与浮点结果会漂。
- **选项 B(1 kernel,Path A-3 模板)**:扫描留在 Python adapter,kernel 只做 join+聚合+归一。
  - 好处:kernel 输入就是一张 `PyDataFrame` + 一张映射表,契约最简单。
  - 代价:1696 分区 glob 的成本没解决;`map_elements` 仍在 Python(除非 join 也搬进 kernel)。
- 待定:选 A 还是 B,或 A 但 kernel 1 直接用 `polars::prelude::PlParquetReader` 而非复刻 `scan_enriched_parquet`。

### Q2 — Fixture 的 5 天 × 30 标的怎么选?

- 必须选**有涨停行**(`consecutive_limit_ups >= 1`)且某概念当日涨停数 **≥ `_MIN_LIMIT_UP` = 3** 的日子
  (`market_mainline.py:37`, 过滤在 L221),否则 `agg.filter(limit_up_count >= 3)` 这条路径根本走不到,fixture 空转。
- 必须**同时**覆盖 `kind="concept"`(成员原样,L119-121)与 `kind="industry"`(截断到 2 级,L117-121),
  两种 kind 的 `member` 取值不同,只测一种等于只测了一半。
- 30 个标的要覆盖:`_sym_up` 大小写归一(L175)、ST 剔除(L187)、多概念 fan-out(一只股票属多个概念)。
- 待定:具体日期与标的清单由 T01 research 定;regenerator 命名沿用 `gen_mainline_fixture.py`。

### Q3 — 平局与 dtype 契约

- `leader_symbol` 来自 `pl.col("symbol").sort_by(pl.col("consecutive_limit_ups"), pl.col("amount"), descending=[True,True]).first()`
  (L210-216)—— `sort_by` 的平局顺序决定龙头归属。**Rust 复刻必须与 Polars 的平局顺序逐位一致**,
  且 fixture 必须挑一个**存在并列**的交易日,否则 parity 断言测不出差异。
- `date` 列 dtype:live enriched parquet 存的是 `Date`(见 ADR-0006 Path A-5 dtype flag L182-L185),
  kernel 按 date 过滤/分组时比较键要转 ISO 字符串,输出要 cast 回入参 dtype。
- `rank` 列:`.rank(method="ordinal", descending=True)`(L237)在 Polars 里是 `UInt32`;
  `score` 是 `Float64`(L229-235);`member` 是 `Utf8`。三者都要写进 kernel 契约文档。
- 待定:`map_elements` 对 `member` 为 null 的行为 —— Python 侧 `_industry_member(str(m), kind)`(L193)
  会把 null 变成字符串 `"None"`。Rust 复刻要**逐字**照做,还是先在 adapter 侧保证非 null?

### Q4 — 批处理规则(是否需要 batch + warmup)

- Path A-5 用 `batch_days=60` + `warmup_days=40`,因为 regime 的 `ma20` 滚动窗口需要预热
  (Spec 0005 L30, ADR-0006 L207-L210)。
- 主线的 rank 归一是**纯横截面**(`over("date")`,L226 / L237),**不跨日**,因此理论上无需 warmup。
- 待定:用 Path A-5 的 batch 模板(带 warmup,简单复用)还是自定义一个更简单的按日切批规则?
  另:`kind="concept"` 与 `kind="industry"` 是两次独立调用,是否在 adapter 里合并成一次 kernel 调用?

### Q5 — 上游 IO helper 是否一并迁?

- `_load_concept_map_df`(`rps_rotation.py:58`,带 `_map_cache` / `_map_ts`,L110-111)与
  `load_risk_warning_symbols`(`market_mainline.py:76-99`,600 s 进程内 cache)都是 IO + cache,
  按 §5 段 S5 应留在 Python。**待定**:是否维持原样,还是顺带把 `build_rps_rotation`(R-2b,共享同一 helper)一起迁?
  捆绑会翻倍 parity 面,本文不建议捆绑。

### Q6 — 落位 crate 命名

- Path A-3/4/5 的惯例是 `crates/indicators/src/services_<domain>/`(Spec 0005 L88 明确不新建 workspace member)。
- 但 ADR-0001 决策 4 预留了 `crates/{factors,signals,shared}`(`crates/factors/src/lib.rs:9` 仍是空占位)。
- **待定**:主线分是"因子"还是"市场维度服务"?若判定为因子 → `crates/factors`;若判定为 service →
  `crates/indicators/src/services_mainline/`。两条路都不破坏 vendor 链路,但影响 Path A-7 的 crate 归属一致性。

### Q7 — 收益基线怎么测

- 本调研**未跑 benchmark**(任务约束:不启服务、不跑 live smoke)。`api/regime.py:291` 的"秒级"是代码注释,非实测。
- **待定**:T01 research 需要给出 Path A-6 之前/之后的同机对比(全量双 kind 重算),
  否则"2-5×"只是本文的估算,不能进 Spec 的验收标准。

### Q8 — 勘误要不要做第二次?

- 现状:Spec 0005 已在 commit `bad6c78` 勘误过一次(把 `mining_candidates._scan_*` 换成
  `repository._scan_daily_batch` / `matrix._scan_matrix_values`);ADR-0006 L265 / L360 **仍未勘误**。
- 本调研的复核结论(§6.4):**换上的这两个新候选也过不了 Path A-6 的门槛** ——
  一个是 16 LOC 的纯 lazy scan(0 处 Python 算术),一个是 numpy 路径。
- **待定**:
  - 选路线甲:再勘误一次,把候选名指向 `market_mainline.compute_mainline_range`(本文 §5.2 推荐),并同步修 ADR-0006 L265 / L360;
  - 还是选路线乙:把 ADR-0006 §"5 段"扩成 numpy interop seam(新 ADR),让 R-8 / R-9 变成可迁目标。
- **本调研不代改 Spec 0005 / ADR-0006**,只给证据与两条路线的后果。

---

## 附:本调研的可复现口径

- 函数 LOC / 模块 LOC / Polars token 计数 / call site 计数:Python `ast` 模块解析 `backend/**/*.py`,
  call site 按 `ast.Call` 的 `func.attr` / `func.id` 统计,`backend/tests/` 与 `backend/app/` 分列。
- 数据源 shape:`polars 1.44.2` 读 `D:/AI-Stock/Stock-Data/` 下的 parquet 实测
  (`kline_daily_enriched` 1696 分区 / 末分区 5567×15;`ext_gn_ths` 5581×5;`ext_hy_ths` 5575×5)。
- **未做**:性能 benchmark、live 数据 smoke、任何源码改动。本文件是本次调研产出的**唯一**新文件。
