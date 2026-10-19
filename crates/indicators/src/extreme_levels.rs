//! `extreme_levels` — production kernel for `levels._extreme_levels` (Spec 0002).
//!
//! Algorithm: 1:1 port of `backend/app/indicators/levels.py::_extreme_levels`
//! using **Approach A** per the Ticket 01 research note
//! (`docs/research/extreme-levels-rust-api.md` §4): `Float64Chunked::to_vec()`
//! once per column, then a single Rust `for i in 0..n` loop that produces all
//! of {60d/250d windowed extremes, per-bar swing highs/lows} in one pass.
//! No Polars expressions / `rolling_max` / `rolling_min` in the hot path
//! (the API survey in research §2-§3 rejected those alternatives).
//!
//! Seam contract: `polars.DataFrame` in, `polars.DataFrame` out, with new
//! columns appended:
//!   - `swing_high: Boolean` (length N): per-bar local-max indicator,
//!     true iff `highs[i] == max(highs[i-swing_win..i+swing_win+1])`.
//!     Ties mark all bars; boundary bars `i < win` and `i >= n - win`
//!     are false (matches Python's `for i in range(win, len(highs) - win)`).
//!   - `swing_low: Boolean` (length N): symmetric, on `lows`.
//!   - `extreme_<w>_high: Float64` (length N, broadcast): `max(highs.tail(w))`
//!     if `df.height >= w` else NaN. Same value on every row; adapter reads
//!     `out["extreme_<w>_high"][0]`.
//!   - `extreme_<w>_low: Float64` (length N, broadcast): `min(lows.tail(w))`.
//!
//! Graceful no-op (per spec §"Algorithm (Rust)" / Ticket 02 code-review): empty
//! df → return the input unchanged (no `swing_high/low` columns appended; matches
//! Python's "return []" + the adapter's `df.is_empty()` check). Missing `high`
//! or `low` column → `PolarsError::ColumnNotFound`, surfaces to Python as a
//! `PyValueError`. The two no-op branches keep the output schema deterministic.

use polars::prelude::*;
use pyo3::prelude::*;
use pyo3_polars::PyDataFrame;

/// Pure-Rust kernel. No Python / no PyO3. Tested directly in `mod tests`.
pub fn extreme_levels_inner(
    df: &DataFrame,
    windows: &[u32],
    swing_win: u32,
) -> PolarsResult<DataFrame> {
    let n = df.height();

    // Graceful no-op: empty df → unchanged (adapter checks `df.is_empty()`).
    if n == 0 {
        return Ok(df.clone());
    }

    // Required columns. Missing → ColumnNotFound bubbles as PyValueError.
    let highs = df.column("high")?.f64()?.to_vec();
    let lows = df.column("low")?.f64()?.to_vec();

    // 60 / 250 窗口极值 — single pass over `highs`/`lows`, multiple fold accumulators.
    // Matches Python `if df.height < n: continue` — when the window doesn't fit,
    // emit NaN (the adapter filters via `math.isfinite` / `_ok`, same as Python).
    let mut h_ext: Vec<f64> = Vec::with_capacity(windows.len());
    let mut l_ext: Vec<f64> = Vec::with_capacity(windows.len());
    for &w in windows {
        let w = w as usize;
        if n < w {
            h_ext.push(f64::NAN);
            l_ext.push(f64::NAN);
            continue;
        }
        let start = n - w;
        let hmax = highs[start..n]
            .iter()
            .filter_map(|x| *x)
            .fold(f64::NEG_INFINITY, f64::max);
        let lmin = lows[start..n]
            .iter()
            .filter_map(|x| *x)
            .fold(f64::INFINITY, f64::min);
        // Match Python's `_ok(v)` which returns False on NaN. f64::NEG_INFINITY
        // / INFINITY from empty folds would otherwise leak.
        let hmax = if hmax.is_finite() { hmax } else { f64::NAN };
        let lmin = if lmin.is_finite() { lmin } else { f64::NAN };
        h_ext.push(hmax);
        l_ext.push(lmin);
    }

    // Per-bar swing detection — tie-aware, boundary-skipped.
    //   `for i in range(win, len(highs) - win):` → `for i in win..n.saturating_sub(win)`
    //   `highs[i] == max(highs[i - win:i + win + 1])` → non-strict `<=` against all
    //   neighbours (matches Python's `== max(...)` exactly; ties mark all bars).
    let win = swing_win as usize;
    let mut swing_high = vec![false; n];
    let mut swing_low = vec![false; n];
    let boundary_end = n.saturating_sub(win);
    if win > 0 && boundary_end > win {
        for i in win..boundary_end {
            let (h, l) = match (highs[i], lows[i]) {
                (Some(h), Some(l)) => (h, l),
                _ => continue,
            };
            let lo = i - win;
            let hi = i + win + 1;
            // Swing high: h >= every neighbour (ties mark).
            swing_high[i] = (lo..hi).all(|j| j == i || highs[j].map_or(true, |x| x <= h));
            swing_low[i] = (lo..hi).all(|j| j == i || lows[j].map_or(true, |x| x >= l));
        }
    }

    // Build the output DataFrame: passthrough columns + new boolean + float64 broadcasts.
    let mut out = df.clone();
    out.with_column(
        BooleanChunked::new(PlSmallStr::from_static("swing_high"), swing_high).into_column(),
    )?;
    out.with_column(
        BooleanChunked::new(PlSmallStr::from_static("swing_low"), swing_low).into_column(),
    )?;
    for (idx, &w) in windows.iter().enumerate() {
        let name_h = format!("extreme_{}_high", w);
        let name_l = format!("extreme_{}_low", w);
        out.with_column(
            Float64Chunked::from_vec(PlSmallStr::from(name_h), vec![h_ext[idx]; n]).into_column(),
        )?;
        out.with_column(
            Float64Chunked::from_vec(PlSmallStr::from(name_l), vec![l_ext[idx]; n]).into_column(),
        )?;
    }
    Ok(out)
}

/// PyO3 entry point — thin wrapper around `extreme_levels_inner`.
#[pyfunction]
#[pyo3(signature = (df, windows=None, swing_win=5))]
pub fn extreme_levels(
    df: PyDataFrame,
    windows: Option<Vec<u32>>,
    swing_win: u32,
) -> PyResult<PyDataFrame> {
    // Default windows to [60, 250] in Rust so the Python adapter doesn't have
    // to pass them every call (matches the spec's "current 60/250 set").
    let windows = windows.unwrap_or_else(|| vec![60, 250]);
    let owned: DataFrame = df.into();
    let out = extreme_levels_inner(&owned, &windows, swing_win)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("{e}")))?;
    Ok(PyDataFrame(out))
}

#[cfg(test)]
mod tests {
    use super::*;
    use polars::df;

    /// Main-path correctness: 30 rows × explicit peaks/troughs at known indices.
    ///
    /// Hand-computed expected outputs (win=5, boundary skip = `i in [5, 25)`):
    ///   - `highs[5]  = 106.0` (peak);     window [0..11] max = 106.0 → swing_high @ 5
    ///   - `highs[13] = 108.0`;            window [8..19] max = 116.0 (bar 17) → not swing
    ///   - `highs[17] = 116.0`;            window [12..23] max = 116.0 → swing_high @ 17
    ///   - `highs[18] = 116.0` (TIE);      window [13..24] max = 116.0 → swing_high @ 18 (TIE!)
    ///   - `highs[25] = 124.0`;            boundary — excluded by `range(5, 25)` → NOT swing
    ///   - `lows[6]  = 92.0` (trough);     window [1..12] min = 92.0 → swing_low @ 6
    ///   - `lows[19] = 88.0`;              window [14..25] min = 88.0 → swing_low @ 19
    ///   - `lows[20] = 88.0` (TIE);        window [15..26] min = 88.0 → swing_low @ 20 (TIE!)
    ///
    /// Windowed extremes (Python `if df.height < n: continue` semantics):
    ///   - `extreme_60_high = NaN`  (n=30 < 60; adapter filters via `math.isfinite`)
    ///   - `extreme_60_low  = NaN`
    ///   - `extreme_250_high = NaN`
    ///   - `extreme_250_low  = NaN`
    #[test]
    fn main_path_with_ties() {
        let highs: Vec<f64> = (0..30)
            .map(|i| match i {
                5 => 106.0,
                13 => 108.0,
                17 | 18 => 116.0, // TIE
                25 => 124.0,      // boundary, excluded
                _ => 100.0 + ((i as f64) % 4.0) * 0.25, // 100.0 .. 100.75
            })
            .collect();
        let lows: Vec<f64> = (0..30)
            .map(|i| match i {
                6 => 92.0,
                10 => 94.0,
                14 => 91.0,
                19 | 20 => 88.0, // TIE
                24 => 89.0,
                _ => 96.0 - ((i as f64) % 4.0) * 0.25, // 95.75 .. 96.0
            })
            .collect();
        let df = df! {
            "high" => highs,
            "low"  => lows,
            "close" => [10.0_f64; 30],
        }
        .unwrap();

        let out = extreme_levels_inner(&df, &[60, 250], 5).unwrap();
        assert_eq!(out.height(), 30, "input height preserved");

        // swing_high: tie-aware, boundary skip
        let sh = out.column("swing_high").unwrap().bool().unwrap();
        let sh_vec: Vec<bool> = (0..30).map(|i| sh.get(i).unwrap_or(false)).collect();
        assert_eq!(sh_vec[5], true, "bar 5 is a local max");
        assert_eq!(sh_vec[17], true, "bar 17 is a local max (tied)");
        assert_eq!(sh_vec[18], true, "bar 18 is a local max (tied)");
        assert_eq!(sh_vec[25], false, "bar 25 is boundary-excluded");
        assert_eq!(sh_vec[13], false, "bar 13 has higher neighbours");
        // Total count
        let true_total: usize = sh_vec.iter().filter(|x| **x).count();
        assert_eq!(true_total, 3, "exactly 3 swing highs: 5, 17, 18");

        // swing_low: same shape
        let sl = out.column("swing_low").unwrap().bool().unwrap();
        let sl_vec: Vec<bool> = (0..30).map(|i| sl.get(i).unwrap_or(false)).collect();
        assert_eq!(sl_vec[6], true, "bar 6 is a local min");
        assert_eq!(sl_vec[19], true, "bar 19 is a local min (tied)");
        assert_eq!(sl_vec[20], true, "bar 20 is a local min (tied)");
        let true_total: usize = sl_vec.iter().filter(|x| **x).count();
        assert_eq!(true_total, 3, "exactly 3 swing lows: 6, 19, 20");

        // Windowed extremes — Python `if df.height < n: continue` semantics.
        // Both 60 and 250 are larger than n=30 → both NaN, adapter filters.
        let h60 = out.column("extreme_60_high").unwrap().f64().unwrap().get(0).unwrap();
        assert!(h60.is_nan(), "60d_high = NaN when n < 60 (mirrors Python skip)");
        let l60 = out.column("extreme_60_low").unwrap().f64().unwrap().get(0).unwrap();
        assert!(l60.is_nan(), "60d_low = NaN when n < 60");
        let h250 = out.column("extreme_250_high").unwrap().f64().unwrap().get(0).unwrap();
        assert!(h250.is_nan(), "250d_high = NaN when n < 250");
        let l250 = out.column("extreme_250_low").unwrap().f64().unwrap().get(0).unwrap();
        assert!(l250.is_nan(), "250d_low = NaN when n < 250");
    }

    /// Graceful no-op: empty df → unchanged output (no new columns appended).
    /// The adapter checks `df.is_empty()` and yields `[]` upstream.
    #[test]
    fn graceful_no_op_empty_df() {
        let df = df! {
            "high" => Vec::<f64>::new(),
            "low"  => Vec::<f64>::new(),
        }
        .unwrap();
        let out = extreme_levels_inner(&df, &[60, 250], 5).unwrap();
        assert_eq!(out.height(), 0, "empty df preserved");
        // No new columns should be appended on empty df.
        assert!(out.column("swing_high").is_err(), "no swing_high on empty df");
        assert!(out.column("extreme_60_high").is_err(), "no extreme_60_high on empty df");
    }

    /// Null handling: a single null in the middle of the high column → that bar
    /// is skipped (not swing, not contribute to extremes). Matches Python's
    /// `_ok(v)` check in `_extreme_levels`.
    #[test]
    fn null_handling() {
        // 30 bars of which bar 29 is None for high; bars 0..28 are Some(100.0).
        let highs: Vec<Option<f64>> = std::iter::repeat(Some(100.0)).take(29).chain(std::iter::once(None)).collect();
        let lows: Vec<Option<f64>>  = std::iter::repeat(Some(95.0)).take(30).collect();
        let df = df! {
            "high" => highs,
            "low"  => lows,
            "close" => [Some(99.0_f64); 30],
        }
        .unwrap();

        // Use windows=[10] (not [60]) so the kernel actually folds over bars
        // instead of short-circuiting to NaN. The point of this test is null
        // handling, not the w>n short-circuit.
        let out = extreme_levels_inner(&df, &[10], 5).unwrap();
        assert_eq!(out.height(), 30);

        // Null at bar 29: the swing scan stops at i < n - win = 25, so bar 29
        // is never even inspected. Windowed extreme folds the last 10 bars
        // (20..30), skipping the null at 29 → max = 100.0.
        let h10 = out.column("extreme_10_high").unwrap().f64().unwrap().get(0).unwrap();
        assert!(h10.is_finite(), "10d_high must be finite despite one null high");
        assert!((h10 - 100.0).abs() < 1e-9, "10d_high = 100.0 (non-null max)");

        let l10 = out.column("extreme_10_low").unwrap().f64().unwrap().get(0).unwrap();
        assert!((l10 - 95.0).abs() < 1e-9, "10d_low = 95.0");
    }
}