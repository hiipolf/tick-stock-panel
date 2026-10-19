# ADR 0005 — `levels._extreme_levels` Rust 迁移 (Path A-2)

Date: 2026-10-19
Status: **Accepted** (sibling record; Path A-3+ service-level seam contract
covered by [ADR-0006](0006-service-level-rust-migration.md))
Tracking: Spec 0002 (now Implemented)
Supersedes: ADR-0001 / ADR-0004 的 "first real indicator" 表述 (从 1 个真实指标 → 2 个)
Implements: Spec 0002 §"Solution" / §"Implementation Decisions"

## 上下文 (Context)

`backend/app/indicators/levels.py::_extreme_levels(df)` 是关键前高/前低组合,出
60日新高/新低、250日新高/新低、近期 swing 高低点(收敛后 5 条)共 8 个左右的
水平 markLine,喂给 `LEVEL_TYPES['extreme']` consumer(前端
`AnalysisKChart.tsx:50` 的横向标记线 + AI 个股分析提示词的价位上下文)。

算法(纯 Python,line 285-340):
- 60/250 窗口极值:`df.tail(60)["high"].max()` / `df.tail(60)["low"].min()`
  - 两次 `max()` / `min()` 扫最后 60 / 250 bars,O(60 + 250) ops
- swing 检测:`for i in range(win, len(highs) - win): if highs[i] == max(highs[i - win:i + win + 1])`
  - **每根 bar 都新建长度为 `2*win+1` 的 slice**,然后调 Python builtin `max`
  - **N×(2*win+1) 次 slice allocation + max() 调用** = Python 解释器 wall
  - 240 行 × win=5 ≈ 2,640 slice 分配 + max() 调用,每次调用都 GC 一次

实测量级(2026-10-19):240 行 fixture 上 `_extreme_levels` 总耗时 0.101ms。绝
对值小,但 **per-bar slice allocation** + Python `max()` 的算法形态跟 Path A-1
`_support_resistance` 的 `for row in range(n)` decay loop 是同类 — 都是
"明明就是 O(N) 算术,但 Python 解释器是 wall"。

Path A-1 / Spec 0001 / ADR-0004 已证明 seam + try-import fallback + dual-track
test 模式可行。Path A-2 完全复用,retire 第二段 Python 循环。

## 决策 (Decision)

**Spec 0002 接受。把 `_extreme_levels` 的计算核心迁到 `crates/indicators/src/extreme_levels.rs`,
Python 端保留为 thin adapter 走 `try-import tf_rs_indicators → ImportError 退
化到 _py_extreme_levels` 模式(ADR-0002 §5 既有契约,本 ADR 不另立新策略)。**

### 三个阶段(对应 4 个 spec ticket)

| Ticket | 产出 | 关键 commit (工作流中,未推送) |
|--------|------|-------------------------------|
| 01 research | 选定 **Approach A**(`to_vec()` 一次 + Rust 循环) | `docs/research/extreme-levels-rust-api.md` |
| 02 kernel | `crates/indicators/src/extreme_levels.rs` 1:1 移植 + 3 个 Rust 单测 | TBD (Ticket 02 commit) |
| 03 adapter | 240-day fixture + 4 个 seam test + thin adapter | TBD (Ticket 03 commit) |

### 实现选择(引用研究笔记 §4)

- 不用 `polars` 表达式 / `rolling_max(center=true)` / `polars_compute::rolling::*`
  (Ticket 01 调研结论)
- inner loop 是 irreducible per-bar state machine(`(lo..hi).all(|j| j == i || x <= h)`
  算 tie-aware swing),Polars 表达无力化,朴素 Rust 循环即可
- `to_vec()` 一次性 Arrow→`Vec<Option<f64>>` 拷贝,后续纯 Rust 算术
- **单 pass 出所有 6 列**(60/250 × high/low + swing high/low boolean)— PyO3
  边界只付一次,跟 Path A-1 同 shape
- Float64 broadcast:`Float64Chunked::from_vec(name, vec![v; n])`,N 行同值
- Boolean per-bar:`BooleanChunked::new(name, vec!...)` N 行
- 60/250 reduce(emit 60日新高/60日新低/250日新高/250日新低)+ swing 聚合
  (±1% 合并 + 距当前价最近 2 个)抽到 `_reduce_extreme_levels` helper,Python
  ref 和 Rust 路径共享
- tie-aware:`(lo..hi).all(|j| j == i || highs[j] <= h)` 等价 Python `highs[i] == max(highs[i-win:i+win+1])`
  (所有命中 max 的 bar 都被标,边界包括 bar N-win 正确排除)

### 多窗口设计选择(Option A — 单 pass)

调研三种方案后选 **Option A(单 pass)**:
1. **Option A** (采用):单 `for i in 0..n` Rust loop,track `h_ext/l_ext` HashMap
   + per-bar swing series。一次 PyO3 边界,返回 N×6 DataFrame。`windows: Vec<u32>`
   参数允许未来扩展(`120`, `500` 等),不写死 60/250。
2. Option B(拒绝):两个独立函数 `rolling_extremes(df, windows) -> DataFrame {6列}` 
   + `swing_levels(df, win) -> DataFrame {2列}`,adapter 串两次。模块化更好
   但 2× PyO3 边界,且两个 pass 的 N 跟 swing 的 N 仍要串,没有真省。
3. Option C(拒绝):60/250 走 `polars_compute::rolling::no_nulls::rolling_max`
   (Ticket 01 §2 已论证:downcast → primitive_array → free fn → rewrap,3 层
   抽象,只为取一个标量,跟 swing scan 不共享 `Vec<Option<f64>>` cache)。

### 双轨测试(ADR-0002 §4)

- **Rust 单测** `crates/indicators/src/extreme_levels.rs::tests`:3 个 case
  - `main_path_with_ties` — 30 行手算 swing set(含 tie cluster @ 17/18 +
    boundary @ 25 excluded),精确 bit 验证三个 surface
  - `graceful_no_op_empty_df` — 空 df 返回 unchanged,无新列
  - `null_handling` — high 列中间一个 None,windowed extreme 用 `filter_map`
    跳过 null,仍 finite
- **Python seam** `backend/tests/test_rs_extreme_levels_seam.py`:4 个 case
  - 240-day parity bit-identical(规格 spec §"Seams" 最高门槛)
  - 强制 `ImportError` 走 `_py_extreme_levels` 路径
  - 1 个 graceful no-op trigger(empty df → `[]`)
  - dtype preservation + Boolean/Float64 broadcast 验证(NaN-aware)

## 后果 (Consequences)

### 正面

- **bit-identical parity**:240 天 fixture 上,新 kernel 输出 = 纯 Python
  reference 输出,`assert result == expected` 跑过。Spec §"Acceptance
  Checklist" "240-day fixture parity test" 项 = green。
- **vendor 链路不受影响**:kernel 不依赖 vendor 之外的 polars 特性,TF-RS-1 的
  5 个 patch site(`unique.rs` / `lp.rs` / `functions.rs` / `row_deletions.rs`
  / `lower_ir.rs`)跟 extreme_levels.rs 不交互。`polars_compute::rolling::*`
  也未实际使用。
- **chart 路径 0 改动**:唯一 caller `compute_levels` 的 `_extreme_levels(df)`
  公共签名不变,前端 `LEVEL_TYPES['extreme']` 消费路径 untouched。
- **try-import fallback 验证**:wheel 缺失时 CI / 裸机环境自动走
  `_py_extreme_levels`,测试 `test_python_fallback_when_wheel_missing` 强制
  mock `__import__` 验证 fallback 路径产物 == 240-day reference 产物。
- **真实数据 end-to-end 验证**:uvicorn 重启后 `/api/stock-analysis/levels
  ?symbol=000001.SZ&days=240` 返回 8 个 extreme dicts(60日/250日新高新低 4
  + 4 个 swing 前高前低),Rust kernel 把 pipeline 串到 chart 后端。
- **多窗口参数化**:`windows: Vec<u32>` 暴露成 PyO3 参数,未来加 120/500 日
  窗口不动 kernel 代码(只调 `extreme_levels(df, [60, 120, 250, 500], 5)`)。
- **复用 Path A-1 测试 scaffolding**:`test_rs_extreme_levels_seam.py` 跟
  `test_rs_levels_seam.py` 同 4-test pattern(skipif-fallback / 两组独立断言 /
  dtype preservation),新人看 Path A-2 commit 就能直接做 Path A-3。
- **12/12 全套 seam test 通过**:Path A-1 4 个 + Path A-2 4 个 + hello-world
  4 个,无 regression。

### 负面 / 成本

- **多一次 build step**:`maturin develop --release` ~4 分钟(release 模式;
  debug 模式更快但 wheel 实际运行会触发 abi3 检查)。**uvicorn 必须先 stop**
  才能重 maturin(`tf_rs_indicators.pyd` 被 import 时 locked;实测 2026-10-19)。
  解决方法:Stop-Process uvicorn PID → maturin develop --release → 重启
  uvicorn。per memory "PS 5.1 `Remove-Item` 拦截" 同源。
- **`_py_extreme_levels` 必须永久保留**:fallback 不是临时,机器没装
  wheel / CI 没装 Rust toolchain 都会走 fallback。Spec 写 "keep indefinitely"。
- **一处 spec drift**:`Spec 0002 §"Algorithm (Rust)" 第 5 个 bullet` 第一版
  写 "max(highs.tail(w)) if df.height >= w else NaN",kernel 实现正确(NaN
  when n < w),但实际 fixture(n=30 < 60)走 NaN 分支,文档说 "frozen extreme
  是 124.0" 是错的;Ticket 02 code-review 修了 research note 跟 test docstring
  (`extreme_60_high` 期望 NaN,`extreme_10_high` 期望 100.0)。
- **Float64 broadcast 不能用真 Polars 1-row scalar**:我们用 `Float64Chunked::from_vec(name, vec![v; n])`
  复制 N 次,每行是同一个值,Python 端 `out[col][0]` 或 `out[col][-1]` 都
  返回同样的值。Memory cost = 8 bytes × N(240 行 = 1.9 KB / 列,4 列 = 7.6 KB,
  完全可接受)。

### 回滚 (Rollback)

5 分钟内:
1. 删 `backend/app/indicators/levels.py` 里的 `_extreme_levels` thin
   adapter 和 `_reduce_extreme_levels` helper,恢复 `_extreme_levels` 旧
   版(可直接 `git show c16aef9:backend/app/indicators/levels.py` 取原
   285-340 行)。
2. 删 `crates/indicators/src/extreme_levels.rs` + `lib.rs` 里 `mod extreme_levels;`
   + `m.add_function(wrap_pyfunction!(extreme_levels::extreme_levels, m)?)?` 三行。
3. 不动 `tf_rs_indicators` wheel 安装(它仍能编,只是没东西调)。

回滚后,`compute_levels` 内的 `_extreme_levels(df)` 仍工作(签名没变),只是
per-bar slice 分配回 Python,0.1ms spike 回来。`sum_close` + `support_resistance`
不受影响(独立 module)。

## 退出标准 (Exit criteria)

- [x] `cargo test -p tf-rs-indicators -- extreme_levels` 3/3 pass
- [x] `cargo test --workspace` 全 green(6 tests in tf-rs-indicators,0 in shared/factors/signals)
- [x] `pytest backend/tests/test_rs_extreme_levels_seam.py -v` 4/4 pass(240-day parity bit-identical)
- [x] `pytest backend/tests/test_rs_levels_seam.py -v` 4/4 pass(Path A-1 无回归)
- [x] `pytest backend/tests/test_rs_indicators_seam.py -v` 4/4 pass(hello-world 无回归)
- [x] 12/12 全套 seam test 通过(4+4+4)
- [x] `maturin develop --release` 成功(uvicorn 必须先 stop;`tf_rs_indicators.pyd` 被 import 锁)
- [x] `tf_rs_indicators.extreme_levels` 从 Python 可见(`['extreme_levels', 'sum_close', 'support_resistance']`)
- [x] 1 个 caller(`compute_levels`)0 改动(签名不变)
- [x] 0 新增 Cargo 依赖,0 新增 feature flag,`Cargo.lock` 不变
- [x] ADR-0001 status bump `seam + first real indicator` → `seam + second real indicator`
- [x] ADR-0005(本 ADR)写完
- [x] Spec 0002 status 更新为 `Implemented`
- [x] 跨项目 memory 教训(已经在 Ticket 01/02 的 memory append 阶段写完)
- [x] 真实数据 e2e 验证(uvicorn + vite + browser 已在 `D:/AI-Stock/Stock-Data`
      数据上验证 Rust kernel 输出 8 个 extreme dicts)

## 交叉引用 (Cross-references)

- **Spec**: `docs/specs/0002-migrate-levels-extreme-levels.md` (Status: Implemented)
- **ADR 链**:
  - `docs/adr/0001-rust-kernel-path-a-scope.md` (选 Path A;本 ADR supersedes "first real indicator" 描述,bumps 到 "second real indicator")
  - `docs/adr/0002-rust-kernel-bindings.md` (§5 try-import fallback 契约;§4 dual-track testing)
  - `docs/adr/0003-polars-0552-vendoring.md` (TF-RS-1 vendor 链路;本 ADR 不与它冲突)
  - `docs/adr/0004-levels-migration.md` (Path A-1 同模式 seam-and-adapter,本 ADR 复用 + 扩展多窗口)
- **Research**: `docs/research/extreme-levels-rust-api.md` (Ticket 01 选 Approach A 的研究笔记,对比 Path A-1 的 `cut`/`qcut`/`search_sorted` 拒绝理由)
- **Context**: `CONTEXT.md` (Path A 决策与原则)
- **实现 commits** (worktree `rust-kernel-path-a` 分支,未推送):
  - (Ticket 02 / 03 commit 待 `git commit -m`)
- **Tag 链**:
  - `path-a-hello-world` (sum_close seam)
  - `path-a-vendor-0552` (vendor 化)
  - `path-a-spec-0001` (spec 写完)
  - `path-a-ticket01-research` (Approach A)
  - `path-a-ticket02-kernel` (kernel 落地)
  - `path-a-ticket02-fixes` (1:1 port 修复)
  - `path-a-ticket03-adapter` (adapter + 240-day parity)
  - `path-a-ticket03-fixes` (helper + test fixes)
  - `path-a-ticket04-closure` (ADR-0004 + Spec 0001 Implemented)
  - **(新)** `path-a2-spec-0002` (Spec 0002 写完)
  - **(新)** `path-a2-ticket01-research` (Approach A + cited excerpts)
  - **(新)** `path-a2-ticket02-kernel` (extreme_levels kernel + 3 unit tests)
  - **(新)** `path-a2-ticket03-adapter` (adapter + 240-day parity + 4 seam tests)
  - **(新)** `path-a2-ticket04-closure` (ADR-0005 + Spec 0002 Implemented)

## Reproducibility(给下一个 dev / 下一个 indicator 移植者的清单)

Per-machine rebuild on a fresh box(Windows / Linux / macOS,2026-10-19 起):

### 必备工具链

1. **Python 3.11+** — system 或 `uv python install 3.11`
2. **Rust 1.98.1** — `rustup` 装好后进仓库自动 activate(`rust-toolchain.toml` pin)
3. **C toolchain**:
   - Windows: Visual Studio Build Tools(MSVC `link.exe` + Windows SDK)
   - Linux: `build-essential`
   - macOS: Xcode Command Line Tools
4. **`uv`** + **`maturin`**:
   ```bash
   pip install uv
   uv sync --extra rs   # 装 maturin + 全部 Python deps(含 polars 1.44.2)
   ```

### 编译并装上 Rust kernel

第一次 ~4 分钟(release 模式 + vendored polars 冷编译),后续 ~30 秒增量:

```bash
cd crates/indicators
maturin develop --release
```

**重要**:运行 maturin 前必须先 stop 任何持有 `tf_rs_indicators.pyd` 的进程
(uvicorn 等),否则 `error: failed to remove file ...tf_rs_indicators.pyd: 拒绝访问`
(os error 5)。`Stop-Process -Id <uvicorn_pid> -Force` 即可。

### Smoke test

```bash
# 直接 import + 调 240-day fixture parity
pytest backend/tests/test_rs_extreme_levels_seam.py -v
# 跑全量 seam
pytest backend/tests/test_rs_levels_seam.py \
       backend/tests/test_rs_extreme_levels_seam.py \
       backend/tests/test_rs_indicators_seam.py -v
# Cargo 全测
cargo test --workspace
```

全 green = ready to merge。

## 进一步说明 (Further Notes)

- **Path A-3 候选**:`_gap_levels` + `_filter_unfilled`(240 行 0.068ms,有 O(N²)
  nested loop),`cards/signals` 的 BOLL / KDJ / MACD / RSI(待定)。本 ADR
  的 seam-and-adapter 模板 + 4-ticket 流程下次直接走,不再重新设计。
- **下一条 spec** 沿用 Spec 0002 §"Acceptance Checklist" 的格式(逐 ticket
  列出 commit / `cargo test` / bit-identical parity 验证 / 跨项目 memory),无
  须另起模板。
- **vendor patches interplay (TF-RS-1)**:kernel 是 pure-Rust loop on
  `Vec<Option<f64>>` from `to_vec()` + `BooleanChunked` / `Float64Chunked`
  construction + `PyDataFrame` round-trip。None of the patched 5 sites
  (`unique.rs`, `lp.rs`, `functions.rs`, `row_deletions.rs`, `lower_ir.rs`)
  are touched by the `extreme_levels` algorithm.
- **后续 indicator 作者** 可以直接 reuse Path A-1 / Path A-2 的 seam-and-adapter
  + 4-ticket 流程,无需重新 spec 一个 4 文件。Spec 0001 §"User Stories #13"
  + Spec 0002 §"User Stories #10" 已经把"1-day exercise"的目标定下,本 ADR
  的 Exit criteria 是该目标的实测证据。