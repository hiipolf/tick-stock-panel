# Path A-2 — `_extreme_levels` Rust kernel API survey (Polars 0.55.2)

> Ticket 01 / Spec 0002. Author: Path A-2 (2026-10-19).
> Status: **Approach A verdict — pure-Rust single pass on `Vec<Option<f64>>` from `to_vec()`**.
> Cross-refs: Spec 0001 research note `docs/research/levels-rust-api.md` (sister decisions: `cut`/`qcut`/`search_sorted` rejected for the same reason).

## 1. Scope of the kernel

`tf_rs_indicators::extreme_levels(df, windows, swing_win)` produces, from a `polars.DataFrame` with OHLCV columns:

| Output column | Type | Cardinality | Semantics |
|---|---|---|---|
| `swing_high` | Boolean | N | per-bar: true iff `highs[i] == max(highs[i-swing_win..i+swing_win+1])` (ties mark all bars) |
| `swing_low` | Boolean | N | symmetric, on `lows` |
| `extreme_<w>_high` | Float64 | N (broadcast) | `max(highs.tail(w))` if `df.height >= w` else NaN/null |
| `extreme_<w>_low` | Float64 | N (broadcast) | `min(lows.tail(w))` if `df.height >= w` else NaN/null |

Plus the passthrough columns (date, open, high, low, close, volume, turnover_rate) so the Python adapter doesn't need to re-index input rows. Same pattern as `tf_rs_indicators::support_resistance` (Spec 0001 / `crates/indicators/src/levels.rs`).

## 2. Question 1: `Float64Chunked::rolling_max` ?

### 2.1 What we tried to find

A natural impulse: `df.column("high")?.f64()?.rolling_max(60, options)` — return a `Float64Chunked` of length N where `[i]` is the rolling 60-day max of `high` ending at bar `i`. Then the adapter reads `out["rolling_60_high"].tail(1)` for the final 60-day high.

### 2.2 What actually exists in 0.55.2

There is **no `pub fn rolling_max` on `Float64Chunked`** as a member method. The `ChunkedArray<T>` impl `ChunkRollApply` only exposes a generic `rolling_map(f, options)` taking a custom closure. Confirmed via vendored source:

```
$reg/polars-core-0.55.2/src/series/implementations/floats.rs:149
    _options: RollingOptionsFixedWindow,         # only field reference, not a method

$reg/polars-core-0.55.2/src/series/implementations/mod.rs:218
    _options: RollingOptionsFixedWindow,         # same — never an actual rolling_max method

$reg/polars-core-0.55.2/src/chunked_array/ops/rolling_window.rs:79
    impl<T: PolarsNumericType> ChunkRollApply for ChunkedArray<T> {
        fn rolling_map(&self, f: ..., mut options: RollingOptionsFixedWindow)
```

`RollingOptionsFixedWindow` itself is well-defined (`polars-core-0.55.2/src/chunked_array/ops/rolling_window.rs:11`):

```rust
pub struct RollingOptionsFixedWindow {
    pub window_size: usize,
    pub min_periods: usize,
    pub weights: Option<Vec<f64>>,
    pub center: bool,
    pub fn_params: Option<RollingFnParams>,
}
```

But to get rolling_max specifically, one must:

1. Downcast the `Float64Chunked` to an Arrow `PrimitiveArray<f64>`:
   ```rust
   let arr = ca.downcast_as_array();   // &PrimitiveArray<f64>
   ```
2. Call the free function in `polars_compute`:
   ```rust
   use polars_compute::rolling::no_nulls::rolling_max;
   let out = rolling_max(arr.values(), window_size, min_periods, false, None, None);
   ```

The free function signatures (verified):

```
polars-compute-0.55.2/src/rolling/no_nulls/min_max.rs (via macro rolling_minmax_func!)
pub fn rolling_max<T>(values: &[T], window_size, min_periods, center, weights, _params) -> PolarsResult<ArrayRef>

polars-compute-0.55.2/src/rolling/nulls/min_max.rs:45
pub fn rolling_max<T>(arr: &PrimitiveArray<T>, window_size, min_periods, center, weights, _params) -> ArrayRef
```

These take slices of `T: NativeType + ... + Bounded + NumCast + Mul + Num`. For `T = f64`, all satisfied.

### 2.3 Verdict on Question 1

**Workable but pointless.** Going through `polars_compute::rolling::no_nulls::rolling_max` for the 60/250 extremes would:

- Add 3 layers of indirection (ChunkedArray → PrimitiveArray → polars_compute free function → re-wrap to ChunkedArray) for a **single scalar** we need (the trailing bar's window max).
- Not unify with the swing-detection per-bar scan, which has fundamentally different semantics (rolling gives the value, we need "is current == rolling max"). The two algorithms would still be two passes over the data with two separate `polars_compute::rolling::*` call sites.
- Force the kernel to know about nulls vs no-nulls dispatch (Polars `arr.has_nulls()` → `polars_compute::rolling::nulls::rolling_max` else `no_nulls`). Our pre-step `to_vec()` returns `Vec<Option<f64>>` which already handles nulls in the `f64` directly.

For **per-bar scan over windows of size 60 and 250**, a plain Rust loop over `&highs[n-w..n]` is the most direct expression:

```rust
let start = n.saturating_sub(w as usize);
let hmax = highs[start..n].iter().filter_map(|x| *x).fold(f64::NEG_INFINITY, f64::max);
let lmin = lows[start..n].iter().filter_map(|x| *x).fold(f64::INFINITY,  f64::min);
```

vs. the polars_compute alternative (≈ 12 lines, with feature gates for nullable arrays). Approach A wins on simplicity, with the same complexity class (one linear scan over ≤ 250 floats).

## 3. Question 2: swing detection — polars expression vs Rust scan?

### 3.1 The semantics

```python
# Original Python (`_extreme_levels` in backend/app/indicators/levels.py)
for i in range(win, len(highs) - win):
    if highs[i] == max(highs[i - win:i + win + 1]):
        swing_highs.append(float(highs[i]))
```

- `win = 5` (constant in current Python).
- Bar at index `i` is a **swing high** iff its `highs[i]` equals the **maximum** of the inclusive window `[i-win, i+win]` (i.e. the slice `highs[i-win : i+win+1]`).
- Equality, not strict: ties mark all bars that hit the max.
- Boundary bars `i < win` and `i >= n - win` are skipped.

### 3.2 What polars gives us

`pl.col("high").rolling_max(window_size=2*win+1, center=True)` returns a `Float64Chunked` where `[i]` is the window max. Per-bar call `chunks[i] == rolling_max[i]` — that *is* the swing condition. So a polars expression would be:

```python
window_max = pl.col("high").rolling_max(window_size=11, center=True)
swing_high = (pl.col("high") == window_max) & (pl.col("high").is_not_null())
```

That's a 2-line expression. Could be exported from the kernel as a `pl.col`-only path. **But**:

1. **It doesn't save the per-bar scan.** The kernel still has to materialize a Boolean `ChunkedArray` of length N to return to Python. Polars will compute that scan internally (same per-bar cost).
2. **It needs a separate kernel function** (because the Rust kernel signature is `DataFrame → DataFrame`, not `LazyFrame → LazyFrame`). Calling `df.lazy().with_columns([...]).collect()` inside a `#[pyfunction]` is awkward and pays a lazy-frame round-trip.
3. **It introduces a feature-flag dependency** on whatever polars feature exposes `rolling_max` at the `pl.col` DSL level (currently in `polars-ops` under feature `rolling_window`). Hello-world + Spec 0001 don't pull this in.

The Rust single-pass version:

```rust
for i in win..(n - win) {
    let (h, l) = match (highs[i], lows[i]) { (Some(h), Some(l)) => (h, l), _ => continue };
    let lo = i - win; let hi = i + win + 1;
    swing_high[i] = (lo..hi).all(|j| j == i || highs[j].map_or(true, |x| x <= h));
    swing_low[i]  = (lo..hi).all(|j| j == i || lows[j].map_or(true,  |x| x >= l));
}
```

is **10 lines**, has **no new feature flag**, and shares the same `Vec<Option<f64>>` cache with the 60/250 extremes — one traversal of `highs` and `lows` produces all six outputs (4 scalars + 2 boolean series).

### 3.3 Verdict on Question 2

**Approach A holds.** Pure-Rust single pass. Same reason as Spec 0001's `_support_resistance`: the algorithm is per-bar stateful (each bar's swing flag is independent, but the per-bar `max(highs[i-win..i+win+1])` allocates a slice + Python builtin `max()` call per iteration), and hoisting into expressions doesn't save work.

## 4. Verdict (Approach A)

The kernel is:

1. `df.column("high")?.f64()?.to_vec()` + `df.column("low")?.f64()?.to_vec()` once — single Arrow-buffer copy per column.
2. Single `for i in 0..n` loop in Rust that:
   - Tracks running max/min over the last `w` bars for each window in `windows` (one scan, multiple `fold` accumulators).
   - Tracks per-bar swing high/low with the all-vs-neighbors check.
3. Output DataFrame: passthrough columns + 2 boolean series + `len(windows) * 2` Float64 broadcast columns.

No new Cargo features. No `polars_compute::rolling::*` calls (the API survey was for completeness; nothing pulled in).

Memory: `Vec<Option<f64>>` of length N (one for high, one for low) ≈ 24 bytes/element × 240 ≈ 6 KB per call. Far under the 52 MB ceiling Spec 0001 measured for `_support_resistance`.

## 5. Open question for Ticket 02's implementer

**Tie-aware semantics** (Spec 0002 §"Algorithm (Rust)"): the algorithm `(lo..hi).all(|j| j == i || highs[j].map_or(true, |x| x <= h))` is equivalent to Python's `highs[i] == max(highs[i - win:i + win + 1])` only when comparing with **non-strict** `<=`. Confirm against fixture before locking the comparator:

| Pattern | Python equivalent | Notes |
|---|---|---|
| `highs[j] < h` | — | strict less, breaks ties |
| `highs[j] <= h` | implicit (since `== max` returns `h` even when there's a tie) | tie-aware, what we want |
| `highs[j] > h` | — | strict greater, wrong direction |

The `<=` form makes ALL bars that tie for max register as swing. Bit-identical to Python.

## 6. Throwaway example

`crates/indicators/examples/research_extreme_levels_api.rs` (committed alongside this note). Builds a 30-row fixture with one tied swing-high cluster at indices 17/18 and a tied swing-low cluster at 19/20, runs the single-pass Rust algorithm on a `Vec<f64>`, prints the resulting boolean series. Compiled + verified:

```bash
cargo check --example research_extreme_levels_api -p tf-rs-indicators   # exit 0
cargo run   --example research_extreme_levels_api -p tf-rs-indicators
```

Output (verbatim, captured during research):

```
[research] n=30 win=5
  swing_high @ [5, 17, 18]
  swing_low  @ [6, 19, 20]
60d_high (last 30) = 124.00
60d_low  (last 30) = 88.00
```

Sanity checks:
- `swing_high @ [5, 17, 18]`: bar 5 (peak 106) is alone (no neighbour higher); bars 17/18 both have 116 (tie → both marked); bar 25 (peak 124) is at the boundary and **correctly excluded** by `range(win, n - win) = 5..25`; intermediate peaks (8, 13, 22) had a higher neighbour in their ±5 window and are correctly NOT marked.
- `swing_low @ [6, 19, 20]`: bar 6 (trough 92) is alone; bars 19/20 both have 88 (tie → both marked); other troughs had a lower neighbour in their ±5 window.
- `60d_high = 124.00` (= bar 25's value, even though bar 25 itself is not in the swing set); `60d_low = 88.00` (bar 19/20 tie, both same value). The windowed extremes are computed over the full DataFrame, independent of the swing-detection boundary skip.

## 7. Cited excerpts (cross-checked against vendored sources)

| # | File:Line | Excerpt |
|---|---|---|
| 1 | `polars-core-0.55.2/src/chunked_array/ops/rolling_window.rs:11-24` | `pub struct RollingOptionsFixedWindow { pub window_size: usize, pub min_periods: usize, pub weights: Option<Vec<f64>>, pub center: bool, pub fn_params: Option<RollingFnParams> }` |
| 2 | `polars-core-0.55.2/src/chunked_array/ops/rolling_window.rs:79` | `impl<T: PolarsNumericType> ChunkRollApply for ChunkedArray<T> { fn rolling_map(&self, f: &dyn Fn(&Series) -> PolarsResult<Series>, mut options: RollingOptionsFixedWindow) -> PolarsResult<Series>` |
| 3 | `polars-core-0.55.2/src/series/implementations/floats.rs:149` (and `mod.rs:218`) | reference to `_options: RollingOptionsFixedWindow` only — no `rolling_max` / `rolling_min` member methods on `Float64Chunked` |
| 4 | `polars-compute-0.55.2/src/rolling/mod.rs:36-48` | `pub enum QuantileMethod { Nearest, Lower, Higher, Midpoint, Linear, Equiprobable }` — the only "rolling-style" enum at the top of polars-compute |
| 5 | `polars-compute-0.55.2/src/rolling/mod.rs:53-70` | `pub enum RollingFnParams { Quantile(...), Var(...), Rank {...}, Skew {...}, Kurtosis {...} }` — **no Max / Min variant** (so the `fn_params: Option<RollingFnParams>` field can't be used to dispatch to max/min; need separate free functions) |
| 6 | `polars-compute-0.55.2/src/rolling/no_nulls/min_max.rs` (macro-generated) | `pub fn rolling_max<T>(values: &[T], window_size: usize, min_periods: usize, center: bool, weights: Option<&[f64]>, _params: Option<RollingFnParams>) -> PolarsResult<ArrayRef>` |
| 7 | `polars-compute-0.55.2/src/rolling/nulls/min_max.rs:45-77` | `pub fn rolling_max<T>(arr: &PrimitiveArray<T>, window_size: usize, min_periods: usize, center: bool, weights: Option<&[f64]>, _params: Option<RollingFnParams>) -> ArrayRef` |
| 8 | `polars-core-0.55.2/src/series/implementations/floats.rs` (`impl Float64Chunked { ... }`) | (skipped — too long to quote; verified by grep that no `rolling_max`/`rolling_min`/`rolling_sum` member methods exist) |

## 8. Comparison with Path A-1 / `_support_resistance` (Spec 0001)

| Aspect | `_support_resistance` (Path A-1) | `_extreme_levels` (Path A-2) |
|---|---|---|
| Per-bar Python loop cost driver | chip decay array update (`chip[j] = chip[j] * (1-turnover) + vol`) | per-bar `max(highs[i-win..i+win+1])` slice allocation |
| Polars expression alternative | `cut` / `qcut` / `search_sorted` (rejected — see Spec 0001 research) | `rolling_max(center=true)` (rejected — see §3.2) |
| Output shape | per-row `chip_pct: List<Float64>` (1-row broadcast for final chips) | per-row `swing_high/low: Boolean` + `extreme_<w>_high/low: Float64` (1-row broadcast for window extremes) |
| PyO3 boundary cost | 1 (single DataFrame round-trip) | 1 (single DataFrame round-trip) |
| Adapter reduce site | `_reduce_to_poc_and_dense(chips, edges, close)` | `_reduce_extreme_levels(sh, sl, highs, lows, close, ext60h, ext60l, ext250h, ext250l)` |
| Algorithm 1:1 port? | yes, with `int()` truncation matching Python's float→int coercion | yes, with tie-aware `<=` matching Python's `== max(...)` |
| Memory peak (240-row) | `Vec<f64>(240 × 40) ≈ 76 KB` (chip array) | `Vec<Option<f64>>(240) × 2 ≈ 6 KB` (highs + lows) |

## 9. Reproducibility

```bash
cd $worktree                            # tick-stock-panel-rust-kernel
cargo check --example research_extreme_levels_api -p tf-rs-indicators
```

Exits 0. Output is deterministic. Run from any fresh checkout with Rust 1.98.1 + Cargo 1.98.1.