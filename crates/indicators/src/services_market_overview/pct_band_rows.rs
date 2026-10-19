//! `pct_band_rows_kernel` — 1:1 port of `_pct_band_rows` (Spec 0003 §"pct_band_rows_kernel signature").
//!
//! Algorithm (matches `backend/app/services/market_overview_builder.py::_pct_band_rows`):
//!   8 fixed buckets, boundary inclusive/exclusive per Python:
//!     <-5%    : v < -0.05                        (high exclusive, no low)
//!     -5~-3%  : -0.05 <= v < -0.03               (low inclusive, high exclusive)
//!     -3~-1%  : -0.03 <= v < -0.01
//!     -1~0%   : -0.01 <= v < 0
//!     0~1%    : 0    <= v < 0.01
//!     1~3%    : 0.01 <= v < 0.03
//!     3~5%    : 0.03 <= v < 0.05
//!     >5%     : v >= 0.05                        (low inclusive, no high)
//!
//! Output schema: `label: String, count: u32, pct: f64` (3 columns × 8 rows always).

use polars::prelude::*;
use pyo3::prelude::*;
use pyo3_polars::PyDataFrame;

struct PctBand {
    label: &'static str,
    low: Option<f64>,
    high: Option<f64>,
}

const BANDS: &[PctBand] = &[
    PctBand { label: "<-5%",   low: None,         high: Some(-0.05) },
    PctBand { label: "-5~-3%", low: Some(-0.05), high: Some(-0.03) },
    PctBand { label: "-3~-1%", low: Some(-0.03), high: Some(-0.01) },
    PctBand { label: "-1~0%",  low: Some(-0.01), high: Some(0.0)   },
    PctBand { label: "0~1%",   low: Some(0.0),   high: Some(0.01)  },
    PctBand { label: "1~3%",   low: Some(0.01),  high: Some(0.03)  },
    PctBand { label: "3~5%",    low: Some(0.03),  high: Some(0.05)  },
    PctBand { label: ">5%",    low: Some(0.05),  high: None         },
];

/// Pure-Rust inner — count values falling in each of 8 fixed bands, compute `pct = count/total*100`.
fn pct_band_rows_inner(values: &[f64]) -> PolarsResult<DataFrame> {
    // Python uses `len(values) or 1` to avoid divide-by-zero. Same here:
    // total is at least 1 so pct is always finite (0.0 when no values).
    let total = (values.len() as f64).max(1.0);

    let mut labels: Vec<&str> = Vec::with_capacity(BANDS.len());
    let mut counts: Vec<u32> = Vec::with_capacity(BANDS.len());
    let mut pcts: Vec<f64> = Vec::with_capacity(BANDS.len());

    for band in BANDS {
        let count = values
            .iter()
            .filter(|v| match (band.low, band.high) {
                (None, Some(h)) => **v < h,
                (Some(l), None) => **v >= l,
                (Some(l), Some(h)) => **v >= l && **v < h,
                _ => false,
            })
            .count() as u32;
        labels.push(band.label);
        counts.push(count);
        pcts.push(count as f64 / total * 100.0);
    }

    df![
        "label" => labels,
        "count" => counts,
        "pct"   => pcts,
    ]
}

/// PyO3 entry point — pass-through (input is `Vec<f64>`, no extraction needed).
#[pyfunction]
pub fn pct_band_rows_kernel(values: Vec<f64>) -> PyResult<PyDataFrame> {
    let df = pct_band_rows_inner(&values)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("{e}")))?;
    Ok(PyDataFrame(df))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Main-path correctness: 18 values exercising all 8 bands.
    /// Hand-computed expected bucket counts (Python semantics):
    ///   <-5%    : 1  (-0.062)
    ///   -5~-3%  : 2  (-0.048, -0.035)
    ///   -3~-1%  : 2  (-0.020, -0.012)
    ///   -1~0%   : 2  (-0.005, -0.001)
    ///   0~1%    : 3  (0.000, 0.002, 0.005)
    ///   1~3%    : 3  (0.010, 0.018, 0.025)
    ///   3~5%    : 2  (0.034, 0.041)
    ///   >5%     : 3  (0.052, 0.063, 0.071)
    ///   total   : 18 → pct = count/18 * 100
    #[test]
    fn main_path_18_values() {
        let values = vec![
            -0.062, -0.048, -0.035, -0.020, -0.012, -0.005, -0.001,
             0.000,  0.002,  0.005,  0.010,  0.018,  0.025,
             0.034,  0.041,  0.052,  0.063,  0.071,
        ];
        let df = pct_band_rows_inner(&values).unwrap();
        assert_eq!(df.height(), 8, "8 fixed buckets");
        assert_eq!(df.width(), 3, "label/count/pct columns");

        let labels: Vec<&str> = df
            .column("label").unwrap().str().unwrap()
            .iter().map(|s| s.unwrap_or("")).collect();
        let counts: Vec<u32> = df
            .column("count").unwrap().u32().unwrap()
            .iter().map(|c| c.unwrap_or(0)).collect();
        let pcts: Vec<f64> = df
            .column("pct").unwrap().f64().unwrap()
            .iter().map(|p| p.unwrap_or(0.0)).collect();

        assert_eq!(labels, vec!["<-5%", "-5~-3%", "-3~-1%", "-1~0%", "0~1%", "1~3%", "3~5%", ">5%"]);
        assert_eq!(counts, vec![1, 2, 2, 2, 3, 3, 2, 3]);

        // 1/18 ≈ 5.555..., 2/18 ≈ 11.111..., 3/18 = 16.666...
        let expected_pcts = vec![
            1.0 / 18.0 * 100.0,
            2.0 / 18.0 * 100.0,
            2.0 / 18.0 * 100.0,
            2.0 / 18.0 * 100.0,
            3.0 / 18.0 * 100.0,
            3.0 / 18.0 * 100.0,
            2.0 / 18.0 * 100.0,
            3.0 / 18.0 * 100.0,
        ];
        for (got, exp) in pcts.iter().zip(expected_pcts.iter()) {
            assert!((got - exp).abs() < 1e-9, "pct mismatch: got {} exp {}", got, exp);
        }
        // Total of pcts = (1+2+2+2+3+3+2+3)/18 * 100 = 18/18 * 100 = 100.0
        let total_pct: f64 = pcts.iter().sum();
        assert!((total_pct - 100.0).abs() < 1e-9, "pct totals 100%");
    }

    /// Boundary inclusiveness: `v == -0.05` falls into `-5~-3%` (low inclusive),
    /// `v == -0.03` falls into `-3~-1%` (high exclusive on the previous band).
    #[test]
    fn boundary_inclusiveness() {
        let values = vec![-0.05, -0.03, -0.01, 0.0, 0.01, 0.03, 0.05];
        let df = pct_band_rows_inner(&values).unwrap();
        let labels: Vec<&str> = df
            .column("label").unwrap().str().unwrap()
            .iter().map(|s| s.unwrap_or("")).collect();
        let counts: Vec<u32> = df
            .column("count").unwrap().u32().unwrap()
            .iter().map(|c| c.unwrap_or(0)).collect();

        // v=-0.05 → -5~-3% (count=1)
        // v=-0.03 → -3~-1% (count=1)
        // v=-0.01 → -1~0%  (count=1)
        // v= 0.00 → 0~1%   (count=1)
        // v= 0.01 → 1~3%   (count=1)
        // v= 0.03 → 3~5%   (count=1)
        // v= 0.05 → >5%    (count=1)
        assert_eq!(counts, vec![0, 1, 1, 1, 1, 1, 1, 1]);
        let _ = labels; // unused, but assert shape: 8 entries.
    }

    /// Empty input → 8-row DataFrame with all counts=0 and pcts=0.0.
    /// Mirrors `_pct_band_rows([])` returning `[{label,count=0,pct=0.0}, ...]`.
    /// `total = max(1, 0) = 1` so pct = 0/1*100 = 0.0.
    #[test]
    fn empty_input_yields_zero_counts() {
        let values: Vec<f64> = vec![];
        let df = pct_band_rows_inner(&values).unwrap();
        assert_eq!(df.height(), 8);
        let counts: Vec<u32> = df
            .column("count").unwrap().u32().unwrap()
            .iter().map(|c| c.unwrap_or(0)).collect();
        assert!(counts.iter().all(|c| *c == 0));
        let pcts: Vec<f64> = df
            .column("pct").unwrap().f64().unwrap()
            .iter().map(|p| p.unwrap_or(0.0)).collect();
        assert!(pcts.iter().all(|p| *p == 0.0));
    }
}