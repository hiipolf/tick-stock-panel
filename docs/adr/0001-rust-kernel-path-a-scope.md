# ADR 0001 — Rust Kernel Path A: Scope

Date: 2026-10-18 (initial)
Updated: 2026-10-19 (status bump after Spec 0001 + Spec 0002 + ADR-0006)
Updated: 2026-10-19 (status bump to **`Service-level seam contract landed`** after Spec 0003 + ADR-0006 close)
Updated: 2026-10-19 (Path A-4 spec freeze: multi-kernel service-level seam contract spec'd in Spec 0004, **implementation in flight**)
Updated: 2026-10-19 (Path A-4 closure: multi-kernel service-level seam implemented, end-to-end verified — `tf_rs_indicators` reaches **14 functions**; 5 Path A-4 ticket commits + 5 milestone tags; ADR-0006 §"5 段"§1+§3 in-place amended for IO helper `PathBuf` accept)
Updated: 2026-10-19 (Path A-5 spec freeze: multi-batch daily-grain service-level seam spec'd in Spec 0005, **implementation in flight**)
Status: **Accepted (indicator-level + service-level seam contracts landed; multi-kernel service-level seam implementation landed in Spec 0004; multi-batch daily-grain service-level seam spec'd in Spec 0005, pending implementation)**

> **Changelog 2026-10-19**: status 由 `seam-only` 升到 `seam + first real
> indicator`(Spec 0001 / ADR-0004),再升到 `seam + second real indicator`
> (Spec 0002 / ADR-0005),再扩到 `seam + second real indicator + service-level
> in flight`(Spec 0003 / ADR-0006)。
>
> **`levels._support_resistance`** (Spec 0001): production kernel 在
> `crates/indicators/src/levels.rs`,thin adapter 在
> `backend/app/indicators/levels.py::_support_resistance`,240-day
> fixture parity bit-identical。详见 [ADR-0004](0004-levels-migration.md)
> + 8 个 milestone tag(`path-a-hello-world` → `path-a-ticket04-closure`)。
>
> **`levels._extreme_levels`** (Spec 0002): production kernel 在
> `crates/indicators/src/extreme_levels.rs`,thin adapter 在
> `backend/app/indicators/levels.py::_extreme_levels`,240-day
> fixture parity bit-identical + multi-window `[60, 250]` 参数化。详见
> [ADR-0005](0005-extreme-levels-migration.md)+ 5 个新 milestone tag
> (`path-a2-spec-0002` → `path-a2-ticket04-closure`)。
>
> **`services.market_overview_builder._dimension_rank` + 2 helpers** (Spec 0003 /
> ADR-0006): service-level Path A pilot — 多参数 kernel signature,Python owns
> IO + cache, 5-segment service-level seam contract。Spec 写完且 Implemented
> (seam contract freeze 在 ADR-0006,glossary 锁在 CONTEXT.md
> §"Service-level seam (Path A-3+)")。5 个 Path A-3 milestone tag
> (`path-a3-spec-0003` → `path-a3-ticket04-adapter`)已落地;
> `path-a3-docs-closure` 在 Ticket 05 commit。
>
> **`sector_rotation.build_sector_rotation` + 5 helpers** (Spec 0004 /
> ADR-0006 §"5 段"§1+§3 amendment in-place): Path A-4 first **multi-kernel**
> service-level migration pilot — 一 public Python function + 5 helpers 拆为
> 8 Rust kernels (4 IO helper + per_bucket_reduce + timeline + cross_events
> + rank_for_mode), 1 thin adapter 编排 10-step orchestration (with full
> algorithm path delegated to `_py_compute` per T04 simplification flag;
> short-circuit path is byte-equal in both implementations), 缓存下沉在
> adapter。Seam contract 5 段仍适用;ADR-0006 §"5 段"§1+§3 in-place amended
> (IO kernel 接受 `PathBuf`,`polars::prelude::PlParquetReader::new(pathbuf).finish()?`
> 是 polars crate 内部 IO, kernel 仍然是 pure-compute on resulting DataFrame)。
> Spec freeze 在 commit `path-a4-spec-0004` (2026-10-19);implementation
> 走完 T01-T05 at commits `93c4867` → `d74f915` → `c3e6794` → `1323386` →
> T05 closure。Glossary 在 CONTEXT.md §"Multi-kernel service-level seam
> (Path A-4+)"。6 个 Path A-4 milestone tag 全部落地 (`path-a4-spec-0004` →
> `path-a4-ticket01-research` → `path-a4-ticket02-kernel` →
> `path-a4-ticket03-fixture` → `path-a4-ticket04-adapter` →
> `path-a4-docs-closure`)。24/24 Path A-1/2/3/4 seam tests pass (4 + 4 +
> 4 + 7 + 5);38/38 cargo tests (16 Path A-1/2/3 + 22 Path A-4);uvicorn
> `/api/sector-rotation` E2E gated on live minute data (MinuteRefreshService
> hasn't populated this worktree; real-data verification deferred to runtime
> when minute partitions appear)。
>
> 累计 **19/19** seam tests pass(4 hello-world + 4 Path A-1 + 4 Path A-2 + 7
> Path A-3);16/16 cargo tests pass(`tf_rs_indicators` 内)。uvicorn 在真实
> `D:/AI-Stock/Stock-Data` 数据上验证:Rust kernel 输出 8 个 extreme dicts
> (60日/250日 高低点 + 4 个 swing 前高前低)+ 3 个 sr dicts + service-level
> `industry_rank` 5+5 / `concept_rank` 5+5 / `top_gainers` 8 / `top_losers` 8 /
> `distribution` 8 — 全部经由 thin adapter,0 caller 改动。
>
> 本 ADR 的原 "seam-only" 措辞被 ADR-0004 / ADR-0005 / ADR-0006 覆盖,但 seam
> 仍是最优先的 invariant(任何 kernel 必须先通过 seam 验证再谈业务逻辑)。
> service-level seam 是 indicator-level seam 的超集:同样服务 IO + cache 在 Python,
> kernel pure-compute;不同处是 multi-arg kernel signature + pre-aggregate
> 阶段(per ADR-0006 §"5 段")。

## 上下文 (Context)

TSP 后端基于 Python 3.11 + Polars 1.44,部分计算热区(支撑位、因子矩阵、信号合并)纯 Python 实现效率受限。仓库锁定 `polars>=1.44,<1.45` 的双大版本(因流式引擎并发回归),引入 Rust 加速需遵守这一约束。

候选策略如下(详见 `docs/PATH-SURVEY.md` 待补):

| 路径 | 描述 | 工作量 | 收益 | 风险 |
|---|---|---|---|---|
| **A** | PyO3 extension + `pyo3-polars`(Seam-based) | 中 | 高 | 低 |
| B | Rust Axum 子服务 | 中高 | 中 | 中 |
| C | Rust 重写整个 sidecar 入口 | 高 | 中 | 高(破坏 vendor 链路) |
| D | Rust CLI 工具集(subprocess) | 低 | 中 | 低 |
| E | 全栈 Axum + polars + duckdb-rs | 极高 | 高 | 极高 |

用户偏好:个人/小团队自用工具,**不发布到 public**(`TickFlow Desktop 不对外发布` 文档原则亦适用于本仓库的二开侧)。决策首要考虑开发成本可控、不破坏现有 vendor 升级链路、可灰度发布。

## 决策 (Decision)

**采用 Path A**,分阶段推进:

1. **形态**:git worktree + 独立 branch `rust-kernel-path-a`,工作目录 `tick-stock-panel-rust-kernel/`。基线 commit = `c16aef9`。worktree 与 `main` 共享 `.git`,文件彼此隔离。
2. **首攻 crate**:`crates/indicators`,对应 `backend/app/indicators/{levels,pipeline}.py`(支撑位、均线族、技术指标)。**未在首交付内迁任何真实计算** —— 接受事实:这部分迁移是后续阶段。
3. **首交付 (seam-only)**:仅落 hello-world `sum_close(df) -> df` 占位函数,验证 `#[pymodule]` 注册、`#[pyfunction]` 签名、`&PyDataFrame` 零拷贝往返。**RICE 评估理由**:首交付目的是**锁住协议边界 (lock the seam)**,不是落地业务逻辑。锁住之后任何业务逻辑迁移都在协议内展开。
4. **预留**:`crates/{factors,signals,shared}` 作为 workspace member 占位,首版无代码。
5. **不引入**:
   - 不开 Axum 子服务(Path B)
   - 不动 PyInstaller sidecar(Path C)
   - 不重写后端整栈(Path E)

## 后果 (Consequences)

**正向**:
- Polars/Polars 是 Polars 的本家实现,Python 侧与 Rust 侧是同一作者同一引擎,版本对齐长期可控
- `pyo3` + `abi3-py310` 单 wheel 覆盖 Python ≥3.10,大幅减少 wheel matrix 复杂度
- 工作树隔离让 cargo build 不污染 vendor 的 uv 同步,两个工具链分别工作
- Hello-world 性质使得失败时定位快(< 1 步 debug);失败成本小(60-100 行 Rust 而已)

**负向**:
- 投资曲线:`pyo3` + `polars` + `pyo3-polars` 编译首次 5-10 分钟,后续靠 cargo cache 加速但 CI 仍需 1-2 分钟
- 二开门槛:自定义信号要插入 Rust 路径需要 Rust 工具链,Python-only 二次开发作者无法贡献计算加速
- 真正的收益(`levels.py` 内核迁)要第二轮 PR 才出,RFC 在 ADR 范围内不算"完成"
- `python/tests/` 是新位置,需要与原 `backend/tests/`(如有)约定避免重复

**回退成本**:中等。ADR-0001 落定后回退意味着:
- 删除 worktree
- git 删除 `rust-kernel-path-a` 分支
- 不影响 `main`(`Cargo.toml` / `crates/` / `python/tests/` 等只在新分支存在)

回退时间:5 分钟(纯 git 操作)。

## 关联

- `CONTEXT.md` 词汇表
- `0002-rust-kernel-bindings.md`(技术绑定)
