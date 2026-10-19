//! Throwaway research example for `_extreme_levels` Rust kernel API (Path A-2).
//!
//! Survey-only: builds a 30-row fixture with one tied swing-high cluster, runs the
//! single-pass Rust algorithm on a `Vec<Option<f64>>`, prints the resulting
//! boolean series. Not the production kernel — see `crates/indicators/src/
//! extreme_levels.rs`.
//!
//! Run:
//!   cargo check --example research_extreme_levels_api -p tf-rs-indicators
//!   cargo run   --example research_extreme_levels_api -p tf-rs-indicators

use std::f64;

fn swing_indices(values: &[Option<f64>], win: usize, mode: SwingMode) -> Vec<usize> {
    let n = values.len();
    if n <= 2 * win {
        return Vec::new();
    }
    let mut out = Vec::new();
    for i in win..(n - win) {
        let (Some(v),) = (values[i],) else { continue };
        let lo = i - win;
        let hi = i + win + 1;
        let is_extremum = match mode {
            SwingMode::High => (lo..hi).all(|j| j == i || values[j].map_or(true, |x| x <= v)),
            SwingMode::Low  => (lo..hi).all(|j| j == i || values[j].map_or(true, |x| x >= v)),
        };
        if is_extremum {
            out.push(i);
        }
    }
    out
}

enum SwingMode { High, Low }

fn main() {
    // 30-row fixture with **clear local maxima / minima** + one tied swing cluster.
    // Baselines stay flat around 100/95 so each peak/trough is the local extremum
    // within its ±5 window (i.e. no neighbour within the window exceeds it).
    let highs: Vec<Option<f64>> = (0..30).map(|i| match i {
        // Peaks spaced far enough apart that no two share a ±5 window.
        5  => Some(106.0),  // local max within [0..11]  — baseline ≤ 100
        13 => Some(108.0),  // local max within [8..19]  — baseline ≤ 100
        17 => Some(116.0),  // local max within [12..23] — baseline ≤ 100
        18 => Some(116.0),  // TIE with 17 — both must be marked
        25 => Some(124.0),  // local max within [20..30] — boundary, baseline ≤ 100
        // mid baseline (not a swing) but keep close-to-flat to make peaks visible
        _  => Some(100.0 + ((i as f64) % 4.0) * 0.25),  // 100.0 .. 100.75
    }).collect();

    let lows: Vec<Option<f64>> = (0..30).map(|i| match i {
        // Troughs spaced apart, with a tied cluster.
        6  => Some(92.0),
        10 => Some(94.0),
        14 => Some(91.0),
        19 => Some(88.0),
        20 => Some(88.0),  // TIE with 19
        24 => Some(89.0),
        _  => Some(96.0 - ((i as f64) % 4.0) * 0.25),  // 95.75 .. 96.0
    }).collect();

    let n = highs.len();
    let win = 5;

    let swing_high = swing_indices(&highs, win, SwingMode::High);
    let swing_low  = swing_indices(&lows,  win, SwingMode::Low);

    // Last-60-day extremes (here n=30 < 60, so we use full n).
    let hmax = highs.iter().filter_map(|x| *x).fold(f64::NEG_INFINITY, f64::max);
    let lmin = lows.iter().filter_map(|x| *x).fold(f64::INFINITY,  f64::min);

    println!("[research] n={} win={}", n, win);
    println!("  swing_high @ {:?}", swing_high);
    println!("  swing_low  @ {:?}", swing_low);
    println!("60d_high (last {}) = {:.2}", n, hmax);
    println!("60d_low  (last {}) = {:.2}", n, lmin);
}