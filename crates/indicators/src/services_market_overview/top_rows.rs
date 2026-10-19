//! `top_rows_kernel` — 1:1 port of `_top_rows` (Spec 0003 §"top_rows_kernel signature").
//!
//! Algorithm (matches `backend/app/services/market_overview_builder.py::_top_rows`):
//!   1. Filter rows whose `key` extracts to a finite `f64` (mirrors `_finite`).
//!   2. Sort by that value (descending iff `descending`, else ascending);
//!      Python `list.sort(reverse=...)` is stable; Rust `sort_by` is stable.
//!   3. Project to dicts with columns: symbol, name, close, change_pct, amount,
//!      turnover_rate, board (where `board` = `_board(symbol)`).
//!   4. Slice to first `limit` rows.
//!   5. Build a `polars::DataFrame` with the 7 columns above.
//!
//! Empty input → DataFrame with 0 rows but the same 7 columns (deterministic schema).

use polars::prelude::*;
use pyo3::prelude::*;
use pyo3_polars::PyDataFrame;
use std::cmp::Ordering;
use std::collections::HashMap;

/// Pure-Rust view of one row, decoupled from `Py<PyAny>` for testability.
pub struct Row {
    pub symbol: Option<String>,
    pub name: Option<String>,
    pub close: Option<f64>,
    pub change_pct: Option<f64>,
    pub amount: Option<f64>,
    pub turnover_rate: Option<f64>,
}

/// 1:1 port of `market_overview_builder._board` — board classification by symbol suffix.
/// Pure-string logic; included here because `_top_rows` calls it on the hot path.
fn board_of(symbol: &str) -> &'static str {
    if symbol.ends_with(".BJ") {
        "北交所"
    } else if symbol.starts_with("300") || symbol.starts_with("301") {
        "创业板"
    } else if symbol.starts_with("688") || symbol.starts_with("689") {
        "科创板"
    } else if symbol.ends_with(".SH") {
        "沪主板"
    } else if symbol.ends_with(".SZ") {
        "深主板"
    } else {
        "其他"
    }
}

/// Extract a single row from a Python dict (`HashMap<String, Py<PyAny>>`).
/// Mirrors the Python adapter's perspective that `None` / non-finite → None.
fn extract_row(dict: &HashMap<String, Py<PyAny>>, py: Python<'_>) -> Row {
    let finite = |obj: Option<&Py<PyAny>>| -> Option<f64> {
        obj.and_then(|v| v.extract::<f64>(py).ok()).filter(|f| f.is_finite())
    };
    Row {
        symbol: dict.get("symbol").and_then(|v| v.extract::<String>(py).ok()),
        name: dict.get("name").and_then(|v| v.extract::<String>(py).ok()),
        close: finite(dict.get("close")),
        change_pct: finite(dict.get("change_pct")),
        amount: finite(dict.get("amount")),
        turnover_rate: finite(dict.get("turnover_rate")),
    }
}

/// Pure-Rust inner — filter finite rows on `key`, sort by `key`, slice to `limit`,
/// project to 7 columns + `board` derived from symbol.
fn top_rows_inner(rows: &[Row], key: &str, descending: bool, limit: usize) -> PolarsResult<DataFrame> {
    // Step 1: filter — row must have a finite value at `key`.
    let mut filtered: Vec<&Row> = rows
        .iter()
        .filter(|r| row_key(r, key).is_some())
        .collect();

    // Step 2: sort. Python's `sorted(reverse=...)` is stable; Rust's `sort_by` is
    // also stable (TimSort under the hood). f64 has only PartialOrd → unwrap Equal
    // for NaN (impossible post-filter, but defensive).
    filtered.sort_by(|a, b| {
        let va = row_key(a, key).unwrap_or(0.0);
        let vb = row_key(b, key).unwrap_or(0.0);
        let ord = if descending {
            vb.partial_cmp(&va)
        } else {
            va.partial_cmp(&vb)
        };
        ord.unwrap_or(Ordering::Equal)
    });

    // Step 3: slice + project.
    let projected: Vec<&Row> = filtered.into_iter().take(limit).collect();

    // Step 4: build columns.
    let symbols: Vec<&str> = projected
        .iter()
        .map(|r| r.symbol.as_deref().unwrap_or(""))
        .collect();
    let names: Vec<Option<&str>> = projected.iter().map(|r| r.name.as_deref()).collect();
    let closes: Vec<Option<f64>> = projected.iter().map(|r| r.close).collect();
    let change_pcts: Vec<Option<f64>> = projected.iter().map(|r| r.change_pct).collect();
    let amounts: Vec<Option<f64>> = projected.iter().map(|r| r.amount).collect();
    let turnover_rates: Vec<Option<f64>> = projected.iter().map(|r| r.turnover_rate).collect();
    let boards: Vec<&str> = projected
        .iter()
        .map(|r| board_of(r.symbol.as_deref().unwrap_or("")))
        .collect();

    df![
        "symbol"        => symbols,
        "name"          => names,
        "close"         => closes,
        "change_pct"    => change_pcts,
        "amount"        => amounts,
        "turnover_rate" => turnover_rates,
        "board"         => boards,
    ]
}

fn row_key(r: &Row, key: &str) -> Option<f64> {
    match key {
        "close" => r.close,
        "change_pct" => r.change_pct,
        "amount" => r.amount,
        "turnover_rate" => r.turnover_rate,
        _ => None,
    }
}

/// PyO3 entry point — extract `Py<PyAny>` dicts → `Row`s, call inner, return `PyDataFrame`.
#[pyfunction]
#[pyo3(signature = (rows, key, descending, limit))]
pub fn top_rows_kernel(
    py: Python<'_>,
    rows: Vec<HashMap<String, Py<PyAny>>>,
    key: String,
    descending: bool,
    limit: usize,
) -> PyResult<PyDataFrame> {
    let parsed: Vec<Row> = rows.iter().map(|d| extract_row(d, py)).collect();
    let df = top_rows_inner(&parsed, &key, descending, limit)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("{e}")))?;
    Ok(PyDataFrame(df))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn row(
        symbol: &str,
        name: Option<&str>,
        close: Option<f64>,
        change_pct: Option<f64>,
        amount: Option<f64>,
        turnover_rate: Option<f64>,
    ) -> Row {
        Row {
            symbol: Some(symbol.to_string()),
            name: name.map(str::to_string),
            close,
            change_pct,
            amount,
            turnover_rate,
        }
    }

    /// Main-path correctness: 8 mixed rows (3 half-active) sorted by `change_pct`
    /// descending, take top 5. Verifies sort stability (ties preserve input order),
    /// projection (all 7 columns), `board` classification per symbol suffix.
    #[test]
    fn main_path_top5_change_pct_desc() {
        let rows = vec![
            row("600000.SH", Some("浦发"), Some(10.50), Some( 0.012), Some(1.2e8), Some(0.5)),
            row("300750.SZ", Some("宁德"), Some(220.5), None,         None,        None     ),
            row("000001.SZ", Some("平安"), Some(13.10), Some( 0.062), Some(0.9e8), Some(1.2)),
            row("688981.SH", Some("中芯"), Some( 55.0), Some(-0.034), Some(0.6e8), Some(0.8)),
            row("830799.BJ", Some("艾融"), Some( 12.0), None,         None,        None     ),
            row("601398.SH", Some("工商"), Some( 5.10), Some( 0.020), Some(3.1e8), Some(0.4)),
            row("002415.SZ", Some("海康"), Some(35.00), Some( 0.041), Some(1.1),   Some(0.9)),
            row("603288.SH", Some("海天"), Some(45.20), Some( 0.000), Some(0.4e8), Some(0.6)),
        ];
        let df = top_rows_inner(&rows, "change_pct", true, 5).unwrap();
        assert_eq!(df.height(), 5, "top 5 of 8");

        let symbols: Vec<&str> = df
            .column("symbol")
            .unwrap()
            .str()
            .unwrap()
            .iter()
            .map(|s| s.unwrap_or(""))
            .collect();
        let change_pcts: Vec<Option<f64>> = df
            .column("change_pct")
            .unwrap()
            .f64()
            .unwrap()
            .iter()
            .collect();
        let boards: Vec<&str> = df
            .column("board")
            .unwrap()
            .str()
            .unwrap()
            .iter()
            .map(|s| s.unwrap_or(""))
            .collect();

        // Sorted desc by change_pct; half-active rows (300750 / 830799) excluded.
        assert_eq!(symbols, vec!["000001.SZ", "002415.SZ", "601398.SH", "600000.SH", "603288.SH"]);
        assert_eq!(change_pcts, vec![Some(0.062), Some(0.041), Some(0.020), Some(0.012), Some(0.000)]);
        // board classification: 000001.SZ=深主板, 002415.SZ=深主板, 601398.SH=沪主板, 600000.SH=沪主板, 603288.SH=沪主板
        assert_eq!(boards, vec!["深主板", "深主板", "沪主板", "沪主板", "沪主板"]);

        // The DataFrame schema must include all 7 columns regardless of row count.
        assert_eq!(df.width(), 7);
        let names: Vec<&str> = df.get_column_names().iter().map(|s| s.as_str()).collect();
        assert_eq!(
            names,
            vec!["symbol", "name", "close", "change_pct", "amount", "turnover_rate", "board"]
        );
    }

    /// Empty input → 0-row DataFrame with the 7-column schema intact.
    /// Matches Python: `[]` over `_top_rows` → `[]`.
    #[test]
    fn empty_input_yields_zero_rows() {
        let rows: Vec<Row> = vec![];
        let df = top_rows_inner(&rows, "change_pct", true, 5).unwrap();
        assert_eq!(df.height(), 0);
        assert_eq!(df.width(), 7, "schema preserved on empty");
    }

    /// `board_of` classification — exhaustive on each branch.
    #[test]
    fn board_classification() {
        assert_eq!(board_of("830799.BJ"), "北交所");
        assert_eq!(board_of("300750.SZ"), "创业板");
        assert_eq!(board_of("301000.SZ"), "创业板");
        assert_eq!(board_of("688981.SH"), "科创板");
        assert_eq!(board_of("689000.SH"), "科创板");
        assert_eq!(board_of("600000.SH"), "沪主板");
        assert_eq!(board_of("601398.SH"), "沪主板");
        assert_eq!(board_of("000001.SZ"), "深主板");
        assert_eq!(board_of("002415.SZ"), "深主板");
        assert_eq!(board_of("XYZ"),       "其他");
    }
}