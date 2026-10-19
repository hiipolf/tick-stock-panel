//! `timeline_kernel` — flatten per-bucket summaries into timeline rows (Spec 0004 row 6).
//!
//! Input: `Vec<HashMap<String, Py<PyAny>>>` — list of per-bucket summaries produced by
//! `per_bucket_reduce_kernel` (via the adapter's `df.iter_rows(named=True)`).
//!
//! Expected dict keys per row:
//!   - `bucket` (str): "HH:MM" — per-bucket time key.
//!   - `names` (list[str]): members in leader-first order.
//!   - `pcts` (list[float]): per-member pct values.
//!   - `top_set` (set/list[str]): top-N leader names (for overlap calc).
//!   - `leader` (str): top-1 leader name.
//!   - `leader_pct` (float | None): top-1 leader's pct.
//!   - `market_pct` (float | None): market avg pct for this bucket.
//!   - `cross_up` (int): count of 0-axis-cross-up events in this bucket.
//!   - `cross_down` (int): count of 0-axis-cross-down events in this bucket.
//!
//! Output: `DataFrame` with columns:
//!   - `time: Str` (bucket key, e.g. "HH:MM")
//!   - `rotation: Float64` — 1 - overlap(top_set_t, top_set_t-REF) clamped to [0, 1]
//!   - `leader: Str` — top-1 leader of this bucket
//!   - `leader_pct: Float64` — leader's pct
//!   - `market_pct: Float64` — market avg pct
//!   - `cross_up: u32` — count of 0-axis-cross-up events
//!   - `cross_down: u32` — count of 0-axis-cross-down events
//!
//! Reference bucket selection (per Python `_reference_index`):
//!   - For bucket at index `i`, find the largest `j < i` whose `(current - bucket_j) >= 60min`.
//!   - If none and `i > 0`, use `j = 0`. If `i == 0`, no reference (`overlap = 1.0`, rotation = 0).
//!   - `overlap = |top_set_t ∩ top_set_j| / min(10, |top_set_t|, |top_set_j|)` if width > 0 else 1.0.
//!   - `rotation = round(1 - min(1, max(0, overlap)), 4)`.

use std::cmp::Ordering;
use std::collections::{HashMap, HashSet};

use polars::prelude::*;
use pyo3::prelude::*;
use pyo3_polars::PyDataFrame;

/// Per-bucket extracted row (mirrors Python's `per_bucket` dict).
struct BucketRow {
    bucket_str: String,    // "HH:MM"
    bucket_epoch_ms: i64,  // for reference-window comparison (0 if unparseable)
    names: Vec<String>,
    pcts: Vec<f64>,
    top_set: HashSet<String>,
    leader: Option<String>,
    leader_pct: Option<f64>,
    market_pct: Option<f64>,
    cross_up: u32,
    cross_down: u32,
}

const TOP_OVERLAP: usize = 10;
const RANK_WINDOW_MIN: i64 = 60;

/// Parse "HH:MM" → minutes-since-midnight. Returns None on parse failure.
fn hhmm_to_minutes(s: &str) -> Option<i64> {
    let mut parts = s.split(':');
    let hh = parts.next()?.parse::<i64>().ok()?;
    let mm = parts.next()?.parse::<i64>().ok()?;
    if !(0..=23).contains(&hh) || !(0..=59).contains(&mm) {
        return None;
    }
    Some(hh * 60 + mm)
}

/// Pure-Rust inner — extract per-bucket dicts, compute `rotation` per row, return the
/// timeline DataFrame with the documented schema.
fn timeline_inner(per_bucket: &[HashMap<String, pyo3::Py<pyo3::PyAny>>], py: Python<'_>) -> PolarsResult<DataFrame> {
    let mut rows: Vec<BucketRow> = Vec::with_capacity(per_bucket.len());
    for dict in per_bucket {
        rows.push(extract_bucket_row(dict, py));
    }

    let n = rows.len();
    let mut times: Vec<String> = Vec::with_capacity(n);
    let mut rotations: Vec<f64> = Vec::with_capacity(n);
    let mut leaders: Vec<Option<String>> = Vec::with_capacity(n);
    let mut leader_pcts: Vec<Option<f64>> = Vec::with_capacity(n);
    let mut market_pcts: Vec<Option<f64>> = Vec::with_capacity(n);
    let mut cross_ups: Vec<u32> = Vec::with_capacity(n);
    let mut cross_downs: Vec<u32> = Vec::with_capacity(n);

    for (i, row) in rows.iter().enumerate() {
        times.push(row.bucket_str.clone());
        leaders.push(row.leader.clone());
        leader_pcts.push(row.leader_pct);
        market_pcts.push(row.market_pct);
        cross_ups.push(row.cross_up);
        cross_downs.push(row.cross_down);

        // Compute rotation via reference bucket.
        let back_idx = reference_index(&rows, i);
        let rotation = if let Some(j) = back_idx {
            let width = TOP_OVERLAP.min(row.top_set.len()).min(rows[j].top_set.len());
            if width == 0 {
                0.0
            } else {
                let inter = row.top_set.intersection(&rows[j].top_set).count();
                let overlap = inter as f64 / width as f64;
                let clamped = overlap.clamp(0.0, 1.0);
                (1.0 - clamped * 1.0).round_to(4) // round to 4 decimals
            }
        } else {
            0.0
        };
        rotations.push(rotation);
    }

    let time_strs: Vec<&str> = times.iter().map(|s| s.as_str()).collect();
    let time_series = StringChunked::from_slice(
        PlSmallStr::from_str("time"),
        &time_strs,
    ).into_series();
    let rotation_series = Float64Chunked::from_slice(
        PlSmallStr::from_str("rotation"),
        &rotations,
    ).into_series();
    let leader_opt_strs: Vec<Option<String>> = leaders.clone();
    let leader_str_refs: Vec<Option<&str>> = leader_opt_strs.iter().map(|s| s.as_deref()).collect();
    let leader_series = StringChunked::from_slice_options(
        PlSmallStr::from_str("leader"),
        &leader_str_refs,
    ).into_series();
    let leader_pct_series = Float64Chunked::from_slice_options(
        PlSmallStr::from_str("leader_pct"),
        &leader_pcts,
    ).into_series();
    let market_pct_series = Float64Chunked::from_slice_options(
        PlSmallStr::from_str("market_pct"),
        &market_pcts,
    ).into_series();
    let cross_up_series = UInt32Chunked::from_slice(
        PlSmallStr::from_str("cross_up"),
        &cross_ups,
    ).into_series();
    let cross_down_series = UInt32Chunked::from_slice(
        PlSmallStr::from_str("cross_down"),
        &cross_downs,
    ).into_series();

    DataFrame::new(n, vec![
        time_series.into_column(),
        rotation_series.into_column(),
        leader_series.into_column(),
        leader_pct_series.into_column(),
        market_pct_series.into_column(),
        cross_up_series.into_column(),
        cross_down_series.into_column(),
    ])
}

/// Reference-bucket index for bucket `index` (mirrors Python's `_reference_index`).
/// Returns None only when `index == 0` (no reference for first bucket).
fn reference_index(rows: &[BucketRow], index: usize) -> Option<usize> {
    if index == 0 {
        return None;
    }
    let cur_min = hhmm_to_minutes(&rows[index].bucket_str);
    if cur_min.is_none() {
        // Fall back: just use index-1 (any earlier bucket).
        return Some(0);
    }
    let cur = cur_min.unwrap();
    for back in (0..index).rev() {
        let back_min = hhmm_to_minutes(&rows[back].bucket_str).unwrap_or(0);
        // The "minutes since midnight" comparison wraps around midnight (e.g. 14:30 vs 09:30 = 5h gap).
        let delta = if cur >= back_min {
            cur - back_min
        } else {
            cur + (24 * 60 - back_min)
        };
        if delta >= RANK_WINDOW_MIN {
            return Some(back);
        }
    }
    Some(0)
}

/// Extract one bucket row from a Python dict.
fn extract_bucket_row(dict: &HashMap<String, pyo3::Py<pyo3::PyAny>>, py: Python<'_>) -> BucketRow {
    let get_str = |key: &str| -> Option<String> {
        dict.get(key).and_then(|v| v.extract::<String>(py).ok())
    };
    let get_f64 = |key: &str| -> Option<f64> {
        dict.get(key).and_then(|v| {
            // Extract via Python float() — handles int and float correctly.
            v.extract::<f64>(py).ok().filter(|f| f.is_finite())
        })
    };
    let get_u32 = |key: &str| -> Option<u32> {
        dict.get(key).and_then(|v| v.extract::<u32>(py).ok())
    };
    let get_list_str = |key: &str| -> Vec<String> {
        // Use Python iteration to convert list → Vec<String>.
        if let Some(v) = dict.get(key) {
            if let Ok(iter) = v.bind(py).try_iter() {
                return iter
                    .filter_map(|item| item.ok().and_then(|x| x.extract::<String>().ok()))
                    .collect();
            }
        }
        vec![]
    };
    let get_list_f64 = |key: &str| -> Vec<f64> {
        if let Some(v) = dict.get(key) {
            if let Ok(iter) = v.bind(py).try_iter() {
                return iter
                    .filter_map(|item| {
                        item.ok()
                            .and_then(|x| x.extract::<f64>().ok())
                            .filter(|f| f.is_finite())
                    })
                    .collect();
            }
        }
        vec![]
    };
    let get_set_str = |key: &str| -> HashSet<String> {
        let v = get_list_str(key);
        v.into_iter().collect()
    };

    let bucket_str = get_str("bucket").unwrap_or_default();
    let bucket_epoch_ms = hhmm_to_minutes(&bucket_str).unwrap_or(0) * 60_000;
    let names = get_list_str("names");
    let pcts = get_list_f64("pcts");
    let top_set = get_set_str("top_set");
    let leader = get_str("leader");
    let leader_pct = get_f64("leader_pct");
    let market_pct = get_f64("market_pct");
    let cross_up = get_u32("cross_up").unwrap_or(0);
    let cross_down = get_u32("cross_down").unwrap_or(0);

    BucketRow {
        bucket_str,
        bucket_epoch_ms,
        names,
        pcts,
        top_set,
        leader,
        leader_pct,
        market_pct,
        cross_up,
        cross_down,
    }
}

/// Helper trait — round-to-4-decimals (mirrors `_r4` in Python).
trait RoundTo {
    fn round_to(self, decimals: u32) -> Self;
}
impl RoundTo for f64 {
    fn round_to(self, decimals: u32) -> Self {
        let factor = 10f64.powi(decimals as i32);
        (self * factor).round() / factor
    }
}

/// PyO3 entry point — extract dicts, call inner, return PyDataFrame.
#[pyfunction]
pub fn timeline_kernel(
    py: Python<'_>,
    per_bucket: Vec<HashMap<String, pyo3::Py<pyo3::PyAny>>>,
) -> PyResult<PyDataFrame> {
    let df = timeline_inner(&per_bucket, py)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("{e}")))?;
    Ok(PyDataFrame(df))
}

#[cfg(test)]
mod tests {
    use super::*;
    use pyo3::types::PyDict;

    /// Build a synthetic bucket row dict (mimics `df.iter_rows(named=True)` output).
    fn make_bucket<'py>(
        py: Python<'py>,
        bucket: &str,
        names: &[&str],
        pcts: &[f64],
        top_set: &[&str],
        leader: Option<&str>,
        leader_pct: Option<f64>,
        market_pct: Option<f64>,
        cross_up: u32,
        cross_down: u32,
    ) -> HashMap<String, pyo3::Py<pyo3::PyAny>> {
        let dict = PyDict::new(py);
        dict.set_item("bucket", bucket).unwrap();
        let names_list: Vec<&str> = names.to_vec();
        dict.set_item("names", names_list).unwrap();
        let pcts_list: Vec<f64> = pcts.to_vec();
        dict.set_item("pcts", pcts_list).unwrap();
        let top_set_list: Vec<&str> = top_set.to_vec();
        dict.set_item("top_set", top_set_list).unwrap();
        dict.set_item("leader", leader).unwrap();
        dict.set_item("leader_pct", leader_pct).unwrap();
        dict.set_item("market_pct", market_pct).unwrap();
        dict.set_item("cross_up", cross_up).unwrap();
        dict.set_item("cross_down", cross_down).unwrap();
        let mut map: HashMap<String, pyo3::Py<pyo3::PyAny>> = HashMap::new();
        for (k, v) in dict.iter() {
            let kstr: String = k.extract().unwrap();
            map.insert(kstr, v.into());
        }
        map
    }

    /// Smoke: 3 buckets with same top_set (overlap=1.0, rotation=0.0).
    /// `reference_index(0)` → None → rotation = 0.0.
    /// `reference_index(1)` → 0 (since 09:35 - 09:30 = 5 min < 60 min, falls back to 0).
    /// `reference_index(2)` → 1 (since 10:35 - 09:35 = 60 min exactly = RANK_WINDOW_MIN).
    #[test]
    fn smoke_three_buckets_same_top_set() {
        pyo3::Python::initialize();
        let mut per_bucket: Vec<HashMap<String, pyo3::Py<pyo3::PyAny>>> = Vec::new();
        Python::attach(|py| {
            for (bucket, leader, lpct, mpct) in [
                ("09:30", "Tech", 0.01_f64, 0.005_f64),
                ("09:35", "Tech", 0.015, 0.008),
                ("10:35", "Banks", 0.005, 0.002),
            ] {
                per_bucket.push(make_bucket(
                    py,
                    bucket,
                    &["Tech", "Banks"],
                    &[0.01, -0.005],
                    &["Tech", "Banks"],
                    Some(leader),
                    Some(lpct),
                    Some(mpct),
                    0, 0,
                ));
            }
        });
        Python::attach(|py| {
            let df = timeline_inner(&per_bucket, py).unwrap();
            assert_eq!(df.width(), 7, "7 columns: time/rotation/leader/leader_pct/market_pct/cross_up/cross_down");
            assert_eq!(df.height(), 3, "3 buckets");
            let times: Vec<&str> = df
                .column("time").unwrap().str().unwrap()
                .iter().map(|s| s.unwrap_or("")).collect();
            assert_eq!(times, vec!["09:30", "09:35", "10:35"]);
            let rotations: Vec<f64> = df
                .column("rotation").unwrap().f64().unwrap()
                .iter().map(|v| v.unwrap_or(0.0)).collect();
            // Same top_set across all buckets → overlap=1.0 → rotation=0.0
            assert_eq!(rotations, vec![0.0, 0.0, 0.0]);
        });
    }

    /// `reference_index=0` for the FIRST bucket — no prior bucket exists.
    /// rotation = 0.0 (no overlap to compare against).
    #[test]
    fn reference_index_zero_for_first_bucket() {
        pyo3::Python::initialize();
        Python::attach(|py| {
            let per_bucket = vec![make_bucket(
                py, "09:30", &["Tech"], &[0.01], &["Tech"],
                Some("Tech"), Some(0.01), Some(0.005), 0, 0,
            )];
            let df = timeline_inner(&per_bucket, py).unwrap();
            assert_eq!(df.height(), 1);
            let rotations: Vec<f64> = df
                .column("rotation").unwrap().f64().unwrap()
                .iter().map(|v| v.unwrap_or(0.0)).collect();
            assert_eq!(rotations, vec![0.0], "first bucket: rotation=0.0 (no reference)");
        });
    }
}
