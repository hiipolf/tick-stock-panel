//! `rank_for_mode_kernel` — 6-axis ranking (Spec 0004 §"8 Rust kernels" row 8).
//!
//! Algorithm (matches `backend/app/services/sector_rotation.py::_rank_for_mode`):
//!   `sort_by ∈ {"activity", "flow", "pct", "rank_change", "momentum", default}`
//!
//! Per Q16=a — 6 tests cover 5 `sort_by` branches + score input order preservation:
//!   - `activity`: sectors sorted by `activity_map` desc; if `has_amount=false` or
//!     `activity_map` empty → return input order (`sectors` list as-is).
//!   - `flow`: sort sectors by `flow_by_member[member]` desc; if `flow_available=false`
//!     → return input order.
//!   - `pct`: sort by sector's `pct_now` desc; None last.
//!   - `rank_change`: sort by `rank_change` desc; None last.
//!   - `momentum`: sort by `(pct_now - pct_prev)` desc; both-None last.
//!   - default (`score`/anything else): return input order.
//!
//! Input: `Vec<HashMap<String, Py<PyAny>>>` + 4 scalar params.
//! Each sector dict has keys: `name`, `pct_now` (Option<f64>), `pct_prev` (Option<f64>),
//! `rank_now` (Option<u32>), `rank_prev` (Option<u32>), `rank_change` (Option<i32>),
//! `flow` (Option<f64>), `score` (Option<f64>).
//!
//! Output: `Vec<String>` — sector names in rank order (descending by the chosen metric;
//! input order preserved on fallback branches).

use std::cmp::Ordering;
use std::collections::HashMap;

use pyo3::prelude::*;

/// Internal sector row (decoupled from Py<PyAny> for testability).
#[derive(Clone, Debug)]
pub(crate) struct SectorRow {
    pub name: String,
    pub pct_now: Option<f64>,
    pub pct_prev: Option<f64>,
    pub rank_now: Option<u32>,
    pub rank_prev: Option<u32>,
    pub rank_change: Option<i32>,
    pub flow: Option<f64>,
    pub score: Option<f64>,
}

/// Pure-Rust inner — extract sectors, apply `sort_by`, return ranked names.
fn rank_for_mode_inner(
    sectors: &[HashMap<String, pyo3::Py<pyo3::PyAny>>],
    activity_map: &HashMap<String, f64>,
    has_amount: bool,
    flow_by_member: &HashMap<String, f64>,
    flow_available: bool,
    sort_by: &str,
    py: Python<'_>,
) -> Vec<String> {
    let rows = extract_sectors(sectors, py);

    match sort_by {
        "activity" => {
            if has_amount && !activity_map.is_empty() {
                // Sort sectors by activity_map[name] desc.
                let mut indexed: Vec<(usize, f64)> = rows
                    .iter()
                    .enumerate()
                    .map(|(i, r)| (i, activity_map.get(&r.name).copied().unwrap_or(0.0)))
                    .collect();
                indexed.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(Ordering::Equal));
                return indexed.into_iter().map(|(i, _)| rows[i].name.clone()).collect();
            }
            // Fallback: input order.
            rows.into_iter().map(|r| r.name).collect()
        }
        "flow" => {
            if flow_available {
                let mut indexed: Vec<(usize, Option<f64>)> = rows
                    .iter()
                    .enumerate()
                    .map(|(i, r)| (i, flow_by_member.get(&r.name).copied()))
                    .collect();
                // Sort by (None-last, value desc) — Python's key=(is_not_none, value).
                indexed.sort_by(|a, b| {
                    let a_has = a.1.is_some();
                    let b_has = b.1.is_some();
                    b_has.cmp(&a_has).then_with(|| {
                        // Both present: descending by value.
                        let av = a.1.unwrap_or(0.0);
                        let bv = b.1.unwrap_or(0.0);
                        bv.partial_cmp(&av).unwrap_or(Ordering::Equal)
                    })
                });
                return indexed.into_iter().map(|(i, _)| rows[i].name.clone()).collect();
            }
            // Fallback: input order.
            rows.into_iter().map(|r| r.name).collect()
        }
        "pct" => {
            let mut indexed: Vec<(usize, Option<f64>)> = rows
                .iter()
                .enumerate()
                .map(|(i, r)| (i, r.pct_now))
                .collect();
            indexed.sort_by(|a, b| {
                let a_has = a.1.is_some();
                let b_has = b.1.is_some();
                b_has.cmp(&a_has).then_with(|| {
                    let av = a.1.unwrap_or(0.0);
                    let bv = b.1.unwrap_or(0.0);
                    bv.partial_cmp(&av).unwrap_or(Ordering::Equal)
                })
            });
            indexed.into_iter().map(|(i, _)| rows[i].name.clone()).collect()
        }
        "rank_change" => {
            let mut indexed: Vec<(usize, Option<i32>)> = rows
                .iter()
                .enumerate()
                .map(|(i, r)| (i, r.rank_change))
                .collect();
            indexed.sort_by(|a, b| {
                let a_has = a.1.is_some();
                let b_has = b.1.is_some();
                b_has.cmp(&a_has).then_with(|| {
                    let av = a.1.unwrap_or(0);
                    let bv = b.1.unwrap_or(0);
                    bv.cmp(&av)
                })
            });
            indexed.into_iter().map(|(i, _)| rows[i].name.clone()).collect()
        }
        "momentum" => {
            // momentum = pct_now - pct_prev; both-None last.
            let mut indexed: Vec<(usize, Option<f64>)> = rows
                .iter()
                .enumerate()
                .map(|(i, r)| {
                    let m = match (r.pct_now, r.pct_prev) {
                        (Some(n), Some(p)) => Some(n - p),
                        _ => None,
                    };
                    (i, m)
                })
                .collect();
            indexed.sort_by(|a, b| {
                let a_has = a.1.is_some();
                let b_has = b.1.is_some();
                b_has.cmp(&a_has).then_with(|| {
                    let av = a.1.unwrap_or(0.0);
                    let bv = b.1.unwrap_or(0.0);
                    bv.partial_cmp(&av).unwrap_or(Ordering::Equal)
                })
            });
            indexed.into_iter().map(|(i, _)| rows[i].name.clone()).collect()
        }
        _ => {
            // Default / score: input order.
            rows.into_iter().map(|r| r.name).collect()
        }
    }
}

/// Extract sector rows from Python dicts.
fn extract_sectors(
    sectors: &[HashMap<String, pyo3::Py<pyo3::PyAny>>],
    py: Python<'_>,
) -> Vec<SectorRow> {
    sectors
        .iter()
        .map(|dict| {
            let name = dict
                .get("name")
                .and_then(|v| v.extract::<String>(py).ok())
                .unwrap_or_default();
            let pct_now = dict.get("pct_now").and_then(|v| v.extract::<f64>(py).ok()).filter(|f| f.is_finite());
            let pct_prev = dict.get("pct_prev").and_then(|v| v.extract::<f64>(py).ok()).filter(|f| f.is_finite());
            let rank_now = dict.get("rank_now").and_then(|v| v.extract::<u32>(py).ok());
            let rank_prev = dict.get("rank_prev").and_then(|v| v.extract::<u32>(py).ok());
            let rank_change = dict.get("rank_change").and_then(|v| v.extract::<i32>(py).ok());
            let flow = dict.get("flow").and_then(|v| v.extract::<f64>(py).ok()).filter(|f| f.is_finite());
            let score = dict.get("score").and_then(|v| v.extract::<f64>(py).ok()).filter(|f| f.is_finite());
            SectorRow {
                name,
                pct_now,
                pct_prev,
                rank_now,
                rank_prev,
                rank_change,
                flow,
                score,
            }
        })
        .collect()
}

/// PyO3 entry point — extract dicts + scalars, call inner, return Vec<String>.
#[pyfunction]
#[pyo3(signature = (
    sectors,
    activity_map,
    has_amount,
    flow_by_member,
    flow_available,
    sort_by
))]
pub fn rank_for_mode_kernel(
    py: Python<'_>,
    sectors: Vec<HashMap<String, pyo3::Py<pyo3::PyAny>>>,
    activity_map: HashMap<String, f64>,
    has_amount: bool,
    flow_by_member: HashMap<String, f64>,
    flow_available: bool,
    sort_by: &str,
) -> PyResult<Vec<String>> {
    let result = rank_for_mode_inner(&sectors, &activity_map, has_amount, &flow_by_member, flow_available, sort_by, py);
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    use pyo3::types::PyDict;

    fn make_sector<'py>(
        py: Python<'py>,
        name: &str,
        pct_now: Option<f64>,
        pct_prev: Option<f64>,
        rank_now: Option<u32>,
        rank_prev: Option<u32>,
        rank_change: Option<i32>,
        flow: Option<f64>,
        score: Option<f64>,
    ) -> HashMap<String, pyo3::Py<pyo3::PyAny>> {
        let dict = PyDict::new(py);
        dict.set_item("name", name).unwrap();
        dict.set_item("pct_now", pct_now).unwrap();
        dict.set_item("pct_prev", pct_prev).unwrap();
        dict.set_item("rank_now", rank_now).unwrap();
        dict.set_item("rank_prev", rank_prev).unwrap();
        dict.set_item("rank_change", rank_change).unwrap();
        dict.set_item("flow", flow).unwrap();
        dict.set_item("score", score).unwrap();
        let mut map: HashMap<String, pyo3::Py<pyo3::PyAny>> = HashMap::new();
        for (k, v) in dict.iter() {
            let kstr: String = k.extract().unwrap();
            map.insert(kstr, v.into());
        }
        map
    }

    /// `sort_by="activity"` + has_amount=true: sectors sorted by activity_map desc.
    #[test]
    fn activity_branch() {
        pyo3::Python::initialize();
        let mut activity = HashMap::new();
        activity.insert("Tech".to_string(), 1.5e7);
        activity.insert("Banks".to_string(), 3.0e7);
        activity.insert("Consumer".to_string(), 2.0e7);
        Python::attach(|py| {
            let sectors = vec![
                make_sector(py, "Tech", Some(0.02), Some(0.01), Some(2), Some(1), Some(-1), Some(1.0), Some(60.0)),
                make_sector(py, "Banks", Some(0.05), Some(0.03), Some(1), Some(2), Some(1), Some(2.0), Some(80.0)),
                make_sector(py, "Consumer", Some(0.03), Some(0.02), Some(3), Some(3), Some(0), Some(1.5), Some(70.0)),
            ];
            let ranked = rank_for_mode_inner(&sectors, &activity, true, &HashMap::new(), false, "activity", py);
            // Banks (3.0e7) > Consumer (2.0e7) > Tech (1.5e7).
            assert_eq!(ranked, vec!["Banks".to_string(), "Consumer".to_string(), "Tech".to_string()]);
        });
    }

    /// `sort_by="flow"` + flow_available=true: sectors sorted by flow_by_member desc.
    #[test]
    fn flow_branch() {
        pyo3::Python::initialize();
        let mut flow = HashMap::new();
        flow.insert("Tech".to_string(), 1.0e6);
        flow.insert("Banks".to_string(), 3.0e6);
        flow.insert("Consumer".to_string(), 2.0e6);
        Python::attach(|py| {
            let sectors = vec![
                make_sector(py, "Tech", Some(0.02), Some(0.01), Some(2), Some(1), Some(-1), Some(1.0), Some(60.0)),
                make_sector(py, "Banks", Some(0.05), Some(0.03), Some(1), Some(2), Some(1), Some(3.0), Some(80.0)),
                make_sector(py, "Consumer", Some(0.03), Some(0.02), Some(3), Some(3), Some(0), Some(2.0), Some(70.0)),
            ];
            let ranked = rank_for_mode_inner(&sectors, &HashMap::new(), true, &flow, true, "flow", py);
            assert_eq!(ranked, vec!["Banks".to_string(), "Consumer".to_string(), "Tech".to_string()]);
        });
    }

    /// `sort_by="pct"`: sectors sorted by pct_now desc; None last.
    #[test]
    fn pct_branch() {
        pyo3::Python::initialize();
        Python::attach(|py| {
            let sectors = vec![
                make_sector(py, "Tech", Some(0.02), Some(0.01), Some(2), Some(1), Some(-1), Some(1.0), Some(60.0)),
                make_sector(py, "Banks", Some(0.05), Some(0.03), Some(1), Some(2), Some(1), Some(3.0), Some(80.0)),
                make_sector(py, "NoData", None, None, None, None, None, None, None),
            ];
            let ranked = rank_for_mode_inner(&sectors, &HashMap::new(), true, &HashMap::new(), false, "pct", py);
            // Banks (0.05) > Tech (0.02) > NoData (None last).
            assert_eq!(ranked, vec!["Banks".to_string(), "Tech".to_string(), "NoData".to_string()]);
        });
    }

    /// `sort_by="rank_change"`: sectors sorted by rank_change desc (positive = 切入).
    #[test]
    fn rank_change_branch() {
        pyo3::Python::initialize();
        Python::attach(|py| {
            let sectors = vec![
                make_sector(py, "Tech", Some(0.02), Some(0.01), Some(2), Some(1), Some(-1), Some(1.0), Some(60.0)),
                make_sector(py, "Banks", Some(0.05), Some(0.03), Some(1), Some(2), Some(1), Some(3.0), Some(80.0)),
                make_sector(py, "Consumer", Some(0.03), Some(0.02), Some(3), Some(3), Some(0), Some(2.0), Some(70.0)),
            ];
            let ranked = rank_for_mode_inner(&sectors, &HashMap::new(), true, &HashMap::new(), false, "rank_change", py);
            // Banks (+1) > Consumer (0) > Tech (-1).
            assert_eq!(ranked, vec!["Banks".to_string(), "Consumer".to_string(), "Tech".to_string()]);
        });
    }

    /// `sort_by="momentum"`: sectors sorted by (pct_now - pct_prev) desc.
    /// Banks: 0.05 - 0.03 = 0.02; Tech: 0.02 - 0.01 = 0.01; Consumer: 0.03 - 0.02 = 0.01.
    /// Banks leads; Tech and Consumer tied → input order (Tech first in input).
    #[test]
    fn momentum_branch() {
        pyo3::Python::initialize();
        Python::attach(|py| {
            let sectors = vec![
                make_sector(py, "Tech", Some(0.02), Some(0.01), Some(2), Some(1), Some(-1), Some(1.0), Some(60.0)),
                make_sector(py, "Banks", Some(0.05), Some(0.03), Some(1), Some(2), Some(1), Some(3.0), Some(80.0)),
                make_sector(py, "Consumer", Some(0.03), Some(0.02), Some(3), Some(3), Some(0), Some(2.0), Some(70.0)),
            ];
            let ranked = rank_for_mode_inner(&sectors, &HashMap::new(), true, &HashMap::new(), false, "momentum", py);
            assert_eq!(ranked[0], "Banks", "Banks leads (momentum=0.02)");
            // Tech and Consumer tied at 0.01 → input order: Tech then Consumer.
            assert_eq!(ranked[1], "Tech");
            assert_eq!(ranked[2], "Consumer");
        });
    }

    /// `sort_by="score"` (or unknown) fallback: input order preserved (per Q16=a).
    #[test]
    fn score_input_order_preservation() {
        pyo3::Python::initialize();
        Python::attach(|py| {
            let sectors = vec![
                make_sector(py, "Tech", Some(0.02), Some(0.01), Some(2), Some(1), Some(-1), Some(1.0), Some(60.0)),
                make_sector(py, "Banks", Some(0.05), Some(0.03), Some(1), Some(2), Some(1), Some(3.0), Some(80.0)),
                make_sector(py, "Consumer", Some(0.03), Some(0.02), Some(3), Some(3), Some(0), Some(2.0), Some(70.0)),
            ];
            let ranked = rank_for_mode_inner(&sectors, &HashMap::new(), true, &HashMap::new(), false, "score", py);
            assert_eq!(ranked, vec!["Tech".to_string(), "Banks".to_string(), "Consumer".to_string()]);

            let ranked2 = rank_for_mode_inner(&sectors, &HashMap::new(), true, &HashMap::new(), false, "unknown_branch", py);
            assert_eq!(ranked2, vec!["Tech".to_string(), "Banks".to_string(), "Consumer".to_string()]);
        });
    }
}
