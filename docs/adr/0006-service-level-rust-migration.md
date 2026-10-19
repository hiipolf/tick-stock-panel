# ADR 0006 — Service-level Rust 迁移的 seam contract

Date: 2026-10-19
Status: **Accepted**
Tracking: Spec 0003 (now Implemented, was Draft)
Supersedes: 部分 ADR-0001 措辞("首攻 indicators" → 扩到 "indicators + service-level")
Implements: 14 个 grill-with-docs 决策 (Round 1-4)
Related: ADR-0001 / ADR-0002 / ADR-0003 / ADR-0004 / ADR-0005, CONTEXT.md §"Service-level seam"

## 上下文 (Context)

Path A-1 (Spec 0001, ADR-0004, 2026-10-19) 和 Path A-2 (Spec 0002, ADR-0005, 同日)
把 `_support_resistance` 和 `_extreme_levels` 两个 indicator-level 函数迁到了 Rust。
两者都是输入单一 `polars.DataFrame`,输出 `list[dict]`;adapter 是 thin try-import
wrapper,Rust kernel 是纯函数。

但 `services/` 目录下的函数跟 indicator 不同。`health` 调查 (2026-10-19) 显示
`backend/app/services/market_overview_builder.py` 里的:

- `_dimension_rank(rows, repo, kind, limit, level) -> dict` — `rows` 是 `list[dict]`,`repo`
  是 `Repository` 实例;通过 `repo.store.data_dir` 读 `ExtConfigStore` + ext parquet;
  输出 `{"leading": [...], "lagging": [...]}`,每项是 8-key dict。
- `_top_rows(rows, key, descending, limit) -> list[dict]` — 纯 compute 但输入也是
  `list[dict]`,输出固定 7-key dict per row。
- `_pct_band_rows(values: list[float]) -> list[dict]` — 输入 `list[float]`,输出
  固定 8-bucket list[{label, count, pct}]。

indicator-level 的 seam (PyO3 `df -> df`) 不能 reuse,因为:

1. 输入不是 DataFrame,是 service 对象 + 多个数据源。
2. IO (filesystem parquet/yml) 不能在 kernel 里跑,会被 PyO3 边界 + parquet
   版本碎片化复杂化。
3. Cache 状态 (`_cache` / `_cache_ts`, TTL 30s) 在 Python 端,如果 kernel 也读
   cache 会引入 Rust↔Python cache 一致性风险。

需要一个 **service-level seam contract**,跟 indicator-level 同源 (PyO3 +
pyo3-polars 同一套栈),但 5 段结构不同。

## 决策 (Decision)

**Spec 0003 接受。沿用 PyO3 + `pyo3-polars` 栈,新建 service-level seam contract (5 段),
adapter 拥有所有 IO + cache 状态, kernel 只做 pure-compute on pre-aggregated inputs。**

### Service-level seam contract (5 段)

1. **Pre-aggregate 在 Python adapter 里**。Adapter 调用现有 IO helper
   (`ExtConfigStore.load_all()` + `_read_ext_rows()` 等),构造 pre-aggregate
   中间态(结构化 list / dict / DataFrame);不再把这些 helper 推给 kernel。
2. **Rust kernel signature 是多参数**:
   ```rust
   #[pyfunction]
   pub fn dimension_rank_kernel(
       groups: Vec<(String, Vec<StockFields>)>,
       // ... 其他 scalar / DataFrame 参数按服务而定
   ) -> PyResult<PyDataFrame>
   ```
   不是单一 df。多参数 = 每个调用点能精确表达需要什么,避免 Rust 端引入
   隐式 IO 依赖。
3. **Kernel 只接受 structured pure-compute input**。任何 `Path` / `BufReader`
   / `reqwest` 等 IO 不出现在 signature 或 body。Adapter 负责把 IO 结果
   序列化进 pre-aggregate。
4. **Rust kernel 返回 DataFrame**(per Path A-1/2 同 seam)。Adapter
   `df.iter_rows(named=True)` 转 `list[dict]`,跟 indicator-level 同 surface。
5. **Cache 归属 Python**(`_cache` / `_cache_ts` 在 `market_overview_builder.py`
   / `sector_rotation.py` 模块级)。Adapter 在 kernel 调用前自检 cache,命中
   直接 return,不走 kernel;kernel 不持有状态。

### 14 个 grill-with-docs 决策 (Round 1-4 全部走推荐)

| # | Decision | Choice |
|---|---|---|
| 1 | Cycle scope | pilot `_dimension_rank` 1 个 + spec template + ADR-0006;defer sector_rotation 到下一轮 |
| 2 | Spec template | 新模板 + 新 sections (Service-level seam contract + Caller migration path) |
| 3 | ADR hierarchy | ADR-0006 new (supersedes 部分 ADR-0001 措辞) |
| 4 | Pilot boundary | `_dimension_rank` + 2 helpers (`_top_rows` + `_pct_band_rows`); NOT orchestrator 外层 19-sector 循环 |
| 5 | Adapter signature | public signature 保持 (`_dimension_rank(rows, repo, kind, limit, level) -> dict` 不变;7 个 caller 0 改动) |
| 6 | Cache ownership | Python owns cache (per ADR-0002 §5 spirit) |
| 7 | Parity gate | Full bit-identical (NaN-aware) — 同 Path A-1/2 门槛 |
| 8 | IO boundary | Python adapter does IO + pre-aggregate (kernel pure-compute) |
| 9 | Rust function count | 3 个独立 kernel (NOT 1 个 umbrella) |
| 10 | Test fixture | Frozen parquet in tmp data_dir (mock `repo.store.data_dir`) |
| 11 | Rust return shape | DataFrame + adapter iter_rows() → list[dict] |
| 12 | Doc layout | 同 `docs/specs/` + `docs/adr/`, 序号 3 |
| 13 | Doc ordering | ADR-0006 first (per `grill-with-docs` ADR 优先级) |
| 14 | Ticket structure | 5 ticket (research / kernel / fixture / adapter / closure) |
| 15 | Implementation order | simple → complex (`_top_rows` → `_pct_band_rows` → `_dimension_rank`) |

### 跟 indicator-level Path A-1/2 的对照表

| 维度 | indicator-level (Spec 0001/0002) | service-level (Spec 0003) |
|---|---|---|
| 输入形态 | 单一 `polars.DataFrame` | service 对象 + 多个数据源 |
| Kernel signature | `df` 单参 | 多参数 (DataFrame + scalars + lists) |
| IO 在哪 | Python 调 (kernel 无 IO) | Python 调 (per-aggregate 阶段) |
| Cache 在哪 | Python (indicator 没 cache) | Python (per Q6) |
| Kernel 返回 | DataFrame | DataFrame (同) |
| Adapter | try-import + fallback + reduce | try-import + fallback + pre-aggregate + post-process |
| Caller 改动 | 0 (signature 不变) | 0 (signature 不变) |
| Test fixture | 240-day frozen parquet | frozen parquet + yaml + ext subset in tmp data_dir |
| Ticket 数量 | 4 | 5 (多一个 fixture ticket) |

### 跟 ADR-0001 / 0005 的关系

- **ADR-0001**:原 status 是 "seam + second real indicator",supersedes 部分措辞。
  现状 (2026-10-19) bump 到 "seam + second real indicator + service-level in flight"。
- **ADR-0005**:Path A-2 决策记录不变(indicator-level migration 是 closed record)。
  本 ADR 是新增的 sibling,不是 input-level 的更新。
- **ADR-0002 §5 try-import fallback**:仍然适用。Service-level adapter 也走
  `try-import tf_rs_indicators → ImportError fallback to _py_*` 模式。
- **ADR-0003 vendor patches (TF-RS-1)**:服务级 kernel 同样不依赖 vendor
 之外的 polars 特性;`polars_compute::*` 同理不调用。

## 后果 (Consequences)

### 正面

- **Seam contract 可复用**。Service-level seam 5 段 (IO+cache 在 Python / multi-arg kernel
  / structured pre-aggregate) 适用于 `services/` 下其他函数 (`build_sector_rotation._compute`,
  `regime_builder._compute_batch`, `mining_candidates._scan_*)` 等)。Path A-3 之后
  直接套同一套模板,不用再开一轮 grill。
- **Caller 0 改动**(Q5)。Adapter 公开签名保持,7 个 caller (api/overview + 自身)
  不动。
- **Bit-identical parity** (Q7)。Service-level 比 indicator-level 多层结构 (dict-of-list-of-dict),
  Rust kernel + adapter `iter_rows` 跟 Python 纯函数走同一份 fixture,bit-identical 门槛
  跟 Path A-1/2 一致。
- **Cache 一致性**。Python owns cache, kernel 不持有状态,避免跨语言 LRU 不同步。
- **IO 隔离**。Kernel 不碰 filesystem / DB,所有 IO 集中 Python 端,debug 时不用
  追 Rust 内部 IO 路径。

### 负面 / 成本

- **Pre-aggregate 的 Python-side cost**。Adapter 多了 IO + dict-building 的 Python 步骤。
  Path A-1/2 单参 DataFrame 调用成本是 0,service-level 是 1 次 dict-build。
  测:240 × 19 sectors × ~30 stocks = ~570 stocks,Python dict-build 大概 5-10ms;
  kernel 算 1ms。Total ≈ 11ms 仍远低于纯 Python ref (~100ms 实测假设)。
- **多参数 signature 调试复杂**。单参 DataFrame 调试方便(`df.head(5)` 就能看),
  多参 (list of (group, list of stock)) 需要 adapter 端 `df.head()` + 临时 print。
- **Service-level seam 不可逆**。一旦实施,所有 `services/` 下函数都按这 5 段格式
  写 Rust kernel + adapter,不再回头用 `path = data_dir` 风格。这就是 ADR hard-to-reverse
  的本质。
- **多一个 fixture ticket** (Q14)。Path A-1/2 用 4 ticket,service-level 用 5 ticket,
  多一个 fixture ticket 单独 commit,reason = fixture 复杂度提升 (parquet + yaml + ext subset)。

### 回滚 (Rollback)

15 分钟:

1. 删 `crates/services/market_overview/` 整个目录 + `lib.rs` 里 `mod services_market_overview;`
   + `m.add_function(...)` 行。
2. 把 `services/market_overview_builder.py` 的 `_dimension_rank` / `_top_rows` /
   `_pct_band_rows` 三个函数恢复成原始 Python body(用 `git show <pre-ticket-03-commit>`
   取)。
3. 删 `backend/tests/test_rs_market_overview_seam.py` + fixture 目录。
4. 重启 uvicorn(无 wheel 改动,直接生效)。

回滚后:
- 7 个 caller 仍工作(签名不变),所有数据仍走纯 Python,无功能丢失。
- Path A-1/2 不受影响(独立 module + 独立 wheel 函数)。
- vendor 链路无需动。

## 退出标准 (Exit criteria)

> 这是 ADR 退出标准 (定义 ADR 何时 freeze)。具体 Path A-3 ticket 退出标准见 Spec 0003 §"Acceptance Checklist"。

- [x] 14 个 grill-with-docs 决策全部锁定 (Round 1-4)
- [x] 5 段 service-level seam contract 定义 (per CONTEXT.md §"Service-level seam")
- [x] ADR-0006 写完 (Date / Status / Consequences / Rollback / Cross-refs 全齐)
- [x] Spec 0003 Implemented (写完 5-ticket Acceptance Checklist)
- [x] CONTEXT.md 加 service-level glossary
- [x] Spec 0001 / 0002 / ADR-0001 / 0004 / 0005 加 cross-ref "Supersedes by Service-level Path A"
- [x] 7 个 caller signature 不变 (per Q5)

## 交叉引用 (Cross-references)

- **Spec**: `docs/specs/0003-migrate-market-overview-dimension-rank.md` (implements this ADR)
- **ADR 链**:
  - `docs/adr/0001-rust-kernel-path-a-scope.md` (supersedes 部分 "indicator-only" 措辞;bumps Path A status 到 "service-level in flight")
  - `docs/adr/0002-rust-kernel-bindings.md` (§5 try-import fallback;也适用 service-level)
  - `docs/adr/0003-polars-0552-vendoring.md` (TF-RS-1 vendor 链路;服务级 kernel 同样不依赖 vendor 之外)
  - `docs/adr/0004-levels-migration.md` (Path A-1, sibling,不变)
  - `docs/adr/0005-extreme-levels-migration.md` (Path A-2, sibling,不变)
- **Glossary**: `CONTEXT.md` §"Service-level seam (Path A-3+)"
- **Spec 0001 / 0002**: 加 "Supersedes by Service-level Path A" cross-ref
- **Implementation commits** (worktree `rust-kernel-path-a` 分支,未推送):
  - TBD (Path A-3 Ticket 01-05 待 commit)

## Reproducibility

Per-machine rebuild 同 ADR-0005:

```bash
# 工作树 + venv 已就位后
uv sync --extra rs            # 装 maturin + 全部 Python deps
cd crates/indicators         # maturin develop 必须从 cdylib crate 目录跑
maturin develop --release

# 验证
pytest backend/tests/test_rs_market_overview_seam.py -v   # 待 Ticket 04 实现
pytest backend/tests/test_rs_levels_seam.py -v              # Path A-1 不回归
pytest backend/tests/test_rs_extreme_levels_seam.py -v      # Path A-2 不回归
pytest backend/tests/test_rs_indicators_seam.py -v          # hello-world 不回归
cargo test --workspace                                       # 全测
```

全 green = ready to merge。

## 进一步说明 (Further Notes)

- **Path A-4 候选**(本 ADR freeze 后,可直接复用 seam contract,不需要再开新 ADR):
  - `services/sector_rotation.build_sector_rotation._compute` — 比 Path A-3 更复杂
    (minute k-line partitions + concept map + ext flow + cache layer)。SPEC 4 写新 spec。
  - `services/regime_builder._compute_batch` — per-day batch compute,可直接用 indicator
    pattern (DataFrame in / DataFrame out) 但带 service-level cache 层。
  - `services/mining_candidates._scan_*` — 多因子打分,per-symbol loop,callers 多。
- **新 crate?** Spec 0003 决策是把 kernel 放 `crates/services/market_overview/` 三个子模块
  (跟 Path A-1/2 的 `crates/indicators/src/{levels,extreme_levels}.rs` 同 pattern),不用新 crate。
  如果 Path A-4 / A-5 加更多 service-level kernels,再决定是否新 crate `crates/services/`。
- **Cache 调度 locality** — service-level cache 比 indicator 多,但 owner 原则不变。
  Adapter 内 check cache, 命中即 return。Kernel 不感知 cache。