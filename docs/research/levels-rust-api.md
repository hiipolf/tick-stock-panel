# Levels `_support_resistance` - Polars 0.55.2 Rust API Survey

> Research note for **Spec 0001** (migrate `backend/app/indicators/levels.py::_support_resistance`
> to a Rust kernel at `crates/indicators/src/levels.rs`).
>
> Verdict: **Approach A** - `Float64Chunked::to_vec()` once per column, then plain Rust loop.
> No Polars expression / `cut` / `qcut` / `search_sorted` needed for this algorithm.
>
> Author: Ticket 01 (research). Date: 2026-10-18.

## 1. Background

The upstream Python algorithm lives at `backend/app/indicators/levels.py:62-165` and is the
"Volume Profile / 筹码分布" chip-distribution routine that underpins every
support/resistance line on the chart. It runs in two conceptually distinct steps:

- **(a) Per-row bin-index math** - for each K-line, compute
  `k_low  = floor((lows[i] - lo) / step)` and
  `k_high = floor((highs[i] - lo) / step)`, then `min(int, bins-1)` and `max(int, 0)` to clamp
  into `[0, bins-1]`. This is a stateless per-row transform - could in principle be expressed
  as a Polars expression.
- **(b) Sequential state update** - `chips[k] *= decay_ratio; for k in [k_low, k_high]:
  chips[k] += share`. Each row's update depends on the previous row's `chips` array.
  **This step cannot be vectorised by Polars** - there is no `cum_reduce` / `scan` / `map_batches`
  primitive in 0.55.2 that captures row-to-row state, so step (b) must remain a plain Rust
  `for i in 0..n` loop on `Vec<f64>`.

The final reduction (POC argmax + top-2 above-mean candidates) operates on a `Vec<f64>` of
length `bins` (default 40) and is trivially tiny - also plain Rust.

## 2. API survey

All three APIs live in `polars-ops` and are reached as free functions through the meta-crate.
Source citations are to the vendored 0.55.2 tree on disk; where the helper file lives in the
cargo registry cache (not vendored), the registry path is given.

### 2.1 `polars_ops::series::ops::cut::cut`

**Signature (cited from the vendored source):**

```rust
// vendor/polars-ops/src/series/ops/cut.rs:151-175
pub fn cut(
    s: &Series,
    mut breaks: Vec<f64>,
    labels: Option<Vec<PlSmallStr>>,
    left_closed: bool,
    include_breaks: bool,
) -> PolarsResult<Series> {
    polars_ensure!(!breaks.iter().any(|x| x.is_nan()), ComputeError: "breaks cannot be NaN");
    breaks.sort_unstable_by(|a, b| a.partial_cmp(b).unwrap());
    polars_ensure!(breaks.windows(2).all(|x| x[0] != x[1]), Duplicate: "breaks are not unique");
    if !breaks.is_empty() {
        polars_ensure!(breaks[0] > f64::NEG_INFINITY, ComputeError: "don't include -inf in breaks");
        polars_ensure!(breaks[breaks.len() - 1] < f64::INFINITY, ComputeError: "don't include inf in breaks");
    }
    let cut_labels = if let Some(l) = labels {
        polars_ensure!(l.len() == breaks.len() + 1, ShapeMismatch: "provide len(quantiles) + 1 labels");
        l
    } else {
        compute_labels(&breaks, left_closed)?
    };
    map_enum_cats(s, &cut_labels, &breaks, left_closed, include_breaks)
}
```

**What it does:** assigns every element of `s` to one of `len(breaks)+1` categorical buckets,
labelled `[lo, mid)`, `(mid, hi]`, etc. via `partition_point` over a sorted breaks vector.

**What it returns:** a `Series` whose dtype is either a `CategoricalChunked` (or, with
`include_breaks=true`, a `StructChunked { category, breakpoint }`). The integer bucket index
itself is *not* exposed - to recover it you'd have to walk the categorical's physical
representation or do a join against the labels.

**What it does NOT do that one might expect:**
  - It does not return the integer index directly. It returns string labels.
  - It does not operate per-row on two arrays (low, high) - only on a single column.
  - It silently casts to `Float64` first, so the cost is at least as much as `to_vec` + plain Rust.

**When you would use it:** bucketing a single column into labelled categories for an EDA or
`group_by` downstream. Not useful here because the algorithm needs the integer bucket index
AND needs to do `(low-bin, high-bin)` paired lookups AND a state-machine over those bins.

### 2.2 `polars_ops::series::ops::cut::qcut`

**Signature (cited from the vendored source):**

```rust
// vendor/polars-ops/src/series/ops/cut.rs:177-240
pub fn qcut(
    s: &Series,
    probs: Vec<f64>,
    labels: Option<Vec<PlSmallStr>>,
    left_closed: bool,
    allow_duplicates: bool,
    include_breaks: bool,
) -> PolarsResult<Series> {
    polars_ensure!(!probs.iter().any(|x| x.is_nan()), ComputeError: "quantiles cannot be NaN");
    let s = s.cast(&DataType::Float64)?;
    let s2 = s.sort(SortOptions::default())?;
    let ca = s2.f64()?;
    let ca = ca.set(&ca.is_nan(), None)?;
    if ca.null_count() == ca.len() {
        /* all-null shortcut */
    }
    let mut qbreaks: Vec<_> = ca
        .quantiles(&probs, QuantileMethod::Linear)?
        .into_iter()
        .map(|opt| opt.unwrap())
        .collect();
    /* NaN-breakpoint guard, sort, dedup */
    let cut_labels = if let Some(l) = labels {
        polars_ensure!(l.len() == qbreaks.len() + 1, ShapeMismatch: "provide len(quantiles) + 1 labels");
        l
    } else {
        compute_labels(&qbreaks, left_closed)?
    };
    map_categorical_cats(&s, &cut_labels, &qbreaks, left_closed, include_breaks)
}
```

**What it does:** same shape as `cut`, but the breaks are derived from the *quantiles* of the
input column (e.g. `probs = [0.25, 0.5, 0.75]` -> quartiles).

**Returns:** identical dtype story to `cut` (Categorical or Struct).

**What it does NOT do:** quantiles need an O(n log n) sort of the column. It always casts to
Float64, sorts, and runs the quantile routine. For uniform-spaced bins (our case), `cut` with
precomputed breaks is cheaper.

**When you would use it:** when you want equal-population bins (e.g. deciles for an ML
preprocessing step). Not applicable to `_support_resistance`, which needs **uniform-spaced**
price bins over a fixed `[lo, hi]` interval.

### 2.3 `polars_ops::series::ops::search_sorted::search_sorted` + `SearchSortedSide`

**Signature (cited from the vendored source):**

```rust
// vendor/polars-ops/src/series/ops/search_sorted.rs:6-11
pub fn search_sorted(
    s: &Series,
    search_values: &Series,
    side: SearchSortedSide,
    descending: bool,
) -> PolarsResult<IdxCa> {
```

**`SearchSortedSide` enum (cargo registry cache, not vendored):**

```rust
// C:/Users/admin/.cargo/registry/src/index.crates.io-1949cf8c6b5b557f/polars-core-0.55.2/src/chunked_array/ops/search_sorted.rs:8-16
#[derive(Copy, Clone, Debug, Hash, Eq, PartialEq, Default)]
pub enum SearchSortedSide {
    #[default]
    Any,
    Left,
    Right,
}
```

**What it does:** given a sorted Series `s`, returns the position at which each element of
`search_values` would be inserted to keep `s` sorted. Internally `lower_bound_chunks` does a
binary search (`vendor/polars-ops/src/series/ops/search_sorted.rs:38-40`, called from
`polars-core/src/chunked_array/ops/search_sorted.rs:135-235` `binary_search_ca`).

**What it returns:** `IdxCa` (UInt32 chunked array) of positions. Each element is `Found` if
the search value is present in `s` (returned position matches), or `NotFound`
(returned position is where it would go). The enum is `Any` / `Left` / `Right` to control
how ties on existing elements are broken - `Any` is the default, `Left` picks the first equal,
`Right` the last equal. All three return *a* position; the function never errors on a
not-found match.

**What it does NOT do that one might expect:**
  - It does **not** return `Option<usize>`. There is no `Result::Err` for "not found". Both
    found and not-found return a `IdxSize`; you must check `app == log2(&v)` (or compare
    against `s[app]` for `Found` vs `NotFound`) yourself.
  - It does **not** honour a `descending` sorted array automatically - pass `descending: true`
    explicitly.
  - Both `s` and `search_values` must have the same dtype (see the
    `polars_ensure!(original_dtype == search_values.dtype(), op = "search_sorted", ...)`
    guard in `vendor/polars-ops/src/series/ops/search_sorted.rs:13-18`).

**When you would use it:** finding the join key position for a range join, or recovering the
`cut`-equivalent integer bucket index when you already have a sorted break-edge array.

## 3. Per-row bin-index in plain Rust

The kernel does not need any of the three APIs above. The bin-index math reduces to two
scalar ops per row; vectorising it through Polars expressions (`col("low").sub(lit(lo))
.div(lit(step))`) buys nothing because the next step (b) needs a per-row loop anyway.

**Snippet (the same `bin_index` helper used in `research_levels_api.rs`):**

```rust
// crates/indicators/src/levels.rs (sketch - Ticket 02 will fill in)
use polars::prelude::*;

#[inline]
fn bin_index(price: f64, lo: f64, step: f64, bins: usize) -> usize {
    let raw = ((price - lo) / step).floor() as i64;
    raw.clamp(0, (bins as i64) - 1) as usize
}

// Per-row bin pair - identical to Python `levels.py:127-128`:
let k_low  = bin_index(lows_v[i].unwrap_or(lo),  lo, step, bins);
let k_high = bin_index(highs_v[i].unwrap_or(lo), lo, step, bins);
let (k_low, k_high) = if k_low > k_high { (k_high, k_low) } else { (k_low, k_high) };
```

**Why not a Polars expression?** Three reasons, in increasing importance:

1. **Cost.** `df.lazy().select([col("low").sub(lo_lit).div(step_lit).cast(DataType::UInt32)])`
   still has to allocate two intermediate `Series` (`low - lo` and `(...).div(step)`) and a
   `UInt32` cast result, each backed by one or more Arrow buffers. The plain Rust version
   operates on a single contiguous `Vec<f64>` and writes to `Vec<u32>` - strictly less
   allocator pressure for the same number of FLOPs.
2. **Reuse.** `lows`, `highs`, `volumes`, `turnovers` are *all* needed in the inner loop
   (step b reads `chips`, `turnovers[i]`, `volumes[i]`, `lows[i]`, `highs[i]`). Lifting them
   once via `to_vec()` and looping in Rust avoids 4 separate `lazy().collect()` round-trips.
3. **Branch-only fallback.** The Python code already does `df["high"].to_list()` (i.e. eager
   Python-side lift) on lines 107-109. The Rust port doing the same lift (`to_vec()`) is the
   literal 1:1 translation, and lets the parity test compare apples to apples.

## 4. Feature flag check

Workspace currently enables `polars = { features = ["lazy", "parquet", "csv"] }`
(`Cargo.toml:23`). What does that resolve to for the three APIs?

| API | reachable today? | feature needed | source line |
|-----|------------------|----------------|-------------|
| `polars_ops::series::ops::cut::cut` | **no** | `polars-ops/cutqcut` | `polars-ops-0.55.2/src/series/ops/mod.rs:24-26` (`#[cfg(feature = "cutqcut")]`) |
| `polars_ops::series::ops::cut::qcut` | **no** | same as above | same source line |
| `polars_ops::series::ops::search_sorted::search_sorted` | **yes** | `polars-ops/search_sorted` (transitively on via vendored polars-stream) | `polars-ops-0.55.2/src/series/ops/mod.rs:27-29`; `cargo tree -e features` confirms `polars-ops feature "search_sorted"` is enabled by `polars-stream v0.55.2`. |
| `polars_core::chunked_array::ops::search_sorted::SearchSortedSide` | **yes** | none (always re-exported) | `polars-ops-0.55.2/src/series/ops/mod.rs:30` `pub use polars_core::chunked_array::ops::search_sorted::SearchSortedSide;` |

Why `cut`/`qcut` are off today: the meta `polars` crate has
`cutqcut = ["polars-lazy?/cutqcut"]` (`polars-0.55.2/Cargo.toml:141`), and `polars-lazy`'s
default feature set enables `polars-ops` with `chunked_ids` only - *not* `cutqcut`.
`polars-ops/cutqcut = ["dtype-categorical", "dtype-struct"]` (`polars-ops-0.55.2/Cargo.toml:54-57`),
so flipping it on would pull in `dtype-categorical` (categorical dtypes) and `dtype-struct`
(struct dtypes) into the workspace build. Those are fine to have, but the kernel does not
need them - see the Recommendation below.

Why `search_sorted` is on by accident: the vendored `polars-stream` crate depends on
`polars-ops`, and `polars-ops'search_sorted = []` is a "marker" feature that `polars-stream`
activates. The `cargo tree -e features` output (verbatim, 2026-10-18) confirms:

```
polars-ops feature "search_sorted"
`-- polars-stream v0.55.2 (vendor/polars-stream)
```

## 5. Recommendation

**Use Approach A**: `Float64Chunked::to_vec()` once per column (cheap - single Arrow-buffer
copy), then a plain Rust `for i in 0..n` loop on `Vec<Option<f64>>` that handles **both**
step (a) and step (b). The final reduction (POC + top-2) is also plain Rust on a `Vec<f64>` of
length `bins`.

**Why A and not the others:**

- **A (chosen).** Step (b) is irreducibly a stateful Rust loop. Hoisting step (a) into Polars
  expressions does not eliminate the loop - it just adds four `lazy().collect()` round-trips
  and the dtype-coercion / null-mask / Arrow-buffer overhead that comes with them. `to_vec()`
  is the minimum-overhead lift that gets the data into a Rust-native layout.
- **B (mixed).** Strictly worse than A. The "mix" buys you zero extra vectorisation (the bin
  math is already trivial scalar arithmetic) and forces you to maintain two memory layouts
  for the same data.
- **C (all-Polars).** Wrong. `df.lazy().select(...).collect()` is fundamentally a batch
  transform with no row-to-row dependency. There is no `cum_scan` / `reduce_with_state`
  expression in 0.55.2 that captures the chip-decay update.
- **D (use `cut`).** Wrong. `cut` returns a *Categorical* of string labels (`[10.0, 12.0)`,
  `[12.0, 14.0)`, ...), not the integer bucket indices the inner loop needs. To recover the
  indices you'd then need to do a second pass to map labels -> `u32`. That is strictly more
  work than the plain-Rust `((price - lo) / step).floor() as i64` clamp, and it would also
  force enabling `polars-ops/cutqcut` (see section 4).

**Constraint bonus.** Approach A does not require any new `polars` feature flag, which means
no `Cargo.toml` change in Ticket 02. (`search_sorted` is reachable but we don't need it;
`cut` / `qcut` are not reachable without a feature flip we don't need.)

## 6. Gotchas

### 6.1 `int()` floor for negative numbers

Python `int()` *truncates toward zero* (e.g. `int(-0.5) == 0`), not floor. Our inner
`bin_index` uses `.floor()` which goes the other way (`floor(-0.5) == -1`). For the chip-
distribution algorithm this does not matter, because the next operation is `.clamp(0, bins-1)`,
which catches both cases. Verified by reading `levels.py:127-128` plus the Python interpreter
semantics. Empirical confirmation in `research_levels_api.rs` row 2 (high=11.29, low=10.04 -
both positive, so no edge case triggered there). To exercise a negative case manually, set
`lows[i] < lo` in the fixture.

### 6.2 `search_sorted` side enum (for completeness - not used by the kernel)

`SearchSortedSide::{Any, Left, Right}` controls how ties on existing break values resolve:
`Any` is "either is fine" (default), `Left` returns the first equal position, `Right`
returns the last. For our `(lo, hi]` style bucketing, `Right` would be the right choice if
we ever needed `search_sorted` here, because we want `break_edge == value` to land in the
lower-indexed bin only when the next break is strictly greater. Source:
`vendor/polars-ops/src/series/ops/search_sorted.rs:9` (parameter) plus
`polars-core-0.55.2/src/chunked_array/ops/search_sorted.rs:148-160` (which side wins on
null positions) and lines 164-234 (which side wins on equality).

### 6.3 `cut` dtype coercion

`cut` casts the input Series to `Float64` unconditionally (`map_enum_cats` calls `s.cast(&DataType::Float64)?`
on `vendor/polars-ops/src/series/ops/cut.rs:15`, and `qcut` does the same on `cut.rs:187`).
For our `Float64` input this is a no-op, but it's not free for an `Int64` input. Doesn't
apply to the kernel - just noting why we don't use it.

### 6.4 Null / NaN handling in `to_vec()`

`Float64Chunked::to_vec()` (defined at `polars-core-0.55.2/src/chunked_array/to_vec.rs:7`)
returns `Vec<Option<f64>>`, preserving the validity bitmap. If we *know* the column has no
nulls, `to_vec_null_aware()` returns `Either<Vec<f64>, Vec<Option<f64>>>` so we can skip the
null-unwrapping branch. For the chip-distribution fixture, OHLCV columns are non-null by
construction (Polars `DataFrame::new` panics if you hand it mismatched lengths, and the chart
pipeline strips nulls upstream), so the example uses the simpler `to_vec()` + `.unwrap_or(0.0)`.
The Python original uses the same `or 0` fallback (`levels.py:115`, `levels.py:125`).

### 6.5 `Float64Chunked` indexing overhead vs raw `&[f64]`

`Float64Chunked::get(i)` goes through `downcast_iter` + Arrow `Array::value_unchecked` + null
check (see `polars-core-0.55.2/src/chunked_array/iterator/mod.rs:11-19` for the iterator path).
After `to_vec()` you're indexing a plain `Vec<Option<f64>>` which is a bounds-checked slice
index - same cost in practice, but no virtual call into Arrow. For the chip loop (n=240 on a
single symbol; up to n x 5,400 on a universe refresh), the cost difference is in the noise.
`to_vec()` is a single Arrow -> `Vec` memcpy, not a per-element copy, so it's a one-shot cost.

## 7. Reproduction

The throwaway example lives at `crates/indicators/examples/research_levels_api.rs` and
compiles + runs end-to-end with the workspace's current feature set (no `Cargo.toml` change
needed).

**Compile:**

```
$ cargo check --example research_levels_api -p tf-rs-indicators
    Checking tf-rs-indicators v0.0.0 (...tick-stock-panel-rust-kernel\crates\indicators)
    Finished `dev` profile [unoptimized + debuginfo] target(s) in 6.04s
```

**Run:**

```
$ cargo run --example research_levels_api -p tf-rs-indicators
== Input fixture (n=30, bins=5, step=2.00, hi-lo=[10.00, 20.00]) ==
first 5 rows of (high, low, vol, turnover):
  [ 0] h=11.015 l=10.000 v=4.289e6 t=0.299
  [ 1] h=11.113 l=10.000 v=6.498e5 t=2.244
  [ 2] h=11.290 l=10.043 v=1.282e6 t=1.507
  [ 3] h=12.160 l=10.000 v=2.399e5 t=0.023
  [ 4] h=12.434 l=10.233 v=4.780e6 t=0.836

== Bin indices for first 5 rows ==
  [ 0] k_low=0 k_high=0  price=[10.00, 11.01] -> bin [0, 0]
  [ 1] k_low=0 k_high=0  price=[10.00, 11.11] -> bin [0, 0]
  [ 2] k_low=0 k_high=0  price=[10.04, 11.29] -> bin [0, 0]
  [ 3] k_low=0 k_high=1  price=[10.00, 12.16] -> bin [0, 1]
  [ 4] k_low=0 k_high=1  price=[10.23, 12.43] -> bin [0, 1]

== Final chips array (after 30 rows of decay+distribute) ==
  bin 0 [10.00, 12.00)  chips =     1.026147e7
  bin 1 [12.00, 14.00)  chips =     1.302786e7
  bin 2 [14.00, 16.00)  chips =     1.162522e7
  bin 3 [16.00, 18.00)  chips =     1.656343e7
  bin 4 [18.00, 20.00)  chips =     1.391004e7
  total chips across bins = 6.538801e7

== Reduction (POC + top-2 dense zones) ==
  POC   bin 3  mid=17.000  chips=1.656343e7
  Dense bin 4  mid=19.000  chips=1.391004e7
```

### Compile constraint (cdylib-only)

`crates/indicators/Cargo.toml:15` declares `crate-type = ["cdylib"]` only (no `rlib`).
Per Cargo's standard behaviour, an `examples/` file is compiled as a standalone `bin`-style
target and does NOT need to `use tf_rs_indicators::*` - the workspace `polars` dependency is
reachable directly. So `cargo run --example research_levels_api -p tf-rs-indicators` works
as-is and the example makes zero calls into the indicator library. Verified with the compile
output above.

If a future example *did* need to call into `tf_rs_indicators::sum_close` /
`tf_rs_indicators::support_resistance`, the workaround would be either:
  (a) add `rlib` to the crate-type list (`crate-type = ["cdylib", "rlib"]`) - Ticket 02 may
      decide this anyway if more examples land; or
  (b) call the wheel directly via `pyo3` from the example (heavyweight).

Neither is needed for Ticket 01 / Approach A.

## 8. Open questions for the implementer (Ticket 02)

- **Parity test fixture.** Spec 0001 mentions a frozen 240-day parquet fixture. The 30-row
  fixture here is for fast iteration; the parity-test fixture will need to be regenerated
  by running the current Python reference once and committing the result.
- **Null `turnover_rate`.** Python does `[0.0] * n` fallback at line 105. The Rust port should
  do the same - `turns_v.get(i).copied().unwrap_or(Some(0.0))` then `unwrap_or(0.0)` again, or
  pre-build a `vec![Some(0.0); n]` when the column is absent.
- **Float precision.** Python `int()` and Rust `as i64` can disagree at the boundary of `i64::MAX`
  (irrelevant for prices in `[10.0, 20.0]`), and Rust `f64` arithmetic is otherwise identical to
  Python's IEEE-754 double. No drift expected for valid OHLCV ranges, but the parity test will
  catch any surprises.
