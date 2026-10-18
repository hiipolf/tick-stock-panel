# ADR 0003 — Polars 0.55.2 上游漏洞 vendor 化

Date: 2026-10-18
Status: **Accepted**
Tracking: **TF-RS-1**
Supersedes: 部分覆盖 ADR-0002 §8 升级路径中"polars 0.55.3+ 修好 match 完整性可删 patch"的预案

## 上下文 (Context)

`cargo check --workspace` 在 `polars 0.55.2` 上反复触发 5 处 `error[E0004]: non-exhaustive patterns`,全部来自同一个根因:**上层 `polars_core` 的 `dtype-full` 默认开启 → `object` 变体存在;`polars` 的 `python` 默认未开启(我们用 `pyo3-polars` 替代)→ `PythonScan`/`Delta` 变体却仍然存在**(被 PyO3 类型转换显式构造)。结果,`polars-ops` / `polars-mem-engine` / `polars-stream` 三个 crate 的 `match` 表达式里,被 `#[cfg(feature = "python"|"object")]` 门控的 arm 在我们 build 时是 OFF 的,留下未匹配变体。

最初用 cargo cache 源码直改兜底(`unique.rs:163` 等 5 处加 `_ => unreachable!()`),但这违反 durability 原则:
- `cargo clean` 之后丢
- 同事 clone 仓库时无法复现
- 任何`cargo update` 都会重写 cargo cache
- 不可审计,不可 revert

候选策略:

| 方案 | 描述 | 接受度 |
|---|---|---|
| A. 改 cargo cache(短期) | 已采用,作为桥接手段 | 临时,不可持续 |
| B. 改 workspace 用 `polars=0.55.1` | Hivebook 实证可用,但同 bug 系列 | 不可信(同 patch 系列) |
| C. vendor polars 0.55.2 + `[patch.crates-io]` | 把 3 个有 bug 的 crate 复制到 `vendor/`,workspace 直接引 path 源 | ✅ 选 |
| D. 上游 PR | 等 polars 0.55.3+ 修 | 时机不可控,我们不能等 |

## 决策 (Decision)

**采用 C:把 3 个有 bug 的 crate 复制到 `vendor/`,在 workspace `Cargo.toml` 加 `[patch.crates-io]`。**

### 1. vendor 范围(3 crates)

| crate | version | files | size | patch 文件:行 |
|---|---|---|---|---|
| `polars-ops` | 0.55.2 | 134 | 951 KB | `src/series/ops/unique.rs:163` (catch-all `_ =>`) |
| `polars-mem-engine` | 0.55.2 | 38 | 251 KB | `src/planner/lp.rs:386`、`src/scan_predicate/functions.rs:528` (`#[cfg(not(python))] PythonScan/Delta`) |
| `polars-stream` | 0.55.2 | 181 | 1652 KB | `src/nodes/io_sources/multi_scan/components/row_deletions.rs:99`、`src/physical_plan/lower_ir.rs:982` (PythonScan)、`:819` (PythonDataset) |

合计 **353 文件 / ~2.85 MB**(压缩后会更小,git LFS 不需要)。

### 2. workspace `[patch.crates-io]`

```toml
[patch.crates-io]
polars-ops        = { path = "vendor/polars-ops" }
polars-mem-engine = { path = "vendor/polars-mem-engine" }
polars-stream     = { path = "vendor/polars-stream" }
```

cargo build 解析时优先 path 源,完全跳过 crates.io 上同版本。

### 3. cargo cache 旧 patch 处置

**保留不删**。理由:
- 删除 cargo cache 中的旧 patch 会让首次 `cargo check` 重新失败(若 vendor 配错)
- 真正能删的时机:Polars 0.55.3+ 修复后,删 `vendor/` + 删 `[patch.crates-io]` 段,再 cargo check 仍然通过 → 此时再清理 cargo cache
- 当前 cargo cache 仍带 `TF-RS-1` 标记的 patch,与 `vendor/` 内的 patch **等价**;两者任一单独存在,cargo build 都通过;两者同时存在也通过(以 path 优先)

### 4. ADR-0002 的"升级路径"细化

ADR-0002 §8 写"Polars 1.45/1.46 上游发布时跑 stress + 联动 bump + 新 ADR"。现在补一个 **vendor 路径下**的升级步骤:

1. 跑 `scripts/stress_polars_concurrency.py` 验证 0.55.3 实际可用
2. bump `Cargo.toml` 的 `polars = "=0.55.3"` 锁
3. `cargo update -p polars -p polars-ops -p polars-mem-engine -p polars-stream` 触发 cargo 重新解 4 个 crate
4. `cargo check --workspace` — 若通过,说明 0.55.3 已修 E0004,删 `vendor/` + 删 `[patch.crates-io]` 段
5. 若仍失败,看 stderr;只在该 bug 已修的 crate 上删 `vendor/<x>/` + 移除对应 patch 行,其他保留
6. ADR-0003 status 改 `Superseded`;新 ADR-0004 记录 bump 步骤

## 后果 (Consequences)

**正向**:
- **durable**:`vendor/` 入 git,任何 clone 都自动生效,不依赖 cargo cache
- **可审计**:5 个 patch 都有 `TF-RS-1` 标记和说明注释,git blame 跟得到
- **可 revert**:删 `vendor/` + 删 `[patch.crates-io]` 段 → 还原到未 vendor 状态
- **vendor 包大小可控**:3 个 crate 加起来 2.85 MB,不是巨型子树
- **`cargo clean` 安全**:`cargo clean` 清 target/ 不动 vendor/;重启 box 也不影响

**负向**:
- **上游修了我们不知道**:vendor 不会自动跟上上游,需要监控 polars 0.55.3+ changelog 或订阅 GitHub
- **vendor 同步成本**:未来若 `cargo update` 拉到 polars 0.55.3,我们的 vendor 还停在 0.55.2,需要手动同步
- **dev 摩擦**:新增 kernel crate 时,如果它依赖某个被 vendor 的 polars 子 crate,要 `Cargo.toml` 显式声明 `polars-ops.workspace = true`(workspace 看到 path 源)
- **重新 vendor 工作量**:上游 polars-ops 有大改时,需重新拷贝 + 重新打 patch。脚本化(见下)能 5 min 完成

**回退成本**:低。`rm -rf vendor/ && cargo check` 验证还原,5 分钟。

## 重新 vendor 流程(可脚本化)

```powershell
# scripts/revendor-polars.ps1  (建议加进 scripts/,本次未做)
$regBase = 'C:\Users\admin\.cargo\registry\src\index.crates.io-1949cf8c6b5b557f'
$wt = 'D:\AI-Data\路线分析\tmp_cmp\tick-stock-panel-rust-kernel'
$vendor = Join-Path $wt 'vendor'

# 1. rm -rf vendor/{polars-ops,polars-mem-engine,polars-stream}
# 2. 拷贝 cargo registry 新版本
Copy-Item "$regBase/polars-ops-0.55.2" "$vendor/polars-ops" -Recurse
Copy-Item "$regBase/polars-mem-engine-0.55.2" "$vendor/polars-mem-engine" -Recurse
Copy-Item "$regBase/polars-stream-0.55.2" "$vendor/polars-stream" -Recurse
# 3. 删 .cargo-checksum.json (mavis-trash,避免 sandbox 拦截)
# 4. 重打 TF-RS-1 patches (脚本对比 + sed/patch)
# 5. cargo check --workspace (若失败,看新 bug 出现位置,扩展 patch)
# 6. maturin develop --release + pytest 端到端
```

## 退出条件

- `polars-ops 0.55.3+` 修复 `unique.rs:160-161` 的 `Object` arm 完整性
- `polars-mem-engine 0.55.3+` 修复 `lp.rs:374` + `functions.rs:509` 的 `PythonScan`/`Delta` arm 完整性
- `polars-stream 0.55.3+` 修复 `row_deletions.rs:57` + `lower_ir.rs:194` + `:751` 同款

任何一个修了,删对应 `vendor/<x>/` + 移除对应 patch 行 + 在本 ADR 改 `Superseded`。

## 关联

- ADR-0001(scope,worktree / 首攻 indicators / seam-only)
- ADR-0002(bindings / polars 0.55.2 / pyo3 0.29 / pyo3-polars 0.28.0)
- 上游 polars 仓库:<https://github.com/pola-rs/polars>(待 0.55.3+ release 跟踪)
- TF-RS-1 跟踪:本 ADR 涵盖,无外部跟踪链接
