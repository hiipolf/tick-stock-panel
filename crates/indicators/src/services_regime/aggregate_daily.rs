//! `aggregate_daily_kernel` — Path A-5 / Spec 0005 kernel 2.
//!
//! Algorithm (matches `backend/app/services/regime_builder.py::_aggregate_daily`):
//!   1. Group the per-stock `per_stock_df` (which already has `compute_indicators` +
//!      `compute_limit_signals` + `with_prev_consecutive` Polars expression chain
//!      applied by the Python adapter per Q3=a) by `date`.
//!   2. Per-group compute 14 aggregations + 1 `ma20_above_pct` compound:
//!        `change_pct.gt(0).sum() → up_count`
//!        `change_pct.lt(0).sum() → down_count`
//!        `pl.len() → total_count`
//!        `change_pct.mean() → avg_pct`
//!        `change_pct.median() → median_pct`
//!        `change_pct.ge(0.03).sum() → strong_up_count`
//!        `change_pct.le(-0.03).sum() → strong_down_count`
//!        `signal_limit_up.cast(Boolean).sum() → limit_up`
//!        `signal_limit_down.cast(Boolean).sum() → limit_down`
//!        `signal_broken_limit_up.cast(Boolean).sum() → broken_limit`
//!        `consecutive_limit_ups.max() → max_consecutive`
//!        `amount.sum() → total_amount`
//!        `amount.mean() → avg_amount`
//!        `(close.gt(ma20).cast(Int64).sum() / close.count()) → ma20_above_pct`
//!   3. Join the per-day summary against `index_pct_map` (HashMap<String, f64>) by
//!      the date string (per Spec 0005 §"Implementation Decisions" kernel 2 step 4).
//!   4. Return DataFrame: 1 row per date × 15 columns (14 aggregations +
//!      `ma20_above_pct` + `index_pct`).
//!
//! Per **ADR-0006 §"5 段"§4 Path A-4 amendment** (`polars::prelude` IO), kernel 2
//! is pure-compute on a Python-pre-aggregated DataFrame (no IO, no filesystem,
//! no cache). Adapter (`regime_builder`) owns cache + IO orchestration.
//!
//! **T02 implementation divergence from spec literal wording** (mirrors
//! `per_bucket_reduce_kernel` Path A-4 T02 precedent): the algorithm is
//! implemented as a pure-Rust row-wise reduce over the per-stock DataFrame (rather
//! than a Polars lazy `group_by + agg` expression chain) for the following
//! reasons:
//!   - The 14 aggregations + `index_pct_map` join are all row-wise deterministic
//!     operations; pure-Rust is simpler to debug + control edge cases (null
//!     handling for `change_pct`, finite-pair gate for `ma20_above_pct`,
//!     `signal_*` cast handling).
//!   - The output schema is identical at the row level (15 columns: date +
//!     14 agg + `index_pct`).
//!   - Testability: each aggregation can be asserted in the test fixtures
//!     without depending on polars' expression-evaluation order.
//!
//! This divergence is documented in the T02 closure report for T05 to amend
//! Spec 0005 §"Implementation Decisions" kernel 2 step 3 wording to read
//! "pure-Rust reduce equivalent to the following `group_by + agg` block".

use std::collections::{BTreeMap, HashMap};

use polars::prelude::*;
use pyo3::prelude::*;
use pyo3_polars::PyDataFrame;

// ---------------------------------------------------------------------------
// Per-day output row (internal type — mirrors the 15-column output schema).
// ---------------------------------------------------------------------------

#[derive(Clone, Debug)]
pub(crate) struct DailySummary {
    pub date: String,
    pub up_count: u32,
    pub down_count: u32,
    pub total_count: u32,
    pub avg_pct: f64,
    pub median_pct: f64,
    pub strong_up_count: u32,
    pub strong_down_count: u32,
    pub limit_up: u32,
    pub limit_down: u32,
    pub broken_limit: u32,
    pub max_consecutive: u32,
    pub total_amount: f64,
    pub avg_amount: f64,
    pub ma20_above_pct: f64,
    pub index_pct: Option<f64>,
}

// ---------------------------------------------------------------------------
// Pure-Rust inner — group by date + 14 aggregations + ma20_above_pct + index join
// ---------------------------------------------------------------------------

/// Pure-Rust inner — group `per_stock_df` by `date`, compute 14 aggregations +
/// `ma20_above_pct` compound, join `index_pct_map` by `str(date)`.
/// Returns a DataFrame with 15 columns + N date-rows.
pub(crate) fn aggregate_daily_inner(
    per_stock_df: &DataFrame,
    index_pct_map: &HashMap<String, f64>,
) -> PolarsResult<DataFrame> {
    let summaries = compute_daily_summaries(per_stock_df, index_pct_map)?;
    Ok(build_summary_dataframe(&summaries))
}

/// Pure-Rust reduce: group rows by `date` (str), compute the 14 aggregations +
/// `ma20_above_pct` compound, look up `index_pct` from the HashMap.
fn compute_daily_summaries(
    per_stock_df: &DataFrame,
    index_pct_map: &HashMap<String, f64>,
) -> PolarsResult<Vec<DailySummary>> {
    let n_rows = per_stock_df.height();
    if n_rows == 0 {
        return Ok(vec![]);
    }

    // Read `date` (str) + scalar columns. Use `.iter()` not `.into_iter()` per
    // polars 0.55 `ChunkedArray::into_iter()` removal memory entry.
    let date_col = per_stock_df.column("date")?.str()?;
    let date_dates: Vec<String> = (0..n_rows)
        .map(|i| date_col.get(i).unwrap_or("").to_string())
        .collect();

    // change_pct (Float64, nullable)
    let pct_col_opt = per_stock_df.column("change_pct").ok().and_then(|c| {
        let casted = c.cast(&DataType::Float64).ok()?;
        casted.f64().ok().map(|ca| ca.clone())
    });
    // amount (Float64, nullable)
    let amt_col_opt = per_stock_df.column("amount").ok().and_then(|c| {
        let casted = c.cast(&DataType::Float64).ok()?;
        casted.f64().ok().map(|ca| ca.clone())
    });
    // signal_limit_up / signal_limit_down / signal_broken_limit_up (cast to Boolean if possible)
    let sig_lu = read_bool_col(per_stock_df, "signal_limit_up")?;
    let sig_ld = read_bool_col(per_stock_df, "signal_limit_down")?;
    let sig_bl = read_bool_col(per_stock_df, "signal_broken_limit_up")?;
    // consecutive_limit_ups (UInt32, nullable)
    let consec_col_opt = per_stock_df.column("consecutive_limit_ups").ok().and_then(|c| {
        c.cast(&DataType::UInt32).ok().and_then(|cc| cc.u32().ok().map(|ca| ca.clone()))
    });
    // close (Float64, nullable) + ma20 (Float64, nullable)
    let close_col_opt = per_stock_df.column("close").ok().and_then(|c| {
        c.cast(&DataType::Float64).ok().and_then(|cc| cc.f64().ok().map(|ca| ca.clone()))
    });
    let ma20_col_opt = per_stock_df.column("ma20").ok().and_then(|c| {
        c.cast(&DataType::Float64).ok().and_then(|cc| cc.f64().ok().map(|ca| ca.clone()))
    });

    // Group by date (BTreeMap for deterministic iteration order, ascending date).
    let mut by_date: BTreeMap<String, Vec<usize>> = BTreeMap::new();
    for (i, d) in date_dates.iter().enumerate() {
        by_date.entry(d.clone()).or_default().push(i);
    }

    let mut summaries: Vec<DailySummary> = Vec::with_capacity(by_date.len());
    for (date, row_idxs) in &by_date {
        let mut up_count = 0u32;
        let mut down_count = 0u32;
        let mut strong_up_count = 0u32;
        let mut strong_down_count = 0u32;
        let mut pcts_for_mean: Vec<f64> = Vec::with_capacity(row_idxs.len());
        let mut pcts_for_median: Vec<f64> = Vec::with_capacity(row_idxs.len());
        let mut total_amount = 0.0_f64;
        let mut amount_count = 0u32;
        let mut limit_up = 0u32;
        let mut limit_down = 0u32;
        let mut broken_limit = 0u32;
        let mut max_consecutive: u32 = 0;
        let mut above_count: u32 = 0;
        let mut total_for_above: u32 = 0;

        for &i in row_idxs {
            // change_pct
            if let Some(p) = pct_col_opt.as_ref().and_then(|p| p.get(i)) {
                if p.is_finite() {
                    if p > 0.0 {
                        up_count += 1;
                    } else if p < 0.0 {
                        down_count += 1;
                    }
                    if p >= 0.03 {
                        strong_up_count += 1;
                    }
                    if p <= -0.03 {
                        strong_down_count += 1;
                    }
                    pcts_for_mean.push(p);
                    pcts_for_median.push(p);
                }
            }
            // amount
            if let Some(a) = amt_col_opt.as_ref().and_then(|a| a.get(i)) {
                if a.is_finite() {
                    total_amount += a;
                    amount_count += 1;
                }
            }
            // signal_limit_up / signal_limit_down / signal_broken_limit_up (Boolean)
            if sig_lu.get(i).copied().unwrap_or(false) {
                limit_up += 1;
            }
            if sig_ld.get(i).copied().unwrap_or(false) {
                limit_down += 1;
            }
            if sig_bl.get(i).copied().unwrap_or(false) {
                broken_limit += 1;
            }
            // consecutive_limit_ups max
            if let Some(c) = consec_col_opt.as_ref().and_then(|c| c.get(i)) {
                if c > max_consecutive {
                    max_consecutive = c;
                }
            }
            // ma20_above_pct: count finite pairs (close, ma20) where close > ma20.
            if let (Some(c), Some(m)) = (
                close_col_opt.as_ref().and_then(|c| c.get(i)),
                ma20_col_opt.as_ref().and_then(|m| m.get(i)),
            ) {
                if c.is_finite() && m.is_finite() {
                    total_for_above += 1;
                    if c > m {
                        above_count += 1;
                    }
                }
            }
        }

        let avg_pct = if pcts_for_mean.is_empty() {
            0.0
        } else {
            pcts_for_mean.iter().sum::<f64>() / pcts_for_mean.len() as f64
        };
        let median_pct = if pcts_for_median.is_empty() {
            0.0
        } else {
            // partial_cmp with Ordering::Equal fallback per Path A-3 + Path A-4 memory.
            pcts_for_median.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
            let mid = pcts_for_median.len() / 2;
            if pcts_for_median.len() % 2 == 0 {
                (pcts_for_median[mid - 1] + pcts_for_median[mid]) / 2.0
            } else {
                pcts_for_median[mid]
            }
        };
        let avg_amount = if amount_count == 0 {
            0.0
        } else {
            total_amount / amount_count as f64
        };
        let ma20_above_pct = if total_for_above == 0 {
            0.0
        } else {
            above_count as f64 / total_for_above as f64
        };
        // Join `index_pct_map` by `str(date)` — per Spec 0005 kernel 2 step 4.
        let index_pct = index_pct_map.get(date).copied();

        summaries.push(DailySummary {
            date: date.clone(),
            up_count,
            down_count,
            total_count: row_idxs.len() as u32,
            avg_pct,
            median_pct,
            strong_up_count,
            strong_down_count,
            limit_up,
            limit_down,
            broken_limit,
            max_consecutive,
            total_amount,
            avg_amount,
            ma20_above_pct,
            index_pct,
        });
    }

    Ok(summaries)
}

/// Read a column as `Vec<bool>` (treating absent / non-castable as `false`).
/// Mirrors `signal_limit_up.cast(Boolean).sum()` semantics in the Polars expression.
fn read_bool_col(df: &DataFrame, col_name: &str) -> PolarsResult<Vec<bool>> {
    let n = df.height();
    let col = match df.column(col_name) {
        Ok(c) => c,
        Err(_) => return Ok(vec![false; n]),
    };
    let bool_ca = match col.cast(&DataType::Boolean) {
        Ok(c) => match c.bool() {
            Ok(ca) => ca.clone(),
            Err(_) => return Ok(vec![false; n]),
        },
        Err(_) => return Ok(vec![false; n]),
    };
    Ok((0..n).map(|i| bool_ca.get(i).unwrap_or(false)).collect())
}

/// Build the 16-column output DataFrame from the summaries vector.
fn build_summary_dataframe(summaries: &[DailySummary]) -> DataFrame {
    let dates: Vec<&str> = summaries.iter().map(|s| s.date.as_str()).collect();
    let up_counts: Vec<u32> = summaries.iter().map(|s| s.up_count).collect();
    let down_counts: Vec<u32> = summaries.iter().map(|s| s.down_count).collect();
    let total_counts: Vec<u32> = summaries.iter().map(|s| s.total_count).collect();
    let avg_pcts: Vec<f64> = summaries.iter().map(|s| s.avg_pct).collect();
    let median_pcts: Vec<f64> = summaries.iter().map(|s| s.median_pct).collect();
    let strong_ups: Vec<u32> = summaries.iter().map(|s| s.strong_up_count).collect();
    let strong_downs: Vec<u32> = summaries.iter().map(|s| s.strong_down_count).collect();
    let limit_ups: Vec<u32> = summaries.iter().map(|s| s.limit_up).collect();
    let limit_downs: Vec<u32> = summaries.iter().map(|s| s.limit_down).collect();
    let broken_limits: Vec<u32> = summaries.iter().map(|s| s.broken_limit).collect();
    let max_consecs: Vec<u32> = summaries.iter().map(|s| s.max_consecutive).collect();
    let total_amounts: Vec<f64> = summaries.iter().map(|s| s.total_amount).collect();
    let avg_amounts: Vec<f64> = summaries.iter().map(|s| s.avg_amount).collect();
    let ma20_above_pcts: Vec<f64> = summaries.iter().map(|s| s.ma20_above_pct).collect();
    let index_pcts: Vec<Option<f64>> = summaries.iter().map(|s| s.index_pct).collect();

    df![
        "date"              => dates,
        "up_count"          => up_counts,
        "down_count"        => down_counts,
        "total_count"       => total_counts,
        "avg_pct"           => avg_pcts,
        "median_pct"        => median_pcts,
        "strong_up_count"   => strong_ups,
        "strong_down_count" => strong_downs,
        "limit_up"          => limit_ups,
        "limit_down"        => limit_downs,
        "broken_limit"      => broken_limits,
        "max_consecutive"   => max_consecs,
        "total_amount"      => total_amounts,
        "avg_amount"        => avg_amounts,
        "ma20_above_pct"    => ma20_above_pcts,
        "index_pct"         => index_pcts,
    ]
    .expect("aggregate_daily_kernel output DataFrame construction")
}

// ---------------------------------------------------------------------------
// PyO3 entry point
// ---------------------------------------------------------------------------

/// PyO3 entry point — extract PyDataFrame + HashMap<String, f64>, call inner, return PyDataFrame.
///
/// Signature (verbatim per Spec 0005 §"Implementation Decisions" kernel 2):
///   `aggregate_daily_kernel(per_stock_df: PyDataFrame, index_pct_map: HashMap<String, f64>) -> PyResult<PyDataFrame>`
#[pyfunction]
pub fn aggregate_daily_kernel(
    per_stock_df: PyDataFrame,
    index_pct_map: HashMap<String, f64>,
) -> PyResult<PyDataFrame> {
    let per_stock: DataFrame = per_stock_df.into();
    let df = aggregate_daily_inner(&per_stock, &index_pct_map)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("{e}")))?;
    Ok(PyDataFrame(df))
}

// ---------------------------------------------------------------------------
// Tests — 4 per Spec 0005 §"Test plan (per Q13)"
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    /// Build a per-stock DataFrame mimicking the post-`compute_indicators` +
    /// `compute_limit_signals` + `with_prev_consecutive` Polars expression chain
    /// output. Each row: (date, symbol, change_pct, amount, signal_limit_up,
    /// signal_limit_down, signal_broken_limit_up, consecutive_limit_ups, close, ma20).
    fn make_per_stock_df(
        rows: &[(
            &str,    // date
            &str,    // symbol
            Option<f64>, // change_pct
            Option<f64>, // amount
            bool,     // signal_limit_up
            bool,     // signal_limit_down
            bool,     // signal_broken_limit_up
            u32,      // consecutive_limit_ups
            Option<f64>, // close
            Option<f64>, // ma20
        )],
    ) -> DataFrame {
        let dates: Vec<&str> = rows.iter().map(|r| r.0).collect();
        let symbols: Vec<String> = rows.iter().map(|r| r.1.to_string()).collect();
        let sym_refs: Vec<&str> = symbols.iter().map(|s| s.as_str()).collect();
        let pcts: Vec<Option<f64>> = rows.iter().map(|r| r.2).collect();
        let amts: Vec<Option<f64>> = rows.iter().map(|r| r.3).collect();
        let sig_lu: Vec<bool> = rows.iter().map(|r| r.4).collect();
        let sig_ld: Vec<bool> = rows.iter().map(|r| r.5).collect();
        let sig_bl: Vec<bool> = rows.iter().map(|r| r.6).collect();
        let consec: Vec<u32> = rows.iter().map(|r| r.7).collect();
        let closes: Vec<Option<f64>> = rows.iter().map(|r| r.8).collect();
        let ma20s: Vec<Option<f64>> = rows.iter().map(|r| r.9).collect();

        df![
            "symbol"                  => sym_refs,
            "date"                    => dates,
            "change_pct"              => pcts,
            "amount"                  => amts,
            "signal_limit_up"         => sig_lu,
            "signal_limit_down"       => sig_ld,
            "signal_broken_limit_up"  => sig_bl,
            "consecutive_limit_ups"   => consec,
            "close"                   => closes,
            "ma20"                    => ma20s,
        ]
        .expect("per_stock_df construction")
    }

    /// Test 1 (smoke): 1 day × 3 stocks. Verify up_count / down_count / total_count
    /// + schema (15 columns).
    #[test]
    fn smoke_one_day_three_stocks() {
        let per_stock = make_per_stock_df(&[
            ("2025-01-10", "600000.SH", Some(0.02),  Some(1e8), true,  false, false, 2, Some(11.0), Some(10.0)),  // up + limit_up
            ("2025-01-10", "000001.SZ", Some(-0.01), Some(2e8), false, false, false, 0, Some(12.0), Some(12.5)),  // down
            ("2025-01-10", "002415.SZ", Some(0.05),  Some(3e8), true,  false, true,  3, Some(15.0), Some(14.0)),  // up + limit_up + broken_limit
        ]);
        let mut index_pct_map: HashMap<String, f64> = HashMap::new();
        index_pct_map.insert("2025-01-10".to_string(), 0.005);

        let df = aggregate_daily_inner(&per_stock, &index_pct_map).unwrap();
        assert_eq!(df.height(), 1, "1 day summary row");
        assert_eq!(df.width(), 16, "16 cols: date + 14 agg + index_pct + ma20_above_pct");

        let up: u32 = df.column("up_count").unwrap().u32().unwrap().get(0).unwrap();
        let down: u32 = df.column("down_count").unwrap().u32().unwrap().get(0).unwrap();
        let total: u32 = df.column("total_count").unwrap().u32().unwrap().get(0).unwrap();
        let limit_up: u32 = df.column("limit_up").unwrap().u32().unwrap().get(0).unwrap();
        let broken: u32 = df.column("broken_limit").unwrap().u32().unwrap().get(0).unwrap();
        let max_consec: u32 = df.column("max_consecutive").unwrap().u32().unwrap().get(0).unwrap();
        let avg_pct: f64 = df.column("avg_pct").unwrap().f64().unwrap().get(0).unwrap();
        let ma20_above: f64 = df.column("ma20_above_pct").unwrap().f64().unwrap().get(0).unwrap();
        let index_pct: Option<f64> = df.column("index_pct").unwrap().f64().unwrap().get(0);

        assert_eq!(up, 2, "2 up (0.02 + 0.05)");
        assert_eq!(down, 1, "1 down (-0.01)");
        assert_eq!(total, 3, "3 stocks total");
        assert_eq!(limit_up, 2, "2 limit_up signals");
        assert_eq!(broken, 1, "1 broken_limit signal");
        assert_eq!(max_consec, 3, "max consecutive = 3");
        // avg_pct = (0.02 + -0.01 + 0.05) / 3 = 0.02
        assert!((avg_pct - 0.02).abs() < 1e-9, "avg_pct ~0.02, got {avg_pct}");
        // ma20_above_pct: all 3 pairs (close, ma20) are finite; 2 of 3 have close > ma20
        // (11>10, 12<12.5, 15>14) → 2/3.
        assert!((ma20_above - 2.0 / 3.0).abs() < 1e-9, "ma20_above_pct ~2/3, got {ma20_above}");
        assert_eq!(index_pct, Some(0.005), "index_pct join by str(date)");
    }

    /// Test 2 (multi-day): 3 days × 3 stocks. Verify avg_pct per day.
    /// Suggested diff across days so we can tell apart:
    ///   Day 1: [+0.01, -0.02, +0.03] → avg_pct = 0.02/3 ≈ 0.00667
    ///   Day 2: [+0.05, -0.04, +0.01] → avg_pct = 0.02/3 ≈ 0.00667
    ///   Day 3: [-0.05, -0.10, -0.03] → avg_pct = -0.18/3 = -0.06
    #[test]
    fn multi_day_three_days_three_stocks_avg_pct() {
        let per_stock = make_per_stock_df(&[
            ("2025-01-10", "600000.SH", Some(0.01), Some(1e8), false, false, false, 0, Some(10.0), Some(10.0)),
            ("2025-01-10", "000001.SZ", Some(-0.02), Some(1e8), false, false, false, 0, Some(10.0), Some(10.0)),
            ("2025-01-10", "002415.SZ", Some(0.03), Some(1e8), false, false, false, 0, Some(10.0), Some(10.0)),
            ("2025-01-11", "600000.SH", Some(0.05), Some(1e8), false, false, false, 0, Some(10.0), Some(10.0)),
            ("2025-01-11", "000001.SZ", Some(-0.04), Some(1e8), false, false, false, 0, Some(10.0), Some(10.0)),
            ("2025-01-11", "002415.SZ", Some(0.01), Some(1e8), false, false, false, 0, Some(10.0), Some(10.0)),
            ("2025-01-12", "600000.SH", Some(-0.05), Some(1e8), false, false, false, 0, Some(10.0), Some(10.0)),
            ("2025-01-12", "000001.SZ", Some(-0.10), Some(1e8), false, false, false, 0, Some(10.0), Some(10.0)),
            ("2025-01-12", "002415.SZ", Some(-0.03), Some(1e8), false, false, false, 0, Some(10.0), Some(10.0)),
        ]);
        let index_pct_map: HashMap<String, f64> = HashMap::new();

        let df = aggregate_daily_inner(&per_stock, &index_pct_map).unwrap();
        assert_eq!(df.height(), 3, "3 day-summary rows");

        let avg_pcts: Vec<f64> = df
            .column("avg_pct").unwrap().f64().unwrap()
            .iter().map(|v| v.unwrap_or(0.0)).collect();
        let dates: Vec<&str> = df
            .column("date").unwrap().str().unwrap()
            .iter().map(|s| s.unwrap_or("")).collect();
        assert_eq!(dates, vec!["2025-01-10", "2025-01-11", "2025-01-12"], "sorted by date ASC");

        // Day 1: (0.01 + -0.02 + 0.03) / 3 = 0.02 / 3 ≈ 0.00667
        assert!((avg_pcts[0] - 0.02 / 3.0).abs() < 1e-9, "day 1 avg_pct ~0.00667, got {}", avg_pcts[0]);
        // Day 2: (0.05 + -0.04 + 0.01) / 3 = 0.02 / 3 ≈ 0.00667
        assert!((avg_pcts[1] - 0.02 / 3.0).abs() < 1e-9, "day 2 avg_pct ~0.00667, got {}", avg_pcts[1]);
        // Day 3: (-0.05 + -0.10 + -0.03) / 3 = -0.18 / 3 = -0.06
        assert!((avg_pcts[2] - (-0.06)).abs() < 1e-9, "day 3 avg_pct ~-0.06, got {}", avg_pcts[2]);
    }

    /// Test 3 (strong_pct threshold boundary): verify ≥3% / ≤-3% strong_up/down
    /// counts. Boundary at exactly 3% / -3% is INCLUSIVE (per spec: `.ge(0.03)` /
    /// `.le(-0.03)`).
    #[test]
    fn strong_pct_threshold_boundary_inclusive() {
        // Boundary values:
        //   0.0299 → NOT strong_up (just below 3%)
        //   0.0300 → strong_up (boundary inclusive)
        //   0.0500 → strong_up
        //  -0.0299 → NOT strong_down (just above -3%)
        //  -0.0300 → strong_down (boundary inclusive)
        //  -0.0500 → strong_down
        // All on date "2025-01-10" → 1 day summary.
        let per_stock = make_per_stock_df(&[
            ("2025-01-10", "A", Some(0.0299), Some(1e8), false, false, false, 0, Some(10.0), Some(10.0)),  // NOT strong
            ("2025-01-10", "B", Some(0.0300), Some(1e8), false, false, false, 0, Some(10.0), Some(10.0)),  // strong_up
            ("2025-01-10", "C", Some(0.0500), Some(1e8), false, false, false, 0, Some(10.0), Some(10.0)),  // strong_up
            ("2025-01-10", "D", Some(-0.0299), Some(1e8), false, false, false, 0, Some(10.0), Some(10.0)), // NOT strong
            ("2025-01-10", "E", Some(-0.0300), Some(1e8), false, false, false, 0, Some(10.0), Some(10.0)), // strong_down
            ("2025-01-10", "F", Some(-0.0500), Some(1e8), false, false, false, 0, Some(10.0), Some(10.0)), // strong_down
        ]);
        let index_pct_map: HashMap<String, f64> = HashMap::new();

        let df = aggregate_daily_inner(&per_stock, &index_pct_map).unwrap();
        assert_eq!(df.height(), 1);

        let strong_up: u32 = df.column("strong_up_count").unwrap().u32().unwrap().get(0).unwrap();
        let strong_down: u32 = df.column("strong_down_count").unwrap().u32().unwrap().get(0).unwrap();
        let up: u32 = df.column("up_count").unwrap().u32().unwrap().get(0).unwrap();
        let down: u32 = df.column("down_count").unwrap().u32().unwrap().get(0).unwrap();
        assert_eq!(strong_up, 2, "boundary 0.03 + 0.05");
        assert_eq!(strong_down, 2, "boundary -0.03 + -0.05");
        assert_eq!(up, 3, "0.0299 + 0.03 + 0.05");
        assert_eq!(down, 3, "-0.0299 + -0.03 + -0.05");
    }

    /// Test 4 (ma20_above_pct edge case): mixed finite / null / non-finite pairs.
    /// Only finite (close, ma20) pairs count. Of those, count `close > ma20`.
    #[test]
    fn ma20_above_pct_edge_finite_pairs_subset() {
        // 6 stocks on date "2025-01-10":
        //   A: close=11, ma20=10 → above (finite pair)
        //   B: close=12, ma20=12 → NOT above (finite pair, equal)
        //   C: close=13, ma20=14 → NOT above (finite pair)
        //   D: close=NaN, ma20=10 → NOT counted (close non-finite)
        //   E: close=15, ma20=NaN → NOT counted (ma20 non-finite)
        //   F: close=null, ma20=null → NOT counted (both null)
        //   G: close=10, ma20=null → NOT counted (ma20 null)
        // Expected: 1 of 2 finite pairs (A, B, C) → ma20_above_pct = 1/3 ≈ 0.333.
        let per_stock = make_per_stock_df(&[
            ("2025-01-10", "A", Some(0.01), Some(1e8), false, false, false, 0, Some(11.0), Some(10.0)), // above
            ("2025-01-10", "B", Some(0.01), Some(1e8), false, false, false, 0, Some(12.0), Some(12.0)), // equal
            ("2025-01-10", "C", Some(0.01), Some(1e8), false, false, false, 0, Some(13.0), Some(14.0)), // below
            ("2025-01-10", "D", Some(0.01), Some(1e8), false, false, false, 0, Some(f64::NAN), Some(10.0)), // NaN close
            ("2025-01-10", "E", Some(0.01), Some(1e8), false, false, false, 0, Some(15.0), Some(f64::NAN)), // NaN ma20
            ("2025-01-10", "F", Some(0.01), Some(1e8), false, false, false, 0, None, None),                 // both null
            ("2025-01-10", "G", Some(0.01), Some(1e8), false, false, false, 0, Some(10.0), None),          // ma20 null
        ]);
        let index_pct_map: HashMap<String, f64> = HashMap::new();

        let df = aggregate_daily_inner(&per_stock, &index_pct_map).unwrap();
        assert_eq!(df.height(), 1);

        let ma20_above: f64 = df.column("ma20_above_pct").unwrap().f64().unwrap().get(0).unwrap();
        // 1 above (A) out of 3 finite pairs (A, B, C) = 1/3.
        assert!(
            (ma20_above - 1.0 / 3.0).abs() < 1e-9,
            "ma20_above_pct ~1/3 (A above, B equal, C below; D/E/F/G non-finite or null), got {ma20_above}"
        );
        // Also verify total_count = 7 (all 7 stocks counted in total_count).
        let total: u32 = df.column("total_count").unwrap().u32().unwrap().get(0).unwrap();
        assert_eq!(total, 7, "all 7 stocks in total_count");
    }
}