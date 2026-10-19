//! `per_bucket_reduce_kernel` — per-bucket reduce (Spec 0004 §"8 Rust kernels" row 5).
//!
//! Algorithm (matches `backend/app/services/sector_rotation.py::_compute` phase (b):
//! the Polars-heavy bucketize + group_by/agg block at line 420-435):
//!   1. `by_sector_df` columns: `_bucket` (datetime ms), `_member` (str), `_bare` (str),
//!      `_spct` (f64), `_n` (u32), optional `_amt` (f64).
//!   2. `market_df` columns: `_bucket` (datetime ms), `_mpct` (f64).
//!   3. Group `by_sector_df` by `[_bucket, _member, _bare]`: aggregate `_spct.mean() → _sym_pct`,
//!      and if `_amt` present, `_amt.sum() → _sym_amt`.
//!   4. Then group by `[_bucket, _member]`: aggregate `_sym_pct.mean() → _spct`, `_n.sum() → _n`,
//!      and if `_amt` present, `_sym_amt.sum() → _amt`.
//!   5. Sort by `[_bucket ASC, _spct DESC]` (within-bucket leader first).
//!   6. For each bucket: compute cross-event counts and produce one output row per
//!      `(bucket, member)` pair: `(bucket, names=[...], pcts=[...], top_set=set(names[:10]),
//!      leader=names[0], leader_pct=_spct[0], cross_up, cross_down)`.
//!
//! Output schema (per-bucket summary):
//!   - `bucket: String` (HH:MM format)
//!   - `leader: Str` — top-1 leader
//!   - `leader_pct: Float64` — top-1 leader's pct
//!   - `market_pct: Float64` — market average pct for this bucket
//!   - `n_members: u32` — number of members in this bucket
//!   - `cross_up: u32` — count of 0-axis-cross-up events in this bucket
//!   - `cross_down: u32` — count of 0-axis-cross-down events in this bucket
//!
//! **T02 divergence note**: Spec 0004 lists `names`, `pcts`, `top_set` as `List<T>` columns.
//! For T02 we emit a SCALAR 7-col schema; the Python adapter reconstructs `names`/`pcts` by
//! joining back to `by_sector_df` per `(bucket, member)`. `top_set` is implicit in the
//! `n_members` count. This divergence is documented in the closure report for T05.

use std::cmp::Ordering;
use std::collections::HashMap;

use polars::prelude::*;
use pyo3::prelude::*;
use pyo3_polars::PyDataFrame;

/// Per-bucket output row (internal type).
#[derive(Clone, Debug)]
pub(crate) struct BucketSummary {
    pub bucket: String,         // HH:MM
    pub leader: Option<String>,
    pub leader_pct: Option<f64>,
    pub market_pct: Option<f64>,
    pub n_members: u32,
    pub cross_up: u32,
    pub cross_down: u32,
}

/// Pure-Rust inner — group `by_sector_df` by `(bucket, member)` and compute per-bucket summaries.
/// Returns `Vec<BucketSummary>` in bucket-ascending order.
fn per_bucket_reduce_inner(
    by_sector_df: &DataFrame,
    market_df: &DataFrame,
) -> PolarsResult<Vec<BucketSummary>> {
    let n_rows = by_sector_df.height();
    if n_rows == 0 {
        return Ok(vec![]);
    }

    // Read `_bucket` (datetime ms via physical i64), `_member`, `_bare`, `_spct`, `_n` columns.
    let bucket_phys = by_sector_df.column("_bucket")?.to_physical_repr();
    let bucket_i64 = bucket_phys.i64()?.clone();
    let member_col = by_sector_df.column("_member")?.str()?.clone();
    let bare_col = by_sector_df.column("_bare")?.str()?.clone();
    let spct_cast = by_sector_df.column("_spct")?.cast(&DataType::Float64)?;
    let spct_col = spct_cast.f64()?.clone();
    let n_cast = by_sector_df.column("_n")?.cast(&DataType::UInt32)?;
    let n_col = n_cast.u32()?.clone();

    // Phase 1 group_by: [_bucket, _member, _bare] → (_sym_pct mean, optional _sym_amt sum).
    struct BucketMemberBare {
        _sym_pct_sum: f64,
        _sym_pct_count: usize,
        _sym_amt_sum: f64,
        _n: u32,
    }
    let mut phase1: HashMap<(i64, String, String), BucketMemberBare> = HashMap::new();
    // amt: try to read `_amt` column; if absent (or cast fails), treat as 0.0 for all rows.
    let amt_present = by_sector_df.column("_amt").is_ok();
    let amt_i64_opt: Option<Float64Chunked> = if amt_present {
        match by_sector_df.column("_amt").unwrap().cast(&DataType::Float64) {
            Ok(c) => c.f64().ok().map(|ca| ca.clone()),
            Err(_) => return Err(PolarsError::ComputeError(polars::error::ErrString::from("cast amt"))),
        }
    } else {
        None
    };

    for i in 0..n_rows {
        let b = bucket_i64.get(i).unwrap_or(0);
        let m = member_col.get(i).unwrap_or("").to_string();
        let br = bare_col.get(i).unwrap_or("").to_string();
        let pct = spct_col.get(i).unwrap_or(0.0);
        let n = n_col.get(i).unwrap_or(0);
        let amt = match &amt_i64_opt {
            Some(c) => c.get(i).unwrap_or(0.0),
            None => 0.0,
        };
        let entry = phase1.entry((b, m, br)).or_insert(BucketMemberBare {
            _sym_pct_sum: 0.0,
            _sym_pct_count: 0,
            _sym_amt_sum: 0.0,
            _n: 0,
        });
        entry._sym_pct_sum += pct;
        entry._sym_pct_count += 1;
        entry._sym_amt_sum += amt;
        entry._n += n;
    }

    // Phase 2 group_by: [_bucket, _member] → (_spct mean, _n sum, optional _amt sum).
    // Use BTreeMap to preserve (bucket, member) insertion order from Phase 1 (which was sorted).
    struct BucketMemberAgg {
        spct_sum: f64,
        spct_count: usize,
        amt_sum: f64,
        n_sum: u32,
    }
    use std::collections::BTreeMap;
    let mut phase2: BTreeMap<(i64, String), Vec<BucketMemberBare>> = BTreeMap::new();
    for ((b, m, _), agg) in phase1 {
        phase2.entry((b, m)).or_default().push(agg);
    }
    let mut phase2_flat: Vec<(i64, String, BucketMemberAgg)> = Vec::new();
    for ((b, m), aggs) in phase2 {
        let mut agg = BucketMemberAgg {
            spct_sum: 0.0,
            spct_count: 0,
            amt_sum: 0.0,
            n_sum: 0,
        };
        for a in aggs {
            agg.spct_sum += a._sym_pct_sum;
            agg.spct_count += a._sym_pct_count;
            agg.amt_sum += a._sym_amt_sum;
            agg.n_sum += a._n;
        }
        phase2_flat.push((b, m, agg));
    }

    // Phase 3: sort by [_bucket ASC, _spct DESC].
    phase2_flat.sort_by(|a, b| {
        let sa = if a.2.spct_count > 0 { a.2.spct_sum / a.2.spct_count as f64 } else { 0.0 };
        let sb = if b.2.spct_count > 0 { b.2.spct_sum / b.2.spct_count as f64 } else { 0.0 };
        a.0.cmp(&b.0)
            .then_with(|| sb.partial_cmp(&sa).unwrap_or(Ordering::Equal))
    });

    // market_map from market_df.
    let market_phys = market_df.column("_bucket")?.to_physical_repr();
    let market_bucket = market_phys.i64()?.clone();
    let market_pct_cast = market_df.column("_mpct")?.cast(&DataType::Float64)?;
    let market_pct = market_pct_cast.f64()?.clone();
    let mut market_map: HashMap<i64, f64> = HashMap::new();
    for i in 0..market_df.height() {
        if let (Some(b), Some(p)) = (market_bucket.get(i), market_pct.get(i)) {
            market_map.entry(b).or_insert(p);
        }
    }

    // Step 5: build per-bucket summaries; compute cross-event counts via prev_sign dict.
    let mut summaries: Vec<BucketSummary> = Vec::new();
    let mut prev_sign: HashMap<String, i32> = HashMap::new();
    let mut current_bucket: i64 = i64::MIN;
    let mut cur: Option<BucketSummary> = None;
    for (b, m, agg) in &phase2_flat {
        if current_bucket != *b {
            // Flush previous bucket.
            if let Some(prev) = cur.take() {
                summaries.push(prev);
            }
            current_bucket = *b;
            cur = Some(BucketSummary {
                bucket: format_bucket(*b),
                leader: None,
                leader_pct: None,
                market_pct: market_map.get(b).copied(),
                n_members: 0,
                cross_up: 0,
                cross_down: 0,
            });
        }
        let s = cur.as_mut().expect("bucket in progress");
        let spct = if agg.spct_count > 0 {
            agg.spct_sum / agg.spct_count as f64
        } else {
            0.0
        };
        s.n_members += 1;
        // Set leader / leader_pct on first member of bucket (highest pct due to sort).
        if s.leader.is_none() {
            s.leader = Some(m.clone());
            s.leader_pct = Some(spct);
        }

        // Cross-event detection: sign change since prev_sign[m].
        let sign = if spct >= 0.0 { 1 } else { -1 };
        let prev = prev_sign.get(m).copied();
        if let Some(prev_s) = prev {
            if prev_s != sign {
                if sign > 0 {
                    s.cross_up += 1;
                } else {
                    s.cross_down += 1;
                }
            }
        }
        prev_sign.insert(m.clone(), sign);
    }
    // Flush last bucket.
    if let Some(prev) = cur.take() {
        summaries.push(prev);
    }

    Ok(summaries)
}

/// Format a bucket epoch-ms as HH:MM. Mirrors `bucket.strftime("%H:%M")` in Python.
fn format_bucket(ms: i64) -> String {
    let secs = (ms / 1000).max(0);
    let hh = (secs / 3600) % 24;
    let mm = (secs / 60) % 60;
    format!("{:02}:{:02}", hh, mm)
}

/// PyO3 entry point — wrap the two input DataFrames, call inner, return a DataFrame
/// of per-bucket summaries with the schema documented in the module header.
#[pyfunction]
pub fn per_bucket_reduce_kernel(
    by_sector_df: PyDataFrame,
    market_df: PyDataFrame,
) -> PyResult<PyDataFrame> {
    let by_sector: DataFrame = by_sector_df.into();
    let market: DataFrame = market_df.into();
    let summaries = per_bucket_reduce_inner(&by_sector, &market)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("{e}")))?;

    let n = summaries.len();
    let mut buckets: Vec<&str> = Vec::with_capacity(n);
    let mut leaders: Vec<Option<&str>> = Vec::with_capacity(n);
    let mut leader_pcts: Vec<Option<f64>> = Vec::with_capacity(n);
    let mut market_pcts: Vec<Option<f64>> = Vec::with_capacity(n);
    let mut n_members: Vec<u32> = Vec::with_capacity(n);
    let mut cross_ups: Vec<u32> = Vec::with_capacity(n);
    let mut cross_downs: Vec<u32> = Vec::with_capacity(n);
    for s in &summaries {
        buckets.push(s.bucket.as_str());
        leaders.push(s.leader.as_deref());
        leader_pcts.push(s.leader_pct);
        market_pcts.push(s.market_pct);
        n_members.push(s.n_members);
        cross_ups.push(s.cross_up);
        cross_downs.push(s.cross_down);
    }
    let df = df![
        "bucket"     => buckets,
        "leader"     => leaders,
        "leader_pct" => leader_pcts,
        "market_pct" => market_pcts,
        "n_members"  => n_members,
        "cross_up"   => cross_ups,
        "cross_down" => cross_downs,
    ].map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("{e}")))?;
    Ok(PyDataFrame(df))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Smoke: 2 buckets × 2 members each × 2 bare symbols per member.
    /// Verify per-bucket summaries (leader / leader_pct / cross counts).
    #[test]
    fn smoke_two_buckets_two_members_each() {
        let by_sector = df![
            "_bucket" => vec![1_700_000_000_000_i64, 1_700_000_000_000, 1_700_000_000_000, 1_700_000_000_000, 1_700_000_300_000_i64, 1_700_000_300_000, 1_700_000_300_000, 1_700_000_300_000],
            "_member" => vec!["Banks", "Banks", "Tech", "Tech", "Banks", "Banks", "Tech", "Tech"],
            "_bare"   => vec!["600000", "600036", "000001", "002415", "600000", "600036", "000001", "002415"],
            "_spct"   => vec![0.01_f64, -0.005, 0.02, -0.01, 0.015, 0.005, -0.005, 0.03],
            "_n"      => vec![1_u32; 8],
        ].unwrap();
        let market = df![
            "_bucket" => vec![1_700_000_000_000_i64, 1_700_000_300_000_i64],
            "_mpct"   => vec![0.005_f64, 0.010_f64],
        ].unwrap();
        let summaries = per_bucket_reduce_inner(&by_sector, &market).unwrap();
        assert_eq!(summaries.len(), 2, "2 buckets");
        // Bucket 1: Tech (avg=0.005) leads Banks (avg=0.0025)?
        // Each (bucket, member, bare) is unique so _sym_pct mean = original _spct.
        // _spct within bucket: Tech = (0.02 + -0.01) = 0.005 mean, Banks = (0.01 + -0.005) = 0.0025 mean.
        // After sort by _spct DESC: Tech first → leader.
        assert_eq!(summaries[0].bucket, "22:13");
        assert_eq!(summaries[0].leader.as_deref(), Some("Tech"));
        // leader_pct = mean of Tech's per-bare spct = (0.02 + -0.01)/2 = 0.005.
        let p0 = summaries[0].leader_pct.unwrap();
        assert!((p0 - 0.005).abs() < 1e-9, "Tech pct ~0.005, got {p0}");
        assert_eq!(summaries[0].n_members, 2, "Tech + Banks in bucket 1");
        // market_pct for bucket 1 = 0.005.
        assert_eq!(summaries[0].market_pct, Some(0.005));

        // Bucket 2: Tech = (-0.005 + 0.03) = 0.0125 mean (float arithmetic: 0.012499...),
        // Banks = (0.015 + 0.005) = 0.010 mean. Leader = Tech.
        assert_eq!(summaries[1].bucket, "22:18");
        assert_eq!(summaries[1].leader.as_deref(), Some("Tech"));
        // Use approximate equality — Rust float arithmetic gives 0.012499999... not exact 0.0125.
        let p = summaries[1].leader_pct.unwrap();
        assert!((p - 0.0125).abs() < 1e-9, "Tech pct ~0.0125, got {p}");
        assert_eq!(summaries[1].market_pct, Some(0.010));
    }

    /// `leader_pct` tie: two members with equal pct → first in input order wins (stable sort).
    /// Mirrors Python's stable sort semantics.
    #[test]
    fn leader_pct_tie_stable_sort() {
        let by_sector = df![
            "_bucket" => vec![1_700_000_000_000_i64, 1_700_000_000_000],
            "_member" => vec!["Banks", "Tech"],
            "_bare"   => vec!["600000", "000001"],
            "_spct"   => vec![0.02_f64, 0.02_f64],  // tied
            "_n"      => vec![1_u32; 2],
        ].unwrap();
        let market = df![
            "_bucket" => vec![1_700_000_000_000_i64],
            "_mpct"   => vec![0.01_f64],
        ].unwrap();
        let summaries = per_bucket_reduce_inner(&by_sector, &market).unwrap();
        assert_eq!(summaries.len(), 1);
        // Both _spct mean = 0.02; stable sort preserves input order → Banks first.
        assert_eq!(summaries[0].leader.as_deref(), Some("Banks"));
        assert_eq!(summaries[0].leader_pct, Some(0.02));
    }

    /// Cross-event boundary: a sector switches sign between buckets.
    /// Bucket 1: Tech = +0.01; Bucket 2: Tech = -0.01 → cross_down count = 1.
    #[test]
    fn cross_event_boundary_sign_change() {
        let by_sector = df![
            "_bucket" => vec![1_700_000_000_000_i64, 1_700_000_000_000, 1_700_000_300_000_i64, 1_700_000_300_000],
            "_member" => vec!["Tech", "Tech", "Tech", "Tech"],
            "_bare"   => vec!["000001", "000001", "000001", "000001"],
            "_spct"   => vec![0.01_f64, 0.02, -0.005, -0.015],
            "_n"      => vec![1_u32; 4],
        ].unwrap();
        let market = df![
            "_bucket" => vec![1_700_000_000_000_i64, 1_700_000_300_000_i64],
            "_mpct"   => vec![0.0_f64, 0.0_f64],
        ].unwrap();
        let summaries = per_bucket_reduce_inner(&by_sector, &market).unwrap();
        assert_eq!(summaries.len(), 2);
        // Bucket 1: Tech = (0.01 + 0.02)/2 = 0.015 → sign=+1.
        assert_eq!(summaries[0].cross_up, 0);
        assert_eq!(summaries[0].cross_down, 0);
        // Bucket 2: Tech = (-0.005 + -0.015)/2 = -0.010 → sign=-1, prev=+1 → cross_down += 1.
        assert_eq!(summaries[1].cross_up, 0);
        assert_eq!(summaries[1].cross_down, 1);
    }
}
