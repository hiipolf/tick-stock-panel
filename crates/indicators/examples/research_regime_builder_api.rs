//! Throwaway research example for Path A-5 service-level kernels
//! (`compute_batch_kernel` / `aggregate_daily_kernel`).
//!
//! Survey-only: builds a 5-day × 30-symbol daily synthetic fixture
//! (in-memory DataFrames, no parquet IO, no Python runtime attached), exercises
//! the 2 stub kernels' **PyO3 0.29.3 + pyo3-polars 0.28.0 + polars 0.55.2 type
//! surface** (2 `#[pyfunction]` stubs with production signatures), constructs the
//! resulting `polars::DataFrame` outputs via column-wise append (mirrors
//! Path A-3 + Path A-4 precedent: pure-Rust loop on per-stock DataFrame +
//! `HashMap<String, f64>` fixture). Verifies that:
//!
//! 1. `PathBuf` parameter compiles (kernel 1 IO helper).
//! 2. `u32` scalar parameter compiles (kernel 1 `warmup_days`).
//! 3. 2× `PyDataFrame` round-trip compiles (kernel 2 input/output).
//! 4. `HashMap<String, f64>` parameter compiles (kernel 2 `index_pct_map`).
//! 5. `LazyFrame::scan_parquet(...).filter(...).collect()` chain compiles
//!    (kernel 1 IO path).
//! 6. `LazyFrame::group_by("date").agg([...14 expressions...])` chain compiles
//!    (kernel 2 heavy reduce).
//!
//! Run:
//!   cargo check --example research_regime_builder_api -p tf-rs-indicators
//!   cargo run   --example research_regime_builder_api
//!
//! Note: `#[pyfunction]` attributes are declared on the stub functions to confirm
//! the signature compiles with pyo3-polars types. Production registration
//! (`wrap_pyfunction!` into `m`) happens in Ticket 02 in
//! `crates/indicators/src/services_regime/{compute_batch,aggregate_daily}.rs`.

use std::collections::HashMap;
use std::path::PathBuf;

use polars::prelude::*;
use pyo3::prelude::*;
use pyo3_polars::PyDataFrame;

// ---------------------------------------------------------------------------
// Stub kernel signatures (production-grade; bodies are T01 placeholders).
// These compile-check the PyO3 0.29.3 + pyo3-polars 0.28.0 type surface but
// are NOT invoked from `cargo run` (no Python runtime attached).
// ---------------------------------------------------------------------------

/// Stub for kernel 1 — `compute_batch_kernel(data_dir: PathBuf, batch_start: &str,
/// batch_end: &str, warmup_days: u32) -> PyDataFrame`.
///
/// Internally (per Spec 0005 §"Implementation Decisions"):
///   1. warmup_start = batch_start - timedelta(days=warmup_days)
///   2. LazyFrame::scan_parquet(data_dir / "**" / "*.parquet").filter(...).collect()
///   3-5. compute_indicators + compute_limit_signals + with_prev_consecutive
///        (per Q3=a Path A-3 — these Polars expression chains stay in the
///        Python adapter; the stub body just returns synthesized columns)
///   6. Filter date >= batch_start (drop warmup rows)
///   7. Return resulting DataFrame (per-stock rows with indicator columns)
///
/// Empty case: DataFrame::default() (no enriched parquet → empty).
#[pyfunction]
#[pyo3(signature = (data_dir, batch_start, batch_end, warmup_days))]
fn compute_batch_kernel_stub(
    data_dir: PathBuf,
    batch_start: &str,
    batch_end: &str,
    warmup_days: u32,
) -> PyResult<PyDataFrame> {
    let _ = (data_dir, batch_start, batch_end, warmup_days);
    Err(pyo3::exceptions::PyNotImplementedError::new_err(
        "T01 stub — Ticket 02 implements body",
    ))
}

/// Stub for kernel 2 — `aggregate_daily_kernel(per_stock_df: PyDataFrame,
/// index_pct_map: HashMap<String, f64>) -> PyDataFrame`.
///
/// Internally (per Spec 0005 §"Implementation Decisions"):
///   1. Filter `needed` cols (date, change_pct, amount, signal_limit_up,
///      signal_limit_down, signal_broken_limit_up, consecutive_limit_ups, close, ma20)
///   2. If consecutive_limit_ups + symbol: with_prev_consecutive (Polars expression
///      chain — may stay in Python per Q3=a)
///   3. Single group_by("date").agg([...14 aggregations...])
///   4. Join `index_pct_map` by str(date) → index_pct column
///   5. Return resulting DataFrame (per-day rows with 14 indicator columns + index_pct)
#[pyfunction]
fn aggregate_daily_kernel_stub(
    per_stock_df: PyDataFrame,
    index_pct_map: HashMap<String, f64>,
) -> PyResult<PyDataFrame> {
    let _ = (per_stock_df, index_pct_map);
    Err(pyo3::exceptions::PyNotImplementedError::new_err(
        "T01 stub — Ticket 02 implements body",
    ))
}

// ---------------------------------------------------------------------------
// T01 pure-Rust demo path: same algorithm, no Python runtime.
// Mirrors `crates/indicators/examples/research_build_sector_rotation_api.rs`
// (Path A-4 T01) — internal `Row` struct for ergonomic per-stock iteration
// without fighting HashMap<String, Option<f64>> type unification.
// Production kernels operate on the PyO3 surface; this demo path is solely
// for surveying algorithm behaviour + DataFrame construction.
// ---------------------------------------------------------------------------

/// Per-row fixture (synthetic daily bar for one symbol at one date).
#[derive(Clone, Debug)]
struct DailyRow {
    symbol: String,
    date: String,            // ISO date "YYYY-MM-DD"
    change_pct: Option<f64>,
    amount: Option<f64>,
    signal_limit_up: bool,
    signal_limit_down: bool,
    signal_broken_limit_up: bool,
    consecutive_limit_ups: u32,
    close: Option<f64>,
    ma20: Option<f64>,
}

fn daily_row(
    symbol: &str,
    date: &str,
    change_pct: Option<f64>,
    amount: Option<f64>,
    signal_limit_up: bool,
    signal_limit_down: bool,
    signal_broken_limit_up: bool,
    consecutive_limit_ups: u32,
    close: Option<f64>,
    ma20: Option<f64>,
) -> DailyRow {
    DailyRow {
        symbol: symbol.to_string(),
        date: date.to_string(),
        change_pct,
        amount,
        signal_limit_up,
        signal_limit_down,
        signal_broken_limit_up,
        consecutive_limit_ups,
        close,
        ma20,
    }
}

// ---- Pure-Rust reduce kernels (mirror what kernels 1/2 will do in T02) ----

/// Demo body for `compute_batch_kernel` — synthesize the per-stock batch
/// DataFrame (the per-day 30-symbol rows after `compute_indicators` +
/// `compute_limit_signals` + `with_prev_consecutive` Polars expression chain
/// runs in Python per Q3=a). The T02 kernel reads parquet via
/// `LazyFrame::scan_parquet` and runs the expression chain in Rust.
///
/// Output schema (per T03 fixture design):
///   - symbol: String
///   - date: String (ISO "YYYY-MM-DD")
///   - change_pct: Float64
///   - amount: Float64
///   - signal_limit_up: Boolean
///   - signal_limit_down: Boolean
///   - signal_broken_limit_up: Boolean
///   - consecutive_limit_ups: UInt32
///   - close: Float64
///   - ma20: Float64
///   - prev_consecutive_limit_ups: UInt32 (per `pl.col().shift(1).over("symbol")`)
fn synthesize_per_stock_batch_df(rows: &[DailyRow]) -> DataFrame {
    let n = rows.len();
    let mut symbols: Vec<&str> = Vec::with_capacity(n);
    let mut dates: Vec<&str> = Vec::with_capacity(n);
    let mut pcts: Vec<Option<f64>> = Vec::with_capacity(n);
    let mut amts: Vec<Option<f64>> = Vec::with_capacity(n);
    let mut sig_lu: Vec<bool> = Vec::with_capacity(n);
    let mut sig_ld: Vec<bool> = Vec::with_capacity(n);
    let mut sig_bl: Vec<bool> = Vec::with_capacity(n);
    let mut consec: Vec<u32> = Vec::with_capacity(n);
    let mut closes: Vec<Option<f64>> = Vec::with_capacity(n);
    let mut ma20s: Vec<Option<f64>> = Vec::with_capacity(n);
    let mut prev_consec: Vec<Option<u32>> = Vec::with_capacity(n);

    // Apply `with_prev_consecutive` semantics: shift(1).over("symbol")
    // We compute the prev_consecutive per row by looking up the previous
    // (by date desc) row with the same symbol — T02 will use Polars expression.
    let mut prev_lookup: HashMap<(String, String), u32> = HashMap::new();
    for r in rows {
        prev_lookup.insert((r.symbol.clone(), r.date.clone()), r.consecutive_limit_ups);
    }
    // Find the previous date for each (symbol, date). For the synthetic fixture,
    // the previous date is well-defined because we sort by (symbol, date).
    let mut sorted: Vec<&DailyRow> = rows.iter().collect();
    sorted.sort_by(|a, b| a.symbol.cmp(&b.symbol).then_with(|| a.date.cmp(&b.date)));
    let mut last_consec_per_symbol: HashMap<String, u32> = HashMap::new();
    // Walk in (symbol, date) ascending; prev_consec = last_consec_per_symbol
    // (which holds the previous row's consec, or 0 on first occurrence).
    let mut ordered: Vec<(usize, u32)> = Vec::with_capacity(n);
    for r in &sorted {
        let prev = last_consec_per_symbol.get(&r.symbol).copied().unwrap_or(0);
        ordered.push((0, prev)); // placeholder; will fill below
        last_consec_per_symbol.insert(r.symbol.clone(), r.consecutive_limit_ups);
    }
    // Map back: build (row index in original `rows` order) → prev_consec.
    let mut prev_by_orig: HashMap<usize, Option<u32>> = HashMap::new();
    for (sorted_idx, r) in sorted.iter().enumerate() {
        // Find the original index of this row.
        let orig_idx = rows.iter().position(|rr| std::ptr::eq(rr, *r)).unwrap_or(0);
        let prev = if sorted_idx == 0 {
            None
        } else {
            Some(ordered[sorted_idx - 1].1)
        };
        prev_by_orig.insert(orig_idx, prev);
    }

    for (i, r) in rows.iter().enumerate() {
        symbols.push(&r.symbol);
        dates.push(&r.date);
        pcts.push(r.change_pct);
        amts.push(r.amount);
        sig_lu.push(r.signal_limit_up);
        sig_ld.push(r.signal_limit_down);
        sig_bl.push(r.signal_broken_limit_up);
        consec.push(r.consecutive_limit_ups);
        closes.push(r.close);
        ma20s.push(r.ma20);
        prev_consec.push(prev_by_orig.get(&i).copied().flatten());
    }

    df![
        "symbol"                     => symbols,
        "date"                       => dates,
        "change_pct"                 => pcts,
        "amount"                     => amts,
        "signal_limit_up"            => sig_lu,
        "signal_limit_down"          => sig_ld,
        "signal_broken_limit_up"     => sig_bl,
        "consecutive_limit_ups"      => consec,
        "close"                      => closes,
        "ma20"                       => ma20s,
        "prev_consecutive_limit_ups" => prev_consec,
    ]
    .expect("compute_batch_kernel output DataFrame construction")
}

/// Demo body for `aggregate_daily_kernel` — compute the 14-aggregation
/// group_by + join `index_pct_map` by str(date).
///
/// Output schema (per Spec 0005 §"Implementation Decisions" kernel 2 step 3-4):
///   - date: String
///   - up_count: UInt32
///   - down_count: UInt32
///   - total_count: UInt32
///   - avg_pct: Float64
///   - median_pct: Float64
///   - strong_up_count: UInt32
///   - strong_down_count: UInt32
///   - limit_up: UInt32
///   - limit_down: UInt32
///   - broken_limit: UInt32
///   - max_consecutive: UInt32
///   - total_amount: Float64
///   - avg_amount: Float64
///   - ma20_above_pct: Float64
///   - index_pct: Float64 (from HashMap join)
#[derive(Clone, Debug)]
struct DailySummary {
    date: String,
    up_count: u32,
    down_count: u32,
    total_count: u32,
    avg_pct: f64,
    median_pct: f64,
    strong_up_count: u32,
    strong_down_count: u32,
    limit_up: u32,
    limit_down: u32,
    broken_limit: u32,
    max_consecutive: u32,
    total_amount: f64,
    avg_amount: f64,
    ma20_above_pct: f64,
    index_pct: Option<f64>,
}

fn aggregate_daily_pure(
    rows: &[DailyRow],
    index_pct_map: &HashMap<String, f64>,
) -> Vec<DailySummary> {
    use std::collections::BTreeMap;
    let mut by_date: BTreeMap<String, Vec<&DailyRow>> = BTreeMap::new();
    for r in rows {
        by_date.entry(r.date.clone()).or_default().push(r);
    }

    by_date
        .into_iter()
        .map(|(date, day_rows)| {
            let total_count = day_rows.len() as u32;
            let mut up_count = 0u32;
            let mut down_count = 0u32;
            let mut strong_up_count = 0u32;
            let mut strong_down_count = 0u32;
            let mut pcts_for_mean = Vec::with_capacity(day_rows.len());
            let mut pcts_for_median: Vec<f64> = Vec::with_capacity(day_rows.len());
            let mut total_amount = 0.0_f64;
            let mut amount_count = 0u32;
            let mut limit_up = 0u32;
            let mut limit_down = 0u32;
            let mut broken_limit = 0u32;
            let mut max_consecutive: u32 = 0;
            let mut above_count: u32 = 0;
            let mut total_for_above: u32 = 0;

            for r in &day_rows {
                if let Some(p) = r.change_pct {
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
                if let Some(a) = r.amount {
                    total_amount += a;
                    amount_count += 1;
                }
                if r.signal_limit_up {
                    limit_up += 1;
                }
                if r.signal_limit_down {
                    limit_down += 1;
                }
                if r.signal_broken_limit_up {
                    broken_limit += 1;
                }
                if r.consecutive_limit_ups > max_consecutive {
                    max_consecutive = r.consecutive_limit_ups;
                }
                // ma20_above_pct: fraction where close > ma20 (finite pairs only)
                if let (Some(c), Some(m)) = (r.close, r.ma20) {
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
            let index_pct = index_pct_map.get(&date).copied();

            DailySummary {
                date,
                up_count,
                down_count,
                total_count,
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
            }
        })
        .collect()
}

fn build_daily_summary_df(summaries: Vec<DailySummary>) -> DataFrame {
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
        "date"               => dates,
        "up_count"           => up_counts,
        "down_count"         => down_counts,
        "total_count"        => total_counts,
        "avg_pct"            => avg_pcts,
        "median_pct"         => median_pcts,
        "strong_up_count"    => strong_ups,
        "strong_down_count"  => strong_downs,
        "limit_up"           => limit_ups,
        "limit_down"         => limit_downs,
        "broken_limit"       => broken_limits,
        "max_consecutive"    => max_consecs,
        "total_amount"       => total_amounts,
        "avg_amount"         => avg_amounts,
        "ma20_above_pct"     => ma20_above_pcts,
        "index_pct"          => index_pcts,
    ]
    .expect("aggregate_daily_kernel output DataFrame construction")
}

// ---- Lazy scan_parquet / filter / group_by+agg pattern compile-probes ----
//
// These don't actually run on the synthetic fixture (which is built via
// synthesize_per_stock_batch_df above). They compile-check the LazyFrame +
// scan_parquet + filter + group_by + agg chain that T02 will use, so any
// future polars-version drift in those APIs is caught at T01 time.

#[allow(dead_code)]
fn probe_lazy_scan_chain() -> PolarsResult<DataFrame> {
    // LazyFrame::scan_parquet — per polars-lazy-0.55.2/src/scan/parquet.rs:169
    // The glob path here is a stub; the actual kernel reads from data_dir.
    let glob = PlRefPath::new("test/**/*.parquet");
    let _lf = LazyFrame::scan_parquet(glob, ScanArgsParquet::default())?
        // .filter() — per polars-lazy-0.55.2/src/frame/mod.rs:950
        .filter(col("date").gt_eq(lit("2025-01-01")).and(col("date").lt_eq(lit("2025-01-05"))))
        .select([col("date"), col("symbol"), col("close")])
        // .collect() — per polars-lazy-0.55.2/src/frame/mod.rs:746
        .collect()?;
    let df = _lf;
    Ok(df)
}

#[allow(dead_code)]
fn probe_group_by_agg_chain(per_stock_df: DataFrame) -> PolarsResult<DataFrame> {
    // 14-aggregation group_by("date").agg([...]) — per Spec 0005 §"Implementation Decisions"
    // kernel 2 step 3; this is the production kernel 2 reduce expression chain.
    let aggs: Vec<Expr> = vec![
        col("change_pct").gt(lit(0.0)).sum().alias("up_count"),
        col("change_pct").lt(lit(0.0)).sum().alias("down_count"),
        len().alias("total_count"),
        col("change_pct").mean().alias("avg_pct"),
        col("change_pct").median().alias("median_pct"),
        col("change_pct").gt_eq(lit(0.03)).sum().alias("strong_up_count"),
        col("change_pct").lt_eq(lit(-0.03)).sum().alias("strong_down_count"),
        col("signal_limit_up").cast(DataType::Boolean).sum().alias("limit_up"),
        col("signal_limit_down").cast(DataType::Boolean).sum().alias("limit_down"),
        col("signal_broken_limit_up").cast(DataType::Boolean).sum().alias("broken_limit"),
        col("consecutive_limit_ups").max().alias("max_consecutive"),
        col("amount").sum().alias("total_amount"),
        col("amount").mean().alias("avg_amount"),
        // ma20_above_pct as 2 intermediate cols + 1 final
        col("close")
            .gt(col("ma20"))
            .cast(DataType::Int64)
            .sum()
            .true_div(col("close").count())
            .alias("ma20_above_pct"),
    ];
    // LazyFrame::group_by — per polars-lazy-0.55.2/src/frame/mod.rs:1052
    // LazyGroupBy::agg — per polars-lazy-0.55.2/src/frame/mod.rs:2070
    let _lf = per_stock_df
        .lazy()
        .group_by([col("date")])
        .agg(aggs)
        .collect()?;
    let df = _lf;
    Ok(df)
}

// ---------------------------------------------------------------------------
// IO + filter / group_by / agg expression-probe (for T02 attention).
// These compile-check the *Expr::shift.over* chain for kernel 1
// `with_prev_consecutive` (per Spec 0005 kernel 1 step 5) — even though the
// heavy Polars expression stays in Python per Q3=a, T02 may move it into
// the kernel as Polars expressions; the probe below validates the chain.
// ---------------------------------------------------------------------------

#[allow(dead_code)]
fn probe_shift_over_chain(per_stock_df: DataFrame) -> PolarsResult<DataFrame> {
    // Expr::shift — per polars-plan-0.55.2/src/dsl/mod.rs:634-637
    // Expr::over — per polars-plan-0.55.2/src/dsl/mod.rs:818-823
    let prev_consec = col("consecutive_limit_ups")
        .shift(lit(1))
        .over(["symbol"])?;
    let _lf = per_stock_df
        .lazy()
        .with_columns([prev_consec.alias("prev_consecutive_limit_ups")])
        .collect()?;
    let df = _lf;
    Ok(df)
}

// ---------------------------------------------------------------------------
// Main — exercise both kernels via synthesized DataFrames.
// ---------------------------------------------------------------------------

fn main() {
    // -----------------------------------------------------------------------
    // Synthetic fixture: 5 trading days × 30 symbols daily-grain = 150 rows.
    // Mirrors the T03 fixture design (subset of Path A-3's 38 symbols).
    // -----------------------------------------------------------------------
    let dates: Vec<String> = (0..5)
        .map(|i| format!("2025-01-{:02}", 10 + i))
        .collect();
    let symbols: Vec<String> = (0..30)
        .map(|i| format!("60000{i:02}.SH"))
        .collect();
    let mut rows: Vec<DailyRow> = Vec::new();
    for (d_idx, date) in dates.iter().enumerate() {
        for (s_idx, sym) in symbols.iter().enumerate() {
            // Synthetic change_pct: linear ramp by symbol index, alternating sign.
            let pct_raw = -0.05 + (s_idx as f64 / 30.0) * 0.10;
            let change_pct = Some(if d_idx % 2 == 0 { pct_raw } else { -pct_raw });
            let amount = Some(1.0e8 + (s_idx as f64) * 1.0e6);
            // Every 7th symbol-day is a limit-up signal
            let signal_limit_up = s_idx % 7 == 0 && d_idx % 3 == 0;
            // Every 11th symbol-day is a limit-down signal
            let signal_limit_down = s_idx % 11 == 0 && d_idx % 4 == 0;
            let signal_broken_limit_up = signal_limit_up && (s_idx + d_idx) % 2 == 0;
            let consecutive_limit_ups = if signal_limit_up { (d_idx as u32) + 1 } else { 0 };
            let close = Some(10.0 + (s_idx as f64) * 0.5 + d_idx as f64 * 0.05);
            let ma20 = Some(10.0 + (s_idx as f64) * 0.5 + d_idx as f64 * 0.04);
            rows.push(daily_row(
                sym,
                date,
                change_pct,
                amount,
                signal_limit_up,
                signal_limit_down,
                signal_broken_limit_up,
                consecutive_limit_ups,
                close,
                ma20,
            ));
        }
    }

    // -----------------------------------------------------------------------
    // Demo 1: kernel 1 — `compute_batch_kernel_stub` signature probe +
    // synthesize per-stock batch DataFrame (IO surrogate for T02).
    // -----------------------------------------------------------------------
    println!("[research] compute_batch_kernel_stub signature compiles (data_dir: PathBuf, batch_start: &str, batch_end: &str, warmup_days: u32).");

    let per_stock_df = synthesize_per_stock_batch_df(&rows);
    println!("[research] kernel 1 compute_batch_kernel synthesize → {} rows × {} cols:", per_stock_df.height(), per_stock_df.width());
    println!("{}", per_stock_df.head(Some(5)));

    // -----------------------------------------------------------------------
    // Demo 2: kernel 2 — `aggregate_daily_kernel_stub` signature probe +
    // synthesize per-day summary DataFrame (heavy group_by + agg surrogate).
    // -----------------------------------------------------------------------
    println!("[research] aggregate_daily_kernel_stub signature compiles (per_stock_df: PyDataFrame, index_pct_map: HashMap<String, f64>).");

    // Synthesize the index_pct_map: 5 dates with synthetic index change_pct values
    let mut index_pct_map: HashMap<String, f64> = HashMap::new();
    for (i, date) in dates.iter().enumerate() {
        index_pct_map.insert(date.clone(), -0.01 + (i as f64 / 5.0) * 0.02);
    }
    let summaries = aggregate_daily_pure(&rows, &index_pct_map);
    let daily_summary_df = build_daily_summary_df(summaries);
    println!("[research] kernel 2 aggregate_daily_kernel synthesize → {} rows × {} cols:", daily_summary_df.height(), daily_summary_df.width());
    println!("{}", daily_summary_df);

    // -----------------------------------------------------------------------
    // PyO3 signature compile probe.
    // The 2 stubs above are #[pyfunction]-annotated; we don't call them
    // (no Python runtime attached in a standalone `cargo run`). The fact
    // that this file compiles at all proves the PyO3 0.29.3 + pyo3-polars
    // 0.28.0 type surface is well-formed against the vendored deps.
    // -----------------------------------------------------------------------
    let _ = (
        compute_batch_kernel_stub as fn(_, _, _, _) -> _,
        aggregate_daily_kernel_stub as fn(_, _) -> _,
    );
    println!("[research] 2 PyO3 signature probes compiled OK.");

    // -----------------------------------------------------------------------
    // Lazy scan + group_by + agg + shift.over chain compile-probes.
    // These confirm the polars 0.55.2 lazy APIs used by T02 production kernels
    // are well-formed against the workspace pin.
    // -----------------------------------------------------------------------
    println!("[research] Probe polars-lazy-0.55.2 lazy chains:");
    println!("  - LazyFrame::scan_parquet + filter + collect chain: signature compiles (probe_lazy_scan_chain)");
    println!("  - LazyFrame::group_by + 14-agg chain: signature compiles (probe_group_by_agg_chain)");
    println!("  - Expr::shift.over([symbol]) chain: signature compiles (probe_shift_over_chain)");
    let _ = probe_lazy_scan_chain as fn() -> _;
    let _ = probe_group_by_agg_chain as fn(_) -> _;
    let _ = probe_shift_over_chain as fn(_) -> _;
}
