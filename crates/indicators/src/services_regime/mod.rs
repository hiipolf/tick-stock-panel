//! `services_regime` — Path A-5 multi-batch daily-grain kernel module (Spec 0005).
//!
//! Two production kernels, each a 1:1 port of a Python helper in
//! `backend/app/services/regime_builder.py`:
//!   - `compute_batch_kernel(data_dir: PathBuf, batch_start: &str,
//!                            batch_end: &str, warmup_days: u32) -> df`
//!     1:1 port of `_compute_batch` (line 329). Per Spec 0005 §"Implementation
//!     Decisions" kernel 1: T02 implements the IO + warmup date-range filter
//!     (steps 1-2 + 6-7); the heavy Polars expression chain (`compute_indicators`
//!     + `compute_limit_signals` + `with_prev_consecutive` per Q3=a) stays in
//!     the Python adapter.
//!   - `aggregate_daily_kernel(per_stock_df: PyDataFrame,
//!                              index_pct_map: HashMap<String, f64>) -> df`
//!     1:1 port of `_aggregate_daily` (line 177-). The 14-aggregation
//!     `group_by + agg` block + `index_pct_map` join by `str(date)`. T02
//!     implements as a pure-Rust reduce (algorithmically equivalent — see
//!     `aggregate_daily.rs` module docstring for the T02 divergence note).
//!
//! Per **ADR-0006 §"5 段"§1+§3 Path A-4 amendment** (kernel 1 IO + `PathBuf`):
//! - `polars` crate handles IO internally; kernel remains pure-compute on the
//!   resulting `DataFrame`.
//! - Per Path A-4 T01 finding carry-over, `data_dir: PathBuf` (not `&Path` —
//!   `&Path` has no PyO3 0.29.3 `FromPyObject` impl per
//!   `pyo3-0.29.3/src/conversions/std/path.rs:15`).
//!
//! Per **Path A-3 + Path A-4 9 lessons reuse notes** (see
//! `docs/research/regime_builder-rust-api.md`):
//! - **T01 carry-over**: `PathBuf` for `data_dir` (kernel 1); `HashMap<String, f64>`
//!   for `index_pct_map` (PyO3 universal extract per
//!   `pyo3-0.29.3/src/conversions/std/map.rs:106-129`); `PyDataFrame` for both
//!   kernels' returns (per `pyo3-polars-0.28.0/src/types.rs:254` IntoPyObject).
//! - **T01 implementation note 1**: kernel 1 glob path constructed via
//!   `format!("{}/**/*.parquet", data_dir.display())` + `PlRefPath::new` to avoid
//!   the Windows-backslash normalization issue noted in
//!   `polars-utils-0.55.2/src/pl_path.rs:86-87`.
//! - **T01 implementation note 2**: kernel 1 implements steps 1-2 (warmup_start
//!   calc + scan_parquet + filter + collect) + the empty-case graceful (no
//!   parquet → empty DataFrame). Per Q3=a Path A-3 precedent, steps 3-5
//!   (compute_indicators + compute_limit_signals + with_prev_consecutive
//!   Polars expression chain) stay in Python.
//! - **Path A-3 + Path A-4 memory carry-overs**: `ChunkedArray::into_iter()`
//!   use `.iter()` not `.into_iter()`; `Iterator::max_by` closure arg order
//!   (new, accumulator); `df![...]!` macro for DataFrame construction.
//!
//! **Pure-Rust inner / PyO3 wrapper split** (per Path A-3 + Path A-4 convention):
//! each kernel has a pure-Rust `*_inner(...)` function unit-tested without Python.
//! The `#[pyfunction]` wrapper extracts `Py<PyAny>` → kernel-internal types,
//! calls inner, returns the wrapped PyO3 surface type (`PyDataFrame`).

// `use pyo3::prelude::*;` is intentionally unused here — re-exports of
// `#[pyfunction]`-annotated items are picked up by `lib.rs` via path
// (e.g. `services_regime::compute_batch_kernel`). The Path A-4
// `services_sector_rotation/mod.rs` has the same pattern; suppress the warning.
#[allow(unused_imports)]
use pyo3::prelude::*;

mod aggregate_daily;
mod compute_batch;

// Re-export the PyO3 entry points so `lib.rs` can `wrap_pyfunction!` them.
pub use aggregate_daily::aggregate_daily_kernel;
pub use compute_batch::compute_batch_kernel;