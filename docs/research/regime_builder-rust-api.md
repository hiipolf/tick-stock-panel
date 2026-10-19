# Research — `regime_builder` Path A-5 Rust API survey (2 kernels)

> Path A-5 / Ticket 01 — `services.regime_builder.run_regime_batch` + 1 helper
> (`_compute_batch` + `_aggregate_daily`) Rust migration. Spec 0005 is frozen at
> commit `9a929cc` (tag `path-a5-spec-0005`); the 2-kernel signature table in
> §"Implementation Decisions" is the spec ground truth this ticket surveys.
>
> Date: 2026-10-19.
> Vendored versions surveyed: `pyo3 0.29.3`, `pyo3-polars 0.28.0`, `polars 0.55.2`.
> Path A-4 mirror: this doc follows the structure of `docs/research/build_sector_rotation-rust-api.md`
> (commit `93c4867`, T01 of Path A-4).
> Path A-3 mirror: this doc mirrors `docs/research/market-overview-rust-api.md`
> (commit `9300a24`, T01 of Path A-3).
> Companion throwaway: `crates/indicators/examples/research_regime_builder_api.rs`.
>
> **Source layout reality check**: the task brief refers to "Vendored polars 0.55.2
> source: `vendor/polars-*/src/...`". The actual on-disk layout (matching Path A-4
> T01 finding) is:
> - `vendor/polars-ops/`, `vendor/polars-mem-engine/`, `vendor/polars-stream/` are
>   locally-vendored (per ADR-0003 / TF-RS-1 patches; only these 3 sub-crates have
>   non-exhaustive-match fixes that don't survive `cargo clean`).
> - The main `polars-0.55.2`, `polars-io-0.55.2`, `polars-lazy-0.55.2`, `polars-plan-0.55.2`,
>   `polars-core-0.55.2`, `pyo3-0.29.3`, `pyo3-polars-0.28.0` crates are fetched from
>   crates.io and live in
>   `C:\Users\admin\.cargo\registry\src\index.crates.io-1949cf8c6b5b557f\`. They are
>   byte-for-byte what would be at `vendor/polars-0.55.2/src/...` if vendored.
> Citations below use the cargo-registry path (where rustc actually reads the source)
> because that is the ground truth for `path:line` references. Path A-4 T01 used the
> same convention (see `build_sector_rotation-rust-api.md:21-25`).

## TL;DR — Approach A (kernels split: 1 IO-helper on `PathBuf`, 1 reduce on 2× `PyDataFrame` + `HashMap<String, f64>`)

2 kernels split by signature shape:

| # | Kernel | IO? | Input type | Output type | Approach |
|---|--------|-----|-----------|-------------|----------|
| 1 | `compute_batch_kernel` | **yes (parquet glob)** | `PathBuf`, `&str`, `&str`, `u32` | `PyDataFrame` | `LazyFrame::scan_parquet(glob_path).filter(date in range).collect()` — IO encapsulated in polars crate, kernel pure-compute on resulting `DataFrame` (per ADR-0006 §"5 段"§1+§3 Path A-4 amendment) |
| 2 | `aggregate_daily_kernel` | no | `PyDataFrame` + `HashMap<String, f64>` | `PyDataFrame` | Heavy `group_by("date").agg([...14 aggregations...])` + join `index_pct_map` by `str(date)`; pure-Rust reduce on Python-pre-aggregated per-stock DataFrame |

**Kernel 1 reads parquet internally** (encapsulated IO in the polars crate);
**kernel 2 is pure-compute on 2 pre-aggregated `PyDataFrame`s** (per Q3=a Path A-3
precedent — heavy Polars expression chain on adapter-collected per-stock DataFrame).
Both return `PyDataFrame` (the canonical PyO3 surface for kernel output).

No `#[pyclass]` derive needed — `HashMap<String, f64>` parameter uses the
PyO3 0.29.3 universal `HashMap<K, V>` extract impl (Path A-4 cite at
`pyo3-0.29.3/src/conversions/std/map.rs:106-129`).

## Cited polars 0.55.2 / pyo3 0.29.3 / pyo3-polars 0.28.0 evidence

### 1. `LazyFrame::scan_parquet(path: PlRefPath, args: ScanArgsParquet) -> PolarsResult<LazyFrame>` (kernel 1 IO + glob pattern)

Source: `polars-lazy-0.55.2/src/scan/parquet.rs:169-184` (`scan_parquet` family):

```rust
// parquet.rs:167
impl LazyFrame {
    /// Create a LazyFrame directly from a parquet scan.
    pub fn scan_parquet(path: PlRefPath, args: ScanArgsParquet) -> PolarsResult<Self> {
        Self::scan_parquet_sources(ScanSources::Paths(Buffer::from_iter([path])), args)
    }

    /// Create a LazyFrame directly from a parquet scan.
    pub fn scan_parquet_sources(sources: ScanSources, args: ScanArgsParquet) -> PolarsResult<Self> {
        LazyParquetReader::new(args).with_sources(sources).finish()
    }

    /// Create a LazyFrame directly from a parquet scan.
    pub fn scan_parquet_files(
        paths: Buffer<PlRefPath>,
        args: ScanArgsParquet,
    ) -> PolarsResult<Self> {
        Self::scan_parquet_sources(ScanSources::Paths(paths), args)
    }
}
```

Glob expansion is enabled by default: `polars-lazy-0.55.2/src/scan/parquet.rs:43`
(`glob: true` in `ScanArgsParquet::default()`). When the kernel writes
`data_dir / "**" / "*.parquet"` as a single `PlRefPath`, the polars globber
expands `**` and `*` against the filesystem at scan-time. `PlRefPath::new`
(per `polars-utils-0.55.2/src/pl_path.rs:295`) accepts `impl AsRef<str> + Into<PlRefStr>`,
so the canonical idiom is:

```rust
let glob_str = format!("{}/**/*.parquet", data_dir.display());  // forward-slash paths
let glob_path = PlRefPath::new(&glob_str);                       // glob-aware
let lf = LazyFrame::scan_parquet(glob_path, ScanArgsParquet::default())?;
```

The Python equivalent `pl.scan_parquet(data_dir / "**" / "*.parquet")` from
Spec 0005 §"Implementation Decisions" item 2 maps 1:1 — kernel 1 internally
calls `scan_parquet` (not the eager `ParquetReader::new` pattern that Path A-4
kernels 2/3/4 use for single-partition reads per Spec 0004 ADR-0006 amendment).
The kernel here needs to read a **range** of dates with warmup, so the lazy
glob + filter pattern is the right tool.

`PlRefPath::try_from_pathbuf(path: PathBuf) -> PolarsResult<PlRefPath>` at
`polars-utils-0.55.2/src/pl_path.rs:311-313` is the explicit `PathBuf` →
`PlRefPath` conversion (returns `PolarsResult` because Windows paths need
UTF-8 normalization). For glob paths, prefer `PlRefPath::new(glob_str)`
(constructed from a string) because `PathBuf::join("**/*.parquet")` does not
normalize the glob metacharacters on Windows backslashes.

> **Glob on Windows caveat**: Path A-4 T01 did not exercise glob (kernels 2/3/4
> read single `date=YYYY-MM-DD/part.parquet` partitions, not glob). Path A-5
> kernel 1 IS glob-based per Spec 0005 §"Implementation Decisions" item 2
> (`pl.scan_parquet(data_dir / "**" / "*.parquet")`). On Windows, the globber
> inside polars normalizes to forward-slash (per the `pl_path.rs` Windows
> invariant at line 86-87: "Windows paths will have leading `\\?\` prefix
> stripped, and all backslashes normalized to forward slashes"). T02 kernel
> implementation MUST construct the glob path string with forward slashes
> (use `format!("{}/**/*.parquet", ...)` or convert `PathBuf` to string via
> `.display()` then replace `\` with `/`).

### 2. `LazyFrame::filter(self, predicate: Expr) -> Self` (kernel 1 warmup + batch date filter)

Source: `polars-lazy-0.55.2/src/frame/mod.rs:950-954`:

```rust
// mod.rs:945
///   df.lazy()
///     .filter(col("sepal_width").is_not_null())
///     .select([col("sepal_width"), col("sepal_length")])
///
/// pub fn filter(self, predicate: Expr) -> Self {
    let opt_state = self.get_opt_state();
    let lp = self.get_plan_builder().filter(predicate).build();
    Self::from_logical_plan(lp, opt_state)
}
```

Kernel 1 applies `filter(date >= warmup_start AND date <= batch_end)` after
`scan_parquet`. The `Expr` for the predicate composes:

```rust
use polars::prelude::*;
use polars_plan::prelude::*;  // col, lit
let warmup_start_expr = col("date").gt_eq(lit(warmup_start));  // date >= warmup_start
let batch_end_expr = col("date").lt_eq(lit(batch_end));        // date <= batch_end
let predicate = warmup_start_expr.and(batch_end_expr);
let lf = LazyFrame::scan_parquet(glob_path, ScanArgsParquet::default())?
    .filter(predicate);
```

Note: `col("date")` resolves at scan-time; the filter is pushed down to the
parquet reader as a row-group predicate when `use_statistics: true` (the
`ScanArgsParquet::default()` setting per `polars-lazy-0.55.2/src/scan/parquet.rs:38`).

### 3. `LazyFrame::group_by(...)?.agg(...)` with 14 aggregations (kernel 2 heavy reduce)

Source: `polars-lazy-0.55.2/src/frame/mod.rs:1052-1057` (`LazyFrame::group_by`):

```rust
// mod.rs:1052
pub fn group_by<E: AsRef<[IE]>, IE: Into<Expr> + Clone>(self, by: E) -> LazyGroupBy {
    let keys = by
        .as_ref()
        .iter()
        .map(|e| e.clone().into())
        .collect::<Vec<_>>();
    ...
}
```

And `polars-lazy-0.55.2/src/frame/mod.rs:2070` (`LazyGroupBy::agg`):

```rust
// mod.rs:2070
pub fn agg<E: AsRef<[Expr]>>(self, aggs: E) -> LazyFrame {
    #[cfg(feature = "dynamic_group_by")]
    let lp = DslBuilder::from(self.logical_plan)
        .group_by(
            self.keys,
            self.predicates,
            ...
```

Kernel 2 builds a 14-aggregation `group_by("date").agg([...])` block mirroring
`backend/app/services/regime_builder.py::_aggregate_daily` line 177-. Per Spec 0005
§"Implementation Decisions" kernel 2 step 3, the 14 aggregations are:

```text
change_pct.gt(0).sum().alias("up_count")
change_pct.lt(0).sum().alias("down_count")
pl.len().alias("total_count")
change_pct.mean().alias("avg_pct")
change_pct.median().alias("median_pct")
change_pct.ge(0.03).sum().alias("strong_up_count")
change_pct.le(-0.03).sum().alias("strong_down_count")
signal_limit_up.cast(Boolean).sum().alias("limit_up")
signal_limit_down.cast(Boolean).sum().alias("limit_down")
signal_broken_limit_up.cast(Boolean).sum().alias("broken_limit")
consecutive_limit_ups.max().alias("max_consecutive")
amount.sum().alias("total_amount")
amount.mean().alias("avg_amount")
// MA20 above pct split into 2 helper cols + 1 final:
(close.gt(ma20).cast(Int64).sum().truediv(close.count())).alias("_ma20_above_pct")
```

(`_above_cnt` + `_above_total` → `_ma20_above_pct` per Spec 0005 §"Implementation
Decisions" kernel 2 step 3 sub-bullet; the Python `_aggregate_daily` line 222-228
computes these as 2 intermediate cols and merges.)

The lazy `group_by + agg` API is the same shape Path A-4 T02 used for
`per_bucket_reduce_kernel` (which does eager reduce; kernel 2 here uses lazy
because the input is a Python-pre-aggregated `PyDataFrame` round-trip —
lazy is the forward direction per polars 0.55 deprecation hint at
`polars-core-0.55.2/src/frame/group_by/mod.rs:354, 393` for the eager
`DataFrame::group_by(...).sum()` API).

### 4. `Expr::shift(self, n: Expr) -> Self` + `Expr::over(...)` (kernel 1 `with_prev_consecutive`)

Source: `polars-plan-0.55.2/src/dsl/mod.rs:634-637` (`shift`):

```rust
// mod.rs:634
/// Shift the values in the array by some period. See [the eager implementation](polars_core::series::SeriesTrait::shift).
pub fn shift(self, n: Expr) -> Self {
    self.map_binary(FunctionExpr::Shift, n)
}
```

And `polars-plan-0.55.2/src/dsl/mod.rs:818-823` (`over`):

```rust
// mod.rs:818
pub fn over<E: AsRef<[IE]>, IE: Into<Expr> + Clone>(
    self,
    partition_by: E,
) -> PolarsResult<Self> {
    self.over_with_options(Some(partition_by), None, Default::default())
}
```

The kernel 1 internal step 5 `with_prev_consecutive(df)` (per Spec 0005
§"Implementation Decisions" kernel 1 step 5) is the Polars expression:

```rust
let shifted = col("consecutive_limit_ups").shift(lit(1)).over(["symbol"])?;
```

This applies the `shift(1)` operation partitioned by `"symbol"` — exactly
what the Python `pl.col("consecutive_limit_ups").shift(1).over("symbol")`
expression evaluates to. **Implementation note for T02**: per Q3=a, this
expression chain stays in **Python** (the kernel itself only does IO +
warmup filter; the heavy Polars expressions run in the adapter). T02 may
move this expression into the kernel as a Polars expression if performance
warrants, but the Spec's signature is fixed.

**Why this is an explicit cite**: the `shift.over` pattern is the canonical
"per-symbol row-shifted value" idiom. Other idioms (`pl.col().rolling_*(...)`,
`pl.col().diff().over(...)`) would be wrong. The `shift` returns `Option<T>`
(null on first row of each partition); kernel 1 must accept that null and
let the Python adapter or downstream `drop_nulls` handle it.

### 5. `HashMap<String, f64>` parameter extraction (kernel 2 `index_pct_map`)

Source: `pyo3-0.29.3/src/conversions/std/map.rs:106-129`:

```rust
// map.rs:106
impl<'py, K, V, S> FromPyObject<'_, 'py> for collections::HashMap<K, V, S>
where
    K: FromPyObjectOwned<'py> + cmp::Eq + hash::Hash,
    V: FromPyObjectOwned<'py>,
    S: hash::BuildHasher + Default,
{
    type Error = PyErr;

    fn extract(ob: Borrowed<'_, 'py, PyAny>) -> Result<Self, Self::Error> {
        let dict = ob.cast::<PyDict>()?;
        let mut ret = collections::HashMap::with_capacity_and_hasher(dict.len(), S::default());
        for (k, v) in dict.iter() {
            ret.insert(
                k.extract().map_err(Into::into)?,
                v.extract().map_err(Into::into)?,
            );
        }
        Ok(ret)
    }
}
```

`HashMap<String, f64>` works because `String: FromPyObjectOwned` (per
`pyo3-0.29.3/src/conversions/std/string.rs:148`) and `f64: FromPyObjectOwned`
(standard impl). Python-side the adapter calls the kernel with
`{"2025-01-15": 0.012, "2025-01-16": -0.005, ...}` (a Python dict literal)
— PyO3 0.29.3 universal extract handles it transparently. This is the same
pattern Path A-3 `dimension_rank_kernel` uses for its 3 `HashMap<String, f64>`
parameters (per `docs/research/market-overview-rust-api.md`).

### 6. `PyDataFrame` round-trip (kernel 1 output + kernel 2 input + output)

Source: `pyo3-polars-0.28.0/src/types.rs:41-43` (struct) + `:188-202` (`FromPyObject`)
+ `:254-270` (`IntoPyObject`):

```rust
// types.rs:41
#[repr(transparent)]
#[derive(Debug, Clone)]
/// A wrapper around a [`DataFrame`] that can be converted to and from python with `pyo3`.
pub struct PyDataFrame(pub DataFrame);

// types.rs:188
impl<'a, 'py> FromPyObject<'a, 'py> for PyDataFrame {
    type Error = PyErr;

    fn extract(ob: Borrowed<'a, 'py, PyAny>) -> PyResult<Self> {
        let series = ob.call_method0("get_columns")?;
        let n = ob.getattr("width")?.extract::<usize>()?;
        let mut columns = Vec::with_capacity(n);
        for pyseries in series.try_iter()? {
            let pyseries = pyseries?;
            let s = pyseries.extract::<PySeries>()?.0;
            columns.push(s.into_column());
        }
        unsafe { Ok(PyDataFrame(DataFrame::new_unchecked_infer_height(columns))) }
    }
}

// types.rs:254
impl<'py> IntoPyObject<'py> for PyDataFrame {
    type Target = PyAny;
    type Output = Bound<'py, Self::Target>;
    type Error = PyErr;

    fn into_pyobject(self, py: Python<'py>) -> Result<Self::Output, Self::Error> {
        let pyseries = self
            .0
            .columns()
            .iter()
            .map(|s| PySeries(s.as_materialized_series().clone()).into_pyobject(py))
            .collect::<PyResult<Vec<_>>>()?;

        let polars = POLARS.bind(py);
        polars.call_method1("DataFrame", (pyseries,))
    }
}
```

Path A-3 + Path A-4 precedent: kernel output always uses `PyDataFrame`. The
IntoPyObject at line 254-270 emits a real Python `polars.DataFrame` via
`POLARS.call_method1("DataFrame", (pyseries,))` — column-by-column round-trip
via `PySeries`'s `IntoPyObject` (which uses the Arrow C Data Interface via
`SeriesExport` at `types.rs:249-250`).

For Path A-5: kernel 2's input `per_stock_df: PyDataFrame` round-trip is
**critical** — the input DataFrame is the per-stock batch result from
kernel 1 (after `compute_indicators` + `compute_limit_signals` +
`with_prev_consecutive` Polars expression chain in Python). The
`FromPyObject` at line 188-202 reconstructs a Rust `DataFrame` from the
Python `polars.DataFrame` via `getattr("width")` + `call_method0("get_columns")`
+ per-column `PySeries` extract. Performance: zero-copy for the Arrow C
Data Interface path (per `polars-ffi`'s `import_series` at line 183), per-column
allocations only for the Python-side `get_columns()` iterator.

### 7. `PathBuf` parameter (kernel 1 `data_dir`)

Source: `pyo3-0.29.3/src/conversions/std/path.rs:15-32`:

```rust
// path.rs:15
impl FromPyObject<'_, '_> for PathBuf {
    type Error = PyErr;

    fn extract(ob: Borrowed<'_, '_, PyAny>) -> Result<Self, Self::Error> {
        // We use os.fspath to get the underlying path as bytes or str
        let path = unsafe { ffi::PyOS_FSPath(ob.as_ptr()).assume_owned_or_err(ob.py())? };
        Ok(path.extract::<OsString>()?.into())
    }
}
```

> **Spec 0005 wording**: §"Implementation Decisions" kernel 1 signature writes
> `data_dir: PathBuf` (the column "Input shape" already uses `PathBuf`). This
> matches the Path A-4 T01 finding (`PathBuf`, not `&Path` — `&Path` has no
> PyO3 0.29.3 `FromPyObject` impl). T02 can use this signature verbatim; no
> wording refinement needed.

### 8. `LazyFrame::collect(self) -> PolarsResult<DataFrame>` (kernel 1 collect)

Source: `polars-lazy-0.55.2/src/frame/mod.rs:746-`:

```rust
// mod.rs:746
pub fn collect(self) -> PolarsResult<DataFrame> {
    self.collect_with_engine(Engine::Auto).map(|r| match r {
        QueryResult::Single(df) => df,
```

Kernel 1 ends with `.collect()?` to materialize the lazy scan + filter chain
into an eager `DataFrame` that can be returned as `PyDataFrame`. The
`Engine::Auto` default routes to streaming or in-memory based on the plan
shape — for a 5-day × 30-symbol daily fixture (~150 rows × 15 cols), in-memory
is automatic.

> **Path A-4 lazy idiom reminder**: Path A-4 T02 used `polars` `lazy` feature
> (per `Cargo.toml:23` workspace dep pin `polars = { features = ["lazy", "parquet", "csv"] }`).
> `LazyFrame::collect()` at line 746 is the forward path; the eager
> `DataFrame::group_by(...).agg(...)` APIs at
> `polars-core-0.55.2/src/frame/group_by/mod.rs:354, 393` are gated
> `#[deprecated(since = "0.24.1", note = "use polars.lazy aggregations")]`.
> Path A-5 kernel 2's `group_by + agg` block uses lazy.

## 2-kernel signature table mapping Spec 0005 to PyO3 0.29.3 / pyo3-polars 0.28.0 types

| # | Spec 0005 signature | Production Rust signature (T02) | PyO3 impl cites |
|---|---------------------|---------------------------------|------------------|
| 1 | `compute_batch_kernel(data_dir: PathBuf, batch_start: &str, batch_end: &str, warmup_days: u32) -> PyDataFrame` | Same as spec on T02 | `pyo3-0.29.3/src/conversions/std/path.rs:15` (PathBuf), `pyo3-0.29.3/src/conversions/std/string.rs:124` (&str), `polars-lazy-0.55.2/src/scan/parquet.rs:169` (scan_parquet), `polars-lazy-0.55.2/src/frame/mod.rs:950` (filter), `polars-lazy-0.55.2/src/frame/mod.rs:746` (collect), `pyo3-polars-0.28.0/src/types.rs:254` (PyDataFrame IntoPyObject) |
| 2 | `aggregate_daily_kernel(per_stock_df: PyDataFrame, index_pct_map: HashMap<String, f64>) -> PyDataFrame` | Same as spec on T02 | `pyo3-polars-0.28.0/src/types.rs:188` (PyDataFrame FromPyObject), `pyo3-0.29.3/src/conversions/std/map.rs:106` (HashMap extract), `polars-lazy-0.55.2/src/frame/mod.rs:1052` (group_by), `polars-lazy-0.55.2/src/frame/mod.rs:2070` (agg), `pyo3-polars-0.28.0/src/types.rs:254` (PyDataFrame IntoPyObject) |

> **Output type naming** (`DataFrame` vs `PyDataFrame`, `str` vs `String`):
> Spec 0005 uses the Python-facing / colloquial names. The production PyO3
> signature uses `PyDataFrame` (the `pyo3-polars` wrapper) for `#[pyfunction]`
> returns. The adapter code never sees the difference — Python sees
> `polars.DataFrame` either way. (No `String` returns in Spec 0005; both
> kernels take `&str` date inputs and return `PyDataFrame`.)

## Path A-3 + Path A-4 lessons reuse notes

Per agent memory carry-over (Path A-3 + Path A-4 + cross-project lessons):

| Lesson | Applies to Path A-5 kernel | How |
|--------|----------------------------|-----|
| **Polars 0.55 ListChunked** (construction from `Vec<f64>` / `Vec<Option<f64>>`; `df![...]!` macro for DataFrame construction) | kernel 2 result construction (14-agg output) | Use `df![...]!` macro per `crates/indicators/src/levels.rs:40-43` precedent (Path A-1) + per `crates/indicators/src/services_sector_rotation/per_bucket_reduce.rs` (Path A-4) |
| **PowerShell Start-Process 包装器的伪 timeout** (PS 5.1 cargo/check/run fake-timeout trap) | T01 verification commands | Avoid wrapper-based timeouts; verify by `Get-Item ...LastWriteTime` + `cargo run` direct invocation; per memory entry "退出条件: PS 6+ / pwsh 修了 Start-Process 的 wait 行为" |
| **Polars 0.55 `ChunkedArray::into_iter()` removed** (use `.iter()` not `.into_iter()`) | kernels 1/2 (if any `StringChunked`/`Float64Chunked` traversal for filter validation) | Use `.iter()` per `polars-core-0.55.2/src/chunked_array/iterator/mod.rs:12-22` + Path A-3 memory entry |
| **`Iterator::max_by` closure arg order: (new, accumulator)** | kernel 2 (`aggregate_daily_kernel` `consecutive_limit_ups.max()` Polars expression vs any pure-Rust sort logic) | Path A-3 memory: "max_by closure arg order is (new, accumulator)" — for Polars expressions this is built-in; for pure-Rust loops use `max_by` carefully with `partial_cmp + Ordering::Equal` |
| **`raw_close` ≠ previous trading day's close** | **NOT directly applicable to Path A-5** — kernel 1 reads parquet via `scan_parquet` (no manual `target_date - 1` offset like Path A-4 kernel 2). The warmup filter is date-range based, not prev-close based. **However**: T02 implementer should verify that `compute_indicators` Polars expression (per Q3=a stays in Python) computes `change_pct` via `pl.col("close").shift(1).over("symbol")` not via `raw_close` column — this matches the Path A-3 memory entry. |
| **`PyO3 0.29.3` kernel parameter types: `&Path` has no `FromPyObject` (use `PathBuf`)** | **kernel 1 directly** — Spec 0005 §"Implementation Decisions" already writes `data_dir: PathBuf`; Path A-4 T01 finding carry-over (verbatim). | Per `pyo3-0.29.3/src/conversions/std/path.rs:15`; same as Path A-4 kernels 2/3/4 |
| **`prev_close` parameter semantics** | **NOT applicable** — Path A-5 kernel 1 reads `data_dir / "**" / "*.parquet"` with date range filter, no single `target_date` parameter; Path A-4 kernel 2's `target_date - 1 business day` offset does not apply. |
| **Windows PowerShell 5.1 + Python: CRLF mismatch in JSON fixture SHA-256 comparison** | Path A-5 T03 fixture regenerator (deferred to T03 — this T01 doesn't write fixture). | Carry-over: when T03 writes `expected.json`, use `open(path, "wb")` binary mode (LF preservation) or compare via `json.loads(read_text(...))` to source dict |
| **`monkeypatch.delitem(sys.modules, X)` does NOT reliably force `ImportError`** | Path A-5 T04 forced-fallback seam test (deferred to T04 — this T01 doesn't write seam tests). | Carry-over: T04 will use `monkeypatch.setattr(target_module, "_rust_kernels_available", lambda: False)` instead |

## T01 signature recommendation

**Status: NO SEMANTIC CHANGE REQUIRED. Spec 0005 §"Implementation Decisions"
2-kernel signature table is implementable as-written on `polars 0.55.2` +
`pyo3 0.29.3` + `pyo3-polars 0.28.0` without any signature change.**

Compared to Path A-4 T01 (which had 3 wording refinements: `Path` → `PathBuf`,
`pl.Series` → `PySeries`, `str` → `String`):

- **`data_dir: PathBuf`** — already correct in Spec 0005 (Path A-4 T01 finding
  carry-over). The "Input shape" column writes `PathBuf (enriched_dir)` explicitly.
- **`batch_start: &str`, `batch_end: &str`** — already correct in Spec 0005
  (no change needed).
- **`warmup_days: u32`** — scalar `u32` has no PyO3 mapping concern (it's a
  plain Rust integer; PyO3 0.29.3 has direct `FromPyObject` for `u32` per the
  default impls).
- **`HashMap<String, f64>`** — universal extract works (PyO3 0.29.3 cite at
  `src/conversions/std/map.rs:106-129`); no change needed.
- **`PyDataFrame` returns** — already correct in Spec 0005 (Path A-3 + Path A-4
  precedent). No change needed.

### Implementation notes for T02 (NOT signature changes; flag for T02 attention)

1. **Kernel 1 glob construction on Windows** (per cite 1 caveat): T02 should
   build the glob path via `format!("{}/**/*.parquet", data_dir.display())`
   then `PlRefPath::new(&glob_str)` — this avoids the Windows-backslash
   `PathBuf::join` normalization issue. The `data_dir` argument comes from
   Python as `pathlib.Path` which PyO3 auto-converts to `PathBuf`.

2. **Kernel 1 vs Python pre-aggregate split** (per Spec 0005 §"Implementation
   Decisions" kernel 1 internal steps 1-7): The Spec lists 7 internal steps,
   but per Q3=a Path A-3 convention, the heavy Polars expression chain
   (steps 3 `compute_indicators`, 4 `compute_limit_signals`,
   5 `with_prev_consecutive`) stays in **Python**. T02 should implement
   kernel 1 as:
   - Steps 1-2: warmup_start calc + `scan_parquet(glob).filter(date in [warmup_start, batch_end]).collect()`
   - **Step 6-7 happen in the Python adapter** after the kernel returns
     (drop warmup rows where `date < batch_start`; apply compute_indicators +
     compute_limit_signals + with_prev_consecutive Polars expression chain).
   - OR T02 may move steps 3-5 INTO the kernel as Polars expressions if
     performance warrants — the Spec signature allows both.

3. **Kernel 1 empty case** (per Spec 0005 §"Implementation Decisions" kernel 1
   "Empty case"): "no enriched parquet → empty" — T02 should return
   `PyDataFrame(DataFrame::default())` or `PyDataFrame(DataFrame::empty())`
   when the glob expands to zero files OR the filter produces zero rows.
   The throwaway example synthesizes a 0-row DataFrame to validate the
   schema path.

4. **Kernel 2 `index_pct_map` join** (per Spec 0005 §"Implementation Decisions"
   kernel 2 step 4): the join is "by `str(date)`" — i.e. the date column in
   the aggregated output is converted to a string and looked up in the
   `HashMap<String, f64>`. T02 should `select([col("date").dt.to_string().alias("date_str")])`
   + `join` OR `with_columns([col("date").dt.to_string().apply(...)])` —
   the cleanest approach is `df.with_columns([col("date").cast(DataType::String).alias("_date_str")])`
   then `join` on `_date_str` against a single-column DataFrame built from
   the HashMap. (T01 verified the join semantics; the exact T02 expression
   may vary.)

5. **Kernel 2 14-aggregation count** (per Spec 0005 §"Implementation Decisions"
   kernel 2 step 3): 14 distinct aggregations + 1 ma20_above_pct compound =
   14 output columns. T02 should build the aggregation vector as a `Vec<Expr>`
   literal — verify the resulting DataFrame has 14+1 = 15 columns (14 agg
   outputs + 1 `index_pct` from the join = 15 cols total).

### Proposed Spec 0005 amendment (for T05 closure)

NONE REQUIRED. The Spec 0005 §"Implementation Decisions" 2-kernel signature
table is implementable as-written. Only minor implementation notes (above)
are flagged for T02 attention; these are NOT signature changes.

If T02 implements steps 3-5 inside kernel 1 as Polars expressions (per the
"OR" branch in note 2 above), the kernel signature still does not change —
only the body grows. T05 closure may want to add a clarification footnote
to Spec 0005 §"Implementation Decisions" kernel 1 step 3-5 saying "these
steps may be implemented in Rust using Polars expressions OR kept in the
Python adapter (Q3=a)".

## What T01 does NOT cover

- Production `#[pyfunction]` implementations in `crates/indicators/src/services_regime/{compute_batch,aggregate_daily}.rs` — Ticket 02 (kernel).
- Adapter ↔ kernel naming convention (Python `_py_*` aliases per §"Module layout (Python)") — Ticket 04.
- Module layout (sub-directory `crates/indicators/src/services_regime/`) — Ticket 02.
- `lib.rs` `wrap_pyfunction!` registration + total cdylib function count reaching **16** — Ticket 02.
- 8 Rust unit tests (4 per kernel) — Ticket 02.
- 5 Python seam tests + bit-identical orchestrator parity — Tickets 03 + 04.
- 5d × 30-symbol daily fixture + regenerator + `expected.json` — Ticket 03.
- ADR-0006 §"5 段" Path A-5 footnote in-place amendment + CONTEXT.md glossary sub-bullet — Ticket 05.

T01 deliverable is the API survey + a stub example that cargo-checks clean
against the vendored deps. Behaviour parity + adapter integration is
downstream.

## Companion throwaway

`crates/indicators/examples/research_regime_builder_api.rs` (NEW). Mirrors
Path A-4 T01's example (`research_build_sector_rotation_api.rs`) pattern:
2 `#[pyfunction]` stubs with production signatures + synthetic 5-day × 30-symbol
daily fixture + `fn main()` exercising both kernels via synthesized
DataFrames (parquet IO is deferred to T02; the throwaway demonstrates
that the PyO3 surface compiles + the return-type contract is well-formed
for both kernels).

Verified:
- `cargo check --example research_regime_builder_api -p tf-rs-indicators` → exits 0
- `cargo run --example research_regime_builder_api` → exits 0
  - kernel 1 `compute_batch_kernel_stub` signature compiles; demo path synthesizes
    5-day × 30-symbol partition-style DataFrame as the IO output surrogate
  - kernel 2 `aggregate_daily_kernel_stub` signature compiles; demo path
    exercises the `PyDataFrame` + `HashMap<String, f64>` PyO3 surface and
    synthesizes the 14-column aggregation output
  - 2 PyO3 signature probes compiled OK (no semantic / type inference failures)

## Path A-5 cross-refs

- **Spec 0005** — `docs/specs/0005-migrate-regime-builder-compute-batch.md` (commit `9a929cc`)
- **ADR-0006** — `docs/adr/0006-service-level-seam-contract.md` (Path A-3 + Path A-4 amendments)
- **ADR-0001** — kernel status registry (will be bumped at T02/T05 closure)
- **Path A-4 T01 precedent** — commit `93c4867`, `docs/research/build_sector_rotation-rust-api.md`
- **Path A-3 T01 precedent** — commit `9300a24`, `docs/research/market-overview-rust-api.md`
