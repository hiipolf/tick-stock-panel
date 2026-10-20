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
    // ── 梯队指标 (ladder) — 1:1 port of `market_phase.ladder_daily_aggs()` +
    // `market_phase.ladder_promo_aggs()` (T04 finding: these 7 aggs are part
    // of the SAME `group_by("date").agg(...)` block as the 14 base aggs, so a
    // true 1:1 port must include them).
    pub first_board: u32,
    pub ge2_count: u32,
    pub ge3_count: u32,
    pub ge5_count: u32,
    pub rungs_filled: u32,
    pub promo_pool: u32,
    pub promo_ok: u32,
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
    // Original `date` dtype (e.g. `Date` for live enriched data) — the grouping
    // key is the ISO 8601 String form, but the output column is cast back to this
    // dtype so the kernel output matches the Python `_aggregate_daily` reference.
    let date_dtype = per_stock_df
        .column("date")
        .map(|c| c.dtype().clone())
        .unwrap_or(DataType::String);
    let summaries = compute_daily_summaries(per_stock_df, index_pct_map)?;
    Ok(build_summary_dataframe(&summaries, &date_dtype))
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

    // Read `date` + scalar columns. Use `.iter()` not `.into_iter()` per
    // polars 0.55 `ChunkedArray::into_iter()` removal memory entry.
    //
    // The grouping key is always the ISO 8601 String form (per Spec 0005
    // §"Implementation Decisions" kernel 2 step 4 — join by `str(date)`), so
    // cast to String first: the live enriched data has `date: Date` (verified
    // 2026-10-20 against `D:/AI-Stock/Stock-Data/kline_daily_enriched/.../part.parquet`
    // schema `('date', Date)`), and a raw `Date` column would fail the `.str()`
    // accessor. The output column dtype is restored by the caller.
    let date_col_cast = per_stock_df
        .column("date")?
        .cast(&DataType::String)?;
    let date_col = date_col_cast.str()?;
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
    // `_prev_consec` (optional; produced by `market_phase.with_prev_consecutive`).
    // Required for the 晋级率 (promo) aggs; absent → promo_pool = promo_ok = 0
    // (mirrors the Python `ladder_promo_aggs()` being conditionally omitted when
    // `"_prev_consec" not in df.columns`).
    let prev_col_opt = per_stock_df.column("_prev_consec").ok().and_then(|c| {
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
        // ── 梯队指标 (ladder) accumulators ──
        let mut first_board: u32 = 0;
        let mut ge2_count: u32 = 0;
        let mut ge3_count: u32 = 0;
        let mut ge5_count: u32 = 0;
        let mut rungs_filled_set: std::collections::BTreeSet<u32> = std::collections::BTreeSet::new();
        let mut promo_pool: u32 = 0;
        let mut promo_ok: u32 = 0;

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
                // ── 梯队指标: 1:1 port of `ladder_daily_aggs()` ──
                //   consec.eq(1).sum()        → first_board
                //   consec.ge(2).sum()        → ge2_count
                //   consec.ge(3).sum()        → ge3_count
                //   consec.ge(5).sum()        → ge5_count
                //   consec.filter(ge(2)).n_unique() → rungs_filled
                if c == 1 {
                    first_board += 1;
                }
                if c >= 2 {
                    ge2_count += 1;
                    rungs_filled_set.insert(c);
                }
                if c >= 3 {
                    ge3_count += 1;
                }
                if c >= 5 {
                    ge5_count += 1;
                }
                // ── 晋级率: 1:1 port of `ladder_promo_aggs()` ──
                //   prev.ge(1).sum()                        → promo_pool
                //   (prev.ge(1) & consec.eq(prev + 1)).sum() → promo_ok
                if let Some(p) = prev_col_opt.as_ref().and_then(|p| p.get(i)) {
                    if p >= 1 {
                        promo_pool += 1;
                        if c == p + 1 {
                            promo_ok += 1;
                        }
                    }
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
            first_board,
            ge2_count,
            ge3_count,
            ge5_count,
            rungs_filled: rungs_filled_set.len() as u32,
            promo_pool,
            promo_ok,
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
///
/// `date_dtype` is the ORIGINAL dtype of the input `date` column. The grouping
/// key is always the ISO 8601 String form (per Spec 0005 §"Implementation
/// Decisions" kernel 2 step 4 — join by `str(date)`), but the output column is
/// cast back to `date_dtype` so the kernel's output dtype matches the Python
/// `_aggregate_daily` reference exactly (live enriched data has `date: Date`).
fn build_summary_dataframe(summaries: &[DailySummary], date_dtype: &DataType) -> DataFrame {
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
    let first_boards: Vec<u32> = summaries.iter().map(|s| s.first_board).collect();
    let ge2_counts: Vec<u32> = summaries.iter().map(|s| s.ge2_count).collect();
    let ge3_counts: Vec<u32> = summaries.iter().map(|s| s.ge3_count).collect();
    let ge5_counts: Vec<u32> = summaries.iter().map(|s| s.ge5_count).collect();
    let rungs_filleds: Vec<u32> = summaries.iter().map(|s| s.rungs_filled).collect();
    let promo_pools: Vec<u32> = summaries.iter().map(|s| s.promo_pool).collect();
    let promo_oks: Vec<u32> = summaries.iter().map(|s| s.promo_ok).collect();

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
        // ── 梯队指标 (ladder) — same group_by block as the 14 base aggs ──
        "first_board"       => first_boards,
        "ge2_count"         => ge2_counts,
        "ge3_count"         => ge3_counts,
        "ge5_count"         => ge5_counts,
        "rungs_filled"      => rungs_filleds,
        "promo_pool"        => promo_pools,
        "promo_ok"          => promo_oks,
    ]
    .expect("aggregate_daily_kernel output DataFrame construction")
    // Cast the `date` output column back to the input dtype (e.g. Date) so the
    // kernel output is dtype-identical to the Python `_aggregate_daily` reference.
    .lazy()
    .with_column(col("date").cast(date_dtype.clone()))
    .collect()
    .expect("aggregate_daily_kernel output date dtype cast")
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
        assert_eq!(
            df.width(),
            23,
            "23 cols: date + 14 agg + index_pct + ma20_above_pct + 7 ladder aggs"
        );

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

    // -----------------------------------------------------------------------
    // T04 regression: live enriched-daily parquet has `date: Date` (verified
    // 2026-10-20 against `D:/AI-Stock/Stock-Data/kline_daily_enriched/.../part.parquet`:
    //   `('symbol', String), ('date', Date), ...`). The T02 unit tests build
    // `date: String` (via `df!["date" => dates]` with `&str` values), so the
    // kernel's `.str()` accessor on a `Date` column was never exercised. The
    // fix: cast `date` to String for the grouping key (join is by `str(date)`
    // per Spec 0005) and cast the output column back to the input dtype so the
    // kernel output is dtype-identical to the Python `_aggregate_daily` reference.
    // This test guards both halves.
    // -----------------------------------------------------------------------

    #[test]
    fn date_typed_input_column_produces_date_typed_output() {
        // Same data as `smoke_one_day_three_stocks`, but with `date: Date`.
        let per_stock_str = make_per_stock_df(&[
            ("2025-01-10", "600000.SH", Some(0.02),  Some(1e8), true,  false, false, 2, Some(11.0), Some(10.0)),
            ("2025-01-10", "000001.SZ", Some(-0.01), Some(2e8), false, false, false, 0, Some(12.0), Some(12.5)),
            ("2025-01-10", "000002.SZ", Some(0.04),  Some(3e8), true,  false, false, 1, Some(10.5), Some(9.0)),
        ]);
        // Cast the `date` column to Date (i32 days since 1970-01-01).
        let epoch_2025_01_10 = 20089i32; // 2025-01-10
        let per_stock = per_stock_str
            .lazy()
            .with_column(
                lit(epoch_2025_01_10)
                    .cast(DataType::Int32)
                    .cast(DataType::Date)
                    .alias("date"),
            )
            .collect()
            .unwrap();
        assert_eq!(per_stock.column("date").unwrap().dtype(), &DataType::Date);

        let index_pct_map: HashMap<String, f64> = HashMap::new();
        let df = aggregate_daily_inner(&per_stock, &index_pct_map).unwrap();

        // Output `date` column dtype must be restored to `Date` (dtype parity
        // with the Python `_aggregate_daily` reference on the same input).
        assert_eq!(
            df.column("date").unwrap().dtype(),
            &DataType::Date,
            "output date dtype must match input date dtype"
        );
        assert_eq!(df.height(), 1, "1 date group expected");
        // Same aggregations as the String-typed variant.
        let up: u32 = df.column("up_count").unwrap().u32().unwrap().get(0).unwrap();
        let down: u32 = df.column("down_count").unwrap().u32().unwrap().get(0).unwrap();
        let limit_up: u32 = df.column("limit_up").unwrap().u32().unwrap().get(0).unwrap();
        assert_eq!(up, 2, "up_count should match the String-typed variant");
        assert_eq!(down, 1, "down_count should match the String-typed variant");
        assert_eq!(limit_up, 2, "limit_up should match the String-typed variant");
    }

    // -----------------------------------------------------------------------
    // T04 finding: the 7 梯队 (ladder) aggs are part of the SAME
    // `group_by("date").agg(...)` block as the 14 base aggs
    // (`market_phase.ladder_daily_aggs()` + `ladder_promo_aggs()`), so a true
    // 1:1 port of `_aggregate_daily`'s group_by must include them. T01's survey
    // missed them (the research doc contains no `ladder` reference), so the
    // kernel was extended in T04. This test pins the semantics.
    // -----------------------------------------------------------------------

    #[test]
    fn ladder_aggs_match_python_ladder_daily_and_promo_aggs() {
        // 6 stocks on "2025-01-10" with consecutive_limit_ups:
        //   A: consec=1  → first_board
        //   B: consec=2  → ge2_count
        //   C: consec=3  → ge2_count + ge3_count
        //   D: consec=5  → ge2_count + ge3_count + ge5_count
        //   E: consec=7  → ge2_count + ge3_count + ge5_count
        //   F: consec=0  → none
        // Expected:
        //   first_board = 1 (A)
        //   ge2_count   = 4 (B, C, D, E)
        //   ge3_count   = 3 (C, D, E)
        //   ge5_count   = 2 (D, E)
        //   rungs_filled = 4 (distinct consec values >= 2 → {2, 3, 5, 7})
        // `_prev_consec` (promo):
        //   A: prev=0 → not in pool
        //   B: prev=1, consec=2 = prev+1 → pool + ok
        //   C: prev=2, consec=3 = prev+1 → pool + ok
        //   D: prev=1, consec=5 ≠ 2   → pool only
        //   E: prev=3, consec=7 ≠ 4   → pool only
        //   F: prev=0 → not in pool
        // Expected: promo_pool = 4 (B, C, D, E), promo_ok = 2 (B, C)
        let per_stock = make_per_stock_df(&[
            ("2025-01-10", "A", Some(0.01), Some(1e8), false, false, false, 1, Some(11.0), Some(10.0)),
            ("2025-01-10", "B", Some(0.01), Some(1e8), false, false, false, 2, Some(11.0), Some(10.0)),
            ("2025-01-10", "C", Some(0.01), Some(1e8), false, false, false, 3, Some(11.0), Some(10.0)),
            ("2025-01-10", "D", Some(0.01), Some(1e8), false, false, false, 5, Some(11.0), Some(10.0)),
            ("2025-01-10", "E", Some(0.01), Some(1e8), false, false, false, 7, Some(11.0), Some(10.0)),
            ("2025-01-10", "F", Some(0.01), Some(1e8), false, false, false, 0, Some(11.0), Some(10.0)),
        ]);
        // Attach `_prev_consec` (the column `market_phase.with_prev_consecutive`
        // produces via `pl.col("consecutive_limit_ups").shift(1).over("symbol")`).
        // prev values: A=0, B=1, C=2, D=1, E=3, F=0
        let prev_series = Series::new("__prev".into(), vec![0u32, 1, 2, 1, 3, 0]);
        let per_stock = per_stock
            .lazy()
            .with_column(
                lit(prev_series)
                    .cast(DataType::UInt32)
                    .alias("_prev_consec"),
            )
            .collect()
            .unwrap();

        let index_pct_map: HashMap<String, f64> = HashMap::new();
        let df = aggregate_daily_inner(&per_stock, &index_pct_map).unwrap();
        assert_eq!(df.height(), 1);

        let first_board: u32 = df.column("first_board").unwrap().u32().unwrap().get(0).unwrap();
        let ge2: u32 = df.column("ge2_count").unwrap().u32().unwrap().get(0).unwrap();
        let ge3: u32 = df.column("ge3_count").unwrap().u32().unwrap().get(0).unwrap();
        let ge5: u32 = df.column("ge5_count").unwrap().u32().unwrap().get(0).unwrap();
        let rungs: u32 = df.column("rungs_filled").unwrap().u32().unwrap().get(0).unwrap();
        let pool: u32 = df.column("promo_pool").unwrap().u32().unwrap().get(0).unwrap();
        let ok: u32 = df.column("promo_ok").unwrap().u32().unwrap().get(0).unwrap();

        assert_eq!(first_board, 1, "first_board = consec==1 count");
        assert_eq!(ge2, 4, "ge2_count = consec>=2 count");
        assert_eq!(ge3, 3, "ge3_count = consec>=3 count");
        assert_eq!(ge5, 2, "ge5_count = consec>=5 count");
        assert_eq!(rungs, 4, "rungs_filled = distinct consec values >= 2");
        assert_eq!(pool, 4, "promo_pool = prev>=1 count");
        assert_eq!(ok, 2, "promo_ok = prev>=1 AND consec==prev+1 count");
    }
}