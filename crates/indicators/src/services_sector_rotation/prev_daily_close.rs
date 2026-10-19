//! `prev_daily_close_kernel` — 1:1 port of `_prev_daily_close` (Spec 0004 §"8 Rust kernels" row 2).
//!
//! Algorithm (matches `backend/app/services/sector_rotation.py::_prev_daily_close`):
//!   1. Look in `<data_dir>/kline_daily/` for `date=YYYY-MM-DD/part.parquet` partitions.
//!   2. Filter to partitions whose date is strictly less than `target_date`
//!      (per Path A-3 memory `raw_close ≠ previous trading day's close`:
//!      `target_date` is the date *whose previous close* is wanted, not the close
//!      of `target_date` itself).
//!   3. Read the latest such partition's parquet via `polars::prelude::ParquetReader`
//!      (per Spec 0004 §"ADR-0006 amendment (per Q12+a)", `polars` crate-internal IO
//!      is allowed — the kernel remains pure-compute on the resulting `DataFrame`).
//!      If the schema provides a `prev_close` column directly, use it (fast path).
//!      Otherwise project `(symbol, close) → (_bare, prev_close)` via Polars expression.
//!   4. Drop `prev_close <= 0` or non-finite rows (else pct = inf blows up JSON).
//!   5. `unique(subset=["_bare"], keep="last")` — last wins on duplicate bare symbols.
//!   6. Return the resulting `DataFrame`; empty (0-row, 2-col) if no partitions or schema missing.
//!
//! Output schema: `_bare: String, prev_close: Float64` (2 columns × N rows; 0 rows on missing partition).

use std::fs::File;
use std::path::PathBuf;

use polars::prelude::*;
use pyo3::prelude::*;
use pyo3_polars::PyDataFrame;

use super::bare_symbol;

// Re-export the inner function so other kernels can call it (per Spec 0004 T02 kernel 3
// delegates to kernel 2).
// (no extra use; the function is defined in this module and accessed via `super::prev_daily_close::prev_daily_close_inner`)

/// Pure-Rust inner — implement the parquet-read + date-offset logic.
/// `data_dir` is the repo's `data/` root; `target_date` is "YYYY-MM-DD".
/// Returns a 0-row DataFrame with the declared schema on missing partition / read failure.
pub(crate) fn prev_daily_close_inner(data_dir: &std::path::Path, target_date: &str) -> PolarsResult<DataFrame> {
    let daily = data_dir.join("kline_daily");

    // Step 1-2: enumerate partition dates strictly less than target_date.
    let prev_date = match latest_date_lt(&daily, target_date) {
        Some(d) => d,
        None => return empty_prev_close_df(),
    };

    // Step 3: read parquet for `prev_date`. Per T01 implementation note: we first
    // check the parquet schema for a `prev_close` column; if present, use it directly
    // (avoids the date-offset read). Otherwise read the date-X parquet and project
    // to (symbol, close), then apply the bare rename and prev_close filter.
    let part_path = daily.join(format!("date={prev_date}")).join("part.parquet");
    let df = match ParquetReader::new(File::open(&part_path)?).finish() {
        Ok(d) => d,
        Err(_) => return empty_prev_close_df(),
    };

    // Detect `prev_close` column presence (the schema-fast-path).
    let has_prev_close_col = df.schema().iter_names().any(|n| n.as_str() == "prev_close");
    if has_prev_close_col {
        // Schema already has prev_close — project (symbol, prev_close) → (_bare, prev_close).
        let df = df.select(["symbol", "prev_close"])?;
        return finalize_prev_close(df);
    }

    // Default: project (symbol, close) → derive (_bare, prev_close).
    let df = df.select(["symbol", "close"])?;
    finalize_prev_close(df)
}

/// Build an empty 0-row DataFrame with the declared schema.
fn empty_prev_close_df() -> PolarsResult<DataFrame> {
    let bares: Vec<&str> = vec![];
    let closes: Vec<f64> = vec![];
    df![
        "_bare" => bares,
        "prev_close" => closes,
    ]
}

/// Apply `_bare` rename + finite+positive filter + last-wins unique on `_bare`.
/// Mirrors Python:
///     df.with_columns(_bare().alias("_bare"))
///       .select([pl.col("_bare"), pl.col("close").cast(Float64).alias("prev_close")])
///       .filter(prev_close.is_finite() & (prev_close > 0))
///       .unique(subset=["_bare"], keep="last")
fn finalize_prev_close(df: DataFrame) -> PolarsResult<DataFrame> {
    // 1. Compute `_bare` from `symbol`: cast(String) → trim → split(".").first()
    //    polars 0.55 expr: `col("symbol").cast(String).str.strip_chars().str.split(".").list.first()`.
    let n = df.height();
    let symbol_col = df.column("symbol")?.str()?;
    let bares: Vec<String> = (0..n)
        .map(|i| bare_symbol(symbol_col.get(i).unwrap_or("")))
        .collect();
    let bare_refs: Vec<&str> = bares.iter().map(|s| s.as_str()).collect();

    // 2. Read close (or prev_close) column, filter to finite+positive.
    let src_name = if df.schema().iter_names().any(|n| n.as_str() == "prev_close") {
        "prev_close"
    } else {
        "close"
    };
    let val_col = df.column(src_name)?.cast(&DataType::Float64)?;
    let val_f64 = val_col.f64()?;
    let vals: Vec<Option<f64>> = (0..n)
        .map(|i| val_f64.get(i).filter(|v| v.is_finite() && *v > 0.0))
        .collect();

    // 3. Build filtered (_bare, prev_close) DataFrame; then unique keep="last".
    //    Filter is by index (None entries in `vals` are dropped).
    let mut kept_bares: Vec<&str> = Vec::new();
    let mut kept_vals: Vec<f64> = Vec::new();
    for (b, v) in bare_refs.iter().zip(vals.iter()) {
        if let Some(v) = v {
            kept_bares.push(*b);
            kept_vals.push(*v);
        }
    }
    let out = df![
        "_bare" => kept_bares,
        "prev_close" => kept_vals,
    ]?;

    // 4. unique on _bare keep="last" — polars 0.55 unique with subset.
    //    subset takes &[String] — pass `&["_bare".to_string()][..]`.
    let subset: Vec<String> = vec!["_bare".to_string()];
    out.unique_stable(Some(&subset), UniqueKeepStrategy::Last, None)
}

/// Find the latest `date=YYYY-MM-DD` partition in `daily` strictly less than `target_date`.
fn latest_date_lt(daily: &std::path::Path, target_date: &str) -> Option<String> {
    if !daily.exists() {
        return None;
    }
    let mut dates: Vec<String> = Vec::new();
    for entry in std::fs::read_dir(daily).ok()?.flatten() {
        let name = entry.file_name();
        let s = name.to_string_lossy();
        if let Some(date) = s.strip_prefix("date=") {
            if entry.path().join("part.parquet").is_file() && date < target_date {
                dates.push(date.to_string());
            }
        }
    }
    dates.sort();
    dates.last().cloned()
}

/// PyO3 entry point — extract PathBuf + &str, call inner, return PyDataFrame.
#[pyfunction]
pub fn prev_daily_close_kernel(data_dir: PathBuf, target_date: &str) -> PyResult<PyDataFrame> {
    let df = prev_daily_close_inner(&data_dir, target_date)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("{e}")))?;
    Ok(PyDataFrame(df))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::sync::atomic::{AtomicUsize, Ordering};

    /// Counter for unique temp dirs across tests.
    static TEST_COUNTER: AtomicUsize = AtomicUsize::new(0);

    /// Build a minimal test fixture: N partitions under `data_dir/kline_daily/`.
    /// Each partition: M symbols with `close` set. Schema mirrors real kline_daily.
    fn make_fixture(name: &str, partitions: &[(&str, &[(&str, f64)])]) -> PathBuf {
        let n = TEST_COUNTER.fetch_add(1, Ordering::SeqCst);
        let base = std::env::temp_dir().join(format!("tf_rs_prev_close_test_{name}_{n}"));
        let _ = fs::remove_dir_all(&base);
        let daily = base.join("kline_daily");
        for (date, rows) in partitions {
            let part = daily.join(format!("date={date}")).join("part.parquet");
            fs::create_dir_all(part.parent().unwrap()).unwrap();
            let symbols: Vec<String> = rows.iter().map(|(s, _)| s.to_string()).collect();
            let sym_refs: Vec<&str> = symbols.iter().map(|s| s.as_str()).collect();
            let closes: Vec<f64> = rows.iter().map(|(_, c)| *c).collect();
            let df = df![
                "symbol" => sym_refs,
                "close" => closes,
            ].unwrap();
            let mut f = File::create(&part).unwrap();
            ParquetWriter::new(&mut f).finish(&mut df.clone()).unwrap();
        }
        base
    }

    /// Smoke: 3 partitions (2026-10-15, 2026-10-16, 2026-10-19); target = 2026-10-19.
    /// Expected: read 2026-10-16 (latest partition strictly < 2026-10-19).
    /// Output: 2 rows × 2 cols (`_bare`, `prev_close`) with `_bare` derived from `symbol`.
    #[test]
    fn smoke_single_date_with_partitions() {
        let dir = make_fixture("smoke", &[
            ("2026-10-15", &[("600000.SH", 10.0), ("000001.SZ", 12.0)]),
            ("2026-10-16", &[("600000.SH", 10.5), ("000001.SZ", 12.5)]),
            ("2026-10-19", &[("600000.SH", 11.0), ("000001.SZ", 13.0)]),
        ]);
        let df = prev_daily_close_inner(&dir, "2026-10-19").unwrap();
        assert_eq!(df.width(), 2);
        assert_eq!(df.height(), 2);

        let bares: Vec<&str> = df
            .column("_bare").unwrap().str().unwrap()
            .iter().map(|s| s.unwrap_or("")).collect();
        let closes: Vec<f64> = df
            .column("prev_close").unwrap().f64().unwrap()
            .iter().map(|v| v.unwrap_or(0.0)).collect();
        // `_bare` strips the exchange suffix via split(".").first().
        assert_eq!(bares, vec!["600000", "000001"]);
        // prev_close = the 2026-10-16 close (NOT 2026-10-19 — per Path A-3 memory).
        assert_eq!(closes, vec![10.5, 12.5]);

        let _ = fs::remove_dir_all(&dir);
    }

    /// Missing partition: only the target date, no prev. Expected: 0-row DataFrame
    /// with the declared 2-col schema intact (graceful degradation).
    #[test]
    fn missing_partition_graceful_zero_rows() {
        let dir = make_fixture("missing", &[
            ("2026-10-19", &[("600000.SH", 11.0)]),  // only target date, no prev
        ]);
        let df = prev_daily_close_inner(&dir, "2026-10-19").unwrap();
        assert_eq!(df.width(), 2);
        assert_eq!(df.height(), 0);
        assert!(df.schema().iter_names().any(|n| n.as_str() == "_bare"));
        assert!(df.schema().iter_names().any(|n| n.as_str() == "prev_close"));

        let _ = fs::remove_dir_all(&dir);
    }
}
