//! Throwaway research example for Path A-3 service-level kernels
//! (`top_rows_kernel` / `pct_band_rows_kernel` / `dimension_rank_kernel`).
//!
//! Survey-only: builds a 3-sector × 4-stock synthetic fixture with mixed `change_pct`
//! (some `None`) and `amount` fields, exercises the 3 stub kernels in pure-Rust mode
//! (no Python runtime attached), and constructs `polars::DataFrame` outputs via
//! column-wise append. Verifies the **PyO3 0.29.3 + pyo3-polars 0.28.0 + polars 0.55.2**
//! signature shape compiles + behaves as documented in
//! `docs/research/market-overview-rust-api.md`.
//!
//! Run:
//!   cargo check --example research_market_overview_api -p tf-rs-indicators
//!   cargo run   --example research_market_overview_api -p tf-rs-indicators
//!
//! Note: `#[pyfunction]` attributes are declared on the stub functions to confirm
//! the signature compiles with pyo3-polars types (`Vec<HashMap<String, Py<PyAny>>>`,
//! `Vec<f64>`, `Vec<(String, Vec<HashMap<String, Py<PyAny>>>)>`). Production
//! registration (wrap_pyfunction! into m) happens in Ticket 02.

use std::collections::HashMap;

use polars::prelude::*;
use pyo3::prelude::*;
use pyo3_polars::PyDataFrame;

// ---------------------------------------------------------------------------
// Stub kernel signatures (production-grade signatures; bodies are T01 placeholders).
// These compile-check the PyO3 0.29.3 + pyo3-polars 0.28.0 type surface but
// are NOT invoked from `cargo run` (no Python runtime attached).
// ---------------------------------------------------------------------------

/// Stub for `_top_rows(rows, key, descending, limit) -> list[dict]`.
/// PyO3-level signature is `Vec<HashMap<String, Py<PyAny>>>` — no #[pyclass] needed.
#[pyfunction]
fn top_rows_kernel_stub(
    rows: Vec<HashMap<String, Py<PyAny>>>,
    key: String,
    descending: bool,
    limit: usize,
) -> PyResult<PyDataFrame> {
    let _ = (rows, key, descending, limit);
    Err(pyo3::exceptions::PyNotImplementedError::new_err(
        "T01 stub — Ticket 02 implements body",
    ))
}

/// Stub for `_pct_band_rows(values) -> list[dict]`.
#[pyfunction]
fn pct_band_rows_kernel_stub(values: Vec<f64>) -> PyResult<PyDataFrame> {
    let _ = values;
    Err(pyo3::exceptions::PyNotImplementedError::new_err(
        "T01 stub — Ticket 02 implements body",
    ))
}

/// Stub for `_dimension_rank(groups, limit) -> list[dict]`.
/// PyO3-level signature is `Vec<(String, Vec<HashMap<String, Py<PyAny>>>)>` —
/// group_name + per-stock dicts (no #[pyclass] needed).
#[pyfunction]
fn dimension_rank_kernel_stub(
    groups: Vec<(String, Vec<HashMap<String, Py<PyAny>>>)>,
    limit: usize,
) -> PyResult<PyDataFrame> {
    let _ = (groups, limit);
    Err(pyo3::exceptions::PyNotImplementedError::new_err(
        "T01 stub — Ticket 02 implements body",
    ))
}

// ---------------------------------------------------------------------------
// T01 pure-Rust demo path: same algorithm, no Python runtime.
// Uses an internal `Row` type (not the PyO3 surface) for ergonomic pure-Rust
// reduce without fighting HashMap<String, Option<f64>> + symbol-as-string
// type unification. Production kernels will operate on the PyO3 surface; this
// demo path is solely for surveying algorithm behaviour.
// ---------------------------------------------------------------------------

struct Row {
    symbol: String,
    close: Option<f64>,
    change_pct: Option<f64>,
    amount: Option<f64>,
}

impl Row {
    fn get(&self, key: &str) -> Option<f64> {
        match key {
            "close" => self.close,
            "change_pct" => self.change_pct,
            "amount" => self.amount,
            _ => None,
        }
    }
}

fn row(symbol: &str, close: Option<f64>, change_pct: Option<f64>, amount: Option<f64>) -> Row {
    Row { symbol: symbol.to_string(), close, change_pct, amount }
}

/// Demo body for `top_rows_kernel` on a synthetic fixture.
/// Mirrors the Python `_py_top_rows` algorithm (filter finite, slice to limit).
fn top_rows_pure(rows: &[Row], key: &str, descending: bool, limit: usize) -> Vec<(String, Option<f64>, Option<f64>)> {
    let mut filtered: Vec<&Row> = rows.iter().filter(|r| r.get(key).is_some()).collect();
    filtered.sort_by(|a, b| {
        let va = a.get(key).unwrap_or(0.0);
        let vb = b.get(key).unwrap_or(0.0);
        if descending {
            vb.partial_cmp(&va).unwrap_or(std::cmp::Ordering::Equal)
        } else {
            va.partial_cmp(&vb).unwrap_or(std::cmp::Ordering::Equal)
        }
    });
    filtered
        .into_iter()
        .take(limit)
        .map(|r| (r.symbol.clone(), r.close, r.change_pct))
        .collect()
}

/// One band in the 8-bucket scheme: label + (low_inclusive, high_exclusive).
struct PctBand {
    label: &'static str,
    low: Option<f64>,
    high: Option<f64>,
}

/// Demo body for `pct_band_rows_kernel` — 8 fixed buckets.
fn pct_band_rows_pure(values: &[f64]) -> Vec<(&'static str, u32, f64)> {
    let bands = vec![
        PctBand { label: "<-5%",  low: None,         high: Some(-0.05) },
        PctBand { label: "-5~-3%", low: Some(-0.05), high: Some(-0.03) },
        PctBand { label: "-3~-1%", low: Some(-0.03), high: Some(-0.01) },
        PctBand { label: "-1~0%",  low: Some(-0.01), high: Some(0.0) },
        PctBand { label: "0~1%",   low: Some(0.0),   high: Some(0.01) },
        PctBand { label: "1~3%",   low: Some(0.01),  high: Some(0.03) },
        PctBand { label: "3~5%",   low: Some(0.03),  high: Some(0.05) },
        PctBand { label: ">5%",    low: Some(0.05),  high: None },
    ];
    let total = (values.len() as f64).max(1.0);
    bands
        .iter()
        .map(|band| {
            let count = values
                .iter()
                .filter(|v| match (band.low, band.high) {
                    (None, Some(h)) => **v < h,
                    (Some(l), None) => **v >= l,
                    (Some(l), Some(h)) => **v >= l && **v < h,
                    _ => false,
                })
                .count() as u32;
            (band.label, count, count as f64 / total * 100.0)
        })
        .collect()
}

/// Demo body for `dimension_rank_kernel` — per-group reduce.
fn dimension_rank_pure(
    groups: &[(&str, &[Row])],
    _limit: usize,
) -> Vec<(String, u32, f64, u32, u32, f64, Option<String>, Option<f64>)> {
    groups
        .iter()
        .filter_map(|(group_name, stocks)| {
            let changes: Vec<f64> = stocks.iter().filter_map(|s| s.change_pct).collect();
            if changes.is_empty() {
                return None;
            }
            let count = stocks.len() as u32;
            let avg_pct = changes.iter().sum::<f64>() / changes.len() as f64;
            let up_count = changes.iter().filter(|v| **v > 0.0).count() as u32;
            let down_count = changes.iter().filter(|v| **v < 0.0).count() as u32;
            let amount: f64 = stocks.iter().filter_map(|s| s.amount).sum();
            let leader = stocks
                .iter()
                .max_by(|a, b| {
                    let ka = a.change_pct.unwrap_or(f64::NEG_INFINITY);
                    let kb = b.change_pct.unwrap_or(f64::NEG_INFINITY);
                    ka.partial_cmp(&kb).unwrap_or(std::cmp::Ordering::Equal)
                });
            let leader_symbol = leader.map(|l| l.symbol.clone());
            let leader_change = leader.and_then(|l| l.change_pct);
            Some((
                (*group_name).to_string(),
                count,
                avg_pct,
                up_count,
                down_count,
                amount,
                leader_symbol,
                leader_change,
            ))
        })
        .collect()
}

/// Construct a `polars::DataFrame` from the `dimension_rank_pure` output columns.
/// Mirrors what the real `dimension_rank_kernel` will do at its tail.
fn build_dimension_rank_df(
    rows: Vec<(String, u32, f64, u32, u32, f64, Option<String>, Option<f64>)>,
) -> DataFrame {
    let names: Vec<&str> = rows.iter().map(|r| r.0.as_str()).collect();
    let counts: Vec<u32> = rows.iter().map(|r| r.1).collect();
    let avg_pcts: Vec<f64> = rows.iter().map(|r| r.2).collect();
    let up_counts: Vec<u32> = rows.iter().map(|r| r.3).collect();
    let down_counts: Vec<u32> = rows.iter().map(|r| r.4).collect();
    let amounts: Vec<f64> = rows.iter().map(|r| r.5).collect();
    let leaders: Vec<Option<&str>> = rows.iter().map(|r| r.6.as_deref()).collect();
    let leader_pcts: Vec<Option<f64>> = rows.iter().map(|r| r.7).collect();

    df![
        "name"              => names,
        "count"             => counts,
        "avg_pct"           => avg_pcts,
        "up_count"          => up_counts,
        "down_count"        => down_counts,
        "amount"            => amounts,
        "leader_symbol"     => leaders,
        "leader_change_pct" => leader_pcts,
    ]
    .expect("dimension_rank DataFrame construction")
}

fn main() {
    // -----------------------------------------------------------------------
    // Synthetic fixture: 12 stocks across 3 sectors (banks / tech / consumer).
    // Mixed `change_pct` (some None) + `amount` values.
    // -----------------------------------------------------------------------
    let rows = vec![
        // Banks (4 stocks)
        row("600000.SH", Some(10.50), Some( 0.012), Some(1.2e8)),
        row("600036.SH", Some(38.20), Some(-0.005), Some(2.4e8)),
        row("601398.SH", Some( 5.10), Some( 0.020), Some(3.1e8)),
        row("601939.SH", Some( 6.85), None,         None),
        // Tech (4 stocks)
        row("000001.SZ", Some(13.10), Some( 0.062), Some(0.9e8)),
        row("000063.SZ", Some(28.90), Some(-0.034), Some(1.5e8)),
        row("002415.SZ", Some(35.00), Some( 0.041), Some(1.1e8)),
        row("300750.SZ", Some(220.5), None,         None),
        // Consumer (4 stocks)
        row("600519.SH", Some(1700.0), Some(-0.022), Some(2.0e8)),
        row("000858.SZ", Some(150.5), Some( 0.008), Some(0.7e8)),
        row("600887.SH", Some(28.30), Some( 0.015), Some(0.5e8)),
        row("603288.SH", Some(45.20), Some(-0.011), Some(0.4e8)),
    ];

    // -----------------------------------------------------------------------
    // Demo 1: top_rows_kernel on rows, sort by change_pct descending, top 5.
    // -----------------------------------------------------------------------
    let top5 = top_rows_pure(&rows, "change_pct", true, 5);
    println!("[research] top_rows_pure(change_pct, desc, 5):");
    for (sym, close, pct) in &top5 {
        println!("  {} close={:?} change_pct={:?}", sym, close, pct);
    }

    // -----------------------------------------------------------------------
    // Demo 2: pct_band_rows_kernel on a 17-element fixture.
    // Hand-computed expected:
    //   <-5%:    1 (-0.062)
    //   -5~-3%:  1 (-0.048)
    //   -3~-1%:  3 (-0.035, -0.020, -0.012)
    //   -1~0%:   2 (-0.005, -0.001)
    //   0~1%:    3 (0.000, 0.002, 0.005)
    //   1~3%:    2 (0.010, 0.018)
    //   3~5%:    2 (0.025, 0.034)
    //   >5%:     4 (0.041, 0.052, 0.063, 0.071)
    //   total:   17
    // -----------------------------------------------------------------------
    let pct_values: Vec<f64> = vec![
        -0.062, -0.048, -0.035, -0.020, -0.012, -0.005, -0.001,
         0.000,  0.002,  0.005,  0.010,  0.018,  0.025,
         0.034,  0.041,  0.052,  0.063,  0.071,
    ];
    let bands = pct_band_rows_pure(&pct_values);
    println!("[research] pct_band_rows_pure on {}-element fixture:", pct_values.len());
    for (label, count, pct) in &bands {
        println!("  {:>7}  count={:3}  pct={:6.2}%", label, count, pct);
    }

    // -----------------------------------------------------------------------
    // Demo 3: dimension_rank_kernel on 3 sector groups.
    // -----------------------------------------------------------------------
    let groups: Vec<(&str, &[Row])> = vec![
        ("Banks",    &rows[0..4]),
        ("Tech",     &rows[4..8]),
        ("Consumer", &rows[8..12]),
    ];
    let dr = dimension_rank_pure(&groups, 5);
    let df = build_dimension_rank_df(dr);
    println!("[research] dimension_rank_pure → DataFrame:");
    println!("{}", df);

    // -----------------------------------------------------------------------
    // PyO3 signature compile probe.
    // The stubs above are #[pyfunction]-annotated; we don't call them
    // (no Python runtime attached in a standalone `cargo run`).
    // The fact that this file compiles at all proves the PyO3 0.29.3
    // signature shape (`Vec<HashMap<String, Py<PyAny>>>`, `Vec<f64>`,
    // `Vec<(String, Vec<HashMap<String, Py<PyAny>>>)>`) is well-formed
    // against the vendored deps.
    // -----------------------------------------------------------------------
    let _ = (
        top_rows_kernel_stub as fn(_, _, _, _) -> _,
        pct_band_rows_kernel_stub as fn(_) -> _,
        dimension_rank_kernel_stub as fn(_, _) -> _,
    );
    println!("[research] PyO3 signature probes compiled OK.");
}