//! `tf_rs_indicators` — Python extension module exposing indicator kernels.
//!
//! **Status: Path A — service-level migration in flight (ADR-0001, ADR-0006, Spec 0003).**
//! Six functions registered today:
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

use pyo3::prelude::*;

mod services_market_overview;
mod sum;
mod levels;
mod extreme_levels;

/// Python entry point — registers `tf_rs_indicators` with the interpreter.
/// Two-step binding: keep this signature minimal; per-function bindings
/// live next to their implementations (see `sum.rs`, `levels.rs`,
/// `extreme_levels.rs`, `services_market_overview/{top_rows,pct_band_rows,
/// dimension_rank}.rs`).
#[pymodule]
fn tf_rs_indicators(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(sum::sum_close, m)?)?;
    m.add_function(wrap_pyfunction!(levels::support_resistance, m)?)?;
    m.add_function(wrap_pyfunction!(extreme_levels::extreme_levels, m)?)?;
    m.add_function(wrap_pyfunction!(services_market_overview::top_rows_kernel, m)?)?;
    m.add_function(wrap_pyfunction!(services_market_overview::pct_band_rows_kernel, m)?)?;
    m.add_function(wrap_pyfunction!(services_market_overview::dimension_rank_kernel, m)?)?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
