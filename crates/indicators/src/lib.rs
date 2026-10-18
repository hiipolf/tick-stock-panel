//! `tf_rs_indicators` — Python extension module exposing indicator kernels.
//!
//! **Status: Path A hello-world (ADR-0001 / 0002).**
//! Only one function — `sum_close(df) -> df` — exists today, used solely
//! to verify the PyO3 seam: `#[pymodule]` registration, `#[pyfunction]`
//! signature, `PyDataFrame` ↔ `polars::frame::DataFrame` zero-copy
//! hand-off. Real indicator migration (levels.py support/resistance,
//! MA family) lands in subsequent PRs once the seam is signed-off.

use pyo3::prelude::*;

mod sum;

/// Python entry point — registers `tf_rs_indicators` with the interpreter.
/// Two-step binding: keep this signature minimal; per-function bindings
/// live next to their implementations (see `sum.rs`).
#[pymodule]
fn tf_rs_indicators(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(sum::sum_close, m)?)?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
