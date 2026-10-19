//! `cross_events_kernel` — flatten per-bucket summaries into 0-axis-crossing events
//! (Spec 0004 §"8 Rust kernels" row 7).
//!
//! Input: `Vec<HashMap<String, Py<PyAny>>>` — list of per-bucket summaries (same shape
//! as `timeline_kernel`'s input).
//!
//! For each (bucket, name) pair across the per-bucket stream, compute the sign change
//! (`>=0 → >=0`, `>=0 → <0`, etc.) against a `prev_sign` dict that survives across
//! buckets. Emit one event row per sign change:
//!   - `dir = "up"` when sign flips from negative to positive (转强)
//!   - `dir = "down"` when sign flips from positive to negative (转弱)
//!
//! Output: `DataFrame` with 4 columns:
//!   - `time: Str` (bucket key, e.g. "HH:MM")
//!   - `name: Str` (sector name that crossed)
//!   - `dir: Str` ("up" | "down")
//!   - `pct: Float64` (rounded to 4 decimals, matching Python's `_r4`)
//!
//! Tied signs (e.g. `+0.0 → +0.0`) emit no event (Python: `if prev != sign`).

use std::collections::HashMap;

use polars::prelude::*;
use pyo3::prelude::*;
use pyo3_polars::PyDataFrame;

/// Pure-Rust inner — extract per-bucket dicts, walk sign-changes across buckets,
/// return the events DataFrame with the documented 4-col schema.
fn cross_events_inner(
    per_bucket: &[HashMap<String, pyo3::Py<pyo3::PyAny>>],
    py: Python<'_>,
) -> PolarsResult<DataFrame> {
    let mut events: Vec<(String, String, String, f64)> = Vec::new();
    let mut prev_sign: HashMap<String, i32> = HashMap::new();

    for dict in per_bucket {
        let bucket_str = dict
            .get("bucket")
            .and_then(|v| v.extract::<String>(py).ok())
            .unwrap_or_default();
        let names = extract_list_str(dict, "names", py);
        let pcts = extract_list_f64(dict, "pcts", py);

        for (name, pct) in names.iter().zip(pcts.iter()) {
            let sign = if *pct >= 0.0 { 1 } else { -1 };
            let prev = prev_sign.get(name).copied();
            if let Some(prev_s) = prev {
                if prev_s != sign {
                    let dir = if sign > 0 { "up" } else { "down" };
                    events.push((bucket_str.clone(), name.clone(), dir.to_string(), round_to_4(*pct)));
                }
            }
            prev_sign.insert(name.clone(), sign);
        }
    }

    let n = events.len();
    let mut times: Vec<&str> = Vec::with_capacity(n);
    let mut names: Vec<&str> = Vec::with_capacity(n);
    let mut dirs: Vec<&str> = Vec::with_capacity(n);
    let mut pcts: Vec<f64> = Vec::with_capacity(n);
    for e in &events {
        times.push(&e.0); // string lives in `events`; borrow is fine for the DataFrame build
        names.push(&e.1);
        dirs.push(&e.2);
        pcts.push(e.3);
    }
    // To avoid lifetime issues with refs to `events`, materialize owned strings.
    let mut times_owned: Vec<String> = Vec::with_capacity(n);
    let mut names_owned: Vec<String> = Vec::with_capacity(n);
    let mut dirs_owned: Vec<String> = Vec::with_capacity(n);
    for e in &events {
        times_owned.push(e.0.clone());
        names_owned.push(e.1.clone());
        dirs_owned.push(e.2.clone());
    }
    let _ = (times, names, dirs); // suppress unused warnings
    let time_refs: Vec<&str> = times_owned.iter().map(|s| s.as_str()).collect();
    let name_refs: Vec<&str> = names_owned.iter().map(|s| s.as_str()).collect();
    let dir_refs: Vec<&str> = dirs_owned.iter().map(|s| s.as_str()).collect();

    df![
        "time" => time_refs,
        "name" => name_refs,
        "dir"  => dir_refs,
        "pct"  => pcts,
    ]
}

fn round_to_4(v: f64) -> f64 {
    let factor = 10f64.powi(4);
    (v * factor).round() / factor
}

fn extract_list_str(
    dict: &HashMap<String, pyo3::Py<pyo3::PyAny>>,
    key: &str,
    py: Python<'_>,
) -> Vec<String> {
    if let Some(v) = dict.get(key) {
        if let Ok(iter) = v.bind(py).try_iter() {
            return iter
                .filter_map(|item| item.ok().and_then(|x| x.extract::<String>().ok()))
                .collect();
        }
    }
    vec![]
}

fn extract_list_f64(
    dict: &HashMap<String, pyo3::Py<pyo3::PyAny>>,
    key: &str,
    py: Python<'_>,
) -> Vec<f64> {
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
}

/// PyO3 entry point — extract dicts, call inner, return PyDataFrame.
#[pyfunction]
pub fn cross_events_kernel(
    py: Python<'_>,
    per_bucket: Vec<HashMap<String, pyo3::Py<pyo3::PyAny>>>,
) -> PyResult<PyDataFrame> {
    let df = cross_events_inner(&per_bucket, py)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("{e}")))?;
    Ok(PyDataFrame(df))
}

#[cfg(test)]
mod tests {
    use super::*;
    use pyo3::types::PyDict;

    fn make_bucket<'py>(
        py: Python<'py>,
        bucket: &str,
        names: &[&str],
        pcts: &[f64],
    ) -> HashMap<String, pyo3::Py<pyo3::PyAny>> {
        let dict = PyDict::new(py);
        dict.set_item("bucket", bucket).unwrap();
        let names_list: Vec<&str> = names.to_vec();
        dict.set_item("names", names_list).unwrap();
        let pcts_list: Vec<f64> = pcts.to_vec();
        dict.set_item("pcts", pcts_list).unwrap();
        let mut map: HashMap<String, pyo3::Py<pyo3::PyAny>> = HashMap::new();
        for (k, v) in dict.iter() {
            let kstr: String = k.extract().unwrap();
            map.insert(kstr, v.into());
        }
        map
    }

    /// Smoke: 3 buckets, 2 members. Tech flips + → - (down); Banks flips - → + (up).
    /// After bucket 2, Tech flips - → + again (up event). 3 total events.
    #[test]
    fn smoke_sign_changes() {
        pyo3::Python::initialize();
        Python::attach(|py| {
            let per_bucket = vec![
                make_bucket(py, "09:30", &["Tech", "Banks"], &[0.02_f64, -0.01]),
                make_bucket(py, "09:35", &["Tech", "Banks"], &[-0.03_f64, 0.01]),
                make_bucket(py, "09:40", &["Tech", "Banks"], &[0.01_f64, 0.005]),
            ];
            let df = cross_events_inner(&per_bucket, py).unwrap();
            assert_eq!(df.width(), 4, "time/name/dir/pct");
            assert_eq!(df.height(), 3, "Tech +→- (down), Banks -→+ (up), Tech -→+ (up)");

            let times: Vec<&str> = df.column("time").unwrap().str().unwrap()
                .iter().map(|s| s.unwrap_or("")).collect();
            let names: Vec<&str> = df.column("name").unwrap().str().unwrap()
                .iter().map(|s| s.unwrap_or("")).collect();
            let dirs: Vec<&str> = df.column("dir").unwrap().str().unwrap()
                .iter().map(|s| s.unwrap_or("")).collect();
            let pcts: Vec<f64> = df.column("pct").unwrap().f64().unwrap()
                .iter().map(|v| v.unwrap_or(0.0)).collect();
            // Order: events emitted per-bucket in order.
            assert_eq!(times, vec!["09:35", "09:35", "09:40"]);
            assert_eq!(names, vec!["Tech", "Banks", "Tech"]);
            assert_eq!(dirs, vec!["down", "up", "up"]);
            assert_eq!(pcts, vec![-0.03, 0.01, 0.01]);
        });
    }

    /// Tied sign: `+0.0 → +0.0` (or `-0.0 → -0.0`) emits no event.
    /// Python's `if prev != sign` uses integer sign; `0.0 >= 0.0` → sign=1.
    #[test]
    fn tied_sign_no_event() {
        pyo3::Python::initialize();
        Python::attach(|py| {
            let per_bucket = vec![
                make_bucket(py, "09:30", &["Tech"], &[0.02_f64]),
                make_bucket(py, "09:35", &["Tech"], &[0.005_f64]),  // sign still +1
                make_bucket(py, "09:40", &["Tech"], &[-0.01_f64]), // sign flips
            ];
            let df = cross_events_inner(&per_bucket, py).unwrap();
            assert_eq!(df.height(), 1, "only bucket 3 emits an event");
            let names: Vec<&str> = df.column("name").unwrap().str().unwrap()
                .iter().map(|s| s.unwrap_or("")).collect();
            let dirs: Vec<&str> = df.column("dir").unwrap().str().unwrap()
                .iter().map(|s| s.unwrap_or("")).collect();
            assert_eq!(names, vec!["Tech"]);
            assert_eq!(dirs, vec!["down"]);
        });
    }
}
