# Research — Market Overview Path A-3 Rust API survey

> Path A-3 / Ticket 01 — `services.market_overview_builder._dimension_rank` + 2 helpers
> (`_top_rows`, `_pct_band_rows`) Rust migration.
> Date: 2026-10-19.
> Vendored versions surveyed: `pyo3 0.29.3`, `pyo3-polars 0.28.0`, `polars 0.55.2`.
> Approach: pure-Rust kernel on `Vec<HashMap<String, PyObject>>` pre-aggregated by adapter.
> Companion throwaway: `crates/indicators/examples/research_market_overview_api.rs`.

## TL;DR — Approach A (pure-Rust loop on pre-aggregated `Vec<HashMap<String, PyObject>>`)

- **`top_rows_kernel(rows, key, descending, limit)`** — `Vec<HashMap<String, PyObject>>` in,
  filter + sort on `key` + slice → `PyDataFrame`. Adapter `df.iter_rows(named=True)` →
  `list[dict]`. Mirrors Path A-1/2's pattern (Rust compute on DataFrame).
- **`pct_band_rows_kernel(values)`** — `Vec<f64>` in, 8-bucket count → `PyDataFrame` (3 columns).
  Trivial signature.
- **`dimension_rank_kernel(groups, limit)`** — `Vec<(String, Vec<HashMap<String, PyObject>>)>` in,
  per-group reduce → `PyDataFrame` (9 columns). Adapter slices leading/lagging in Python.

No Polars expressions in any hot path. All 3 kernels loop on Rust `Vec`/`HashMap` and
construct the output `DataFrame` by column. pyo3-polars' `PyDataFrame` round-trip is the
sole bridge (kernel out → Python `polars.DataFrame`).

No `#[pyclass]` derive needed — `PyObject` keeps the adapter side Python-idiomatic (`dict`
with string keys, any inner value type) without forcing a Rust-side struct definition.

## Cited PyO3 0.29.3 / pyo3-polars 0.28.0 evidence

### 1. `Vec<T>` extraction works for any `T: FromPyObjectOwned`

`pyo3-0.29.3\src\conversions\std\vec.rs:56`:

```rust
impl<'py, T> FromPyObject<'_, 'py> for Vec<T>
where
    T: FromPyObjectOwned<'py>,
{
    type Error = PyErr;

    fn extract(obj: Borrowed<'_, 'py, PyAny>) -> PyResult<Self> {
        if let Some(extractor) = T::sequence_extractor(obj, crate::conversion::private::Token) {
            return Ok(extractor.to_vec());
        }
        if obj.is_instance_of::<PyString>() {
            return Err(PyTypeError::new_err("Can't extract `str` to `Vec`"));
        }
        extract_sequence(obj)
    }
}

fn extract_sequence<'py, T>(obj: Borrowed<'_, 'py, PyAny>) -> PyResult<Vec<T>>
where
    T: FromPyObjectOwned<'py>,
{
    if unsafe { ffi::PySequence_Check(obj.as_ptr()) } == 0 {
        return Err(CastError::new(obj, PySequence::type_object(obj.py()).into_any()).into());
    }
    let mut v = Vec::with_capacity(obj.len().unwrap_or(0));
    for item in obj.try_iter()? {
        v.push(item?.extract::<T>().map_err(Into::into)?);
    }
    Ok(v)
}
```

This means `Vec<HashMap<String, PyObject>>`, `Vec<(String, Vec<HashMap<String, PyObject>>)>`
all extract cleanly when each layer implements `FromPyObject`.

### 2. `HashMap<K, V>` extraction (any key + value with `FromPyObjectOwned`)

`pyo3-0.29.3\src\conversions\std\map.rs:106`:

```rust
impl<'py, K, V, S> FromPyObject<'_, 'py> for collections::HashMap<K, V, S>
where
    K: FromPyObjectOwned<'py> + cmp::Eq + hash::Hash,
    V: FromPyObjectOwned<'py>,
    S: hash::BuildHasher + Default,
{
    type Error = PyErr;

    fn extract(ob: Borrowed<'_, 'py, PyAny>) -> Result<Self, PyErr> {
        let dict = ob.cast::<PyDict>()?;
        let mut ret = collections::HashMap::with_capacity_and_hasher(dict.len(), S::default());
        for (k, v) in dict.iter() {
            ret.insert(
                k.extract().map_err(Into::into)?,
                v.extract().map_err(Into::into)?,
            );
        }
        Ok(ret)
    }
}
```

`HashMap<String, PyObject>` works: `String: FromPyObjectOwned` ✓, `PyObject` is the
universal Python-object holder (also `FromPyObjectOwned`).

### 3. Tuple variadic extract (up to 12 elements)

`pyo3-0.29.3\src\types\tuple.rs:924`:

```rust
impl<'a, 'py, $($T: FromPyObject<'a, 'py>),+> FromPyObject<'a, 'py> for ($($T,)+) {
    type Error = PyErr;

    fn extract(obj: Borrowed<'a, 'py, PyAny>) -> Result<Self, Self::Error>
    {
        let t = obj.cast::<PyTuple>()?;
        if t.len() == $length {
                Ok(($(
                    unsafe { t.get_borrowed_item_unchecked($n) }
                        .extract::<$T>()
                        .map_err(Into::into)?,
                )+))
        } else {
            Err(wrong_tuple_length(t, $length))
        }
    }
}

tuple_conversion!(1, (0, T0));
tuple_conversion!(2, (0, T0), (1, T1));
...
```

Variadic tuple in `extract_sequence` (vec.rs:151) confirmed to flatten `(1, 2, 3)` Python
tuples into `Vec<i32>`, validating per-element extract semantics. For 2-tuples
`(String, Vec<HashMap<String, PyObject>>)` the same per-position `.extract()` works.

### 4. `Option<T>` extraction — `None` round-trip

`pyo3-0.29.3\src\conversions\std\option.rs:50`:

```rust
impl<'a, 'py, T> FromPyObject<'a, 'py> for Option<T>
where
    T: FromPyObject<'a, 'py>,
{
    type Error = T::Error;

    fn extract(obj: Borrowed<'a, 'py, PyAny>) -> Result<Self, Self::Error> {
        if obj.is_none() {
            Ok(None)
        } else {
            obj.extract().map(Some)
        }
    }
}
```

This is the canonical `None → None, Some(x) → T::extract` behaviour. Any
`HashMap<String, Option<f64>>` field returns `None` when Python's `dict['change_pct'] is None`.

> **Why we don't need `Option<f64>` semantics in `top_rows_kernel`**: the `PyObject`
> variant carries the optional-ness implicitly. We `obj.extract::<f64>()` inside the
> kernel and treat the `Err` (which only fires for `None`) as "missing value" filter.
> Equivalent to Python `_finite` helper.

### 5. `pyo3-polars` `PyDataFrame` round-trip (kernel out → Python DataFrame)

`pyo3-polars-0.28.0\src\types.rs:254`:

```rust
impl<'py> IntoPyObject<'py> for PyDataFrame {
    type Target = PyAny;
    type Output = Bound<'py, Self::Target>;
    type Error = PyErr;

    fn into_pyobject(self, py: Python<'py>) -> Result<Self::Output, Self::Error> {
        let pyseries = self
            .0
            .columns()
            .iter()
            .map(|s| PySeries(s.as_materialized_series().clone()).into_pyobject(py))
            .collect::<PyResult<Vec<_>>>()?;

        let polars = POLARS.bind(py);
        polars.call_method1("DataFrame", (pyseries,))
    }
}
```

Kernel returns `PyDataFrame(df)` → Python side gets a real `polars.DataFrame` with the
correct dtype for each column. From Python, `df.iter_rows(named=True)` yields
`list[dict[str, Any]]`, matching the Spec 0003 adapter pattern verbatim:

```python
out_df = top_rows_kernel(rows, key, descending, limit)  # polars.DataFrame
return list(out_df.iter_rows(named=True))                # list[dict]
```

`iter_rows` is the **Polars Python** method (not pyo3-polars — pyo3-polars has no
`iter_rows` in its source tree; vendored `pyo3-polars-0.28.0\src\` shows
only `alloc.rs / derive.rs / error.rs / export.rs / lib.rs / types.rs`). `iter_rows`
lives in the upstream Polars Python package, called at runtime by the adapter after
the kernel returns.

## Decision matrix

| Signature choice | Pros | Cons | Verdict |
|---|---|---|---|
| `Vec<(String, Vec<StockFields>)>` with `#[pyclass] StockFields` | Strongly typed | `#[pyclass]` derive + per-field `#[pyo3(get)]` boilerplate; changes per Spec revision; harder to evolve | ❌ |
| `Vec<(String, Vec<HashMap<String, PyObject>>)>` | No struct; adapter is Python dict (matches current `_py_dimension_rank` which already passes dicts); `PyObject` retains `None`-or-`f64` semantics; future-proof to add fields without Rust struct change | Inner `PyObject` extraction needs `Bound` API inside kernel; one extra `.extract()` per field | ✅ **CHOSEN** |
| Round-trip via `PyDataFrame` for input too | Adapter sends DataFrame; Rust uses Polars expressions | Heavy; defeats seam's "Python pre-aggregates, Rust pure-compute" intent; Spec 0003 §"Approach A" verdict | ❌ |

Spec 0003 §"Service-level seam contract" §"dimension_rank_kernel signature" specifies
`Vec<(String, Vec<StockFields>)>`. **T01 finds this is unnecessarily restrictive for
PyO3 0.29.3** — `#[pyclass]` derive is workable but adds boilerplate without semantic
gain. **T01 recommendation: amend spec signature to `Vec<(String, Vec<HashMap<String, PyObject>>)>`
during Ticket 02** (kernel). The adapter-side Python shape (`groups: list[tuple[str, list[dict]]]`)
is unchanged — only the Rust function parameter type changes. PyO3 0.29.3's universal
extract handles both transparently from Python's perspective.

## Approach A vs B vs D verdict

Per Spec 0003 §"Implementation Decisions" Approach A vs B vs D:

- **Approach A (pure-Rust kernel on `Vec<HashMap>`, output as `PyDataFrame`)** —
  **VERDICT: SHIP IT.** All 3 kernels are 15-60 lines of pure compute; DataFrame
  construction is a column-append at the kernel tail; no Polars expressions in hot
  path. Mirrors Path A-1/2's `levels.rs` + `extreme_levels.rs` pattern.
- **Approach B (Polars expressions on a per-group DataFrame)** — would require
  the adapter to construct a per-group DataFrame with stable column names, defeating
  the "Python owns schema, kernel pure-compute" intent of the seam.
- **Approach D (kernel takes the raw `repo.store.data_dir`, reads parquet/yaml itself)** —
  violates ADR-0006 §"kernel 不读 filesystem" — kernels are pure-compute, adapter
  owns IO. **Explicitly rejected.**

## What T01 does NOT cover

- The `_dimension_rank` fixture parity data — that's Ticket 03 (frozen `data_dir`
  parquet + yaml + ext subset).
- Bit-identical `_leader_sort_key` port — Ticket 02 (kernel).
- 8-bucket pct_band_rows boundary inclusive/exclusive — Ticket 02 (kernel).
- Adapter ↔ kernel naming convention (Python `_py_*` rename) — Ticket 04.

T01 deliverable is the API survey + a stub kernel that cargo-checks clean against the
vendored deps. Behaviour parity is downstream.

## Companion throwaway

`crates/indicators/examples/research_market_overview_api.rs` (this commit) runs
3 stub kernels over a 12-row synthetic fixture (3 sectors × 4 stocks with mixed
`change_pct` / `amount` / None values), constructs the resulting `DataFrame`s
purely in Rust (no Python), and prints the column vectors. Verifies:

1. PyO3 0.29.3 `FromPyObject` signature compiles for `Vec<HashMap<String, PyObject>>`.
3. Pure-Rust loop on `Vec<(String, Vec<HashMap>)>` produces the expected
   per-group reduce (count, avg_pct, up_count, down_count, amount).
4. `polars::DataFrame` column-construction from `Vec<f64>` + `Vec<u32>` +
   `Vec<Option<String>>` works without Polars expressions in the hot path.

Run:
```
cargo check --example research_market_overview_api -p tf-rs-indicators
cargo run   --example research_market_overview_api -p tf-rs-indicators
```