// Research example for Spec 0001: which Polars 0.55.2 API should `levels.rs` use
// to compute per-row bin indices for the chip-distribution algorithm?
//
// Demonstrates Approach A (recommended): to_vec() once, then plain Rust loop.
// Does NOT touch the tf_rs_indicators crate (cdylib-only; example compiles standalone).
//
// Run: cargo run --example research_levels_api -p tf-rs-indicators

use polars::prelude::*;

fn bin_index(price: f64, lo: f64, step: f64, bins: usize) -> usize {
    let raw = ((price - lo) / step).floor() as i64;
    raw.clamp(0, (bins as i64) - 1) as usize
}

struct Lcg(u64);
impl Lcg {
    fn next_unit(&mut self) -> f64 {
        self.0 = self.0.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        ((self.0 >> 33) as u32 as f64) / (u32::MAX as f64)
    }
}

fn main() {
    let n: usize = 30;
    let bins: usize = 5;
    let lo: f64 = 10.0;
    let hi: f64 = 20.0;
    let step: f64 = (hi - lo) / bins as f64;

    let mut rng = Lcg(0x1234_5678_dead_beef_u64);
    let mut highs: Vec<f64> = Vec::with_capacity(n);
    let mut lows: Vec<f64> = Vec::with_capacity(n);
    let mut vols: Vec<f64> = Vec::with_capacity(n);
    let mut turns: Vec<f64> = Vec::with_capacity(n);
    for i in 0..n {
        let base = 10.0 + (i as f64 / n as f64) * 10.0;
        let span = 0.4 + rng.next_unit() * 1.6;
        highs.push((base + span).min(hi));
        lows.push((base - span).max(lo));
        vols.push(1e5 + rng.next_unit() * 1e7);
        turns.push(rng.next_unit() * 5.0);
    }

    let columns = vec![
        Series::new("high".into(), highs.clone()).into_column(),
        Series::new("low".into(), lows.clone()).into_column(),
        Series::new("volume".into(), vols.clone()).into_column(),
        Series::new("turnover_rate".into(), turns.clone()).into_column(),
    ];
    let df = DataFrame::new(n, columns).expect("build fixture df");

    println!("== Input fixture (n={}, bins={}, step={:.2}, hi-lo=[{:.2}, {:.2}]) ==",
        n, bins, step, lo, hi);
    println!("first 5 rows of (high, low, vol, turnover):");
    for i in 0..5.min(n) {
        println!("  [{:2}] h={:6.3} l={:6.3} v={:.3e} t={:.3}",
            i, highs[i], lows[i], vols[i], turns[i]);
    }
    println!();

    let high_ca: &Float64Chunked = df.column("high").unwrap().f64().unwrap();
    let low_ca:  &Float64Chunked = df.column("low").unwrap().f64().unwrap();
    let vol_ca:  &Float64Chunked = df.column("volume").unwrap().f64().unwrap();
    let turn_ca: &Float64Chunked = df.column("turnover_rate").unwrap().f64().unwrap();

    let highs_v: Vec<Option<f64>> = high_ca.to_vec();
    let lows_v:  Vec<Option<f64>> = low_ca.to_vec();
    let vols_v:  Vec<Option<f64>> = vol_ca.to_vec();
    let turns_v: Vec<Option<f64>> = turn_ca.to_vec();

    let edges: Vec<f64> = (0..=bins).map(|k| lo + k as f64 * step).collect();
    let mut chips: Vec<f64> = vec![0.0; bins];

    for i in 0..n {
        let t = turns_v[i].unwrap_or(0.0);
        let decay_ratio = 1.0 - t.clamp(0.0, 100.0) / 100.0;
        if decay_ratio < 1.0 {
            for k in 0..bins {
                chips[k] *= decay_ratio;
            }
        }
        let v = vols_v[i].unwrap_or(0.0);
        if v > 0.0 {
            let lo_i = lows_v[i].unwrap_or(lo);
            let hi_i = highs_v[i].unwrap_or(lo);
            let k_low  = bin_index(lo_i, lo, step, bins);
            let k_high = bin_index(hi_i, lo, step, bins);
            let (k_low, k_high) = if k_low > k_high { (k_high, k_low) } else { (k_low, k_high) };
            let share = v / (k_high - k_low + 1) as f64;
            for k in k_low..=k_high {
                chips[k] += share;
            }
        }
    }

    println!("== Bin indices for first 5 rows ==");
    for i in 0..5.min(n) {
        let lo_i = lows_v[i].unwrap_or(lo);
        let hi_i = highs_v[i].unwrap_or(lo);
        let kl = bin_index(lo_i, lo, step, bins);
        let kh = bin_index(hi_i, lo, step, bins);
        println!("  [{:2}] k_low={} k_high={}  price=[{:.2}, {:.2}] -> bin [{}, {}]",
            i, kl, kh, lo_i, hi_i, kl, if kh >= kl { kh } else { kl });
    }
    println!();

    println!("== Final chips array (after {} rows of decay+distribute) ==", n);
    let mut total: f64 = 0.0;
    for (k, c) in chips.iter().enumerate() {
        total += c;
        println!("  bin {} [{:5.2}, {:5.2})  chips = {:>14.6e}",
            k, edges[k], edges[k + 1], c);
    }
    println!("  total chips across bins = {:.6e}", total);
    println!();

    let poc_pos = (0..bins).max_by(|&a, &b| {
        chips[a].partial_cmp(&chips[b]).unwrap_or(std::cmp::Ordering::Equal)
    }).unwrap();
    let mean = total / bins as f64;
    let mut candidates: Vec<(usize, f64)> = (0..bins)
        .filter(|&k| k != poc_pos && chips[k] > mean)
        .map(|k| (k, chips[k]))
        .collect();
    candidates.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
    let top2: Vec<(usize, f64)> = candidates.into_iter().take(2).collect();

    println!("== Reduction (POC + top-2 dense zones) ==");
    println!("  POC   bin {}  mid={:.3}  chips={:.6e}",
        poc_pos, (edges[poc_pos] + edges[poc_pos + 1]) / 2.0, chips[poc_pos]);
    for (k, c) in &top2 {
        println!("  Dense bin {}  mid={:.3}  chips={:.6e}",
            k, (edges[*k] + edges[*k + 1]) / 2.0, c);
    }
}
