//! `sum_close` — hello-world kernel.
//!
//! Computes the column-wise sum of an f64 `close` column and appends a
//! single-row `sum_close` column to the input DataFrame. Returns the
//! new DataFrame via `PyDataFrame` (zero-copy where possible — Polars
//! shares Arrow buffers across the bridge).
//!
//! This function is **not** part of the production indicator path; it
//! exists only to verify the PyO3 seam end-to-end. Production indicators
//! (support/resistance, MA family) will live in their own modules and
//! be wired into `backend/app/indicators/pipeline.py` after the seam
//! is signed-off (see ADR-0001).

use pyo3::prelude::*;
use pyo3_polars::PyDataFrame;
use polars::prelude::*;

#[pyfunction]
pub fn sum_close(df: PyDataFrame) -> PyResult<PyDataFrame> {
    // 1. Lift the inbound DataFrame into native Polars.
    let mut owned: DataFrame = df.into();

    // 2. Locate the `close` column and read it as f64 chunked.
    let total: f64 = {
        let close = owned
            .column("close")
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(
                format!("missing 'close' column: {e}")))?;
        let ca = close.f64().map_err(|e| pyo3::exceptions::PyTypeError::new_err(
            format!("'close' dtype must be f64: {e}")))?;
        ca.sum().unwrap_or(0.0)
    };

    // 3. Append the result as a 1-element f64 column named `sum_close`.
    // Polars 0.55+ migrated `DataFrame::with_column` from `Series` to `Column`
    // (the new opaque data type). Convert via `.into_column()` — Series retains
    // its dtype/values, just wraps in a Column.
    let scalar = Series::new("sum_close".into(), vec![total]);
    owned
        .with_column(scalar.into_column())
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(
            format!("failed to append 'sum_close': {e}")))?;

    // 4. Hand back. pyo3-polars moves the Arrow buffer; Python side reuses it.
    Ok(PyDataFrame(owned))
}

#[cfg(test)]
mod tests {
    /// Smoke test: at minimum the rust unit must compile and the
    /// pymodule name in lib.rs must agree with the cdylib export
    /// declared in Cargo.toml. End-to-end behaviour is exercised by
    /// `backend/tests/test_rs_indicators_seam.py` (ADR-0002 §4 — dual
    /// track testing).
    #[test]
    fn crate_metadata_consistent() {
        assert_eq!(env!("CARGO_PKG_NAME"), "tf-rs-indicators");
        assert_eq!(env!("CARGO_PKG_VERSION"), "0.0.0");
    }
}
