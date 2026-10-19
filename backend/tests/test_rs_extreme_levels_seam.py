"""End-to-end seam test for `_extreme_levels` (Spec 0002 Ticket 03).

Verifies the thin Python adapter (`backend/app/indicators/levels.py::_extreme_levels`):

* Rust kernel path: bit-identical output to the pure-Python reference on the
  frozen 240-day fixture (`backend/tests/fixtures/extreme_levels_240d.parquet`
  + `extreme_levels_240d.expected.json`).
* Pure-Python fallback: when `tf_rs_indicators` is unavailable (CI without
  Rust toolchain), the adapter still returns dicts via `_py_extreme_levels`.
* Graceful no-op: empty df → `[]` (matches the kernel's no-op contract).
* Dtype preservation: when the kernel runs, the returned DataFrame keeps all
  input columns plus the 6 new columns (swing_high/low + extreme_<w>_high/low).

Per ADR-0002 §5 (try-import fallback): when the wheel is **not** built
(e.g. CI without Rust toolchain), the Rust-kernel-specific tests are
**skipped** rather than failed. The pure-Python fallback test still runs
because it doesn't depend on the wheel.

Run with:
    uv run -m pytest backend/tests/test_rs_extreme_levels_seam.py -v

Companion Rust unit tests live at `crates/indicators/src/extreme_levels.rs::tests`.
The 240-day fixture was captured once via `backend/tests/fixtures/gen_extreme_levels_240d.py`
and frozen into git; do not regenerate casually.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import polars as pl
import pytest

# --- skipif (mirror test_rs_indicators_seam.py / test_rs_levels_seam.py pattern) ---
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
PARQUET = FIX_DIR / "extreme_levels_240d.parquet"
JSON_EXPECTED = FIX_DIR / "extreme_levels_240d.expected.json"


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
    on a 240-day fixture" gate (Spec 0002 §'Seams')."""
    seam = _require_seam()
    levels = _load_levels_module()
    df, expected = _load_fixture()

    # The adapter should pick the Rust path (wheel is available).
    result = levels._extreme_levels(df)

    assert result == expected, (
        f"Rust-via-adapter output diverges from the Python reference.\n"
        f"  expected: {expected}\n  got: {result}"
    )

    # Sanity: the underlying kernel call directly also returns the expected
    # 6-column output. Verifies the kernel contract independently of the adapter.
    out_df = seam.extreme_levels(df, None, 5)
    for col in ("swing_high", "swing_low", "extreme_60_high", "extreme_60_low",
                "extreme_250_high", "extreme_250_low"):
        assert col in out_df.columns, f"kernel must append column {col!r}"
    assert out_df.height == df.height


# =====================================================================
# 2) Pure-Python fallback when the wheel is missing
# =====================================================================

def test_python_fallback_when_wheel_missing(monkeypatch):
    """Force `ImportError` on `tf_rs_indicators`; the adapter must fall back
    to `_py_extreme_levels` and still return the same dicts as the reference.
    Same shape as Spec 0001's `test_python_fallback_when_wheel_missing`:
    (a) the adapter result matches the frozen 240-day reference;
    (b) the reference itself, called directly, still produces the frozen
        output. Independent assertions, not tautological."""
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

    result = levels._extreme_levels(df)
    direct = levels._py_extreme_levels(df)
    assert result == expected, (
        f"Fallback (adapter) result diverges from the frozen 240-day reference: "
        f"{result} vs {expected}"
    )
    assert direct == expected, (
        f"Reference itself drifted from frozen 240-day output: "
        f"{direct} vs {expected}"
    )


# =====================================================================
# 3) Graceful no-op on empty input
# =====================================================================

def test_graceful_no_op_for_empty_input():
    """Empty df → `[]` without raising. The kernel contract (Spec 0002 §'Algorithm')
    says an empty input is returned unchanged by the kernel; the adapter's
    `if df.is_empty(): return []` upstream gate is what produces `[]`. Both
    the adapter and the reference must agree."""
    levels = _load_levels_module()
    empty = pl.DataFrame({
        "open": pl.Series([], dtype=pl.Float64),
        "high": pl.Series([], dtype=pl.Float64),
        "low":  pl.Series([], dtype=pl.Float64),
        "close": pl.Series([], dtype=pl.Float64),
        "volume": pl.Series([], dtype=pl.Float64),
    })

    # Adapter (any path — wheel may or may not be installed)
    assert levels._extreme_levels(empty) == [], "adapter: empty df → []"
    # Reference directly
    assert levels._py_extreme_levels(empty) == [], "reference: empty df → []"

    # Rust kernel directly: empty df → unchanged output, no columns appended.
    if _tfr_seam is not None:
        out = _tfr_seam.extreme_levels(empty, None, 5)
        assert out.height == 0, "kernel: empty df height preserved"
        assert "swing_high" not in out.columns, "kernel: no columns on empty df"


# =====================================================================
# 4) Dtype preservation when the kernel runs
# =====================================================================

@pytestmark_rust_only
def test_dtype_preservation_with_kernel():
    """The kernel appends 2 Boolean columns (swing_high, swing_low) and
    2*len(windows) Float64 broadcast columns. None of the input columns must
    be mutated. The Float64 broadcasts carry the same value on every row
    (verifies the broadcast pattern, not just the presence of the column)."""
    seam = _require_seam()
    levels = _load_levels_module()
    df, expected = _load_fixture()

    out_df = seam.extreme_levels(df, None, 5)

    # 1) All input columns preserved.
    for c in df.columns:
        assert c in out_df.columns, f"input column {c!r} must be preserved"

    # 2) 2 Boolean columns.
    assert out_df["swing_high"].dtype == pl.Boolean
    assert out_df["swing_low"].dtype == pl.Boolean
    assert out_df["swing_high"].len() == df.height
    assert out_df["swing_low"].len() == df.height

    # 3) 4 Float64 broadcast columns (windows=[60, 250]).
    # NaN-aware equality: Python's `nan != nan` is True, so use `math.isnan`
    # to compare broadcast values (250d is all-NaN when df.height < 250).
    def _nan_eq(a: float, b: float) -> bool:
        if math.isnan(a) and math.isnan(b):
            return True
        return a == b

    for w in (60, 250):
        col_h = f"extreme_{w}_high"
        col_l = f"extreme_{w}_low"
        assert out_df[col_h].dtype == pl.Float64
        assert out_df[col_l].dtype == pl.Float64
        # Broadcast: every row carries the same value (NaN-aware).
        all_h = out_df[col_h].to_list()
        all_l = out_df[col_l].to_list()
        first_h, first_l = all_h[0], all_l[0]
        for i, (h, l) in enumerate(zip(all_h, all_l)):
            assert _nan_eq(h, first_h), f"{col_h}[{i}]={h} must equal [{0}]={first_h}"
            assert _nan_eq(l, first_l), f"{col_l}[{i}]={l} must equal [{0}]={first_l}"

    # 4) Adapter still produces the same dicts (sanity).
    adapter_out = levels._extreme_levels(df)
    assert adapter_out == expected, "adapter output must remain bit-identical"