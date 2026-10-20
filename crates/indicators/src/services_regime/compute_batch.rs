//! `compute_batch_kernel` — Path A-5 / Spec 0005 kernel 1.
//!
//! Algorithm (matches `backend/app/services/regime_builder.py::_compute_batch`):
//!   1. `warmup_start = batch_start - timedelta(days=warmup_days)`
//!   2. `LazyFrame::scan_parquet(data_dir / "**" / "*.parquet")`
//!        `.filter(date >= warmup_start AND date <= batch_end)`
//!        `.collect()`
//!
//! Returns: `DataFrame` with all enriched-daily columns for the
//! `[warmup_start, batch_end]` range (warmup rows INCLUDED so the adapter can
//! apply `with_prev_consecutive(df)` Polars expression chain per Q3=a — the
//! heavy Polars expressions stay in the Python adapter; this kernel does
//! IO + date-range filter only). The adapter then trims to
//! `[batch_start, batch_end]` per Spec 0005 §"Implementation Decisions" kernel 1
//! step 6.
//!
//! Empty case (per Spec 0005 §"Implementation Decisions" kernel 1 "Empty case"):
//! no enriched parquet exists under `data_dir` → empty `DataFrame` (no columns).
//!
//! Per **ADR-0006 §"5 段"§1+§3 Path A-4 amendment**, IO is encapsulated in the
//! polars crate (`LazyFrame::scan_parquet`); the kernel remains pure-compute on
//! the resulting `DataFrame`.
//!
//! Per **T01 implementation note 1**, glob path is constructed via
//! `format!("{}/**/*.parquet", data_dir.display())` + `PlRefPath::new` — this
//! avoids the Windows backslash normalization issue noted in
//! `polars-utils-0.55.2/src/pl_path.rs:86-87` ("Windows paths will have leading
//! `\\?\` prefix stripped, and all backslashes normalized to forward slashes").
//! PathBuf::join("**/*.parquet") does NOT normalize the glob metacharacters on
//! Windows backslashes.

use std::path::{Path, PathBuf};

use polars::prelude::*;
use pyo3::prelude::*;
use pyo3_polars::PyDataFrame;

// ---------------------------------------------------------------------------
// Date arithmetic helper (no chrono dep — Howard Hinnant's algorithms, public domain)
// ---------------------------------------------------------------------------

/// Parse an "YYYY-MM-DD" string into `(year: i32, month: u8, day: u32)`.
/// Returns `None` on malformed input or out-of-range month/day.
fn parse_iso_date(s: &str) -> Option<(i32, u8, u32)> {
    let mut parts = s.split('-');
    let y = parts.next()?.parse::<i32>().ok()?;
    let m = parts.next()?.parse::<u8>().ok()?;
    let d = parts.next()?.parse::<u32>().ok()?;
    // Month/day sanity check (loose; days-in-month is checked by the
    // `civil_from_days` round-trip — `days_from_civil` accepts any
    // `(y, m, d)` triple and produces a valid Julian day).
    if !(1..=12).contains(&m) {
        return None;
    }
    Some((y, m, d))
}

/// Format `(year, month, day)` back into "YYYY-MM-DD" with zero-padding.
fn format_iso_date(y: i32, m: u8, d: u32) -> String {
    format!("{:04}-{:02}-{:02}", y, m, d)
}

/// Convert a civil date `(y, m, d)` to days-since-Unix-epoch (1970-01-01).
/// Algorithm: Howard Hinnant's `days_from_civil` (public domain). Accepts
/// out-of-range `(m, d)` and produces a normalized epoch-day (the round-trip
/// via `civil_from_days` corrects month/day overflow).
fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = (y - era * 400) as u64; // [0, 399]
    let doy = (153 * (if m > 2 { m - 3 } else { m + 9 }) as u64 + 2) / 5 + d as u64 - 1; // [0, 365]
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy; // [0, 146096]
    era * 146097 + doe as i64 - 719468
}

/// Convert days-since-Unix-epoch (1970-01-01) to `(year, month, day)`.
/// Algorithm: Howard Hinnant's `civil_from_days` (public domain).
fn civil_from_days(z: i64) -> (i64, i64, i64) {
    let z = z + 719468;
    let era = if z >= 0 { z } else { z - 146096 } / 146097;
    let doe = (z - era * 146097) as u64; // [0, 146096]
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365; // [0, 399]
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0, 365]
    let mp = (5 * doy + 2) / 153; // [0, 11]
    let d = (doy - (153 * mp + 2) / 5 + 1) as i64; // [1, 31]
    let nm = if mp < 10 { mp + 3 } else { mp - 9 }; // [1, 12]
    let ny = if nm <= 2 { y + 1 } else { y };
    (ny, nm as i64, d)
}

/// Compute `iso_date - days` as an "YYYY-MM-DD" string. Returns `None` on parse failure.
fn subtract_days_from_iso_date(iso_date: &str, days: u32) -> Option<String> {
    let (y, m, d) = parse_iso_date(iso_date)?;
    let jd = days_from_civil(y as i64, m as i64, d as i64);
    let new_jd = jd - days as i64;
    let (ny, nm, nd) = civil_from_days(new_jd);
    Some(format_iso_date(ny as i32, nm as u8, nd as u32))
}

// ---------------------------------------------------------------------------
// Pure-Rust inner
// ---------------------------------------------------------------------------

/// Pure-Rust inner — build the glob, scan parquet lazily, filter by date range,
/// collect. Returns `Ok(df)` for happy path + empty-but-with-schema cases;
/// returns an empty 0-column DataFrame for "no parquet under data_dir" /
/// "all partitions filtered out" / "bad batch_start format" graceful cases.
pub(crate) fn compute_batch_inner(
    data_dir: &Path,
    batch_start: &str,
    batch_end: &str,
    warmup_days: u32,
) -> PolarsResult<DataFrame> {
    // Graceful: data_dir does not exist → empty DF.
    if !data_dir.exists() {
        return Ok(DataFrame::empty());
    }

    // Compute warmup_start. Graceful: bad batch_start → empty DF.
    let warmup_start = match subtract_days_from_iso_date(batch_start, warmup_days) {
        Some(s) => s,
        None => return Ok(DataFrame::empty()),
    };

    // T01 implementation note 1: forward-slash glob string + PlRefPath::new.
    // Avoids Windows backslash normalization issue noted in
    // polars-utils-0.55.2/src/pl_path.rs:86-87.
    let glob_str = format!("{}/**/*.parquet", data_dir.display());
    let glob_path = PlRefPath::new(&glob_str);

    // scan_parquet may error if the glob expands to zero files; treat that
    // as "no enriched parquet" → empty DF (graceful).
    let lf = match LazyFrame::scan_parquet(glob_path, ScanArgsParquet::default()) {
        Ok(lf) => lf,
        Err(_) => return Ok(DataFrame::empty()),
    };

    // Apply warmup + batch_end date filter. String comparison on ISO "YYYY-MM-DD"
    // is correct (lexicographic = chronological for ISO date format).
    let predicate = col("date")
        .gt_eq(lit(warmup_start.as_str()))
        .and(col("date").lt_eq(lit(batch_end)));

    match lf.filter(predicate).collect() {
        Ok(df) => Ok(df),
        Err(_) => Ok(DataFrame::empty()),
    }
}

// ---------------------------------------------------------------------------
// PyO3 entry point
// ---------------------------------------------------------------------------

/// PyO3 entry point — extract PathBuf + 3 scalars, call inner, return PyDataFrame.
///
/// Signature (verbatim per Spec 0005 §"Implementation Decisions" kernel 1):
///   `compute_batch_kernel(data_dir: PathBuf, batch_start: &str, batch_end: &str, warmup_days: u32) -> PyResult<PyDataFrame>`
#[pyfunction]
#[pyo3(signature = (data_dir, batch_start, batch_end, warmup_days))]
pub fn compute_batch_kernel(
    data_dir: PathBuf,
    batch_start: &str,
    batch_end: &str,
    warmup_days: u32,
) -> PyResult<PyDataFrame> {
    let df = compute_batch_inner(&data_dir, batch_start, batch_end, warmup_days)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("{e}")))?;
    Ok(PyDataFrame(df))
}

// ---------------------------------------------------------------------------
// Tests — 4 per Spec 0005 §"Test plan (per Q13)"
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs::{self, File};
    use std::sync::atomic::{AtomicUsize, Ordering};

    /// Counter for unique temp dirs across tests.
    static TEST_COUNTER: AtomicUsize = AtomicUsize::new(0);

    /// Build a test fixture: `data_dir/date=YYYY-MM-DD/part.parquet` partitions
    /// per the spec's enriched_dir layout (Hive-style partition naming).
    /// Each partition: a DataFrame with `(date, symbol, close, amount)` columns.
    fn make_fixture(name: &str, partitions: &[(&str, &[(&str, f64)])]) -> PathBuf {
        let n = TEST_COUNTER.fetch_add(1, Ordering::SeqCst);
        let base = std::env::temp_dir().join(format!("tf_rs_compute_batch_test_{name}_{n}"));
        let _ = fs::remove_dir_all(&base);
        fs::create_dir_all(&base).unwrap();
        for (date, rows) in partitions {
            let part_dir = base.join(format!("date={date}"));
            fs::create_dir_all(&part_dir).unwrap();
            let part_path = part_dir.join("part.parquet");
            let dates: Vec<&str> = vec![date; rows.len()];
            let symbols: Vec<String> = rows.iter().map(|(s, _)| s.to_string()).collect();
            let sym_refs: Vec<&str> = symbols.iter().map(|s| s.as_str()).collect();
            let closes: Vec<f64> = rows.iter().map(|(_, c)| *c).collect();
            // `amount` column — present even if not asserted by tests (mirrors real
            // enriched daily parquet schema).
            let amounts: Vec<f64> = vec![1.0e8; rows.len()];
            let df = df![
                "date"   => dates,
                "symbol" => sym_refs,
                "close"  => closes,
                "amount" => amounts,
            ].unwrap();
            let mut f = File::create(&part_path).unwrap();
            ParquetWriter::new(&mut f).finish(&mut df.clone()).unwrap();
        }
        base
    }

    /// Test 1 (smoke): 3 partitions on disk (2026-10-15, 2026-10-16, 2026-10-19);
    /// batch_start=2026-10-16, batch_end=2026-10-19, warmup_days=0.
    /// Expected: warmup_start = 2026-10-16; the [2026-10-16, 2026-10-19] filter
    /// INCLUDES 2 partitions (2026-10-16 + 2026-10-19) × 2 symbols = 4 rows × 4 cols.
    /// The 2026-10-15 partition is OUTSIDE the filter (before batch_start).
    #[test]
    fn smoke_single_date_with_partitions() {
        let dir = make_fixture("smoke", &[
            ("2026-10-15", &[("600000.SH", 10.0), ("000001.SZ", 12.0)]),
            ("2026-10-16", &[("600000.SH", 10.5), ("000001.SZ", 12.5)]),
            ("2026-10-19", &[("600000.SH", 11.0), ("000001.SZ", 13.0)]),
        ]);
        let df = compute_batch_inner(&dir, "2026-10-16", "2026-10-19", 0).unwrap();
        assert_eq!(df.width(), 4, "4 cols: date / symbol / close / amount");
        assert_eq!(df.height(), 4, "2 partitions in filter × 2 symbols = 4 rows");
        // Verify date column content
        let dates: Vec<&str> = df
            .column("date").unwrap().str().unwrap()
            .iter().map(|s| s.unwrap_or("")).collect();
        let mut unique_dates: Vec<&str> = dates.iter().copied().collect();
        unique_dates.sort();
        unique_dates.dedup();
        assert_eq!(unique_dates, vec!["2026-10-16", "2026-10-19"]);
        // The 2026-10-15 partition is excluded (before batch_start with warmup_days=0).
        assert!(!dates.contains(&"2026-10-15"));

        let _ = fs::remove_dir_all(&dir);
    }

    /// Test 2 (warmup boundary): 3 partitions; batch_start=2026-10-16,
    /// batch_end=2026-10-19, warmup_days=2.
    /// Expected: warmup_start = 2026-10-14, so the [2026-10-14, 2026-10-19] filter
    /// INCLUDES the 2026-10-15 partition (warmup row) and the 2026-10-16 partition
    /// (batch first day). The 2026-10-19 partition is the batch last day.
    /// The warmup-row EXCLUSION happens in the Python adapter (Spec 0005 kernel 1
    /// step 6 — the kernel returns warmup rows for prev_consecutive shift).
    #[test]
    fn warmup_boundary_includes_warmup_rows() {
        let dir = make_fixture("warmup", &[
            ("2026-10-13", &[("600000.SH", 9.5), ("000001.SZ", 11.5)]),  // outside [warmup_start, batch_end]
            ("2026-10-15", &[("600000.SH", 10.0), ("000001.SZ", 12.0)]),  // INSIDE [2026-10-14, 2026-10-19] (warmup row)
            ("2026-10-16", &[("600000.SH", 10.5), ("000001.SZ", 12.5)]),  // INSIDE (batch first day)
            ("2026-10-19", &[("600000.SH", 11.0), ("000001.SZ", 13.0)]),  // INSIDE (batch last day)
            ("2026-10-20", &[("600000.SH", 11.5), ("000001.SZ", 13.5)]),  // outside (after batch_end)
        ]);
        let df = compute_batch_inner(&dir, "2026-10-16", "2026-10-19", 2).unwrap();
        // Expected rows: 3 partitions × 2 symbols = 6 (2026-10-15, 2026-10-16, 2026-10-19).
        assert_eq!(df.height(), 6, "warmup partition 2026-10-15 included");
        let dates: Vec<&str> = df
            .column("date").unwrap().str().unwrap()
            .iter().map(|s| s.unwrap_or("")).collect();
        // All 3 dates (2026-10-15, 2026-10-16, 2026-10-19) appear; 2026-10-13 and
        // 2026-10-20 are filtered out (warmup_start boundary + batch_end boundary).
        assert!(dates.contains(&"2026-10-15"), "2026-10-15 (warmup row) INCLUDED per Spec 0005 step 6 split");
        assert!(dates.contains(&"2026-10-16"), "2026-10-16 (batch first day)");
        assert!(dates.contains(&"2026-10-19"), "2026-10-19 (batch last day)");
        assert!(!dates.contains(&"2026-10-13"), "before warmup_start");
        assert!(!dates.contains(&"2026-10-20"), "after batch_end");

        let _ = fs::remove_dir_all(&dir);
    }

    /// Test 3 (missing parquet graceful): data_dir exists but contains no parquet
    /// partitions. Expected: empty DataFrame (0 rows × 0 cols).
    #[test]
    fn missing_parquet_graceful_zero_rows() {
        let n = TEST_COUNTER.fetch_add(1, Ordering::SeqCst);
        let base = std::env::temp_dir().join(format!("tf_rs_compute_batch_test_missing_{n}"));
        let _ = fs::remove_dir_all(&base);
        fs::create_dir_all(&base).unwrap();
        // Add a non-parquet file so data_dir is non-empty but glob matches nothing.
        fs::write(base.join("README.txt"), "no parquet here").unwrap();

        let df = compute_batch_inner(&base, "2026-10-16", "2026-10-19", 0).unwrap();
        assert_eq!(df.height(), 0, "no parquet → 0 rows");
        assert_eq!(df.width(), 0, "no parquet → 0 cols (schema undefined)");

        let _ = fs::remove_dir_all(&base);
    }

    /// Test 4 (instruments missing graceful): kernel 1 doesn't read instruments.json
    /// (per Q3=a, the limit_signals step is Python pre-aggregate). Verify the kernel
    /// is INDEPENDENT of instruments.json presence — running with NO instruments.json
    /// yields the same output as running WITH instruments.json.
    #[test]
    fn instruments_missing_graceful_independent_of_instruments_json() {
        let dir = make_fixture("instr", &[
            ("2026-10-16", &[("600000.SH", 10.5), ("000001.SZ", 12.5)]),
            ("2026-10-19", &[("600000.SH", 11.0), ("000001.SZ", 13.0)]),
        ]);
        // No instruments.json written — kernel 1 must NOT crash.
        let df = compute_batch_inner(&dir, "2026-10-16", "2026-10-19", 0).unwrap();
        assert_eq!(df.height(), 4, "kernel 1 ignores instruments.json");
        assert_eq!(df.width(), 4);
        // Verify the schema has the documented columns from enriched parquet.
        let names: Vec<String> = df.schema().iter_names().map(|n| n.to_string()).collect();
        assert!(names.contains(&"date".to_string()));
        assert!(names.contains(&"symbol".to_string()));
        assert!(names.contains(&"close".to_string()));
        assert!(names.contains(&"amount".to_string()));

        let _ = fs::remove_dir_all(&dir);
    }

    // -----------------------------------------------------------------------
    // Date arithmetic sanity (proves the Hinnant algorithm is well-formed).
    // -----------------------------------------------------------------------

    #[test]
    fn subtract_days_arithmetic_sanity() {
        assert_eq!(subtract_days_from_iso_date("2026-10-19", 0).as_deref(), Some("2026-10-19"));
        assert_eq!(subtract_days_from_iso_date("2026-10-19", 1).as_deref(), Some("2026-10-18"));
        assert_eq!(subtract_days_from_iso_date("2026-10-19", 2).as_deref(), Some("2026-10-17"));
        assert_eq!(subtract_days_from_iso_date("2026-03-01", 1).as_deref(), Some("2026-02-28"));
        // Leap year boundary (2024 is leap; 2023 is not).
        assert_eq!(subtract_days_from_iso_date("2024-03-01", 1).as_deref(), Some("2024-02-29"));
        assert_eq!(subtract_days_from_iso_date("2025-03-01", 1).as_deref(), Some("2025-02-28"));
        // Year boundary.
        assert_eq!(subtract_days_from_iso_date("2026-01-01", 1).as_deref(), Some("2025-12-31"));
        // Bad input → None.
        assert!(subtract_days_from_iso_date("not-a-date", 0).is_none());
        assert!(subtract_days_from_iso_date("2026-13-01", 0).is_none()); // month=13 invalid
    }
}