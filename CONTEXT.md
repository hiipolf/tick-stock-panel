# CONTEXT — Vocabulary for Rust Kernel Path A

> 本文件是 `rust-kernel-path-a` 工作流的"词汇表"。
> 决策见 `docs/adr/`。本文件**不**记录实现细节,只记术语与含义。

## 身份

- **路径 A (Path A)** — 在不重写后端的前提下,用 PyO3 + `pyo3-polars` 把候选 Python 计算热区迁到 Rust 编译的 Python extension (`cdylib`) 的策略。区别于 Path B(E 子服务)、Path C(重写 sidecar)、Path E(整栈重写)。
- **路径 A 工作树 (worktree workspace)** — git worktree 目录 `tick-stock-panel-rust-kernel/`,绑定分支 `rust-kernel-path-a`。与 `main` 分支共享 `.git`,文件彼此隔离;`git merge rust-kernel-path-a` 可平滑合并回 `main`。
- **基线 commit** — `c16aef9c0873eb35786b14c006c3ce2383f4222`(锁定;后续若 `main` 上游推进,需手动 rebase/merge 后再开发)。

## 类型

- **kernel crate** — `crates/<name>/` 下的 Rust crate,通过 PyO3 注册为 Python extension module(命名 `tf_rs_<name>`)。首例 = `crates/indicators`(下一行)。
- **首攻 crate** — `crates/indicators`。对应 `backend/app/indicators/{levels,pipeline}.py`。**当前真实代码**未迁;hello-world 占位 `sum_close(df)` 待落。
- **预留 crate** — `factors` / `signals` / `shared`。后续阶段启用;目前 workspace member 占位,无代码。
- **`shared` crate** — PyO3 样板 + `pyo3-polars` 桥 helper + `tracing` 初始化 + Polars `prelude` 重导出。多 kernel crate 共享这些 boilerplate。

## 边界

- **seam** — Python 编排 ↔ Rust 计算 的协议边界。具体形态:`PyO3 #[pyfunction]`,签名为 `(PyAny) -> PyResult<PyAny>`,内含 `&PyDataFrame` ↔ `polars::frame::DataFrame` 的零拷贝往返。
- **首交付 (seam-only deliverable)** — 仅暴露一个假函数 `sum_close(df) -> df`,验证 `#[pymodule] fn tf_rs_indicators` 注册、`#[pyfunction]` 签名、跨边界 `PyDataFrame` 来回。**不**实现任何真实因子。
- **try-import fallback** — Python 端用 `try: from tf_rs_indicators import sum_close except ImportError: sum_close = None` 的退化策略。`uv sync` 默认装;`uv sync --extra rs` 显式装 wheel 时激活 Rust 路径;CI / 无 Rust 工具链环境自动回退到原 Polars 实现。
- **`extras = ["rs"]`** — `pyproject.toml` 中 `tf-rs-kernel` 的可选依赖组,标记 maturin wheel。生产/CI 默认不装;开发者/特定机器显式开。

## Service-level seam (Path A-3+)

> Spec 0001 / 0002 迁的指标函数(indicator-level)输入是单一 `polars.DataFrame`。
> Service-level 函数(从 Path A-3 开始)输入是**服务对象**(repo / quote_service / ExtConfigStore 等)
> + 多个数据源,Python adapter 必须做 pre-aggregate + IO, kernel 只做 pure-compute。
> 决策见 ADR-0006 / Spec 0003。本节只定义术语。

- **service-level function** — `backend/app/services/*.py` 下的纯函数(无 Request 依赖),如 `_dimension_rank`、`build_sector_rotation`。区别于 indicator-level:依赖 IO(读 parquet/yml)+ 多个数据源(dict-of-dict),不只单一 DataFrame。
- **service-level seam contract** — service-level 函数的 Rust 迁移协议。固定 5 段:
  1. **Pre-aggregate 由 Python adapter 负责**:adapter 调现有 Python 端的 IO helper(如 `ExtConfigStore.load_all()` + `_read_ext_rows()`)构造 pre-aggregate 中间态;
  2. **Rust kernel signature 是多参数**:`fn kernel(pre_aggregate: ..., scalars: ..., scalars: ...) -> DataFrame`,不是单一 df;
  3. **Kernel 只接受 structured pure-compute input**:所有 IO / cache 状态 / DB lookup 都在 Python 端完成, kernel 不直接读 filesystem;
  4. **Rust kernel 返回 DataFrame**:adapter 端 `df.iter_rows(named=True)` 转 `list[dict]`,跟 indicator 级 path (1) 同模式;
  5. **Cache 归属 Python**:任何 service-level cache(`_cache` / `_cache_ts` / TTL)由 Python 维护, kernel 不持有状态(per ADR-0002 §5 spirit)。
- **pre-aggregate input** — Python adapter 给 kernel 的结构化输入。形态是 list / dict / DataFrame 之一(按 kernel 需求),但所有 filesystem / DB / network 访问都在 pre-aggregate 阶段完成, kernel 不再碰 IO。
- **kernel 不能直接读 filesystem** — Rust kernel 的输入不允许包含 `Path` 或任何 IO 句柄。Adapter 必须把 filesystem 内容序列化进 pre-aggregate, kernel 只读已加载数据。
- **cache ownership = Python** — 任何 LRU / in-memory state 在 Python 模块级(`_cache: dict`, `_cache_ts: dict[tuple, float]`)。Kernel 调用不修改 cache, 也不读 cache。Adapter 在 kernel 调用前自检 cache, 命中则 skip kernel。
- **3-tuple output shape** — `list[dict]` 的每个 dict 含 str / float 混合类型。kernel 返回 DataFrame(列 = dict 的 key 集合, dtype = 各 key 的 Python type), adapter `iter_rows(named=True)` 转 list[dict]。
- **frozen parquet test fixture** — service-level seam test 不再像 indicator 级只用 240-day frozen parquet; 还需 frozen `data/data_sources/*.yaml` + frozen ext parquet 子集,构成完整 frozen `data_dir`。Adapter 通过 mock `repo.store.data_dir` 指向 frozen dir。

## 版本与工具链

- **Rust toolchain pin** — `rust-toolchain.toml` 锁 `1.98.1`(含 clippy / rustfmt)。轮转:Polars 1.44.x 已知依赖图要求 `rustc >= 1.95`,1.98.1 是当前本机可复现稳定版。
- **Polars 双版本对应** — PyPI 侧 `polars 1.44.2`(2026-09-09,1.44.0 yank,1.44.1 跳过),对应 Rust crate `polars 0.55.2`。Python 与 Rust 两侧版本必须锁齐,否则 ABI 不匹配。
- **PyO3 套件固定** — `pyo3 = "=0.29"` + `pyo3-polars = "=0.28.0"` + `pyo3-polars-derive = "=0.22.0"`。`pyo3` 启用 `["extension-module", "abi3-py310"]` → 单 wheel 覆盖 Python ≥3.10(TSP `requires-python >=3.11`,完全兼容)。

## 测试

- **双轨测试 (dual-track tests)** — Rust 单元测试(`#[cfg(test)] mod tests`)验证算法正确性;Python `pytest` 验证 PyO3 边界(`craby<->pyDataFrame`)。
- **4 步验收** — ① `cargo test --workspace` ② `maturin develop --release` ③ `uv run -m pytest python/tests/test_rs_indicators_seam.py` ④ `uv run python -c "import tf_rs_indicators"`。**四步全过** = "seam 接通"。

## 已知风险

- **2026-09 实证样本踩坑**(Hivebook wiki 引):Rust Polars 字符串处理不要调用 `apply_to_buffer`(不存在),用 `apply_into_string_amortized`;`is_elementwise` 标志不会自动启用 streaming / predicate pushdown,只切 batch 模式;`output_type` 必须显式声明。这些对**首交付无影响**(我们只 `sum_close`),真迁 `levels.py` 时再展开。
- **`chrono` pin** — Polars 工作空间钉 `<= 0.4.41`(避免 0.4.42+ breaking change);本 crate 不直接依赖 `chrono`,但 `polars-utils` 引入,版本由 Polars 自己控制。
- **Windows symlink caveat**(Polars 主仓 README 提示) — `cargo` 用 git for Windows 时 symbolic link 行为可能有差异。本 crate 不依赖 git submodule 的 symlink,影响有限。
- **Cargo.lock commit** — Polars deps 敏感,lock 入库保证 `maturin build --release` 出 wheel 复现。

## 缩写

| 缩写 | 含义 |
|---|---|
| Path A | 当前主推策略(Seam-based Rust kernel) |
| ABI | Application Binary Interface(`pyo3-polars` 跨语言对齐) |
| ABI3 | CPython Limited API;Python ≥3.10 单 wheel |
| PYO3 | Python↔Rust 桥框架 |
| seam | Python 编排与 Rust 计算的协议边界 |
| kernel crate | 通过 PyO3 暴露给 Python 的 Rust crate |

## 反向引用

- 决策 1-3(scope) → `docs/adr/0001-rust-kernel-path-a-scope.md`
- 决策 4-9(bindings / fallback / testing) → `docs/adr/0002-rust-kernel-bindings.md`
- Service-level seam contract 决策 → `docs/adr/0006-service-level-rust-migration.md`
- Service-level 第一个目标 (`_dimension_rank` + 2 helpers) 实施计划 → `docs/specs/0003-migrate-market-overview-dimension-rank.md`
