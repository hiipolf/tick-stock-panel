//! `tf_rs_indicators` — Python extension module exposing indicator kernels.
//!
//! **Status: Path A — service-level migration in flight (ADR-0001, ADR-0006, Spec 0003/0004/0005).**
//! Sixteen functions registered today:
//!   - `sum_close(df) -> df` — hello-world seam (kept; the dual-track
//!     `backend/tests/test_rs_indicators_seam.py` still exercises it)
//!   - `support_resistance(df, bins) -> df` — production port of
//!     `backend/app/indicators/levels.py::_support_resistance` (Spec 0001,
//!     Ticket 02). The Python adapter (`_support_resistance` in `levels.py`)
//!     wires this into the caller path.
//!   - `extreme_levels(df, windows=None, swing_win=5) -> df` — production port
//!     of `backend/app/indicators/levels.py::_extreme_levels` (Spec 0002,
//!     Ticket 02). The Python adapter (`_extreme_levels` in `levels.py`) wires
//!     this into the caller path.
//!   - `top_rows_kernel(rows, key, descending, limit) -> df` — service-level port
//!     of `backend/app/services/market_overview_builder.py::_top_rows` (Spec 0003,
//!     Ticket 02). Per ADR-0006 §"5 段", Python adapter pre-aggregates; kernel is
//!     pure-compute on `Vec<HashMap<String, Py<PyAny>>>`.
//!   - `pct_band_rows_kernel(values) -> df` — service-level port of
//!     `backend/app/services/market_overview_builder.py::_pct_band_rows`
//!     (Spec 0003, Ticket 02).
//!   - `dimension_rank_kernel(groups, limit, source_fields=None) -> df` — service-
//!     level per-group reduce port (Spec 0003, Ticket 02); full leading/lagging
//!     sort is the adapter's job.
//!   - 8 Path A-4 kernels (Spec 0004, Ticket 02) — `bare_kernel`,
//!     `prev_daily_close_kernel`, `minute_pcts_kernel`, `sector_flow_kernel`,
//!     `per_bucket_reduce_kernel`, `timeline_kernel`, `cross_events_kernel`,
//!     `rank_for_mode_kernel`.
//!   - 2 Path A-5 kernels (Spec 0005, Ticket 02) — `compute_batch_kernel`,
//!     `aggregate_daily_kernel` (multi-batch daily-grain migration of
//!     `regime_builder._compute_batch` + `_aggregate_daily`).

use pyo3::prelude::*;

mod services_market_overview;
mod services_regime;
mod services_sector_rotation;
mod sum;
mod levels;
mod extreme_levels;

/// Python entry point — registers `tf_rs_indicators` with the interpreter.
/// Two-step binding: keep this signature minimal; per-function bindings
/// live next to their implementations (see `sum.rs`, `levels.rs`,
/// `extreme_levels.rs`, `services_market_overview/{top_rows,pct_band_rows,
/// dimension_rank}.rs`, `services_sector_rotation/{bare,prev_daily_close,
/// minute_pcts,sector_flow,per_bucket_reduce,timeline,cross_events,
/// rank_for_mode}.rs`, `services_regime/{compute_batch,aggregate_daily}.rs`).
#[pymodule]
fn tf_rs_indicators(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(sum::sum_close, m)?)?;
    m.add_function(wrap_pyfunction!(levels::support_resistance, m)?)?;
    m.add_function(wrap_pyfunction!(extreme_levels::extreme_levels, m)?)?;
    m.add_function(wrap_pyfunction!(services_market_overview::top_rows_kernel, m)?)?;
    m.add_function(wrap_pyfunction!(services_market_overview::pct_band_rows_kernel, m)?)?;
    m.add_function(wrap_pyfunction!(services_market_overview::dimension_rank_kernel, m)?)?;
    // Path A-4 — Spec 0004 §"8 Rust kernels"
    m.add_function(wrap_pyfunction!(services_sector_rotation::bare_kernel, m)?)?;
    m.add_function(wrap_pyfunction!(services_sector_rotation::prev_daily_close_kernel, m)?)?;
    m.add_function(wrap_pyfunction!(services_sector_rotation::minute_pcts_kernel, m)?)?;
    m.add_function(wrap_pyfunction!(services_sector_rotation::sector_flow_kernel, m)?)?;
    m.add_function(wrap_pyfunction!(services_sector_rotation::per_bucket_reduce_kernel, m)?)?;
    m.add_function(wrap_pyfunction!(services_sector_rotation::timeline_kernel, m)?)?;
    m.add_function(wrap_pyfunction!(services_sector_rotation::cross_events_kernel, m)?)?;
    m.add_function(wrap_pyfunction!(services_sector_rotation::rank_for_mode_kernel, m)?)?;
    // Path A-5 — Spec 0005 §"2 Rust kernels"
    m.add_function(wrap_pyfunction!(services_regime::compute_batch_kernel, m)?)?;
    m.add_function(wrap_pyfunction!(services_regime::aggregate_daily_kernel, m)?)?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
