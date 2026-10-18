# ADR 0002 — Rust Kernel Path A: 技术绑定 (Bindings)

Date: 2026-10-18
Status: **Accepted**(待 Cargo workspace 落定后 sealed)

## 上下文 (Context)

ADR-0001 选定 Path A 的策略与首交付边界。本 ADR 锁定实现层面三项硬决策:**Workspace 形态**、**版本对齐**、**测试矩阵**、**Fallback 策略**。它们都是"绑死"类(改它成本高),不上 ADR 容易遗忘。

事实基础:Hivebook(2026-09-23 实证)和 Polars 1.44 发行版注记(2026-09-09)。
- PyPI `polars 1.44.0` 因 "when/then/otherwise regression" 被 **yank**;实际命中的是 `polars 1.44.2`
- Rust crate 编号体系独立:PyPI `polars 1.44.x` 对应 crates.io `polars 0.55.x`(具体 `=0.55.2`)
- `pyo3-polars = "=0.28.0"` 必须依赖同版 Polars crate(`polars = 0.55.1`,允许 `^0.55.1` 兼容到 0.55.2)
- Polars 1.44.x 工作空间钉 `<= chrono-0.4.41`,与本 crate 间接关联
- `sysinfo 0.39` 被 `polars-utils` 引入,要求 `rustc >= 1.95`
- 本机工具链 `rustc 1.98.1`,**满足** rustc 底线

## 决策 (Decision)

### 1. Cargo workspace 形态(决策 Q5=b)

- 起步即 workspace,**不是**单 crate 后扩。多 crate 共享 `target/` cache,加新 kernel 时不再重拉 Polars。
- 首期 member:`crates/{indicators,factors,signals,shared}`,其中只有 `indicators/` 写代码,其它三个是空 stub placeholder。
- workspace 根 `Cargo.toml` 用 `[workspace.dependencies]` 集中 crate 版本。

### 2. Rust toolchain pin(决策 Q6=b)

```toml
# rust-toolchain.toml
[toolchain]
channel    = "1.98.1"
components = ["rustfmt", "clippy"]
profile    = "minimal"
```

理由:
- 本机已是 1.98.1,免下载
- 锁死避开 stable 通道下 `polars-utils` 拉入更高 `sysinfo` 导致 rustc 升级压力
- 跨机器(笔记本、CI、第二台开发机)构建一致

### 3. Polars / PyO3 版本绑定(决策 Q7=fact)

workspace `[workspace.dependencies]` 形如下(精确锁定,不锁范围):

```toml
polars              = { version = "=0.55.2", features = ["python", "lazy", "parquet", "csv"] }
pyo3                = { version = "=0.29",   features = ["extension-module", "abi3-py310"] }
pyo3-polars         = { version = "=0.28.0", features = ["derive"] }
pyo3-polars-derive  = "=0.22.0"
```

- 不开 `simd` / `nightly` / `performant`(staging 后视性能数据再开)
- `polars = "=0.55.2"` 不是范围锁:是绝对 pin。后续升级要走标准 ADR 流程,不静默 bump

### 4. 测试矩阵(决策 Q8=b)

双轨:Rust 单元 + Python 端到端 seam 测试,**不可省一边**。

```
crates/indicators/
  src/lib.rs        # PyO3 entry + #[pyfunction] sum_close
  src/sum.rs        # pure algorithm + #[cfg(test)] mod tests
python/tests/
  test_rs_indicators_seam.py
  fixtures/sample_kline.parquet
```

- 单元测试在 Rust 内(< 1ms 千万次),验证算法正确性
- seam 测试在 Python(`pytest`),验证 `import tf_rs_indicators`、`PyDataFrame` 来回、列 dtype 一致
- **两者都过**才叫"协议接得通"

### 5. Python ↔ Rust Fallback(决策 Q9=A + extras opt-in)

```python
# app/indicators/pipeline.py
try:
    from tf_rs_indicators import sum_close
except ImportError:
    sum_close = None  # 走原 Polars expression fallback
```

`pyproject.toml` 用 extras 显式 gate:

```toml
[project.optional-dependencies]
rs = ["tf_rs_indicators"]
```

部署矩阵:
- 默认 `uv sync`:`sum_close = None`,Python 路径接管
- 开发机 `uv sync --extra rs`:装本地 maturin wheel,激活 Rust 路径
- CI 无 Rust 工具链:`uv sync`,自然 fallback
- 生产部署:可控 opt-in

### 6. Cargo.lock 入库(决策 Q12=a)

`Cargo.lock` 必须 **committed**:
- Polars 依赖图深且对 patch 版本敏感
- `sysinfo` 等 transitive dep 易在某次 `cargo update` 时拉进新 patch,引起 ABI 漂移
- 入库保证 `maturin build --release` 出 wheel 复现
- 与 `uv.lock` 已 commit 的策略对齐

### 7. 第一份 commit 分层

init commit 内**不分多个 commit**,一次性 create。理由:worktree 是空工作区,首次提交作为单一里程碑。后续 commit 按"一个 PyO3 类型加进来"或"一个测试加进来"颗粒度切。

## 后果 (Consequences)

**正向**:
- 版本对齐:Python 1.44.2 ↔ Rust 0.55.2,ABI 自动成立,Hivebook 实证样本表明 0.28.0/0.55.2 配对可编译
- `abi3-py310` 单 wheel 覆盖 Python ≥3.10,matrix 大幅简化(无需 per-Python minor wheel)
- 工具链锁:升级 Polars minor 时被迫重做 ADR,不被静默带走
- 测试双轨:Rust 单元测试可零依赖 CI 跑,Python seam 测试是契约

**负向**:
- 必须搭 Rust 工具链(本机 5min,CI 2min)
- `pyo3-polars = "=0.28.0"` 与 `polars = "=0.55.2"` 强绑定,下次升级必须联动 bump(一处忘改 → 编译失败或运行时 panic)
- extras 机制要求二次开发作者知道 `uv sync --extra rs` 才能启用 Rust 路径,文档增点
- `Cargo.lock` 偶发冲突(workspace 加新 crate 时需 `cargo update -p <new>` 单独解决)

**回退成本**:低。改版本是 `Cargo.toml` 数值修改 + `cargo update`。改策略是删除 `pyproject.toml` extras + 回滚 Python import 改回纯 Polars。预计回退 ≤ 1 小时。

**升级路径**:Polars 1.45/1.46 上游发布时:
1. 跑 `scripts/stress_polars_concurrency.py`(上游用来回归并发死锁)再 bump `polars>=1.45,<1.46`
2. Rust 侧绑 `<crate-new>`,`pyo3-polars` 同步 bump
3. `maturin build --release` + `python/tests/test_rs_indicators_seam.py` 跑通
4. ADR-0002 改 version 行,note: status `Superseded`

## 关联

- ADR-0001(scope,工作树 / 首攻 / hello-world 边界)
- `CONTEXT.md` 词汇表
- Hivebook(2026-09-23 实证,Polars 1.44.2 ABI 已经验证)
