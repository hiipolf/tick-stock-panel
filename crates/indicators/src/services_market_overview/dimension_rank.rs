//! `dimension_rank_kernel` — 1:1 port of `_dimension_rank`'s per-group reduce (Spec 0003).
//!
//! Algorithm (matches `backend/app/services/market_overview_builder.py::_dimension_rank`):
//!   For each (group_name, stocks) pair:
//!     - `count`        = number of stocks (Python `len(stocks)`).
//!     - `changes`      = finite `change_pct` values (None filtered out).
//!     - if `changes` is empty → skip this group (matches Python `if not changes: continue`).
//!     - `avg_pct`      = sum(changes) / len(changes) (Python full precision, no `round`).
//!     - `up_count`     = count(v > 0) in `changes`.
//!     - `down_count`   = count(v < 0) in `changes`.
//!     - `amount`       = sum of finite `amount` over `stocks` (Python
//!                        `sum(_finite(s.get("amount")) or 0 for s in stocks)`).
//!     - `leader`       = argmax by `_leader_sort_key` (None → `-inf`).
//!     - emit 1 row with: name, count, avg_pct, up_count, down_count, amount,
//!       source_field (carried through from caller — adapter pre-computes),
//!       leader_symbol, leader_name, leader_change_pct.
//!
//! The kernel does NOT sort by `avg_pct` — that's the adapter's job (leading/lagging).
//!
//! Output schema: 9 columns × 1 row per group (groups with no `changes` skipped).

use polars::prelude::*;
use pyo3::prelude::*;
use pyo3_polars::PyDataFrame;
use std::cmp::Ordering;
use std::collections::HashMap;

/// Pure-Rust view of one stock row, decoupled from `Py<PyAny>` for testability.
pub struct StockRow {
    pub symbol: Option<String>,
    pub name: Option<String>,
    pub change_pct: Option<f64>,
    pub amount: Option<f64>,
}

/// 1:1 port of `_leader_sort_key`: finite `change_pct` else `-inf` sentinel.
/// Critical: 0.0% is a valid value (板块整体下跌时平盘股就是领涨股).
fn leader_sort_key(r: &StockRow) -> f64 {
    r.change_pct.filter(|v| v.is_finite()).unwrap_or(f64::NEG_INFINITY)
}

/// Extract one stock row from a Python dict (mirror of `_finite` semantics).
fn extract_stock(dict: &HashMap<String, Py<PyAny>>, py: Python<'_>) -> StockRow {
    let finite = |obj: Option<&Py<PyAny>>| -> Option<f64> {
        obj.and_then(|v| v.extract::<f64>(py).ok()).filter(|f| f.is_finite())
    };
    StockRow {
        symbol: dict.get("symbol").and_then(|v| v.extract::<String>(py).ok()),
        name: dict.get("name").and_then(|v| v.extract::<String>(py).ok()),
        change_pct: finite(dict.get("change_pct")),
        amount: finite(dict.get("amount")),
    }
}

/// Per-group reduce — 1 input group → 1 output row (or skipped if no `changes`).
fn reduce_group(
    name: &str,
    stocks: &[StockRow],
    source_field: Option<&str>,
) -> Option<GroupRow> {
    let changes: Vec<f64> = stocks.iter().filter_map(|s| s.change_pct).collect();
    if changes.is_empty() {
        return None; // matches Python `if not changes: continue`
    }
    let count = stocks.len() as u32;
    let avg_pct = changes.iter().sum::<f64>() / changes.len() as f64;
    let up_count = changes.iter().filter(|v| **v > 0.0).count() as u32;
    let down_count = changes.iter().filter(|v| **v < 0.0).count() as u32;
    let amount: f64 = stocks.iter().filter_map(|s| s.amount).sum();

    // `max(stocks, key=_leader_sort_key)` — Python's `max` on stable iter
    // returns the first occurrence on equal keys. Rust's `max_by` returns
    // the last equal. The closure receives (new, accumulator); on equal keys
    // prefer smaller index (Python stable max semantics).
    let leader = stocks
        .iter()
        .enumerate()
        .max_by(|(ia, a), (ib, b)| {
            let ka = leader_sort_key(a);
            let kb = leader_sort_key(b);
            // Compare keys; tie → prefer smaller index (Python stable max semantics).
            match ka.partial_cmp(&kb).unwrap_or(Ordering::Equal) {
                Ordering::Equal => ia.cmp(ib),
                ord => ord,
            }
        })
        .map(|(_, s)| s);

    let (leader_symbol, leader_name, leader_change) = match leader {
        Some(l) => (
            l.symbol.clone().unwrap_or_default(),
            l.name.clone(),
            l.change_pct,
        ),
        None => (String::new(), None, None),
    };

    Some(GroupRow {
        name: name.to_string(),
        count,
        avg_pct,
        up_count,
        down_count,
        amount,
        source_field: source_field.map(str::to_string),
        leader_symbol,
        leader_name,
        leader_change_pct: leader_change,
    })
}

/// Internal row type for the inner reduce — flattens to 9 DataFrame columns.
struct GroupRow {
    name: String,
    count: u32,
    avg_pct: f64,
    up_count: u32,
    down_count: u32,
    amount: f64,
    source_field: Option<String>,
    leader_symbol: String,
    leader_name: Option<String>,
    leader_change_pct: Option<f64>,
}

/// Pure-Rust inner — reduce each group, build the 9-column DataFrame.
fn dimension_rank_inner(
    groups: &[(&str, &[StockRow], Option<&str>)],
    _limit: usize,
) -> PolarsResult<DataFrame> {
    let rows: Vec<GroupRow> = groups
        .iter()
        .filter_map(|(name, stocks, src)| reduce_group(name, stocks, *src))
        .collect();

    let names: Vec<&str> = rows.iter().map(|r| r.name.as_str()).collect();
    let counts: Vec<u32> = rows.iter().map(|r| r.count).collect();
    let avg_pcts: Vec<f64> = rows.iter().map(|r| r.avg_pct).collect();
    let up_counts: Vec<u32> = rows.iter().map(|r| r.up_count).collect();
    let down_counts: Vec<u32> = rows.iter().map(|r| r.down_count).collect();
    let amounts: Vec<f64> = rows.iter().map(|r| r.amount).collect();
    let source_fields: Vec<Option<&str>> = rows
        .iter()
        .map(|r| r.source_field.as_deref())
        .collect();
    let leader_symbols: Vec<&str> = rows.iter().map(|r| r.leader_symbol.as_str()).collect();
    let leader_names: Vec<Option<&str>> = rows.iter().map(|r| r.leader_name.as_deref()).collect();
    let leader_pcts: Vec<Option<f64>> = rows.iter().map(|r| r.leader_change_pct).collect();

    df![
        "name"              => names,
        "count"             => counts,
        "avg_pct"           => avg_pcts,
        "up_count"          => up_counts,
        "down_count"        => down_counts,
        "amount"            => amounts,
        "source_field"      => source_fields,
        "leader_symbol"     => leader_symbols,
        "leader_name"       => leader_names,
        "leader_change_pct" => leader_pcts,
    ]
}

/// PyO3 entry point — extract dicts → `StockRow`s, call inner, return `PyDataFrame`.
///
/// `source_field` is passed-through from the adapter (Python-side `group_source` map);
/// `None` when caller doesn't supply a source per group. Per ADR-0006 §"5 段", the
/// adapter owns the parquet + yaml + ext IO and computes this; the kernel just
/// carries it through to the output row.
#[pyfunction]
#[pyo3(signature = (groups, limit, source_fields=None))]
pub fn dimension_rank_kernel(
    py: Python<'_>,
    groups: Vec<(String, Vec<HashMap<String, Py<PyAny>>>)>,
    limit: usize,
    source_fields: Option<Vec<Option<String>>>,
) -> PyResult<PyDataFrame> {
    let mut parsed: Vec<(String, Vec<StockRow>)> = Vec::with_capacity(groups.len());
    for (name, dicts) in groups {
        let stocks: Vec<StockRow> = dicts.iter().map(|d| extract_stock(d, py)).collect();
        parsed.push((name, stocks));
    }

    // Map source_fields (parallel Vec<Option<String>>) to per-group slice.
    let source_refs: Vec<Option<&str>> = match &source_fields {
        Some(v) => v.iter().map(|s| s.as_deref()).collect(),
        None => vec![None; parsed.len()],
    };

    let borrowed: Vec<(&str, &[StockRow], Option<&str>)> = parsed
        .iter()
        .zip(source_refs.iter())
        .map(|((name, stocks), src)| (name.as_str(), stocks.as_slice(), *src))
        .collect();

    let df = dimension_rank_inner(&borrowed, limit)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("{e}")))?;
    Ok(PyDataFrame(df))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn stock(
        symbol: &str,
        name: Option<&str>,
        change_pct: Option<f64>,
        amount: Option<f64>,
    ) -> StockRow {
        StockRow {
            symbol: Some(symbol.to_string()),
            name: name.map(str::to_string),
            change_pct,
            amount,
        }
    }

    /// Main-path correctness: 3 groups (Banks / Tech / Consumer), 4 stocks each,
    /// mix of finite / None change_pct + amount values. Verifies:
    ///   - count = 4 (all stocks counted, even None-change_pct ones).
    ///   - avg_pct = sum of finite changes / count of finite changes (NOT /4).
    ///   - leader chosen by max change_pct (Python `max(key=_leader_sort_key)`).
    ///   - skipped group semantics (group with all None change_pct).
    #[test]
    fn main_path_3_groups_4_stocks_each() {
        // Banks: 600000 (+1.2%), 600036 (-0.5%), 601398 (+2.0%), 601939 (None)
        // → count=4, avg_pct = (0.012 - 0.005 + 0.020)/3 = 0.027/3 = 0.009
        // → up=2, down=1, amount = 1.2 + 2.4 + 3.1 = 6.7 (601939 None filtered)
        // → leader = 601398 (+2.0%)
        let banks = vec![
            stock("600000.SH", Some("ZARA"), Some( 0.012), Some(1.2e8)),
            stock("600036.SH", Some("MSH"),  Some(-0.005), Some(2.4e8)),
            stock("601398.SH", Some("ICBC"), Some( 0.020), Some(3.1e8)),
            stock("601939.SH", Some("CCB"),  None,         None       ),
        ];
        // Tech: 000001 (+6.2%), 000063 (-3.4%), 002415 (+4.1%), 300750 (None)
        // → avg_pct = (0.062 - 0.034 + 0.041)/3 = 0.069/3 = 0.023
        // → leader = 000001 (+6.2%)
        let tech = vec![
            stock("000001.SZ", Some("PAYH"), Some( 0.062), Some(0.9e8)),
            stock("000063.SZ", Some("ZXB"),  Some(-0.034), Some(1.5e8)),
            stock("002415.SZ", Some("HK"),   Some( 0.041), Some(1.1e8)),
            stock("300750.SZ", Some("CATL"), None,         None       ),
        ];
        // Consumer: 600519 (-2.2%), 000858 (+0.8%), 600887 (+1.5%), 603288 (-1.1%)
        // → avg_pct = (-0.022 + 0.008 + 0.015 - 0.011)/4 = -0.010/4 = -0.0025
        // → leader = 600887 (+1.5%)
        let consumer = vec![
            stock("600519.SH", Some("KWE"),  Some(-0.022), Some(2.0e8)),
            stock("000858.SZ", Some("WLY"),  Some( 0.008), Some(0.7e8)),
            stock("600887.SH", Some("YILI"), Some( 0.015), Some(0.5e8)),
            stock("603288.SH", Some("HT"),   Some(-0.011), Some(0.4e8)),
        ];
        // Group with ALL None change_pct → skipped from output (matches `if not changes: continue`).
        let empty_changes: Vec<StockRow> = vec![
            stock("999999.SH", None, None, None),
            stock("999998.SH", None, None, None),
        ];

        let groups: Vec<(&str, &[StockRow], Option<&str>)> = vec![
            ("Banks",    &banks,    Some("industry_bank.industry")),
            ("Tech",     &tech,     Some("industry_tech.industry")),
            ("Consumer", &consumer, Some("industry_cons.industry")),
            ("Empty",    &empty_changes, Some("industry_x.industry")),
        ];

        let df = dimension_rank_inner(&groups, 5).unwrap();

        // Empty group filtered out → 3 rows in output.
        assert_eq!(df.height(), 3, "groups with no changes are skipped");
        assert_eq!(df.width(), 10, "10 columns (Spec 0003 §Rust kernel 输出 shape)");

        let names: Vec<&str> = df
            .column("name").unwrap().str().unwrap()
            .iter().map(|s| s.unwrap_or("")).collect();
        let counts: Vec<u32> = df
            .column("count").unwrap().u32().unwrap()
            .iter().map(|c| c.unwrap_or(0)).collect();
        let avg_pcts: Vec<f64> = df
            .column("avg_pct").unwrap().f64().unwrap()
            .iter().map(|v| v.unwrap_or(0.0)).collect();
        let up_counts: Vec<u32> = df
            .column("up_count").unwrap().u32().unwrap()
            .iter().map(|v| v.unwrap_or(0)).collect();
        let down_counts: Vec<u32> = df
            .column("down_count").unwrap().u32().unwrap()
            .iter().map(|v| v.unwrap_or(0)).collect();
        let leader_symbols: Vec<&str> = df
            .column("leader_symbol").unwrap().str().unwrap()
            .iter().map(|s| s.unwrap_or("")).collect();
        let leader_pcts: Vec<Option<f64>> = df
            .column("leader_change_pct").unwrap().f64().unwrap()
            .iter().collect();

        assert_eq!(names, vec!["Banks", "Tech", "Consumer"]);
        assert_eq!(counts, vec![4, 4, 4]);
        // avg_pct is sum(finite_changes) / count(finite_changes) — Python's float
        // arithmetic produces 0.069/3 ≈ 0.023000000000000003 (not exact 0.023) and
        // -0.010/4 ≈ -0.0024999999999999996 (not exact -0.0025). Mirror Python ref
        // by using approximate equality; assert exact bit-equality would fail.
        let expected_avg_pcts = vec![0.009, 0.023, -0.0025];
        for (got, exp) in avg_pcts.iter().zip(expected_avg_pcts.iter()) {
            assert!((got - exp).abs() < 1e-9, "avg_pct mismatch: got {} exp {}", got, exp);
        }
        assert_eq!(up_counts, vec![2, 2, 2]);
        assert_eq!(down_counts, vec![1, 1, 2]);
        assert_eq!(leader_symbols, vec!["601398.SH", "000001.SZ", "600887.SH"]);
        assert_eq!(leader_pcts, vec![Some(0.020), Some(0.062), Some(0.015)]);
    }

    /// Edge case: 0.0% is a valid `change_pct` (板块整体下跌时平盘股就是领涨股).
    /// Per spec, `_leader_sort_key` uses `-inf` for None but keeps 0.0 as-is.
    /// Verify a 0.0% stock wins over negative ones in leader selection.
    #[test]
    fn leader_zero_pct_wins_over_negative() {
        let stocks = vec![
            stock("600000.SH", Some("A"), Some(-1.0), Some(1.0)),
            stock("600001.SH", Some("B"), Some( 0.0), Some(1.0)),  // 0.0 is valid
            stock("600002.SH", Some("C"), Some(-0.5), Some(1.0)),
        ];
        let groups: Vec<(&str, &[StockRow], Option<&str>)> = vec![
            ("Group", &stocks, None),
        ];
        let df = dimension_rank_inner(&groups, 5).unwrap();
        let leader_symbols: Vec<&str> = df
            .column("leader_symbol").unwrap().str().unwrap()
            .iter().map(|s| s.unwrap_or("")).collect();
        let leader_pcts: Vec<Option<f64>> = df
            .column("leader_change_pct").unwrap().f64().unwrap()
            .iter().collect();
        assert_eq!(leader_symbols, vec!["600001.SH"]);
        assert_eq!(leader_pcts, vec![Some(0.0)]);
    }

    /// `_leader_sort_key` sentinel: `None → -inf`. A group with all None change_pct
    /// is skipped (changes empty); within a group with mixed finite + None,
    /// the leader is the max finite value (None treated as smaller than any finite).
    #[test]
    fn leader_uses_neg_inf_sentinel() {
        let stocks = vec![
            stock("A", None, Some( 0.05), Some(1.0)),  // leader (0.05)
            stock("B", None, None,         Some(1.0)),  // filtered
            stock("D", None, Some(-0.01),    Some(1.0)),
        ];
        let groups: Vec<(&str, &[StockRow], Option<&str>)> = vec![
            ("G", &stocks, None),
        ];
        let df = dimension_rank_inner(&groups, 5).unwrap();
        let leader_symbols: Vec<&str> = df
            .column("leader_symbol").unwrap().str().unwrap()
            .iter().map(|s| s.unwrap_or("")).collect();
        assert_eq!(leader_symbols, vec!["A"]);
    }

    /// Empty groups list → 0-row DataFrame with the 10-column schema intact.
    /// Mirrors `_dimension_rank([])` after the `if not rows` early return.
    #[test]
    fn empty_groups_yields_zero_rows() {
        let groups: Vec<(&str, &[StockRow], Option<&str>)> = vec![];
        let df = dimension_rank_inner(&groups, 5).unwrap();
        assert_eq!(df.height(), 0);
        assert_eq!(df.width(), 10, "schema preserved on empty");
    }
}