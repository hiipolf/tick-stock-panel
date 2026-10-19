# ADR 0004 — `levels._support_resistance` Rust 迁移

Date: 2026-10-19
Status: **Accepted**
Tracking: Spec 0001 (now Implemented)
Supersedes: ADR-0001 的 "seam-only" scope 表述(从 0 真实业务逻辑 → 1 个真实指标)
Implements: Spec 0001 §"Solution" / §"Implementation Decisions"

## 上下文 (Context)

TSP 后端 `backend/app/indicators/levels.py::_support_resistance(df, bins=40)` 是
支撑/压力位 chip-decay 算法,中文 A 股图表 "压力支撑" 标记线 + AI 个股分析
提示词的价位上下文都从这里出。

算法(纯 Python,line 62-165):
- 逐日迭代 240 根 K 线,每根对 `bins=40` 个价位桶做 decay+distribute 更新
- 物理上等价于 `5400 只股票 × 240 天 × 40 桶 = 51,840,000` 次 f64 算术
- Python `for row in range(n)` 是解释器循环,不是 Polars 表达式

用户主观体感:切股票 / 重算 levels 时图表刷新有明显 spike。Profile 没有
量化 budget,但 spec 写的是 "sub-50 ms per symbol"。

ADR-0001 已经选了 Path A(PyO3 + `pyo3-polars` seam),Phase 1 落地 hello-world
`sum_close` 后 seam 验证完成(`backend/tests/test_rs_indicators_seam.py` 4/4
pass)。Ticket 02 / 03 是同一个 seam 的第一次真实业务迁移。

## 决策 (Decision)

**Spec 0001 接受。把 `_support_resistance` 的计算核心迁到 `crates/indicators/src/levels.rs`,
Python 端保留为 thin adapter 走 `try-import tf_rs_indicators → ImportError 退化
到 _py_support_resistance` 模式(ADR-0002 §5 既有契约,本 ADR 不另立新策略)。**

### 三个阶段(对应 4 个 spec ticket)

| Ticket | 产出 | 关键 commit |
|--------|------|-------------|
| 01 research | 选定 **Approach A**(`to_vec()` 一次 + Rust 循环) | `eee251c` + `docs/research/levels-rust-api.md` |
| 02 kernel | `crates/indicators/src/levels.rs` 1:1 移植 + 2 个 Rust 单测 | `9b5b370` + code-review fixes `92604cd` |
| 03 adapter | 240-day fixture + 4 个 seam test + thin adapter | `c800612` + code-review fixes `f7c62ef` |

### 实现选择(引用研究笔记 §5)

- 不用 `polars` 表达式 / `cut` / `qcut` / `search_sorted`(Ticket 01 调研结论)
- inner loop 是 irreducible state machine(每行 chips 依赖上一行),Polars
  表达无力化,朴素 Rust 循环即可
- `to_vec()` 一次性 Arrow→`Vec<Option<f64>>` 拷贝,后续纯 Rust 算术
- 1-row `List<Float64>` 列广播到 N 行,Python 端 `to_list()[-1]` 取最终 chips
- reduce(POC + 2 dense zones)抽到 `_reduce_to_poc_and_dense` helper,Python
  / Rust 两条路径共享
- bin policy 抽到 `_bin_edges(df, bins)` helper,Python 端 `_py_support_resistance`
  和 adapter 都调它(零 Shotgun Surgery)

### 双轨测试(ADR-0002 §4)

- **Rust 单测** `crates/indicators/src/levels.rs::tests`:2 个 case
  - `main_path_hand_computed` — 30×5 bins,手算 `[600, 600, 600, 600, 600]`
  - `graceful_no_op_no_volume_column` — 30 行无 `volume` → null list column
- **Python seam** `backend/tests/test_rs_levels_seam.py`:4 个 case
  - 240-day parity bit-identical(规格 spec §"Seams" 最高门槛)
  - 强制 `ImportError` 走 `_py_support_resistance` 路径
  - 4 个 graceful no-op trigger 都返回 `[]`
  - dtype preservation + List<Float64> 全行 broadcast 验证

## 后果 (Consequences)

### 正面

- **Latency 体感消失**:从 Python 解释器循环降到 Rust 循环 + 零拷贝 PyDataFrame
  往返。`spec §"User Stories" #7` 写 "sub-second for 240 × 5,400",实测留给
  后续 perf ticket 量化。
- **bit-identical parity**:240 天 fixture 上,新 kernel 输出 = 纯 Python
  reference 输出,`assert result == expected` 跑过。Spec §"Acceptance
  Checklist" "240-day fixture parity test" 项 = green。
- **vendor 链路不受影响**:kernel 不依赖 vendor 之外的 polars 特性,TF-RS-1 的
  5 个 patch site(`unique.rs` / `lp.rs` / `functions.rs` / `row_deletions.rs`
  / `lower_ir.rs`)跟 levels.rs 不交互。
- **chart 路径 0 改动**:7 个 caller(`app/api/levels.py` / `app/stock_analysis.py`
  / frontend `StockDataTable.tsx` / `ECharts*` / `LEVEL_TYPES` / `PriceLevel` /
  AI prompt template)全部 untouched,public signature `_support_resistance(df,
  bins=40) -> list[dict]` 不变。
- **try-import fallback 验证**:wheel 缺失时 CI / 裸机环境自动走
  `_py_support_resistance`,测试 `test_python_fallback_when_wheel_missing`
  强制 mock `__import__` 验证 fallback 路径产物 == 240-day reference 产物。

### 负面 / 成本

- **多一次 build step**:`maturin develop --release` ~4 分钟(release 模式;
  debug 模式更快但 wheel 实际运行会触发 abi3 检查)。在 4 阶段内已经优化过
  (vendor 化避开 cargo cache patch + `[patch.crates-io]`,不是每次都冷编译)。
- **`_py_support_resistance` 必须永久保留**:fallback 不是临时,机器没装
  wheel / CI 没装 Rust toolchain 都会走 fallback。Spec 写"keep indefinitely"。
- **一处 spec drift**:`Spec 0001 §"Python wrapper" 示例伪代码` 用 `chip[-1]`
  当 f64,kernel 实际返回 `List<Float64>`。实现修正了 spec bug 但 spec wording
  没追认,**Ticket 04 同步修 spec 措辞**(或者 spec 重写时再清)。

### 回滚 (Rollback)

5 分钟内:
1. 删 `backend/app/indicators/levels.py` 里的 `_support_resistance` thin
   adapter,改回原版(可直接 `git show c16aef9:backend/app/indicators/levels.py`)。
2. 删 `crates/indicators/src/levels.rs` + `lib.rs` 里 `mod levels;` 那一行。
3. 不动 `tf_rs_indicators` wheel 安装(它仍能编,只是没东西调用)。

回滚后,所有 7 个 caller 仍然工作(签名没变),只是每次切股票 spike 回来。
Cargo build 不需要重跑 vendor 链路(`vendor/` 留着无害)。

## 退出标准 (Exit criteria)

- [x] `cargo test -p tf-rs-indicators -- levels` 2/2 pass
- [x] `pytest backend/tests/test_rs_levels_seam.py -v` 4/4 pass(240-day parity bit-identical)
- [x] `pytest backend/tests/test_rs_indicators_seam.py -v` 4/4 pass(sum_close 无回归)
- [x] `cargo test --workspace` 全 green
- [x] `maturin develop --release` 成功,`tf_rs_indicators.support_resistance` 从 Python 可见
- [x] 7 个 caller 0 改动(`git show 9b5b370 --stat` 跟 spec 列出清单对账)
- [x] 0 新增 Cargo 依赖,0 新增 feature flag,`Cargo.lock` 不变
- [x] ADR-0001 status 更新为 `(seam + first real indicator)`
- [x] ADR-0004(本 ADR)写完
- [x] Spec 0001 status 更新为 `Implemented`
- [x] 跨项目 memory 教训(已经在 Ticket 01/02 的 memory append 阶段写完)

## 交叉引用 (Cross-references)

- **Spec**: `docs/specs/0001-migrate-levels-support-resistance.md` (Status: Implemented)
- **ADR 链**:
  - `docs/adr/0001-rust-kernel-path-a-scope.md` (选 Path A;本 ADR supersedes "seam-only" 描述)
  - `docs/adr/0002-rust-kernel-bindings.md` (§5 try-import fallback 契约;§4 dual-track testing)
  - `docs/adr/0003-polars-0552-vendoring.md` (TF-RS-1 vendor 链路;本 ADR 不与它冲突)
- **Research**: `docs/research/levels-rust-api.md` (Ticket 01 选 Approach A 的研究笔记)
- **Context**: `CONTEXT.md` (Path A 决策与原则)
- **实现 commits** (worktree `rust-kernel-path-a` 分支):
  - `eee251c` Ticket 01 research
  - `9b5b370` Ticket 02 kernel
  - `92604cd` Ticket 02 code-review fixes(1:1 port:int bin truncation, no silent null guard)
  - `c800612` Ticket 03 thin adapter + 240-day parity
  - `f7c62ef` Ticket 03 code-review fixes(`_bin_edges` helper + broadcast assertion + fallback split)
- **Tag 链**:
  - `path-a-hello-world` (sum_close seam)
  - `path-a-vendor-0552` (vendor 化)
  - `path-a-spec-0001` (spec 写完)
  - `path-a-ticket01-research` (Approach A)
  - `path-a-ticket02-kernel` (kernel 落地)
  - `path-a-ticket02-fixes` (1:1 port 修复)
  - `path-a-ticket03-adapter` (adapter + 240-day parity)
  - `path-a-ticket03-fixes` (helper + test fixes)

## Further Notes

- **未来 ticket 候选**(不在本 ADR scope,但同 spec 0001 §"Out-of-Band Followups"):
  - Bench harness 量化 "sub-second for 240 × 5,400"(spec 0001 §"Profile" 章节)
  - `crates/factors` 第一个真实 kernel(post-ADR-0001 第二次 attack,等 `custom_signals.py` 选例子)
  - `crates/signals` 第一个真实 kernel(BOLL / KDJ / MACD / RSI 之一)
  - 改 default fallback policy(从 "Python-first" → "Rust-first",需要生产数周
    green parity 之后才能翻)
- **本 ADR 复审触发条件**:
  - 任何 `levels._support_resistance` 算法 / 数值语义变化(改 1:1 port 承诺)
  - ADR-0003 升级(`polars 0.55.3+` 上游修了 TF-RS-1)且 TF-RS-1 删了 → 看 vendor
    链路是否需要联动调整
  - Path A 切到 Path B/C(完全不该发生,本 ADR 只在 Path A 内)

---

(本 ADR 用中文写,因为本仓库 ADR-0001/0002/0003 全部是中文,保持术语 / 缩写
对齐;`<scope>` / `<ticket>` / 数字 hash 等是稳定的跨语言引用。)
