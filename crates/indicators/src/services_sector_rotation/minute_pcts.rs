//! `minute_pcts_kernel` — 1:1 port of `_minute_pcts` (Spec 0004 §"8 Rust kernels" row 3).
//!
//! Algorithm (matches `backend/app/services/sector_rotation.py::_minute_pcts`):
//!   1. Read parquet for `<data_dir>/kline_minute/date=<target>/part.parquet` via
//!      `polars::prelude::ParquetReader::new(File::open(path)?).finish()?`
//!      (per Spec 0004 §"ADR-0006 amendment (per Q12+a)", polars crate-internal IO
//!      is allowed — the kernel remains pure-compute on the resulting `DataFrame`).
//!   2. Drop rows where `close` is null / non-finite / `<= 0` (else pct = inf / -100% spike).
//!   3. Compute `_bare` from `symbol` (trim → split(".").first()).
//!   4. Compute `prev_close` per bare symbol from `prev_daily_close_kernel`
//!      (delegated to `prev_daily_close::prev_daily_close_inner`).
//!   5. Compute `_ref = coalesce(prev_close, first_close_of_day)` and `_pct = close/ref - 1`.
//!   6. Determine `basis` ∈ {"prev_close", "first_close", "mixed"} based on which refs are non-null.
//!   7. Return `(DataFrame {symbol, time, pct}, basis, has_amount)`.
//!
//! Output schema:
//!   - `symbol: String` (full symbol — adapter re-applies _bare as needed)
//!   - `time: Datetime` (original minute timestamp)
//!   - `pct: Float64` (per-minute change_pct)
//!   - `basis: String` ("prev_close" / "first_close" / "mixed")
//!   - `has_amount: bool`
//!
//! On missing partition / read failure: returns `(None, "minute_schema"|"minute_empty", has_amount)`.

use std::fs::File;
use std::path::PathBuf;

use polars::prelude::*;
use pyo3::prelude::*;
use pyo3_polars::PyDataFrame;

use crate::services_sector_rotation::prev_daily_close::prev_daily_close_inner;
use super::bare_symbol;

/// Pure-Rust inner — read minute partition, derive `_pct`, return (df, basis, has_amount).
/// Returns `(None, "minute_schema", false)` on read failure / missing partition.
fn minute_pcts_inner(
    data_dir: &std::path::Path,
    target: &str,
) -> (Option<DataFrame>, String, bool) {
    let path = data_dir
        .join("kline_minute")
        .join(format!("date={target}"))
        .join("part.parquet");

    // Step 1: read parquet via polars::prelude::ParquetReader.
    let bars = match File::open(&path) {
        Ok(f) => match ParquetReader::new(f).finish() {
            Ok(df) => df,
            Err(_) => return (None, "minute_schema".to_string(), false),
        },
        Err(_) => return (None, "minute_schema".to_string(), false),
    };

    let n = bars.height();
    if n == 0 {
        return (None, "minute_empty".to_string(), false);
    }

    // Detect required columns: datetime, close, symbol; amount is optional.
    let dt_col = match bars.column("datetime") {
        Ok(c) => c,
        Err(_) => return (None, "minute_schema".to_string(), false),
    };
    let close_col = match bars.column("close") {
        Ok(c) => c,
        Err(_) => return (None, "minute_schema".to_string(), false),
    };
    let symbol_col = match bars.column("symbol") {
        Ok(c) => c,
        Err(_) => return (None, "minute_schema".to_string(), false),
    };
    let amount_col_opt = bars.column("amount").ok();
    let has_amount = amount_col_opt.is_some();

    // Compute _bare per row.
    let symbol_str = symbol_col.str().unwrap_or_else(|_| {
        panic!("symbol column must be str")
    });
    let mut bares: Vec<String> = Vec::with_capacity(n);
    for i in 0..n {
        bares.push(bare_symbol(symbol_str.get(i).unwrap_or("")));
    }

    // Cast close to Float64 if needed.
    let close_f64 = match close_col.cast(&DataType::Float64) {
        Ok(c) => match c.f64() {
            Ok(ca) => ca.clone(),
            Err(_) => return (None, "minute_schema".to_string(), false),
        },
        Err(_) => return (None, "minute_schema".to_string(), false),
    };

    // Filter on close: keep only rows where close is finite and > 0.
    let mut keep_idx: Vec<usize> = Vec::with_capacity(n);
    for i in 0..n {
        if let Some(v) = close_f64.get(i) {
            if v.is_finite() && v > 0.0 {
                keep_idx.push(i);
            }
        }
    }
    if keep_idx.is_empty() {
        return (None, "minute_empty".to_string(), has_amount);
    }

    // Project datetime as i64 (epoch ms via physical repr).
    // Accept either Datetime(ms) or Int64 (test fixtures may write Int64).
    let dt_kept: Vec<Option<i64>> = match dt_col.dtype() {
        DataType::Datetime(_, _) => {
            let phys = dt_col.to_physical_repr();
            let arr = phys.i64().expect("datetime phys i64");
            keep_idx.iter().map(|&i| arr.get(i)).collect()
        }
        DataType::Int64 => {
            let arr = dt_col.i64().expect("int64 column");
            keep_idx.iter().map(|&i| arr.get(i)).collect()
        }
        _ => return (None, "minute_schema".to_string(), false),
    };
    let close_kept: Vec<Option<f64>> = keep_idx.iter().map(|&i| close_f64.get(i)).collect();
    let amount_kept: Vec<Option<f64>> = if let Some(amt) = amount_col_opt {
        match amt.cast(&DataType::Float64) {
            Ok(casted) => match casted.f64() {
                Ok(ca) => keep_idx.iter().map(|&i| ca.get(i)).collect(),
                Err(_) => vec![None; keep_idx.len()],
            },
            Err(_) => vec![None; keep_idx.len()],
        }
    } else {
        vec![None; keep_idx.len()]
    };
    let bares_kept: Vec<String> = keep_idx.iter().map(|&i| bares[i].clone()).collect();
    let bares_kept_refs: Vec<&str> = bares_kept.iter().map(|s| s.as_str()).collect();

    // Step 4: prev_close per bare symbol.
    let prev = prev_daily_close_inner(data_dir, target);
    let prev_map: std::collections::HashMap<String, f64> = match prev {
        Ok(pdf) => {
            let pdf_bares = pdf.column("_bare").ok().and_then(|c| c.str().ok());
            let pdf_closes = pdf.column("prev_close").ok().and_then(|c| c.f64().ok());
            if let (Some(b), Some(c)) = (pdf_bares, pdf_closes) {
                let mut m = std::collections::HashMap::new();
                for i in 0..pdf.height() {
                    if let (Some(k), Some(v)) = (b.get(i), c.get(i)) {
                        m.entry(k.to_string()).or_insert(v);
                    }
                }
                m
            } else {
                std::collections::HashMap::new()
            }
        }
        Err(_) => std::collections::HashMap::new(),
    };

    // Step 5: _ref = coalesce(prev_close, first_close_of_day) per bare symbol.
    let mut first_close_map: std::collections::HashMap<String, f64> =
        std::collections::HashMap::new();
    let mut rows: Vec<(i64, String, f64)> = Vec::with_capacity(bares_kept.len());
    for i in 0..bares_kept.len() {
        if let (Some(dt), Some(c)) = (dt_kept[i], close_kept[i]) {
            rows.push((dt, bares_kept[i].clone(), c));
        }
    }
    rows.sort_by_key(|r| r.0);
    for (_, bare, close) in rows {
        first_close_map.entry(bare).or_insert(close);
    }

    let total = bares_kept.len();
    let mut refs: Vec<Option<f64>> = Vec::with_capacity(total);
    let mut pcts: Vec<Option<f64>> = Vec::with_capacity(total);
    let mut n_prev_set = 0usize;
    let mut n_first_only = 0usize;
    for i in 0..total {
        let bare = bares_kept[i].as_str();
        let prev_v = prev_map.get(bare).copied();
        let first_v = first_close_map.get(bare).copied();
        let ref_v = prev_v.or(first_v);
        match (prev_v, first_v) {
            (Some(_), _) => n_prev_set += 1,
            (None, Some(_)) => n_first_only += 1,
            _ => {}
        }
        refs.push(ref_v);
        if let (Some(r), Some(c)) = (ref_v, close_kept[i]) {
            if r > 0.0 && c.is_finite() {
                pcts.push(Some(c / r - 1.0));
            } else {
                pcts.push(None);
            }
        } else {
            pcts.push(None);
        }
    }

    let basis = if n_prev_set == total {
        "prev_close"
    } else if n_first_only == total {
        "first_close"
    } else {
        "mixed"
    };

    // Build the output DataFrame: symbol (str), time (datetime ms), pct (f64).
    let time_series = Series::new(
        PlSmallStr::from_str("time"),
        dt_kept.iter().map(|x| x.unwrap_or(0)).collect::<Vec<i64>>(),
    )
    .cast(&DataType::Datetime(TimeUnit::Milliseconds, None))
    .expect("datetime cast");
    let pcts_series = Float64Chunked::from_slice_options(
        PlSmallStr::from_str("pct"),
        &pcts,
    )
    .into_series();
    let sym_refs: Vec<&str> = bares_kept.iter().map(|s| s.as_str()).collect();
    let symbol_series = StringChunked::from_slice(
        PlSmallStr::from_str("symbol"),
        &sym_refs,
    )
    .into_series();

    // amount column is emitted only when present (advisory; adapter can ignore if has_amount=false).
    let mut cols: Vec<polars::prelude::Column> = vec![
        symbol_series.into_column(),
        time_series.into_column(),
        pcts_series.into_column(),
    ];
    if has_amount {
        let amount_series = Float64Chunked::from_slice_options(
            PlSmallStr::from_str("amount"),
            &amount_kept,
        )
        .into_series();
        cols.push(amount_series.into_column());
    }

    let df = DataFrame::new(bares_kept.len(), cols).expect("minute_pcts DataFrame construction");

    // Allow unused variables warnings to stay quiet.
    let _ = (bares_kept_refs, refs);
    (Some(df), basis.to_string(), has_amount)
}

/// PyO3 entry point — extract PathBuf + &str, call inner, return (PyDataFrame, String, bool).
#[pyfunction]
pub fn minute_pcts_kernel(
    data_dir: PathBuf,
    target: &str,
) -> PyResult<(PyDataFrame, String, bool)> {
    let (df_opt, basis, has_amount) = minute_pcts_inner(&data_dir, target);
    let df = df_opt.unwrap_or_else(DataFrame::empty);
    Ok((PyDataFrame(df), basis, has_amount))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::sync::atomic::{AtomicUsize, Ordering};

    static TEST_COUNTER: AtomicUsize = AtomicUsize::new(0);

    /// Build a test fixture: minute partition with N symbols × T timestamps.
    /// Optionally also include `amount` column (controls has_amount branch).
    fn make_minute_fixture(
        name: &str,
        target_date: &str,
        rows: &[(&str, i64, f64, Option<f64>)], // (symbol, epoch_ms, close, amount)
        include_amount: bool,
    ) -> PathBuf {
        let n = TEST_COUNTER.fetch_add(1, Ordering::SeqCst);
        let base = std::env::temp_dir().join(format!("tf_rs_minute_pcts_test_{name}_{n}"));
        let _ = fs::remove_dir_all(&base);
        let part = base
            .join("kline_minute")
            .join(format!("date={target_date}"))
            .join("part.parquet");
        fs::create_dir_all(part.parent().unwrap()).unwrap();
        let symbols: Vec<String> = rows.iter().map(|r| r.0.to_string()).collect();
        let sym_refs: Vec<&str> = symbols.iter().map(|s| s.as_str()).collect();
        let dts: Vec<i64> = rows.iter().map(|r| r.1).collect();
        let closes: Vec<f64> = rows.iter().map(|r| r.2).collect();
        if include_amount {
            let amounts: Vec<f64> = rows.iter().filter_map(|r| r.3).collect();
            let df = df![
                "symbol" => sym_refs,
                "datetime" => dts,
                "close" => closes,
                "amount" => amounts,
            ].unwrap();
            let mut f = File::create(&part).unwrap();
            ParquetWriter::new(&mut f).finish(&mut df.clone()).unwrap();
        } else {
            let df = df![
                "symbol" => sym_refs,
                "datetime" => dts,
                "close" => closes,
            ].unwrap();
            let mut f = File::create(&part).unwrap();
            ParquetWriter::new(&mut f).finish(&mut df.clone()).unwrap();
        }
        base
    }

    /// Smoke + has_amount=true branch:
    ///   - 2 symbols × 3 timestamps (with prev_close available via kline_daily).
    ///   - Verify output is non-empty DataFrame with 3 cols (symbol, time, pct),
    ///     has_amount=true, basis in {"prev_close","first_close","mixed"}.
    #[test]
    fn smoke_with_amount() {
        let dir = make_minute_fixture("smoke", "2026-10-19", &[
            ("600000.SH", 1_700_000_000_000, 11.0, Some(100.0)),
            ("600000.SH", 1_700_000_060_000, 11.5, Some(150.0)),
            ("600000.SH", 1_700_000_120_000, 12.0, Some(200.0)),
            ("000001.SZ", 1_700_000_000_000, 13.0, Some(200.0)),
            ("000001.SZ", 1_700_000_060_000, 13.5, Some(250.0)),
            ("000001.SZ", 1_700_000_120_000, 14.0, Some(300.0)),
        ], true);
        // Add a daily partition so prev_close is available.
        let daily = dir.join("kline_daily").join("date=2026-10-16").join("part.parquet");
        fs::create_dir_all(daily.parent().unwrap()).unwrap();
        let daily_df = df![
            "symbol" => vec!["600000.SH", "000001.SZ"],
            "close"  => vec![10.0_f64, 12.0_f64],
        ].unwrap();
        let mut f = File::create(&daily).unwrap();
        ParquetWriter::new(&mut f).finish(&mut daily_df.clone()).unwrap();

        let (df_opt, basis, has_amount) = minute_pcts_inner(&dir, "2026-10-19");
        let df = df_opt.expect("df present");
        assert!(has_amount, "has_amount=true when amount column present");
        assert_eq!(basis, "prev_close", "prev_close available for both symbols");
        // 3 cols (symbol/time/pct) + optional amount (4 cols when has_amount=true).
        assert_eq!(df.width(), 4, "symbol / time / pct / amount");
        assert_eq!(df.height(), 6, "6 minute rows (3 per symbol)");

        let pct_col = df.column("pct").unwrap().f64().unwrap();
        let pct_vals: Vec<f64> = (0..df.height()).map(|i| pct_col.get(i).unwrap_or(0.0)).collect();
        // First row of 600000: close=11.0 / ref=10.0 - 1 = 0.10
        assert!((pct_vals[0] - 0.10).abs() < 1e-9);
        // First row of 000001: close=13.0 / ref=12.0 - 1 ≈ 0.0833
        assert!((pct_vals[3] - (13.0 / 12.0 - 1.0)).abs() < 1e-9);

        let _ = fs::remove_dir_all(&dir);
    }

    /// has_amount=false branch: no `amount` column in the parquet.
    /// Verify (df, basis, false) returns with has_amount=false.
    #[test]
    fn smoke_without_amount() {
        let dir = make_minute_fixture("no_amount", "2026-10-19", &[
            ("600000.SH", 1_700_000_000_000, 11.0, None),
            ("600000.SH", 1_700_000_060_000, 11.5, None),
        ], false);
        // No daily partition → ref = first_close; basis = first_close.
        let (df_opt, basis, has_amount) = minute_pcts_inner(&dir, "2026-10-19");
        let df = df_opt.expect("df present");
        assert!(!has_amount, "has_amount=false when amount column absent");
        assert_eq!(basis, "first_close");
        // 3 cols (symbol/time/pct); no amount when has_amount=false.
        assert_eq!(df.width(), 3);
        assert_eq!(df.height(), 2);

        let pct_col = df.column("pct").unwrap().f64().unwrap();
        let pct_vals: Vec<f64> = (0..df.height()).map(|i| pct_col.get(i).unwrap_or(0.0)).collect();
        // First row: close=11.0 / ref=first_close=11.0 - 1 = 0.0
        assert!((pct_vals[0] - 0.0).abs() < 1e-9);
        // Second row: close=11.5 / ref=11.0 - 1 = 0.0454...
        assert!((pct_vals[1] - (11.5_f64 / 11.0 - 1.0)).abs() < 1e-9);

        let _ = fs::remove_dir_all(&dir);
    }

    /// Missing partition: target date not present under kline_minute.
    /// Verify (None, "minute_schema", false) is returned.
    #[test]
    fn missing_partition_returns_schema_marker() {
        let n = TEST_COUNTER.fetch_add(1, Ordering::SeqCst);
        let dir = std::env::temp_dir().join(format!("tf_rs_minute_pcts_test_missing_{n}"));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        // No kline_minute subdirectory.
        let (df_opt, basis, has_amount) = minute_pcts_inner(&dir, "2026-10-19");
        assert!(df_opt.is_none(), "no df on missing partition");
        assert_eq!(basis, "minute_schema");
        assert!(!has_amount);

        let _ = fs::remove_dir_all(&dir);
    }
}
