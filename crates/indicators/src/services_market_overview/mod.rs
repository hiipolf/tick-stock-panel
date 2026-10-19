//! `services_market_overview` — service-level kernel module (Path A-3 / Spec 0003).
//!
//! Three production kernels, each a 1:1 port of a Python helper in
//! `backend/app/services/market_overview_builder.py`:
//!   - `top_rows_kernel(rows, key, descending, limit) -> df`
//!     1:1 port of `_top_rows` (15 LOC; filter+sort+project+slice).
//!   - `pct_band_rows_kernel(values) -> df`
//!     1:1 port of `_pct_band_rows` (24 LOC; 8 fixed buckets).
//!   - `dimension_rank_kernel(groups, limit, source_fields=None) -> df`
//!     1:1 port of `_dimension_rank`'s per-group reduce (62 LOC).
//!
//! Per **ADR-0006 §"5 段"** service-level seam contract:
//!   1. Adapter pre-aggregates (reads parquet + yaml + ext, builds groups).
//!   2. Kernel signature is multi-arg (no DataFrame-in for these).
//!   3. Kernel pure-compute; no IO, no filesystem, no globals.
//!   4. Kernel returns a `polars::DataFrame` (via `PyDataFrame`).
//!   5. Adapter owns cache; kernel is stateless.
//!
//! **T01 signature amendment** (see `docs/research/market-overview-rust-api.md`):
//! inputs use `Vec<HashMap<String, Py<PyAny>>>` (universal Python object holder)
//! instead of a `#[pyclass] StockFields` derive — same PyO3 0.29.3 surface,
//! future-proofs schema evolution, no per-field `#[pyo3(get)]` boilerplate.
//!
//! **Pure-Rust inner / PyO3 wrapper split** (per Path A-2 convention):
//! each kernel has a pure-Rust `*_inner(rows: &[Row], ...) -> PolarsResult<DataFrame>`
//! that's unit-tested without Python. The `#[pyfunction]` wrapper extracts
//! `Py<PyAny>` → `Row`, calls inner, returns `PyDataFrame`.

use pyo3::prelude::*;

mod dimension_rank;
mod pct_band_rows;
mod top_rows;

/// Shared `_finite`-equivalent for kernel-side dict extraction.
/// Returns `Some(f64)` iff the Python value extracts to a finite float;
/// `None` / non-finite / non-numeric → `None`.
///
/// Mirrors `market_overview_builder._finite`:
///     if v is None: return None
///     try: f = float(v); except (TypeError, ValueError): return None
///     return f if math.isfinite(f) else None
pub(crate) fn finite(obj: Option<&Py<PyAny>>, py: pyo3::Python<'_>) -> Option<f64> {
    obj.and_then(|v| v.extract::<f64>(py).ok()).filter(|f| f.is_finite())
}

// Re-export the PyO3 entry points so `lib.rs` can `wrap_pyfunction!` them.
pub use dimension_rank::dimension_rank_kernel;
pub use pct_band_rows::pct_band_rows_kernel;
pub use top_rows::top_rows_kernel;