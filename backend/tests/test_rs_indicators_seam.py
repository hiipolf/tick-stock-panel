"""End-to-end seam test for the `tf_rs_indicators` Python extension.

This is the Python half of the dual-track testing strategy (ADR-0002 §4).
Behaviour exercised here:

* `import tf_rs_indicators` resolves after `maturin develop --release`.
* `sum_close(df)` accepts a `polars.DataFrame`, appends a one-row
  `sum_close` column equal to the column-wise sum of `close`.
* Existing columns are preserved across the round-trip.
* Missing `close` column raises (propagates `PyValueError`).

Per ADR-0002 §5 (try-import fallback): when the wheel is **not** built
(e.g. CI without Rust toolchain), the entire module is skipped rather
than failed. Failure-vs-skip distinction is important: a missing
wheel must not block main-branch CI.

Run with:
    uv run -m pytest backend/tests/test_rs_indicators_seam.py -v

Companion Rust unit tests live in crates/indicators/src/sum.rs.
"""

from __future__ import annotations

import pytest

try:
    import polars as pl  # noqa: F401  — required at parse time
except ImportError:
    pl = None  # type: ignore[assignment]

try:
    import tf_rs_indicators as _tfr_seam  # type: ignore[import-not-found]
    _TFR_SEAM_AVAILABLE = True
except ImportError:
    _tfr_seam = None
    _TFR_SEAM_AVAILABLE = False

_seam_unavailable = (
    pl is None
    or not _TFR_SEAM_AVAILABLE
)

# ADR-0002 §5 fallback policy: skip (not fail) when the wheel is absent.
pytestmark = pytest.mark.skipif(
    _seam_unavailable,
    reason="tf_rs_indicators wheel not installed (try-import fallback active)",
)


def _require_seam():
    assert _tfr_seam is not None, "pytest.mark.skipif should have skipped"
    return _tfr_seam


def test_sum_close_appends_column_with_correct_value():
    seam = _require_seam()
    df = pl.DataFrame({"close": [1.0, 2.0, 3.0, 4.0]})
    out = seam.sum_close(df)

    assert isinstance(out, pl.DataFrame)
    assert "sum_close" in out.columns, f"expected `sum_close` column, got {out.columns}"
    val = out.get_column("sum_close")
    assert len(val) == 1, f"sum_close should be 1-row scalar, got {len(val)}"
    assert val[0] == pytest.approx(10.0)


def test_sum_close_preserves_existing_columns():
    seam = _require_seam()
    df = pl.DataFrame({"close": [10.0, 20.0], "extra": [100, 200]})
    out = seam.sum_close(df)
    assert "close" in out.columns
    assert "extra" in out.columns
    # Existing column values are intact.
    assert out.get_column("extra").to_list() == [100, 200]


def test_sum_close_raises_on_missing_close_column():
    seam = _require_seam()
    df = pl.DataFrame({"other": [1.0, 2.0]})
    with pytest.raises(Exception):
        seam.sum_close(df)


def test_module_metadata_exposed():
    seam = _require_seam()
    # lib.rs sets __version__ from CARGO_PKG_VERSION. Contract:
    assert isinstance(seam.__version__, str)
    assert seam.__version__  # non-empty
