"""End-to-end seam test for `_support_resistance` (Spec 0001 Ticket 03).

Verifies the thin Python adapter (`backend/app/indicators/levels.py::_support_resistance`):

* Rust kernel path: bit-identical output to the pure-Python reference on the
  frozen 240-day fixture (`backend/tests/fixtures/levels_240d.parquet` +
  `levels_240d.expected.json`).
* Pure-Python fallback: when `tf_rs_indicators` is unavailable (CI without
  Rust toolchain), the adapter still returns dicts via `_py_support_resistance`.
* Graceful no-op: empty df / no `volume` / `df.height < 20` / `hi <= lo` all
  return `[]` without raising.
* Dtype preservation: when the kernel runs, the returned DataFrame keeps all
  input columns plus a single new `chip_pct` column.

Per ADR-0002 §5 (try-import fallback): when the wheel is **not** built
(e.g. CI without Rust toolchain), the Rust-kernel-specific tests are
**skipped** rather than failed. The pure-Python fallback test still runs
because it doesn't depend on the wheel.

Run with:
    uv run -m pytest backend/tests/test_rs_levels_seam.py -v

Companion Rust unit tests live at `crates/indicators/src/levels.rs::tests`.
The 240-day fixture was captured once via `scripts/gen_levels_240d_fixture.py`
and frozen into git; do not regenerate casually.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import polars as pl
import pytest

# --- skipif (mirror test_rs_indicators_seam.py pattern) ---
try:
    import tf_rs_indicators as _tfr_seam  # type: ignore[import-not-found]
    _TFR_SEAM_AVAILABLE = True
except ImportError:
    _tfr_seam = None
    _TFR_SEAM_AVAILABLE = False

_seam_unavailable = not _TFR_SEAM_AVAILABLE

# Tests that exercise the Rust kernel directly are gated. Tests that exercise
# the adapter's Python fallback (via forced ImportError) always run.
pytestmark_rust_only = pytest.mark.skipif(
    _seam_unavailable,
    reason="tf_rs_indicators wheel not installed (try-import fallback active)",
)


def _require_seam():
    assert _tfr_seam is not None, "pytest.mark.skipif should have skipped"
    return _tfr_seam


# Lazy imports of the adapter + reference so module-level `import` doesn't
# fail when polars / polars setup is missing.
def _load_levels_module():
    """Import the adapter module. Add backend/ to sys.path on first call."""
    backend_dir = Path(__file__).resolve().parent.parent
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))
    from app.indicators import levels  # type: ignore[import-not-found]
    return levels


# --- fixture paths ---
FIX_DIR = Path(__file__).resolve().parent / "fixtures"
PARQUET = FIX_DIR / "levels_240d.parquet"
JSON_EXPECTED = FIX_DIR / "levels_240d.expected.json"


def _load_fixture():
    df = pl.read_parquet(PARQUET)
    expected = json.loads(JSON_EXPECTED.read_text(encoding="utf-8"))
    return df, expected


# =====================================================================
# 1) Rust kernel output == Python reference on 240-day fixture (parity)
# =====================================================================

@pytestmark_rust_only
def test_kernel_matches_python_reference_on_240d_fixture():
    """The Rust kernel, on the frozen 240-day A-share fixture, must produce
    output bit-identical to the pure-Python reference. The expected output
    was captured once via the reference; this test is the spec's "parity
    on a 240-day fixture" gate (Spec 0001 §'Seams')."""
    seam = _require_seam()
    levels = _load_levels_module()
    df, expected = _load_fixture()

    # The adapter should pick the Rust path (wheel is available).
    result = levels._support_resistance(df, 40)

    assert result == expected, (
        f"Rust-via-adapter output diverges from the Python reference.\n"
        f"  expected: {expected}\n  got: {result}"
    )

    # Sanity: the underlying kernel call directly also returns the same
    # intermediate DataFrame. Verifies the kernel contract.
    out_df = seam.support_resistance(df, 40)
    assert "chip_pct" in out_df.columns
    assert out_df.height == df.height


# =====================================================================
# 2) Pure-Python fallback when the wheel is missing
# =====================================================================

def test_python_fallback_when_wheel_missing(monkeypatch):
    """Force `ImportError` on `tf_rs_indicators`; the adapter must fall back
    to `_py_support_resistance` and still return the same dicts as the
    reference call. This is the spec's "try-import default, opt-in via
    `uv sync --extra rs`" gate (ADR-0002 §5)."""
    import builtins
    real_import = builtins.__import__
    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "tf_rs_indicators" or name.startswith("tf_rs_indicators."):
            raise ImportError(
                f"No module named '{name}' (test mock — fallback verification)"
            )
        return real_import(name, globals, locals, fromlist, level)
    monkeypatch.setattr(builtins, "__import__", fake_import)

    levels = _load_levels_module()
    df, expected = _load_fixture()

    # The adapter now routes to the pure-Python reference (wheel is masked
    # by the `__import__` mock). The meaningful assertions are:
    #  (a) the adapter result matches the frozen 240-day reference — this
    #      is the actual "fallback path produces the same dicts" gate
    # (b) the reference itself, called directly, still produces the frozen
    #      output — a smoke test against drift in `_py_support_resistance`
    # The previous form `result == direct == expected` was tautological
    # because under the mock `result` IS `direct`; (a) and (b) are independent.
    result = levels._support_resistance(df, 40)
    direct = levels._py_support_resistance(df, 40)
    assert result == expected, (
        f"Fallback (adapter) result diverges from the frozen 240-day reference: "
        f"{result} vs {expected}"
    )
    assert direct == expected, (
        f"Reference itself drifted from frozen 240-day output: "
        f"{direct} vs {expected}"
    )


# =====================================================================
# 3) Graceful no-op on invalid / empty input
# =====================================================================

def test_graceful_no_op_for_empty_or_invalid_input():
    """All four no-op triggers from the spec must return `[]` without
    raising. The triggers are tested via the adapter (which dispatches to
    either kernel or reference, depending on wheel availability) and also
    via the reference directly. Both must agree on the contract."""
    levels = _load_levels_module()
    seam = _tfr_seam  # may be None

    # 3a) empty df
    empty = pl.DataFrame({
        "open": pl.Series([], dtype=pl.Float64),
        "high": pl.Series([], dtype=pl.Float64),
        "low": pl.Series([], dtype=pl.Float64),
        "close": pl.Series([], dtype=pl.Float64),
        "volume": pl.Series([], dtype=pl.Float64),
    })
    assert levels._support_resistance(empty, 40) == []
    assert levels._py_support_resistance(empty, 40) == []

    # 3b) no `volume` column
    no_vol = pl.DataFrame({
        "open": [10.0] * 30, "high": [12.0] * 30, "low": [10.0] * 30,
        "close": [11.0] * 30,
    })
    assert levels._support_resistance(no_vol, 40) == []
    assert levels._py_support_resistance(no_vol, 40) == []

    # 3c) df.height < 20 (5 rows is enough to fail the threshold)
    short = pl.DataFrame({
        "open": [10.0] * 5, "high": [12.0] * 5, "low": [10.0] * 5,
        "close": [11.0] * 5, "volume": [100.0] * 5, "turnover_rate": [1.0] * 5,
    })
    assert levels._support_resistance(short, 40) == []
    assert levels._py_support_resistance(short, 40) == []

    # 3d) hi <= lo (all rows identical high == low, so hi == lo, fails `hi > lo > 0`)
    flat = pl.DataFrame({
        "open":  [11.0] * 30, "high": [11.0] * 30, "low": [11.0] * 30,
        "close": [11.0] * 30, "volume": [100.0] * 30, "turnover_rate": [1.0] * 30,
    })
    assert levels._support_resistance(flat, 40) == []
    assert levels._py_support_resistance(flat, 40) == []


# =====================================================================
# 4) Dtype preservation: kernel's returned DataFrame keeps input columns
# =====================================================================

@pytestmark_rust_only
def test_dtype_preservation_in_returned_dataframe():
    """The Rust kernel returns a DataFrame with all input columns preserved
    plus a single new `chip_pct: List<Float64>` column. No input column
    values are mutated, no extra columns are introduced beyond `chip_pct`."""
    seam = _require_seam()
    df, _ = _load_fixture()

    out_df = seam.support_resistance(df, 40)

    # All input columns preserved (set equality ignores order; here we want
    # strict equality: every original column is present, and `chip_pct` is
    # the only new one).
    original = set(df.columns)
    returned = set(out_df.columns)
    assert original.issubset(returned), (
        f"input columns missing: {original - returned}"
    )
    assert returned - original == {"chip_pct"}, (
        f"unexpected extra columns: {returned - original - {'chip_pct'}}"
    )

    # Existing column values are byte-for-byte identical.
    for col in df.columns:
        original_values = df.get_column(col).to_list()
        returned_values = out_df.get_column(col).to_list()
        assert returned_values == original_values, (
            f"column `{col}` values diverged"
        )

    # `chip_pct` is a List<Float64> column of length == df.height.
    chip_col = out_df.get_column("chip_pct")
    assert chip_col.dtype == pl.List(pl.Float64), (
        f"chip_pct dtype should be List(Float64), got {chip_col.dtype}"
    )
    assert chip_col.len() == df.height

    # The kernel emits a 1-row List<Float64> column that Polars broadcasts
    # to match input height. All rows must carry the IDENTICAL final-chips
    # list (the broadcast target). If a future polars minor drops list
    # broadcast, this fails loudly — the adapter would otherwise silently
    # hit `chip_lists[-1] is None` and return `[]` without raising.
    chip_lists = chip_col.to_list()
    first = chip_lists[0]
    assert all(lst == first for lst in chip_lists), (
        "chip_pct should be broadcast (all rows identical); got differing lists"
    )
    # And the broadcast list must have the right length.
    assert first is not None and len(first) == 40, (
        f"chip_pct list element should be a 40-bin array, got {first!r}"
    )
