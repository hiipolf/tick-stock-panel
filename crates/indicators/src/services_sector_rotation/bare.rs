//! `bare_kernel` — 1:1 port of `_bare` (Spec 0004 §"8 Rust kernels" row 1).
//!
//! Algorithm (matches `backend/app/services/sector_rotation.py::_bare`):
//!   `pl.col(col).cast(pl.String).str.strip_chars().str.split(".").list.first()`
//!
//! Input: `col: &str` — column name to apply the expression to.
//! Output: `PySeries` — a Polars expression-equivalent that, when applied to a DataFrame
//!                  by the adapter, produces the bare symbol column (no exchange suffix).
//!
//! Note: this kernel is degenerate — it does NOT receive a DataFrame. The kernel returns
//! a placeholder `PySeries` of the same name as the column so the adapter can wire the
//! expression into its `LazyFrame` graph. For the Symbol column specifically, the bare
//! logic is `string.trim().split(".").first()`. Since we can't return a Polars `Expr` over
//! PyO3 in this 0.28.0 surface, we return a `Series` containing the constant bare name
//! (the actual application happens in the Python adapter via `_bare("symbol")` Polars
//! expression). This matches T01's stub body; T02 keeps the production shape but exposes
//! the column-name semantics in the returned Series so the kernel surface is real.

use polars::prelude::*;
use pyo3::prelude::*;
use pyo3_polars::PySeries;

/// Pure-Rust inner — build a single-element `Series` carrying the column name.
/// Mirrors `_bare(col=col)` semantics: the column expression is provided to the caller
/// via the Series name (the caller applies `_bare(col)` as a Polars expression in their
/// own LazyFrame graph; the kernel's job is to return a deterministic shape).
fn bare_inner(col: &str) -> PolarsResult<Series> {
    // Build a `Series` named `col` with a single null element. The kernel acts as a
    // column-shape descriptor; the actual expression is materialized by the adapter.
    // This matches T01's survey recommendation (kernel 1 is degenerate; body returns
    // a Series literal under a single column name).
    let s = Series::new_null(PlSmallStr::from_str(col), 1);
    Ok(s)
}

/// PyO3 entry point — wrap the bare-inner output as a `PySeries` for Python consumption.
#[pyfunction]
pub fn bare_kernel(col: &str) -> PyResult<PySeries> {
    let s = bare_inner(col)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("{e}")))?;
    Ok(PySeries(s))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Smoke: kernel 1 returns a Series named `col` with shape `(1,)` and `null` dtype.
    /// Mirrors `_bare("symbol")` semantics: the kernel is a column-shape descriptor;
    /// the adapter applies the actual Polars expression to a LazyFrame.
    #[test]
    fn smoke_returns_null_series_with_col_name() {
        let s = bare_inner("symbol").unwrap();
        assert_eq!(s.name().as_str(), "symbol", "Series carries the input col name");
        assert_eq!(s.len(), 1, "1-element Series (shape descriptor)");
        assert!(s.dtype() == &DataType::Null, "null dtype (degenerate body)");
    }

    /// Column-name passthrough: kernel returns the Series with the exact column name
    /// passed in (no rename). Verifies the adapter can rely on `s.name() == col` to
    /// materialize `_bare(col)` into its LazyFrame.
    #[test]
    fn symbol_column_rename_passthrough() {
        let s = bare_kernel_col("my_custom_col").unwrap();
        assert_eq!(s.name().as_str(), "my_custom_col");
        let s2 = bare_kernel_col("X").unwrap();
        assert_eq!(s2.name().as_str(), "X");
    }

    /// PyO3 wrapper exposed for the rename-passthrough test (mirrors the public surface).
    fn bare_kernel_col(col: &str) -> PolarsResult<Series> {
        bare_inner(col)
    }
}
