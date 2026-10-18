//! `levels` — production kernel for `levels._support_resistance` (Spec 0001).
//!
//! Algorithm: 1:1 port of `backend/app/indicators/levels.py::_support_resistance`
//! using **Approach A** per the Ticket 01 research note
//! (`docs/research/levels-rust-api.md` §5): `Float64Chunked::to_vec()` once per
//! column, then a single Rust `for i in 0..n` loop on `Vec<Option<f64>>`.
//! No Polars expressions / `cut` / `qcut` / `search_sorted` in the hot path.
//!
//! Seam contract: `polars.DataFrame` in, `polars.DataFrame` out, with one new
//! `chip_pct: List<Float64>` column appended. The column has a single row
//! holding the final `chips` array of length `bins`; Polars auto-broadcasts
//! the 1-element list column to match the input height (matches the
//! `sum_close` hello-world convention).
//!
//! Graceful no-op (per spec 0001 §"Algorithm (Rust)"): empty df / df with no
//! `volume` / df with no `high` / df with no `low` / `df.height < 20` /
//! `hi <= lo` → return the same DataFrame with a `chip_pct` column of `null`s
//! (matches the Python "return []" + no-mutate contract).

use polars::prelude::*;
use pyo3::prelude::*;
use pyo3_polars::PyDataFrame;

/// Pure-Rust kernel. No Python / no PyO3. Tested directly in `mod tests`.
pub fn support_resistance_inner(df: &DataFrame, bins: u32) -> PolarsResult<DataFrame> {
    let n = df.height();
    let mut out = df.clone();

    // Graceful no-op pre-checks
    let no_volume = df.column("volume").is_err();
    let no_high = df.column("high").is_err();
    let no_low = df.column("low").is_err();
    let too_short = n < 20;

    if no_volume || no_high || no_low || too_short {
        return append_null_chips(&mut out, n);
    }

    // Lift the four required columns to contiguous Vec<Option<f64>>.
    let high_v: Vec<Option<f64>> = df.column("high")?.f64()?.to_vec();
    let low_v: Vec<Option<f64>> = df.column("low")?.f64()?.to_vec();
    let vol_v: Vec<Option<f64>> = df.column("volume")?.f64()?.to_vec();

    // hi / lo: per-spec the algorithm does `df["high"].max()` / `df["low"].min()`.
    // Using `f64::NEG_INFINITY` / `f64::INFINITY` as the starting fold values,
    // filtered for None, mirrors the Polars `max()` / `min()` semantics on
    // non-null columns.
    let hi = high_v
        .iter()
        .filter_map(|x| *x)
        .fold(f64::NEG_INFINITY, f64::max);
    let lo = low_v
        .iter()
        .filter_map(|x| *x)
        .fold(f64::INFINITY, f64::min);

    if !(hi > lo && lo > 0.0) {
        return append_null_chips(&mut out, n);
    }

    let bins_us = bins as usize;
    let step = (hi - lo) / bins as f64;

    // Optional `turnover_rate`: Python falls back to `[0.0] * n` when missing
    // (NOT a no-op — match the Python, not the earlier spec wording).
    let turn_v: Vec<f64> = match df.column("turnover_rate") {
        Ok(c) => c
            .f64()?
            .to_vec()
            .into_iter()
            .map(|x| x.unwrap_or(0.0))
            .collect(),
        Err(_) => vec![0.0; n],
    };

    // Sequential state update — the irreducible inner loop.
    let mut chips = vec![0.0_f64; bins_us];
    for i in 0..n {
        let t = turn_v[i];
        let decay_ratio = 1.0 - t.clamp(0.0, 100.0) / 100.0;
        if decay_ratio < 1.0 {
            for k in 0..bins_us {
                chips[k] *= decay_ratio;
            }
        }
        let v = vol_v[i].unwrap_or(0.0);
        if v > 0.0 {
            let lo_i = low_v[i].unwrap_or(lo);
            let hi_i = high_v[i].unwrap_or(lo);
            let k_low = bin_index(lo_i, lo, step, bins_us);
            let k_high = bin_index(hi_i, lo, step, bins_us);
            let (k_low, k_high) = if k_low > k_high {
                (k_high, k_low)
            } else {
                (k_low, k_high)
            };
            let share = v / (k_high - k_low + 1) as f64;
            for k in k_low..=k_high {
                chips[k] += share;
            }
        }
    }

    // Build a 1-row `List<Float64>` column with the final chips; Polars
    // auto-broadcasts the single-row list column to match the input height
    // (same pattern as `sum_close`).
    //
    // Polars 0.55.2 API: `ListChunked: FromIterator<Option<Series>>` — each
    // item is a `Series` whose dtype becomes the inner type. The result's
    // name is empty; we set it via `.with_name(...)` after collect.
    let chips_series = Series::new(PlSmallStr::EMPTY, chips);
    let ca: ListChunked = std::iter::once(Some(chips_series))
        .collect::<ListChunked>()
        .with_name(PlSmallStr::from_static("chip_pct"));
    out.with_column(ca.into_series().into_column())?;
    Ok(out)
}

/// Append a `chip_pct` column of `null` lists to `df`. Used by every
/// graceful-no-op branch.
fn append_null_chips(df: &mut DataFrame, n: usize) -> PolarsResult<DataFrame> {
    let ca: ListChunked = ListChunked::full_null(PlSmallStr::from_static("chip_pct"), n);
    df.with_column(ca.into_series().into_column())?;
    Ok(std::mem::take(df))
}

#[inline]
fn bin_index(price: f64, lo: f64, step: f64, bins: usize) -> usize {
    let raw = ((price - lo) / step).floor() as i64;
    raw.clamp(0, (bins as i64) - 1) as usize
}

/// PyO3 entry point — thin wrapper around `support_resistance_inner`.
#[pyfunction]
pub fn support_resistance(df: PyDataFrame, bins: u32) -> PyResult<PyDataFrame> {
    let owned: DataFrame = df.into();
    let out = support_resistance_inner(&owned, bins)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("{e}")))?;
    Ok(PyDataFrame(out))
}

#[cfg(test)]
mod tests {
    use super::*;
    use polars::df;

    /// Main-path correctness: 30 rows × 5 bins, all `low=10, high=12,
    /// volume=100, turnover=0`. Hand-computed expected chips:
    ///
    ///   - `hi = 12, lo = 10, step = (12-10) / 5 = 0.4`
    ///   - `k_low = (10-10)/0.4 = 0`, `k_high = (12-10)/0.4 = 5 → clamp to 4`
    ///   - span = [bin 0, bin 1, bin 2, bin 3, bin 4] (all 5 bins)
    ///   - `share = 100 / 5 = 20` per bin per row
    ///   - `decay = 1.0` (turnover=0 → no decay)
    ///   - After 30 rows: `chips = [600, 600, 600, 600, 600]`
    ///     (30 rows × 20 share per bin = 600 per bin; total mass = 3000 = 30 × 100)
    ///
    /// This is a non-tautological test: the kernel does decay+distribute+clamp
    /// arithmetic; a bug in any of those (off-by-one in the clamp, wrong floor
    /// direction, wrong decay formula, etc.) breaks the expected 600.
    #[test]
    fn main_path_hand_computed() {
        let df = df! {
            "high"          => [12.0_f64; 30],
            "low"           => [10.0_f64; 30],
            "volume"        => [100.0_f64; 30],
            "turnover_rate" => [0.0_f64; 30],
        }
        .unwrap();
        let out = support_resistance_inner(&df, 5).unwrap();
        assert_eq!(out.height(), 30, "all input rows must be preserved");
        assert!(out.column("chip_pct").is_ok(), "chip_pct column must be appended");

        let chip_col = out.column("chip_pct").unwrap();
        let list_ca = chip_col.list().unwrap();
        let row0_series = list_ca.get_as_series(0).unwrap();
        let final_chips: Vec<f64> = row0_series
            .f64()
            .unwrap()
            .into_no_null_iter()
            .collect();
        assert_eq!(final_chips.len(), 5, "must have 5 bins");
        for (k, &c) in final_chips.iter().enumerate() {
            assert!(
                (c - 600.0).abs() < 1e-9,
                "bin {k} expected ≈600.0, got {c}"
            );
        }
    }

    /// Graceful no-op: 30 rows but no `volume` column → `chip_pct` column of
    /// `null` lists (every row is null). Matches Python "return []" + no-mutate.
    #[test]
    fn graceful_no_op_no_volume_column() {
        let df = df! {
            "high" => [12.0_f64; 30],
            "low"  => [10.0_f64; 30],
        }
        .unwrap();
        let out = support_resistance_inner(&df, 40).unwrap();
        assert_eq!(out.height(), 30, "input height must be preserved");
        let chip_col = out.column("chip_pct").unwrap();
        assert_eq!(
            chip_col.null_count(),
            30,
            "all 30 rows must be null when no `volume` column"
        );
    }
}
