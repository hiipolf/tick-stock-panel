# Research — `build_sector_rotation` Path A-4 Rust API survey (8 kernels)

> Path A-4 / Ticket 01 — `services.sector_rotation.build_sector_rotation` + 5 helpers
> (`_bare`, `_prev_daily_close`, `_minute_pcts`, `_load_sector_flow`, `_rank_for_mode`)
> Rust migration. Spec 0004 is frozen at commit `71ceae6`; the 8 kernel signatures in
> §"8 Rust kernels registered in `tf_rs_indicators`" are the spec ground truth this
> ticket surveys.
>
> Date: 2026-10-19.
> Vendored versions surveyed: `pyo3 0.29.3`, `pyo3-polars 0.28.0`, `polars 0.55.2`.
> Path A-3 mirror: this doc follows the structure of `docs/research/market-overview-rust-api.md`
> (commit `9300a24`, T01 of Path A-3).
> Companion throwaway: `crates/indicators/examples/research_build_sector_rotation_api.rs`.
>
> **Source layout reality check**: the task brief refers to "Vendored polars 0.55.2
> source: `vendor/polars-*/src/...`". The actual on-disk layout is:
> - `vendor/polars-ops/`, `vendor/polars-mem-engine/`, `vendor/polars-stream/` are
>   locally-vendored (per ADR-0003 / TF-RS-1 patches; only these 3 sub-crates have
>   non-exhaustive-match fixes that don't survive `cargo clean`).
> - The main `polars-0.55.2`, `polars-io-0.55.2`, `polars-plan-0.55.2`, `polars-core-0.55.2`,
>   `pyo3-0.29.3`, `pyo3-polars-0.28.0` crates are fetched from crates.io and live in
>   `C:\Users\admin\.cargo\registry\src\index.crates.io-1949cf8c6b5b557f\`. They are
>   byte-for-byte what would be at `vendor/polars-0.55.2/src/...` if vendored.
> Citations below use the cargo-registry path (where rustc actually reads the source)
> because that is the ground truth for `path:line` references.

## TL;DR — Approach A (kernels are mostly pure-Rust on `Vec<HashMap<String, Py<PyAny>>>` / 2× `PyDataFrame`, IO kernels read parquet internally)

8 kernels split by signature shape:

| # | Kernel | IO? | Input type | Output type | Approach |
|---|--------|-----|-----------|-------------|----------|
| 1 | `bare_kernel` | no | `&str` | `Series` | Build a `Series` from a literal expression (stub returns `Series::new`); no Polars expression in hot path |
| 2 | `prev_daily_close_kernel` | **yes (parquet)** | `PathBuf`, `&str` | `DataFrame` | `PlParquetReader::new(path).finish()` returns `DataFrame` directly; column select + filter by date |
| 3 | `minute_pcts_kernel` | **yes (parquet)** | `PathBuf`, `&str` | `(DataFrame, String, bool)` | Same pattern + tuple return; tuple extract impl per `pyo3-0.29.3\src\types\tuple.rs:924` (Path A-3 cite) |
| 4 | `sector_flow_kernel` | **yes (ext parquet)** | `PathBuf`, `&str` | `(DataFrame, bool)` | Same pattern; second element is the `flow_available` boolean |
| 5 | `per_bucket_reduce_kernel` | no | 2× `PyDataFrame` | `DataFrame` | Polars expressions on the two input DataFrames (per-bucket `group_by` + `agg`) per Q3=a (Python-pre-aggregate) |
| 6 | `timeline_kernel` | no | `Vec<HashMap<String, Py<PyAny>>>` | `DataFrame` | Pure-Rust loop on adapter's pre-aggregated `list[dict]`; build output DataFrame by column-append |
| 7 | `cross_events_kernel` | no | `Vec<HashMap<String, Py<PyAny>>>` | `DataFrame` | Same pattern; emits a 4-column event log |
| 8 | `rank_for_mode_kernel` | no | `Vec<HashMap<String, Py<PyAny>>>` + 4 scalars | `Vec<String>` | Pure-Rust reduce with `max_by`/`min_by` (closure arg order `(new, accumulator)` per Path A-3 memory); `Vec<String>` extraction confirmed by `pyo3-0.29.3\src\conversions\std\string.rs:148` |

**Kernels 5/6/7/8 are pure-compute (no IO); kernels 2/3/4 read parquet internally via
`polars::prelude::PlParquetReader` (the IO is encapsulated in the polars crate, so per
ADR-0006 §"5 段"§1 amendment per Q12=a the kernel remains "pure-compute on the resulting
DataFrame").** Kernel 1 is degenerate — its body returns a `Series` literal expression
under a single column name.

No `#[pyclass]` derive needed — kernels 6/7/8 use `Py<PyAny>` (universal Python-object
holder) per Path A-3 precedent.

## Cited polars 0.55.2 / pyo3 0.29.3 / pyo3-polars 0.28.0 evidence

### 1. `PlParquetReader::new(Path).finish() -> PolarsResult<DataFrame>` (kernels 2/3/4 IO)

Source: `polars-io-0.55.2/src/parquet/read/mod.rs:39` (re-export) + `reader.rs:179-241`
(`SerReader` impl) + `reader.rs:203-241` (`finish`):

```rust
// reader.rs:18
pub struct ParquetReader<R: Read + Seek> {
    reader: R,
    rechunk: bool,
    slice: (usize, usize),
    columns: Option<Vec<String>>,
    projection: Option<Vec<usize>>,
    parallel: ParallelStrategy,
    ...
}

// reader.rs:179
impl<R: MmapBytesReader> SerReader<R> for ParquetReader<R> {
    fn new(reader: R) -> Self { ... }
    fn set_rechunk(mut self, rechunk: bool) -> Self { ... }

    // reader.rs:203
    fn finish(mut self) -> PolarsResult<DataFrame> {
        let schema = self.schema()?;
        let metadata = self.get_metadata()?.clone();
        ...
        let mut df = read_parquet(self.reader, self.slice, ...)?;
        if self.rechunk { df.rechunk_mut_par(); }
        ...
        Ok(df)
    }
}
```

The `read/mod.rs:39` re-export (`pub use reader::ParquetReader;`) means kernels
can write `polars::prelude::ParquetReader::new(File::open(path)?).finish()?` (the
canonical PyO3 Path→File idiom is `File::open(path.as_path())`).

> **Note on `PlParquetReader` vs `ParquetReader`**: in the 0.55.2 source tree the
> reader is named `ParquetReader` (the `Pl-` naming scheme applies to newer versions).
> The re-export at `polars-io-0.55.2/src/parquet/read/mod.rs:39` confirms this.

### 2. `df.dt.truncate(every: Expr) -> Expr` (kernel 5 per-bucket bucketize)

Source: `polars-plan-0.55.2/src/dsl/dt.rs:215-221`:

```rust
/// Truncate the Datetime/Date range into buckets.
pub fn truncate(self, every: Expr) -> Expr {
    self.0.map_binary(
        FunctionExpr::TemporalExpr(TemporalFunction::Truncate),
        every,
    )
}
```

Kernel 5 (per-bucket reduce) bucketizes a `timestamp` column by `every = lit(Duration::parse("5m"))`
(or 1/15m depending on `bucket_minutes`). The Python `_compute` adapter already does this
with `.dt.truncate("5m")`; for kernel 5 we still keep the bucketize expression in Python
(per Q3=a: the bucketize + group_by block stays in the adapter), but kernel 5 may still
reference `dt.truncate` via Polars expression if needed in T02.

### 3. `DataFrame::group_by([keys])?.select([...]).sum()` (kernel 5 reduce)

Source: `polars-core-0.55.2/src/frame/group_by/mod.rs:115-122` (entry) + `:355-366` (`mean`) +
`:393-399` (`sum`):

```rust
// mod.rs:115
pub fn group_by<I, S>(&self, by: I) -> PolarsResult<GroupBy<'_>>
where
    I: IntoIterator<Item = S>,
    S: AsRef<str>,
{
    let selected_keys = self.select_to_vec(by)?;
    self.group_by_with_series(selected_keys, true, false)
}

// mod.rs:393 (sum)
pub fn sum(&self) -> PolarsResult<DataFrame> {
    let (mut cols, agg_cols) = self.prepare_agg()?;
    for agg_col in agg_cols {
        let new_name = fmt_group_by_column(agg_col.name().as_str(), GroupByMethod::Sum);
        let mut agg = unsafe { agg_col.agg_sum(&self.groups) };
        agg.rename(new_name);
        cols.push(agg);
    }
    DataFrame::new_infer_height(cols)
}
```

> **Caveat**: the eager `group_by(...).sum()` / `.mean()` APIs are gated
> `#[deprecated(since = "0.24.1", note = "use polars.lazy aggregations")]` (line 354, 393).
> Path A-4 T02 should use lazy: `df.lazy().group_by([...]).agg([...]).collect()?` for
> production kernels (kernels 2/3/4 already read parquet into an eager `DataFrame`;
> the `to_lazy()` + `.collect()?` round-trip is cheap when the DataFrame is small).
> Path A-3 precedent (commit `9300a24`) used the eager API — T02 may follow or migrate
> to lazy. Either works; lazy is the forward direction per polars 0.55 deprecation hint.

### 4. `Vec<HashMap<String, Py<PyAny>>>` extraction (kernels 6/7/8 input)

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

`HashMap<String, Py<PyAny>>` works because `String: FromPyObjectOwned` (per
`pyo3-0.29.3/src/conversions/std/string.rs:148`) and `Py<PyAny>` is the universal
Python-object holder. Same as Path A-3 precedent (`dimension_rank_kernel` signature).

### 5. `PyDataFrame` round-trip (kernels 5 input + 5/6/7 output)

Source: `pyo3-polars-0.28.0/src/types.rs:42-43` (struct) + `:188-202` (`FromPyObject`)
+ `:254-270` (`IntoPyObject`):

```rust
// types.rs:42
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

Path A-3 reference cite at `pyo3-polars-0.28.0/src/types.rs:254` (Path A-3 doc line
"5. `pyo3-polars` `PyDataFrame` round-trip"). Verified: same source for Path A-4.

`PyDataFrame` is the canonical PyO3 surface for kernel IO. A kernel that returns
`PyDataFrame(df)` produces a real Python `polars.DataFrame` with the right dtype per column.

### 6. `&str` / `PathBuf` extraction (kernels 1-4 scalar args)

Sources:
- `pyo3-0.29.3/src/conversions/std/string.rs:124-133` (`&str` extract via `PyString::to_str`):

```rust
// string.rs:124
#[cfg(any(Py_3_10, not(Py_LIMITED_API)))]
impl<'a> FromPyObject<'a, '_> for &'a str {
    type Error = PyErr;
    fn extract(ob: Borrowed<'a, '_, PyAny>) -> Result<Self, Self::Error> {
        ob.cast::<PyString>()?.to_str()
    }
}
```

- `pyo3-0.29.3/src/conversions/std/path.rs:15-32` (`PathBuf` extract via
  `PyOS_FSPath` + `OsString`):

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

> **Spec divergence (minor wording)**: Spec 0004 §"8 Rust kernels" writes the IO kernels
> as `data_dir: Path` (kernels 2/3/4). In Rust, `&Path` has NO `FromPyObject` impl —
> only `PathBuf` does (path.rs:15). For a `#[pyfunction]` parameter that needs ownership
> (to call `File::open(path.as_path())` and own the file for the read), `PathBuf` is the
> canonical idiomatic type. **T01 recommendation**: in T02, declare `data_dir: PathBuf`
> and update Spec 0004 §"8 Rust kernels" wording to `data_dir: PathBuf` (semantically
> unchanged; `Path` was the loose wording).

### 7. `ChunkedArray::iter()` (kernel 1 Series-construction precedent)

Source: `polars-core-0.55.2/src/chunked_array/iterator/mod.rs:7-19`:

```rust
impl<T> ChunkedArray<T>
where
    T: PolarsDataType,
{
    #[inline]
    pub fn iter(&self) -> impl PolarsIterator<Item = Option<T::Physical<'_>>> {
        self.downcast_iter().flat_map(|arr| arr.iter()).trust_my_length(self.len())
    }
}
```

Per Path A-3 memory (`### Polars 0.55 ChunkedArray::into_iter() removed`),
use `.iter()` not `.into_iter()`. Kernel 1 (`bare_kernel`) needs to **construct** a
Series (not iterate one) — the right pattern is `Series::new("name".into(), values)`
or `Float64Chunked::from_vec("name".into(), vec).into_series()` per `polars-core`
chunked_array builder patterns (see `crates/indicators/src/levels.rs:40-43` for the
working pattern).

## 8-kernel signature table mapping Spec 0004 to PyO3 0.29.3 / pyo3-polars 0.28.0 types

| # | Spec 0004 signature | Production Rust signature (T02) | PyO3 impl cites |
|---|---------------------|---------------------------------|------------------|
| 1 | `bare_kernel(col: &str) -> Series` | `bare_kernel(col: &str) -> PyResult<PySeries>` | string.rs:124, pyo3-polars types.rs:42 |
| 2 | `prev_daily_close_kernel(data_dir: Path, target_date: &str) -> DataFrame` | `prev_daily_close_kernel(data_dir: PathBuf, target_date: &str) -> PyResult<PyDataFrame>` | path.rs:15, string.rs:124, parquet/read/reader.rs:179 |
| 3 | `minute_pcts_kernel(data_dir: Path, target: &str) -> (DataFrame, str, bool)` | `minute_pcts_kernel(data_dir: PathBuf, target: &str) -> PyResult<(PyDataFrame, String, bool)>` | path.rs:15, string.rs:124, types.rs:924 |
| 4 | `sector_flow_kernel(data_dir: Path, flow_field: &str) -> (DataFrame, bool)` | `sector_flow_kernel(data_dir: PathBuf, flow_field: &str) -> PyResult<(PyDataFrame, bool)>` | path.rs:15, string.rs:124 |
| 5 | `per_bucket_reduce_kernel(by_sector_df: PyDataFrame, market_df: PyDataFrame) -> DataFrame` | `per_bucket_reduce_kernel(by_sector_df: PyDataFrame, market_df: PyDataFrame) -> PyResult<PyDataFrame>` | pyo3-polars types.rs:188 |
| 6 | `timeline_kernel(per_bucket: Vec<HashMap<String, Py<PyAny>>>) -> DataFrame` | `timeline_kernel(per_bucket: Vec<HashMap<String, Py<PyAny>>>) -> PyResult<PyDataFrame>` | map.rs:106 |
| 7 | `cross_events_kernel(per_bucket: Vec<HashMap<String, Py<PyAny>>>) -> DataFrame` | `cross_events_kernel(per_bucket: Vec<HashMap<String, Py<PyAny>>>) -> PyResult<PyDataFrame>` | map.rs:106 |
| 8 | `rank_for_mode_kernel(sectors: Vec<HashMap<String, Py<PyAny>>>, activity_map: HashMap<String, f64>, has_amount: bool, flow_by_member: HashMap<String, f64>, flow_available: bool, sort_by: &str) -> Vec<String>` | Same as spec on T02 | map.rs:106 (×3 occurrences), string.rs:148 |

> **Output type naming** (`Series` vs `PySeries`, `DataFrame` vs `PyDataFrame`,
> `str` vs `String`): Spec 0004 uses the **Python-facing / colloquial** names. The
> production PyO3 signature should use the `Py-` wrappers for `#[pyfunction]` return
> types (so the Python side sees the right polars type) and `String` (owned) for
> owned returns. The adapter code never sees the difference — Python sees
> `polars.Series` / `polars.DataFrame` / `str` either way.

## Path A-3 lessons reuse notes

Per agent memory carry-over (Path A-3 / Spec 0003 / Spec 0004 cross-ref):

| Path A-3 lesson | Applies to Path A-4 kernel | How |
|------------------|----------------------------|-----|
| **Polars 0.55 ListChunked** (construction from `Vec<f64>` / `Vec<Option<f64>>`) | kernels 1, 6, 7 (output Series/DataFrame construction) | Use `Float64Chunked::from_vec` / `StringChunked::from_iter` or `df![...]!` macro per `crates/indicators/src/levels.rs:40-43` precedent |
| **PowerShell Start-Process 包装器的伪 timeout** (cargo/check/run false-timeout trap on PS 5.1) | T01 verification commands | Avoid wrapper-based timeouts; verify by `Get-Item ...LastWriteTime + python -c "..."` style sanity checks (per memory) — Path A-3 T01 used direct `cargo check` + `cargo run` from the worktree |
| **Polars 0.55 ChunkedArray::into_iter() removed** (use `.iter()` not `.into_iter()`) | kernels 5/6/7 (if any `StringChunked`/`Float64Chunked` traversal) | Use `.iter()` per `polars-core-0.55.2/src/chunked_array/iterator/mod.rs:12` (above) |
| **Iterator::max_by closure arg order: (new, accumulator)** | kernel 8 (`rank_for_mode_kernel` sort logic for `sort_by = "change_pct"` etc.) | `max_by(|a, b| b.partial_cmp(a).unwrap_or(Equal))` — direction is counter-intuitive; Path A-3 memory says "use `max_by` carefully" |
| **raw_close ≠ previous trading day's close** | kernel 2 (`prev_daily_close_kernel`) | Per memory: kernel 2 may need a shift/offset; the spec's `target_date` parameter is the date *whose previous close* we want, not the close of `target_date` itself. T02 must read parquet for `target_date - 1` business day OR use the explicit `prev_close` column if present. T01 recommendation: T02 must explicitly handle the offset; this is not a one-liner `read_parquet(target_date)` |

## T01 signature recommendation

**Status: NO SEMANTIC CHANGE REQUIRED. Minor wording refinement only.**

The 8 kernel signatures in Spec 0004 §"8 Rust kernels registered in `tf_rs_indicators`"
are implementable as-written on `polars 0.55.2` + `pyo3 0.29.3` + `pyo3-polars 0.28.0`
without any signature change.

The only divergences are:

1. **`Path` → `PathBuf`** (wording): Spec 0004 writes `data_dir: Path` for kernels
   2/3/4. In Rust, `&Path` has no `FromPyObject` impl. T02 should use `data_dir: PathBuf`.
   The adapter code (Python) calls the kernel with `bar = PathBuf::from(repo.data_dir)`
   (or simply passes the existing `Path` object — `PathBuf` extracts from `pathlib.Path`
   transparently per `pyo3-0.29.3/src/conversions/std/path.rs:15-32`).
   **No semantic change.**

2. **`Series` → `PySeries` / `str` → `String`** (wording): Spec 0004 uses the Python-facing
   type names. T02 should use the PyO3 wrapper types (`PySeries`, `PyDataFrame`) for
   `#[pyfunction]` returns and `String` for owned string returns. The Python adapter
   sees `polars.Series` / `polars.DataFrame` / `str` either way. **No semantic change.**

3. **`raw_close ≠ previous trading day's close`** (kernel 2): T02 implementer must
   be careful: per Path A-3 memory, the input `target_date` is the date *whose* previous
   close is needed (not the close of `target_date` itself). T02 must read parquet for
   `target_date - 1 business day` or use the explicit `prev_close` column if the
   parquet schema provides it. This is an **implementation detail**, not a signature
   change — flagging for T02 attention.

### Proposed Spec 0004 amendment (for T05 closure)

```
§"8 Rust kernels registered in `tf_rs_indicators`" — table column "Input shape":

OLD wording (kernels 2/3/4 row 1):
    "filesystem `Path` + date string"
  → "filesystem `PathBuf` + date string"  (T01 finding; PyO3 0.29.3 has FromPyObject
    for PathBuf at conversions/std/path.rs:15; &Path has no FromPyObject impl)

OLD wording (output column):
    "`pl.DataFrame`" / "`pl.Series`"
  → "`PyDataFrame`" / "`PySeries`"  (T01 finding; pyo3-polars 0.28.0 wraps the polars
    types in `#[repr(transparent)] PyDataFrame(pub DataFrame)` and the IntoPyObject at
    types.rs:254 emits a real polars.DataFrame on the Python side)

NEW row appended (kernel 2 implementation note):
    "T02 implementer note: `target_date` is the date whose PREVIOUS close is wanted,
     not the close of `target_date` itself. Path A-3 memory entry
     `raw_close ≠ previous trading day's close`. Read parquet for `target_date - 1`
     business day OR use the explicit `prev_close` column if the schema provides it."
```

All other §"8 Rust kernels" rows: no change.

## What T01 does NOT cover

- The 5-day × 30-symbol minute fixture — Ticket 03 (frozen `data_dir` parquet subset
  + `expected.json` baseline).
- Bit-identical `_compute` / `_rank_for_mode` algorithm port — Ticket 02 (kernel).
- Adapter ↔ kernel naming convention (Python `_py_*` aliases per §"Module layout
  (Python)") — Ticket 04.
- Module layout (sub-directory `crates/indicators/src/services_sector_rotation/`)
  — Ticket 02.
- 16-20 Rust unit tests (2-3 per kernel) — Ticket 02.
- 5 Python seam tests + ADR-0006 §"5 段"§1 amendment in place — Ticket 04 + Ticket 05.

T01 deliverable is the API survey + a stub example that cargo-checks clean against the
vendored deps. Behaviour parity + adapter integration is downstream.

## Companion throwaway

`crates/indicators/examples/research_build_sector_rotation_api.rs` (this commit) runs
8 stub kernels over a synthetic 30-symbol × 1-day minute fixture (synthetic DataFrame
constructed in pure Rust — no parquet IO, no Python runtime attached), exercises
the 8 stub kernels' **type signatures** (8 `#[pyfunction]` with production-grade
PyO3 surfaces), constructs the resulting `DataFrame`s purely in Rust (no Polars
expressions in the hot path for kernels 6/7/8), and prints the column vectors.
Verifies:

1. PyO3 0.29.3 `FromPyObject` signature compiles for `Vec<HashMap<String, Py<PyAny>>>`,
   `HashMap<String, f64>`, `(PyDataFrame, ...)` tuples, `PathBuf`, `&str`.
2. Pure-Rust `rank_for_mode` reduce (with the `max_by` direction lesson applied) on
   a 30-symbol × 8-sector fixture.
4. The 2 PyDataFrame-output kernels (timeline_kernel + cross_events_kernel) construct
   the right DataFrame column structure.
5. The 3 IO kernel signatures compile (PathBuf + &str + return-tuple).

Run:
```
cargo check --example research_build_sector_rotation_api -p tf-rs-indicators
cargo run   --example research_build_sector_rotation_api -p tf-rs-indicators
```