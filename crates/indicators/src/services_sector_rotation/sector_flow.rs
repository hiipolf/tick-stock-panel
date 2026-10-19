//! `sector_flow_kernel` — 1:1 port of `_load_sector_flow` (Spec 0004 §"8 Rust kernels" row 4).
//!
//! Algorithm (matches `backend/app/services/sector_rotation.py::_load_sector_flow`):
//!   1. Parse `flow_field` as "table_id.column_name". Both parts must be non-empty.
//!   2. Look for `<data_dir>/ext_data/<table_id>/part.parquet` (snapshot mode)
//!      OR `<data_dir>/ext_data/<table_id>/timeseries/<*.parquet>` (latest timeseries partition).
//!   3. Read parquet via `polars::prelude::ParquetReader::new(File::open(path)?).finish()?`
//!      (per Spec 0004 §"ADR-0006 amendment (per Q12+a)", polars crate-internal IO
//!      is allowed — kernel remains pure-compute on the resulting DataFrame).
//!   4. Detect symbol column (priority: `symbol` / `code` / `股票代码` / `代码`).
//!   5. Cast the user's flow column to Float64; drop null/non-finite/zero rows.
//!   6. `_bare` from the symbol column (trim → split(".").first()).
//!   7. `unique(subset=["_bare"], keep="last")` — last wins (per Python: 日内序列表
//!      分区按时间列升序落盘, 最后一行 = 最新一盘).
//!   8. Return (df, flow_available=true). On any failure → (empty df, false).
//!
//! Output schema: `_bare: String, _flow: Float64` (2 columns × N rows; 0 rows when unavailable).

use std::fs::File;
use std::path::{Path, PathBuf};

use polars::prelude::*;
use pyo3::prelude::*;
use pyo3_polars::PyDataFrame;

use super::bare_symbol;

/// Pure-Rust inner — read ext parquet for the user's chosen flow column.
fn sector_flow_inner(
    data_dir: &std::path::Path,
    flow_field: &str,
) -> (DataFrame, bool) {
    // Step 1: parse "table_id.column_name".
    let (config_id, column) = match parse_flow_field(flow_field) {
        Some((t, c)) => (t, c),
        None => return (empty_flow_df(), false),
    };

    // Step 2: locate the parquet (snapshot or latest timeseries).
    let path = match locate_ext_parquet(data_dir, &config_id) {
        Some(p) => p,
        None => return (empty_flow_df(), false),
    };

    // Step 3: read parquet.
    let df = match File::open(&path) {
        Ok(f) => match ParquetReader::new(f).finish() {
            Ok(d) => d,
            Err(_) => return (empty_flow_df(), false),
        },
        Err(_) => return (empty_flow_df(), false),
    };

    if df.height() == 0 {
        return (empty_flow_df(), false);
    }
    if !df.schema().iter_names().any(|n| n.as_str() == column.as_str()) {
        return (empty_flow_df(), false);
    }

    // Step 4: detect symbol column.
    let sym_col_name = match detect_symbol_column(&df) {
        Some(c) => c,
        None => return (empty_flow_df(), false),
    };

    let n = df.height();
    let sym_col = df.column(&sym_col_name).unwrap();
    let sym_str = match sym_col.str() {
        Ok(s) => s,
        Err(_) => return (empty_flow_df(), false),
    };

    // Cast the user's flow column to Float64.
    let flow_col = match df.column(&column) {
        Ok(c) => match c.cast(&DataType::Float64) {
            Ok(c) => c,
            Err(_) => return (empty_flow_df(), false),
        },
        Err(_) => return (empty_flow_df(), false),
    };
    let flow_f64 = match flow_col.f64() {
        Ok(f) => f,
        Err(_) => return (empty_flow_df(), false),
    };

    // Step 5-6: filter finite + non-zero + non-null; compute _bare.
    let mut kept_bares: Vec<String> = Vec::new();
    let mut kept_flows: Vec<f64> = Vec::new();
    let mut seen: std::collections::HashSet<String> = std::collections::HashSet::new();
    // Walk in reverse so the LAST occurrence per `_bare` wins on `unique(keep="last")`.
    for i in (0..n).rev() {
        let bare = bare_symbol(sym_str.get(i).unwrap_or(""));
        if bare.is_empty() {
            continue;
        }
        if let Some(v) = flow_f64.get(i) {
            if v.is_finite() && v != 0.0 && !seen.contains(&bare) {
                seen.insert(bare.clone());
                kept_bares.push(bare);
                kept_flows.push(v);
            }
        }
    }

    if kept_bares.is_empty() {
        return (empty_flow_df(), false);
    }

    let bare_refs: Vec<&str> = kept_bares.iter().map(|s| s.as_str()).collect();
    let out = df![
        "_bare" => bare_refs,
        "_flow" => kept_flows,
    ].expect("sector_flow DataFrame construction");
    (out, true)
}

/// Empty DataFrame for the (un)available flow column.
fn empty_flow_df() -> DataFrame {
    let bares: Vec<&str> = vec![];
    let flows: Vec<f64> = vec![];
    df![
        "_bare" => bares,
        "_flow" => flows,
    ].expect("empty flow DataFrame construction")
}

fn parse_flow_field(flow_field: &str) -> Option<(String, String)> {
    if flow_field.is_empty() || !flow_field.contains('.') {
        return None;
    }
    let (config_id, rest) = flow_field.split_once('.')?;
    let column = rest.trim_start_matches('.'); // handle "a..b" defensively
    if config_id.is_empty() || column.is_empty() {
        return None;
    }
    Some((config_id.to_string(), column.to_string()))
}

fn locate_ext_parquet(data_dir: &Path, config_id: &str) -> Option<PathBuf> {
    let base = data_dir.join("ext_data").join(config_id);
    let snapshot = base.join("part.parquet");
    if snapshot.is_file() {
        return Some(snapshot);
    }
    // Timeseries: walk recursively, take latest by sorted name.
    let ts_root = base.join("timeseries");
    if ts_root.is_dir() {
        let mut candidates: Vec<PathBuf> = Vec::new();
        collect_parquets(&ts_root, &mut candidates);
        if !candidates.is_empty() {
            candidates.sort();
            return Some(candidates.last().unwrap().clone());
        }
    }
    None
}

fn collect_parquets(dir: &Path, out: &mut Vec<PathBuf>) {
    if let Ok(rd) = std::fs::read_dir(dir) {
        for entry in rd.flatten() {
            let p = entry.path();
            if p.is_dir() {
                collect_parquets(&p, out);
            } else if p.extension().and_then(|s| s.to_str()) == Some("parquet") {
                out.push(p);
            }
        }
    }
}

fn detect_symbol_column(df: &DataFrame) -> Option<String> {
    for c in ["symbol", "code", "股票代码", "代码"] {
        if df.schema().iter_names().any(|n| n.as_str() == c) {
            return Some(c.to_string());
        }
    }
    None
}

/// PyO3 entry point — extract PathBuf + &str, call inner, return (PyDataFrame, bool).
#[pyfunction]
pub fn sector_flow_kernel(
    data_dir: PathBuf,
    flow_field: &str,
) -> PyResult<(PyDataFrame, bool)> {
    let (df, flow_available) = sector_flow_inner(&data_dir, flow_field);
    Ok((PyDataFrame(df), flow_available))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::sync::atomic::{AtomicUsize, Ordering};

    static TEST_COUNTER: AtomicUsize = AtomicUsize::new(0);

    /// Build an ext parquet snapshot at `<base>/ext_data/<config_id>/part.parquet`.
    fn make_ext_fixture(name: &str, config_id: &str, rows: &[(&str, &str, f64)]) -> PathBuf {
        let n = TEST_COUNTER.fetch_add(1, Ordering::SeqCst);
        let base = std::env::temp_dir().join(format!("tf_rs_sector_flow_test_{name}_{n}"));
        let _ = fs::remove_dir_all(&base);
        let part = base.join("ext_data").join(config_id).join("part.parquet");
        fs::create_dir_all(part.parent().unwrap()).unwrap();
        let syms: Vec<String> = rows.iter().map(|r| r.0.to_string()).collect();
        let sym_refs: Vec<&str> = syms.iter().map(|s| s.as_str()).collect();
        let _cols: Vec<String> = rows.iter().map(|r| r.1.to_string()).collect();
        let flows: Vec<f64> = rows.iter().map(|r| r.2).collect();
        let df = df![
            "symbol" => sym_refs,
            "fund_inflow" => flows,
        ].unwrap();
        let mut f = File::create(&part).unwrap();
        ParquetWriter::new(&mut f).finish(&mut df.clone()).unwrap();
        base
    }

    /// Smoke: snapshot ext parquet with `symbol` column + `fund_inflow` column.
    /// Verify (df, true) with `_bare` derived + non-zero/finite filter applied.
    #[test]
    fn smoke_snapshot_flow_available_true() {
        let dir = make_ext_fixture("smoke", "fuyao", &[
            ("600000.SH", "fund_inflow", 1.5e7),
            ("000001.SZ", "fund_inflow", 2.3e7),
            ("830799.BJ", "fund_inflow", 8.0e6),
            // zero-value row (should be filtered).
            ("600999.SH", "fund_inflow", 0.0),
            // empty bare (should be filtered).
            (".", "fund_inflow", 1.0e7),
        ]);
        let (df, available) = sector_flow_inner(&dir, "fuyao.fund_inflow");
        assert!(available, "flow_available=true on valid parquet");
        assert_eq!(df.width(), 2, "_bare + _flow");
        // 5 rows in source; 2 filtered (zero + empty bare) → 3 kept.
        assert_eq!(df.height(), 3);

        let bares: Vec<&str> = df
            .column("_bare").unwrap().str().unwrap()
            .iter().map(|s| s.unwrap_or("")).collect();
        let flows: Vec<f64> = df
            .column("_flow").unwrap().f64().unwrap()
            .iter().map(|v| v.unwrap_or(0.0)).collect();
        // unique(keep="last"): per bare, the LAST occurrence in source order wins.
        // Source order: 600000 (i=0), 000001 (i=1), 830799 (i=2), 600999 (filtered: 0.0), "." (filtered: empty bare).
        // unique keep="last" returns one row per unique bare in input-reversed order.
        assert_eq!(bares, vec!["830799", "000001", "600000"]);
        assert_eq!(flows, vec![8.0e6, 2.3e7, 1.5e7]);

        let _ = fs::remove_dir_all(&dir);
    }

    /// flow_available=false branch: empty `flow_field` (no `.`).
    /// Verify (empty df, false) returned.
    #[test]
    fn flow_field_empty_returns_unavailable() {
        let n = TEST_COUNTER.fetch_add(1, Ordering::SeqCst);
        let dir = std::env::temp_dir().join(format!("tf_rs_sector_flow_test_empty_{n}"));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let (df, available) = sector_flow_inner(&dir, "");
        assert!(!available);
        assert_eq!(df.height(), 0);
        assert_eq!(df.width(), 2);

        let _ = fs::remove_dir_all(&dir);
    }
}
