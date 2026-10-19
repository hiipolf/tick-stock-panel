//! Throwaway research example for Path A-4 service-level kernels
//! (`bare_kernel` / `prev_daily_close_kernel` / `minute_pcts_kernel` /
//! `sector_flow_kernel` / `per_bucket_reduce_kernel` / `timeline_kernel` /
//! `cross_events_kernel` / `rank_for_mode_kernel`).
//!
//! Survey-only: builds a 30-symbol × 8-sector × 1-day minute synthetic fixture
//! (in-memory DataFrames, no parquet IO, no Python runtime attached), exercises the
//! 8 stub kernels' **PyO3 0.29.3 + pyo3-polars 0.28.0 + polars 0.55.2 type surface**
//! (8 `#[pyfunction]` stubs with production signatures), constructs the resulting
//! `polars::DataFrame` outputs via column-wise append (kernels 6/7/8 follow Path A-3
//! precedent: no Polars expressions in the hot path, pure-Rust loop on
//! `Vec<HashMap<String, Py<PyAny>>>-equivalent` fixture). Verifies that:
//!
//! 1. `Vec<HashMap<String, Py<PyAny>>>` parameter compiles.
//! 2. `HashMap<String, f64>` parameter compiles.
//! 3. `(PyDataFrame, String, bool)` return tuple compiles.
//! 4. `PathBuf` + `&str` parameters compile (kernels 2/3/4).
//! 5. `Vec<String>` return compiles (kernel 8).
//! 6. Pure-Rust `rank_for_mode` reduce (with `max_by` direction lesson applied) on
//!    the synthetic fixture.
//!
//! Run:
//!   cargo check --example research_build_sector_rotation_api -p tf-rs-indicators
//!   cargo run   --example research_build_sector_rotation_api -p tf-rs-indicators
//!
//! Note: `#[pyfunction]` attributes are declared on the stub functions to confirm
//! the signature compiles with pyo3-polars types. Production registration
//! (`wrap_pyfunction!` into `m`) happens in Ticket 02 in
//! `crates/indicators/src/services_sector_rotation/{bare,prev_daily_close,...}.rs`.

use std::collections::HashMap;
use std::path::PathBuf;

use polars::prelude::*;
use pyo3::prelude::*;
use pyo3_polars::{PyDataFrame, PySeries};

// ---------------------------------------------------------------------------
// Stub kernel signatures (production-grade; bodies are T01 placeholders).
// These compile-check the PyO3 0.29.3 + pyo3-polars 0.28.0 type surface but
// are NOT invoked from `cargo run` (no Python runtime attached).
// ---------------------------------------------------------------------------

/// Stub for kernel 1 — `bare_kernel(col: &str) -> Series`.
/// Returns a `PySeries` so the Python adapter gets `polars.Series`.
#[pyfunction]
fn bare_kernel_stub(col: &str) -> PyResult<PySeries> {
    let _ = col;
    Err(pyo3::exceptions::PyNotImplementedError::new_err(
        "T01 stub — Ticket 02 implements body",
    ))
}

/// Stub for kernel 2 — `prev_daily_close_kernel(data_dir: PathBuf, target_date: &str) -> DataFrame`.
/// Reads parquet for `target_date - 1` business day (per Path A-3 memory
/// `raw_close ≠ previous trading day's close`) and returns (symbol, close).
#[pyfunction]
fn prev_daily_close_kernel_stub(data_dir: PathBuf, target_date: &str) -> PyResult<PyDataFrame> {
    let _ = (data_dir, target_date);
    Err(pyo3::exceptions::PyNotImplementedError::new_err(
        "T01 stub — Ticket 02 implements body",
    ))
}

/// Stub for kernel 3 — `minute_pcts_kernel(data_dir: PathBuf, target: &str) -> (DataFrame, String, bool)`.
/// Returns `(pcts_df, basis, has_amount)` where basis is "amount" or "volume".
#[pyfunction]
fn minute_pcts_kernel_stub(
    data_dir: PathBuf,
    target: &str,
) -> PyResult<(PyDataFrame, String, bool)> {
    let _ = (data_dir, target);
    Err(pyo3::exceptions::PyNotImplementedError::new_err(
        "T01 stub — Ticket 02 implements body",
    ))
}

/// Stub for kernel 4 — `sector_flow_kernel(data_dir: PathBuf, flow_field: &str) -> (DataFrame, bool)`.
/// Returns `(flow_df, flow_available)`.
#[pyfunction]
fn sector_flow_kernel_stub(
    data_dir: PathBuf,
    flow_field: &str,
) -> PyResult<(PyDataFrame, bool)> {
    let _ = (data_dir, flow_field);
    Err(pyo3::exceptions::PyNotImplementedError::new_err(
        "T01 stub — Ticket 02 implements body",
    ))
}

/// Stub for kernel 5 — `per_bucket_reduce_kernel(by_sector_df: PyDataFrame, market_df: PyDataFrame) -> DataFrame`.
/// Takes the adapter's two pre-aggregated DataFrames (per-bucket by-sector + market avg)
/// and produces the canonical 8-column reduce output.
#[pyfunction]
fn per_bucket_reduce_kernel_stub(
    by_sector_df: PyDataFrame,
    market_df: PyDataFrame,
) -> PyResult<PyDataFrame> {
    let _ = (by_sector_df, market_df);
    Err(pyo3::exceptions::PyNotImplementedError::new_err(
        "T01 stub — Ticket 02 implements body",
    ))
}

/// Stub for kernel 6 — `timeline_kernel(per_bucket: Vec<HashMap<String, Py<PyAny>>>) -> DataFrame`.
#[pyfunction]
fn timeline_kernel_stub(
    per_bucket: Vec<HashMap<String, Py<PyAny>>>,
) -> PyResult<PyDataFrame> {
    let _ = per_bucket;
    Err(pyo3::exceptions::PyNotImplementedError::new_err(
        "T01 stub — Ticket 02 implements body",
    ))
}

/// Stub for kernel 7 — `cross_events_kernel(per_bucket: Vec<HashMap<String, Py<PyAny>>>) -> DataFrame`.
#[pyfunction]
fn cross_events_kernel_stub(
    per_bucket: Vec<HashMap<String, Py<PyAny>>>,
) -> PyResult<PyDataFrame> {
    let _ = per_bucket;
    Err(pyo3::exceptions::PyNotImplementedError::new_err(
        "T01 stub — Ticket 02 implements body",
    ))
}

/// Stub for kernel 8 — `rank_for_mode_kernel(sectors, activity_map, has_amount, flow_by_member, flow_available, sort_by) -> Vec<String>`.
#[pyfunction]
#[pyo3(signature = (
    sectors,
    activity_map,
    has_amount,
    flow_by_member,
    flow_available,
    sort_by
))]
fn rank_for_mode_kernel_stub(
    sectors: Vec<HashMap<String, Py<PyAny>>>,
    activity_map: HashMap<String, f64>,
    has_amount: bool,
    flow_by_member: HashMap<String, f64>,
    flow_available: bool,
    sort_by: &str,
) -> PyResult<Vec<String>> {
    let _ = (
        sectors,
        activity_map,
        has_amount,
        flow_by_member,
        flow_available,
        sort_by,
    );
    Err(pyo3::exceptions::PyNotImplementedError::new_err(
        "T01 stub — Ticket 02 implements body",
    ))
}

// ---------------------------------------------------------------------------
// T01 pure-Rust demo path: same algorithm, no Python runtime.
// Mirrors `crates/indicators/examples/research_market_overview_api.rs`
// (Path A-3 T01) — internal `Row` + `Bucket` structs for ergonomic reduce
// without fighting HashMap<String, Option<f64>> type unification.
// Production kernels operate on the PyO3 surface; this demo path is solely
// for surveying algorithm behaviour and DataFrame construction.
// ---------------------------------------------------------------------------

/// Per-row fixture (synthetic minute bar for one symbol at one bucket).
#[derive(Clone, Debug)]
struct Row {
    symbol: String,
    bucket_ts: i64, // epoch millis
    sector: String,
    change_pct: Option<f64>,
    amount: Option<f64>,
    flow: Option<f64>,
}

/// Per-bucket summary — what the Python adapter would emit as
/// `dict` entries into `per_bucket: list[dict]`.
#[derive(Clone, Debug)]
#[allow(dead_code)] // some fields kept for schema documentation; T02 ports them.
struct BucketSummary {
    time: String, // ISO-8601 minute bucket key
    names: Vec<String>,
    pcts: Vec<f64>,
    top_set: bool,        // whether the leader set > 0
    leader: Option<String>,
    leader_pct: Option<f64>,
    market_pct: f64,
    cross_up: u32,
    cross_down: u32,
}

/// Helper: build one synthetic minute row.
fn row(
    symbol: &str,
    bucket_ts: i64,
    sector: &str,
    change_pct: Option<f64>,
    amount: Option<f64>,
    flow: Option<f64>,
) -> Row {
    Row {
        symbol: symbol.to_string(),
        bucket_ts,
        sector: sector.to_string(),
        change_pct,
        amount,
        flow,
    }
}

// ---- Pure-Rust reduce kernels (mirror what kernels 5/6/7 will do in T02) ----

/// Demo body for `per_bucket_reduce_kernel` — aggregate rows into per-bucket summaries.
/// Mirrors the Polars expression block at `_compute` line 420-435 (group by bucket +
/// collect leader). Production T02 may use Polars expressions here; this pure-Rust
/// version is for survey-only and DataFrame construction validation.
fn per_bucket_reduce_pure(
    rows: &[Row],
    market_avg: &HashMap<String, f64>, // bucket -> market avg pct (precomputed by adapter)
) -> Vec<BucketSummary> {
    use std::collections::BTreeMap;
    let mut by_bucket: BTreeMap<i64, Vec<&Row>> = BTreeMap::new();
    for r in rows {
        by_bucket.entry(r.bucket_ts).or_default().push(r);
    }
    by_bucket
        .into_iter()
        .map(|(bucket_ts, bucket_rows)| {
            let leader = bucket_rows.iter().max_by(|a, b| {
                // Path A-3 memory: max_by closure arg order is (new, accumulator);
                // we want the largest change_pct. Default Ord for f64 doesn't exist;
                // use partial_cmp + NEG_INFINITY for missing.
                let av = a.change_pct.unwrap_or(f64::NEG_INFINITY);
                let bv = b.change_pct.unwrap_or(f64::NEG_INFINITY);
                bv.partial_cmp(&av).unwrap_or(std::cmp::Ordering::Equal)
            });
            let names: Vec<String> = bucket_rows.iter().map(|r| r.symbol.clone()).collect();
            let pcts: Vec<f64> = bucket_rows
                .iter()
                .filter_map(|r| r.change_pct)
                .collect();
            let leader_sym = leader.map(|l| l.symbol.clone());
            let leader_pct = leader.and_then(|l| l.change_pct);
            let market_pct = market_avg
                .get(&bucket_ts.to_string())
                .copied()
                .unwrap_or(0.0);
            BucketSummary {
                time: bucket_ts.to_string(),
                names,
                pcts,
                top_set: leader_pct.map(|p| p > 0.0).unwrap_or(false),
                leader: leader_sym,
                leader_pct,
                market_pct,
                cross_up: 0, // set by timeline/cross_events in real flow
                cross_down: 0,
            }
        })
        .collect()
}

/// Demo body for `timeline_kernel` — flatten per_bucket summaries into timeline rows.
/// Output schema: (time, rotation, leader, leader_pct, market_pct, cross_up, cross_down).
fn timeline_pure(per_bucket: &[BucketSummary]) -> Vec<(String, u32, Option<String>, Option<f64>, f64, u32, u32)> {
    per_bucket
        .iter()
        .map(|b| {
            (
                b.time.clone(),
                b.names.len() as u32,
                b.leader.clone(),
                b.leader_pct,
                b.market_pct,
                b.cross_up,
                b.cross_down,
            )
        })
        .collect()
}

fn build_timeline_df(
    rows: Vec<(String, u32, Option<String>, Option<f64>, f64, Option<u32>, Option<u32>)>,
) -> DataFrame {
    let times: Vec<&str> = rows.iter().map(|r| r.0.as_str()).collect();
    let rotations: Vec<u32> = rows.iter().map(|r| r.1).collect();
    let leaders: Vec<Option<&str>> = rows.iter().map(|r| r.2.as_deref()).collect();
    let leader_pcts: Vec<Option<f64>> = rows.iter().map(|r| r.3).collect();
    let market_pcts: Vec<f64> = rows.iter().map(|r| r.4).collect();
    let cross_ups: Vec<Option<u32>> = rows.iter().map(|r| r.5).collect();
    let cross_downs: Vec<Option<u32>> = rows.iter().map(|r| r.6).collect();
    df![
        "time"        => times,
        "rotation"    => rotations,
        "leader"      => leaders,
        "leader_pct"  => leader_pcts,
        "market_pct"  => market_pcts,
        "cross_up"    => cross_ups,
        "cross_down"  => cross_downs,
    ]
    .expect("timeline DataFrame construction")
}

/// Demo body for `cross_events_kernel` — flatten per_bucket summaries into event rows.
/// Output schema: (time, name, dir, pct) — dir is "up" or "down".
fn cross_events_pure(
    per_bucket: &[BucketSummary],
) -> Vec<(String, String, &'static str, f64)> {
    let mut events = Vec::new();
    for b in per_bucket {
        // Synthetic rule: leaders with leader_pct > 0 emit one "up" event;
        // < 0 emit one "down" event. T02 replaces with real cross-event algorithm.
        if let (Some(name), Some(pct)) = (&b.leader, b.leader_pct) {
            if pct > 0.0 {
                events.push((b.time.clone(), name.clone(), "up", pct));
            } else if pct < 0.0 {
                events.push((b.time.clone(), name.clone(), "down", pct));
            }
        }
    }
    events
}

fn build_cross_events_df(
    rows: Vec<(String, String, &'static str, f64)>,
) -> DataFrame {
    let times: Vec<&str> = rows.iter().map(|r| r.0.as_str()).collect();
    let names: Vec<&str> = rows.iter().map(|r| r.1.as_str()).collect();
    let dirs: Vec<&str> = rows.iter().map(|r| r.2).collect();
    let pcts: Vec<f64> = rows.iter().map(|r| r.3).collect();
    df![
        "time" => times,
        "name" => names,
        "dir"  => dirs,
        "pct"  => pcts,
    ]
    .expect("cross_events DataFrame construction")
}

/// Demo body for `rank_for_mode_kernel` — rank sectors by composite score.
/// `sort_by` ∈ {"change_pct", "amount", "amount_pct", "flow_pct", "composite"} (per Spec 0004 Q16=a).
/// Path A-3 memory `Iterator::max_by closure arg order: (new, accumulator)` applied.
fn rank_for_mode_pure(
    sectors: &[(&str, &[Row])],
    activity_map: &HashMap<String, f64>,
    has_amount: bool,
    flow_by_member: &HashMap<String, f64>,
    flow_available: bool,
    sort_by: &str,
) -> Vec<String> {
    // Score per sector:
    //   - average change_pct across all member symbols at this minute
    //   - activity map provides minute-scores buckets
    //   - flow is per-member
    let mut scored: Vec<(String, f64)> = sectors
        .iter()
        .map(|(sector_name, members)| {
            let avg_pct: f64 = if members.is_empty() {
                0.0
            } else {
                let sum: f64 = members.iter().filter_map(|m| m.change_pct).sum();
                let count = members.iter().filter(|m| m.change_pct.is_some()).count();
                if count == 0 { 0.0 } else { sum / count as f64 }
            };
            let amount: f64 = members.iter().filter_map(|m| m.amount).sum();
            let flow: f64 = members.iter().filter_map(|m| m.flow).sum();
            let _ = flow;
            let activity = activity_map.get(*sector_name).copied().unwrap_or(0.0);
            let flow_by_member_avg: f64 = if members.is_empty() {
                0.0
            } else {
                let sum: f64 = members
                    .iter()
                    .filter_map(|m| flow_by_member.get(&m.symbol).copied())
                    .sum();
                let count = members
                    .iter()
                    .filter(|m| flow_by_member.contains_key(&m.symbol))
                    .count();
                if count == 0 { 0.0 } else { sum / count as f64 }
            };
            // Score dispatch per Spec 0004 Q16=a sort_by branches.
            let score = match sort_by {
                "change_pct" => avg_pct,
                "amount" => if has_amount { amount } else { 0.0 },
                "amount_pct" => {
                    // total_amount across all sectors / this sector's amount share * avg_pct
                    if has_amount {
                        amount.ln() // simple monotonic transform; real formula in T02
                    } else {
                        0.0
                    }
                },
                "flow_pct" => {
                    if flow_available {
                        flow_by_member_avg
                    } else {
                        0.0
                    }
                },
                "composite" | _ => {
                    // weighted composite: avg_pct * 1.0 + activity * 0.5 + flow * 0.3
                    avg_pct + activity * 0.5 + flow_by_member_avg * 0.3
                }
            };
            ((*sector_name).to_string(), score)
        })
        .collect();

    // Stable sort descending by score; ties broken by name (lexical).
    // Use sort_by_key with Reverse for descending, then by name.
    scored.sort_by(|a, b| {
        b.1.partial_cmp(&a.1)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.0.cmp(&b.0))
    });
    scored.into_iter().map(|(name, _)| name).collect()
}

// ---------------------------------------------------------------------------
// IO kernel stubs (kernels 2/3/4) — for the throwaway example, we don't
// actually read parquet (no Python runtime, no fixture ready). We
// synthesize the output DataFrame directly so the *return-type* validates.
// Production T02 will use `polars::prelude::ParquetReader::new(File::open(path)?).finish()?`
// per `polars-io-0.55.2/src/parquet/read/reader.rs:179`.
// ---------------------------------------------------------------------------

fn synthesize_prev_daily_close_df() -> DataFrame {
    // 30 symbols × 1 day, all with prev_close populated. Mock schema:
    // symbol: String, close: Float64.
    let symbols: Vec<String> = (0..30).map(|i| format!("60000{i:02}.SH")).collect();
    let closes: Vec<f64> = (1..=30).map(|i| 10.0 + i as f64).collect();
    let symbol_refs: Vec<&str> = symbols.iter().map(|s| s.as_str()).collect();
    df![
        "symbol" => symbol_refs,
        "close"  => closes,
    ]
    .expect("prev_daily_close DataFrame construction")
}

fn synthesize_minute_pcts_df() -> DataFrame {
    // 30 symbols × 240 minutes, all with pct populated. Mock schema:
    // symbol: String, time: Int64 (epoch millis), pct: Float64.
    let symbols: Vec<String> = (0..30).map(|i| format!("60000{i:02}.SH")).collect();
    let n = symbols.len();
    let minutes: Vec<i64> = (0..240).map(|m| 1_704_067_200_000_i64 + m * 60_000).collect();
    let mut times: Vec<i64> = Vec::with_capacity(n * 240);
    let mut syms: Vec<&str> = Vec::with_capacity(n * 240);
    let mut pcts: Vec<f64> = Vec::with_capacity(n * 240);
    for s in &symbols {
        for &m in &minutes {
            times.push(m);
            syms.push(s.as_str());
            // synthetic: linear pct ramp from -0.02 to +0.05
            pcts.push(-0.02 + (m - minutes[0]) as f64 / (240 * 60_000) as f64 * 0.07);
        }
    }
    df![
        "symbol" => syms,
        "time"   => times,
        "pct"    => pcts,
    ]
    .expect("minute_pcts DataFrame construction")
}

fn synthesize_sector_flow_df() -> DataFrame {
    // 8 sectors × 240 minutes. Mock schema:
    // sector: String, time: Int64, flow: Float64.
    let sectors: Vec<String> = (0..8).map(|i| format!("S{i:02}")).collect();
    let minutes: Vec<i64> = (0..240).map(|m| 1_704_067_200_000_i64 + m * 60_000).collect();
    let n_sectors = sectors.len();
    let mut times: Vec<i64> = Vec::with_capacity(n_sectors * 240);
    let mut secs: Vec<&str> = Vec::with_capacity(n_sectors * 240);
    let mut flows: Vec<f64> = Vec::with_capacity(n_sectors * 240);
    for s in &sectors {
        for (i, &m) in minutes.iter().enumerate() {
            times.push(m);
            secs.push(s.as_str());
            flows.push((i as f64 / 240.0) * 1e6);
        }
    }
    df![
        "sector" => secs,
        "time"   => times,
        "flow"   => flows,
    ]
    .expect("sector_flow DataFrame construction")
}

fn synthesize_market_avg() -> HashMap<String, f64> {
    // 240 buckets, all with avg_pct populated
    let mut m = HashMap::new();
    for i in 0..240 {
        m.insert(
            (1_704_067_200_000_i64 + i as i64 * 60_000).to_string(),
            -0.005 + (i as f64 / 240.0) * 0.04,
        );
    }
    m
}

fn synthesize_activity_map() -> HashMap<String, f64> {
    // 8 sectors, varied activity score 0..1
    let mut m = HashMap::new();
    for i in 0..8 {
        m.insert(format!("S{i:02}"), i as f64 / 8.0);
    }
    m
}

fn synthesize_flow_by_member() -> HashMap<String, f64> {
    let mut m = HashMap::new();
    for i in 0..30 {
        m.insert(format!("60000{i:02}.SH"), (i as f64 - 15.0) / 30.0);
    }
    m
}

fn main() {
    // -----------------------------------------------------------------------
    // Synthetic fixture: 30 symbols × 8 sectors × 1 day × 1 minute (sampled).
    // -----------------------------------------------------------------------
    let sectors: Vec<String> = (0..8).map(|i| format!("S{i:02}")).collect();
    let symbols: Vec<String> = (0..30).map(|i| format!("60000{i:02}.SH")).collect();
    // Distribute 30 symbols across 8 sectors roughly evenly: 4,4,4,4,4,4,3,3.
    let mut rows: Vec<Row> = Vec::new();
    let ts = 1_704_067_200_000_i64; // 2024-01-15 09:30 UTC
    let mut sym_idx = 0usize;
    for (sec_i, sec) in sectors.iter().enumerate() {
        let count = if sec_i < 6 { 4 } else { 3 };
        for _ in 0..count {
            let sym = &symbols[sym_idx];
            sym_idx += 1;
            // Synthetic: pct ramp by symbol index; some None for missing data
            let change_pct = if sym_idx % 7 == 0 {
                None
            } else {
                Some(-0.02 + (sym_idx as f64 / 30.0) * 0.06)
            };
            let amount = if sym_idx % 11 == 0 { None } else { Some(1.0e8 + sym_idx as f64 * 1.0e6) };
            let flow = Some((sec_i as f64 - 4.0) * 0.05);
            rows.push(row(sym, ts, sec, change_pct, amount, flow));
        }
    }

    // -----------------------------------------------------------------------
    // Demo 1: kernel 1 — `bare_kernel("symbol")` stub (signature probe only).
    // -----------------------------------------------------------------------
    println!("[research] bare_kernel_stub signature compiles (path:line {} not cited).", "crates/indicators/examples/research_build_sector_rotation_api.rs:60");

    // -----------------------------------------------------------------------
    // Demo 2-4: kernels 2/3/4 — IO kernel signature probes.
    // We synthesize the output DataFrame (parquet IO deferred to T02).
    // -----------------------------------------------------------------------
    let prev_daily_close = synthesize_prev_daily_close_df();
    println!("[research] kernel 2 prev_daily_close synthesize → {} rows × {} cols:", prev_daily_close.height(), prev_daily_close.width());
    println!("{}", prev_daily_close.head(Some(3)));

    let minute_pcts = synthesize_minute_pcts_df();
    println!("[research] kernel 3 minute_pcts synthesize → {} rows × {} cols:", minute_pcts.height(), minute_pcts.width());
    println!("{}", minute_pcts.head(Some(3)));

    let sector_flow = synthesize_sector_flow_df();
    println!("[research] kernel 4 sector_flow synthesize → {} rows × {} cols:", sector_flow.height(), sector_flow.width());
    println!("{}", sector_flow.head(Some(3)));

    // -----------------------------------------------------------------------
    // Demo 5: kernel 5 — `per_bucket_reduce_kernel` (per-bucket reduce).
    // -----------------------------------------------------------------------
    let market_avg = synthesize_market_avg();
    let per_bucket = per_bucket_reduce_pure(&rows, &market_avg);
    println!("[research] kernel 5 per_bucket_reduce → {} buckets (showing 2):", per_bucket.len());
    for b in per_bucket.iter().take(2) {
        println!("  time={} leader={:?} leader_pct={:?} market_pct={:.4} names={} top_set={}",
            b.time, b.leader, b.leader_pct, b.market_pct, b.names.len(), b.top_set);
    }

    // -----------------------------------------------------------------------
    // Demo 6: kernel 6 — `timeline_kernel` (per-bucket summary → timeline rows).
    // -----------------------------------------------------------------------
    let timeline_rows = timeline_pure(&per_bucket);
    let timeline_df = build_timeline_df(
        timeline_rows
            .into_iter()
            .map(|(t, r, l, lp, mp, cu, cd)| (t, r, l, lp, mp, Some(cu), Some(cd)))
            .collect(),
    );
    println!("[research] kernel 6 timeline → DataFrame:");
    println!("{}", timeline_df);

    // -----------------------------------------------------------------------
    // Demo 7: kernel 7 — `cross_events_kernel` (per-bucket summary → events).
    // -----------------------------------------------------------------------
    let events = cross_events_pure(&per_bucket);
    let events_df = build_cross_events_df(events);
    println!("[research] kernel 7 cross_events → DataFrame:");
    println!("{}", events_df);

    // -----------------------------------------------------------------------
    // Demo 8: kernel 8 — `rank_for_mode_kernel` (5 sort_by branches).
    // -----------------------------------------------------------------------
    let sector_groups: Vec<(&str, &[Row])> = sectors
        .iter()
        .enumerate()
        .map(|(_i, s)| {
            let members: Vec<Row> = rows.iter().filter(|r| r.sector == *s).cloned().collect();
            (s.as_str(), Box::leak(members.into_boxed_slice()) as &[Row])
        })
        .collect();
    let activity_map = synthesize_activity_map();
    let flow_by_member = synthesize_flow_by_member();

    for sort_by in ["change_pct", "amount", "amount_pct", "flow_pct", "composite"] {
        let ranked = rank_for_mode_pure(
            &sector_groups,
            &activity_map,
            true,
            &flow_by_member,
            true,
            sort_by,
        );
        println!("[research] kernel 8 rank_for_mode(sort_by={}) → {} sectors: {:?}",
            sort_by, ranked.len(), ranked);
    }

    // -----------------------------------------------------------------------
    // PyO3 signature compile probe.
    // The 8 stubs above are #[pyfunction]-annotated; we don't call them
    // (no Python runtime attached in a standalone `cargo run`). The fact
    // that this file compiles at all proves the PyO3 0.29.3 + pyo3-polars
    // 0.28.0 type surface is well-formed against the vendored deps.
    // -----------------------------------------------------------------------
    let _ = (
        bare_kernel_stub as fn(_) -> _,
        prev_daily_close_kernel_stub as fn(_, _) -> _,
        minute_pcts_kernel_stub as fn(_, _) -> _,
        sector_flow_kernel_stub as fn(_, _) -> _,
        per_bucket_reduce_kernel_stub as fn(_, _) -> _,
        timeline_kernel_stub as fn(_) -> _,
        cross_events_kernel_stub as fn(_) -> _,
        rank_for_mode_kernel_stub as fn(_, _, _, _, _, _) -> _,
    );
    println!("[research] 8 PyO3 signature probes compiled OK.");
}