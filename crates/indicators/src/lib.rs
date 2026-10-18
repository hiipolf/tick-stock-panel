//! `tf_rs_indicators` — Python extension module exposing indicator kernels.
//!
//! **Status: Path A — first real indicator migration (ADR-0001, Spec 0001).**
//! Two functions registered today:
//!   - `sum_close(df) -> df` — hello-world seam (kept; the dual-track
//!     `backend/tests/test_rs_indicators_seam.py` still exercises it)
//!   - `support_resistance(df, bins) -> df` — production port of
//!     `backend/app/indicators/levels.py::_support_resistance` (Spec 0001
//!     Ticket 02). The Python-side wrapper (Ticket 03) wires this into the
//!     caller path; until then the function is callable from Python but
//!     not yet used by the chart.

use pyo3::prelude::*;

mod sum;
mod levels;

/// Python entry point — registers `tf_rs_indicators` with the interpreter.
/// Two-step binding: keep this signature minimal; per-function bindings
/// live next to their implementations (see `sum.rs` and `levels.rs`).
#[pymodule]
fn tf_rs_indicators(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(sum::sum_close, m)?)?;
    m.add_function(wrap_pyfunction!(levels::support_resistance, m)?)?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
