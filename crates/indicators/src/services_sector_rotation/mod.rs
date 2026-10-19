//! `services_sector_rotation` — Path A-4 multi-kernel service-level module (Spec 0004).
//!
//! Eight production kernels, each a Rust port of a Python helper in
//! `backend/app/services/sector_rotation.py`:
//!   - `bare_kernel(col: &str) -> Series` — 1:1 port of `_bare`
//!     (column expression `pl.col(col).cast(String).str.strip_chars().str.split(".").list.first()`).
//!   - `prev_daily_close_kernel(data_dir: PathBuf, target_date: &str) -> DataFrame` —
//!     1:1 port of `_prev_daily_close` (read parquet for `target_date - 1` business day;
//!     per Path A-3 memory `raw_close ≠ previous trading day's close`).
//!   - `minute_pcts_kernel(data_dir: PathBuf, target: &str) -> (DataFrame, String, bool)` —
//!     1:1 port of `_minute_pcts` (read minute partition + derive `_pct` per symbol).
//!   - `sector_flow_kernel(data_dir: PathBuf, flow_field: &str) -> (DataFrame, bool)` —
//!     1:1 port of `_load_sector_flow` (read ext parquet for the user's chosen flow column).
//!   - `per_bucket_reduce_kernel(by_sector_df: PyDataFrame, market_df: PyDataFrame) -> DataFrame` —
//!     per-bucket reduce (bucket x member pcts/amount, leader + top_set + cross counts).
//!   - `timeline_kernel(per_bucket: Vec<HashMap<String, Py<PyAny>>>) -> DataFrame` —
//!     flatten per-bucket summaries into timeline rows (rotation / leader / market / cross).
//!   - `cross_events_kernel(per_bucket: Vec<HashMap<String, Py<PyAny>>>) -> DataFrame` —
//!     flatten per-bucket summaries into 0-axis-crossing event rows.
//!   - `rank_for_mode_kernel(sectors, activity_map, has_amount, flow_by_member, flow_available, sort_by) -> Vec<String>` —
//!     6-axis ranking (`activity` / `flow` / `pct` / `rank_change` / `momentum` / fallback).
//!
//! Per **ADR-0006 §"5 段"** service-level seam contract (with the Path A-4 amendment per
//! Q12=a / Spec 0004): kernels 2/3/4 read parquet internally via `polars::prelude::ParquetReader`
//! (encapsulated IO in the polars crate; the kernel itself remains pure-compute on the
//! resulting `DataFrame`). Adapter (`build_sector_rotation`) owns cache + IO orchestration.
//!
//! **T01 signature refinement** (see `docs/research/build_sector_rotation-rust-api.md`):
//! inputs use `PathBuf` for filesystem params (kernels 2/3/4 — `&Path` has no PyO3 0.29.3
//! `FromPyObject` impl per `pyo3-0.29.3/src/conversions/std/path.rs:15`); outputs use
//! `PySeries` / `PyDataFrame` (pyo3-polars 0.28.0 wrappers) / `String` (owned) for
//! `#[pyfunction]` returns — the Python adapter sees the same `polars.Series` /
//! `polars.DataFrame` / `str` either way.
//!
//! **Pure-Rust inner / PyO3 wrapper split** (per Path A-3 convention):
//! each kernel has a pure-Rust `*_inner(...)` function unit-tested without Python.
//! The `#[pyfunction]` wrapper extracts `Py<PyAny>` → kernel-internal row types,
//! calls inner, returns the wrapped PyO3 surface type.

use pyo3::prelude::*;

mod bare;
mod cross_events;
mod minute_pcts;
mod per_bucket_reduce;
mod prev_daily_close;
mod rank_for_mode;
mod sector_flow;
mod timeline;

/// Helper — 1:1 port of `sector_rotation._bare(col)`:
///
/// `pl.col(col).cast(pl.String).str.strip_chars().str.split(".").list.first()`.
///
/// Used as a column expression in pure-Rust kernels (we can't apply it as a
/// `pl.Expr` directly without a `LazyFrame`, so we implement the same logic
/// against materialized Series: cast → trim → split on "." → take first).
pub(crate) fn bare_symbol(s: &str) -> String {
    s.trim()
        .split('.')
        .next()
        .unwrap_or("")
        .trim()
        .to_string()
}

// Re-export the PyO3 entry points so `lib.rs` can `wrap_pyfunction!` them.
pub use bare::bare_kernel;
pub use cross_events::cross_events_kernel;
pub use minute_pcts::minute_pcts_kernel;
pub use per_bucket_reduce::per_bucket_reduce_kernel;
pub use prev_daily_close::prev_daily_close_kernel;
pub use rank_for_mode::rank_for_mode_kernel;
pub use sector_flow::sector_flow_kernel;
pub use timeline::timeline_kernel;
