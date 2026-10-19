"""End-to-end seam test for the 3 service-level kernels (Spec 0003 Ticket 04).

Verifies the thin Python adapters in `backend/app/services/market_overview_builder.py`:

* `_dimension_rank(rows, repo, kind, limit, level) -> {"leading": [...], "lagging": [...]}`
* `_top_rows(rows, key, descending, limit) -> list[dict]`
* `_pct_band_rows(values) -> list[dict]`

5 tests per Spec 0003 §"Seams":

1. **Bit-identical 240-day parity**: Rust kernel output == `_py_*` reference ==
   frozen fixture JSON (captured once via `gen_market_overview_dim_fixture.py`).
2. **Pure-Python fallback** when `tf_rs_indicators` is unavailable (CI without
   Rust toolchain): forced `ImportError` → still returns the frozen reference.
3. **Graceful no-op**: empty rows / no `repo.store.data_dir` / no `ext` rows —
   all return `{"leading": [], "lagging": []}` (or equivalent) without raising.
4. **Dtype preservation**: kernel output DataFrame has all expected columns +
   dtypes (NaN-aware, matches Spec 0002 test pattern).
5. **Cache eviction**: repeated call within TTL returns same dict (Python owns
   cache; the kernel is stateless, so two independent calls must produce
   structurally equal output without state leakage).

Per ADR-0002 §5 (try-import fallback): when the wheel is **not** built
(e.g. CI without Rust toolchain), the Rust-kernel-specific tests are
**skipped** rather than failed. The pure-Python fallback test still runs
because it doesn't depend on the wheel.

Run with:
    uv run -m pytest backend/tests/test_rs_market_overview_seam.py -v

Companion Rust unit tests live at `crates/indicators/src/services_market_overview/{top_rows,
pct_band_rows,dimension_rank}.rs::tests`. The fixture was captured once via
`backend/tests/fixtures/gen_market_overview_dim_fixture.py` and frozen into git;
do not regenerate casually.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import polars as pl
import pytest

# --- skipif (mirror test_rs_extreme_levels_seam.py pattern) ---
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
def _load_market_overview_module():
    """Import the adapter module. Add backend/ to sys.path on first call."""
    backend_dir = Path(__file__).resolve().parent.parent
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))
    from app.services import market_overview_builder  # type: ignore[import-not-found]
    return market_overview_builder


# --- fixture paths ---
FIX_DIR = Path(__file__).resolve().parent / "fixtures" / "market_overview_dim_240d"
ROWS_SNAPSHOT = FIX_DIR / "rows_snapshot.json"
DIM_EXPECTED = FIX_DIR / "dimension_rank.expected.json"
TOP_EXPECTED = FIX_DIR / "top_rows.expected.json"
PCT_EXPECTED = FIX_DIR / "pct_band_rows.expected.json"


class _RepoStub:
    """Minimal `repo` stub for `_dimension_rank`: only `.store.data_dir` is read."""
    class _Store:
        def __init__(self, data_dir: Path):
            self.data_dir = data_dir
    def __init__(self, data_dir: Path):
        self.store = self._Store(data_dir)


def _load_fixture():
    rows = json.loads(ROWS_SNAPSHOT.read_text(encoding="utf-8"))
    dim_expected = json.loads(DIM_EXPECTED.read_text(encoding="utf-8"))
    top_expected = json.loads(TOP_EXPECTED.read_text(encoding="utf-8"))
    pct_expected = json.loads(PCT_EXPECTED.read_text(encoding="utf-8"))
    return rows, dim_expected, top_expected, pct_expected


# =====================================================================
# 1) Rust kernel output == Python reference on 240-day fixture (parity)
# =====================================================================

@pytestmark_rust_only
def test_kernel_matches_python_reference_on_240d_fixture():
    """All 3 adapters, on the frozen 38-symbol × 240-day data_dir subset,
    must produce output bit-identical to the pure-Python reference.

    Spec 0003 §"Seams" / Ticket 04 hard gate: Rust parity is bit-exact, not
    "close enough".
    """
    seam = _require_seam()
    mob = _load_market_overview_module()
    rows, dim_expected, top_expected, pct_expected = _load_fixture()
    repo = _RepoStub(FIX_DIR)

    # top_rows
    got_top = mob._top_rows(rows, "change_pct", True, 8)
    assert got_top == top_expected, (
        f"_top_rows (Rust) diverged from frozen reference: "
        f"got={got_top[:2]}... expected={top_expected[:2]}..."
    )

    # pct_band_rows
    values = [r["change_pct"] for r in rows if r.get("change_pct") is not None]
    got_pct = mob._pct_band_rows(values)
    assert got_pct == pct_expected, (
        f"_pct_band_rows (Rust) diverged from frozen reference: "
        f"got={got_pct} expected={pct_expected}"
    )

    # dimension_rank — adapter reshapes flat kernel cols → nested leader dict
    got_dim = mob._dimension_rank(rows, repo, "industry", 5, level=None)
    assert got_dim == dim_expected, (
        f"_dimension_rank (Rust) diverged from frozen reference: "
        f"leading[0]={got_dim['leading'][0] if got_dim['leading'] else None} "
        f"expected={dim_expected['leading'][0] if dim_expected['leading'] else None}"
    )

    # Sanity: kernel-direct call also matches expected shape
    # (verify the 3 kernel functions are exposed with the documented signatures).
    out_df = seam.dimension_rank_kernel(
        [("Group", [{"symbol": "X", "change_pct": 0.01, "amount": 1.0}])],
        5,
        None,
    )
    assert "name" in out_df.columns
    assert "leader_symbol" in out_df.columns
    assert "leader_change_pct" in out_df.columns


# =====================================================================
# 2) Pure-Python fallback when the wheel is missing
# =====================================================================

def test_python_fallback_when_wheel_missing(monkeypatch):
    """Force `ImportError` on `tf_rs_indicators`; all 3 adapters must fall
    back to `_py_*` and still return the same dicts as the frozen reference.

    Per Spec 0003 §"Testing Decisions": independent assertions, not tautological.
    The reference itself, called directly, must also produce the frozen output
    (verifies the reference hasn't drifted from the captured fixture).
    """
    import builtins
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "tf_rs_indicators" or name.startswith("tf_rs_indicators."):
            raise ImportError(
                f"No module named '{name}' (test mock — fallback verification)"
            )
        return real_import(name, globals, locals, fromlist, level)
    monkeypatch.setattr(builtins, "__import__", fake_import)

    mob = _load_market_overview_module()
    rows, dim_expected, top_expected, pct_expected = _load_fixture()
    repo = _RepoStub(FIX_DIR)

    # Adapter (fallback path) matches frozen reference
    got_top = mob._top_rows(rows, "change_pct", True, 8)
    assert got_top == top_expected, "fallback _top_rows diverged"

    values = [r["change_pct"] for r in rows if r.get("change_pct") is not None]
    got_pct = mob._pct_band_rows(values)
    assert got_pct == pct_expected, "fallback _pct_band_rows diverged"

    got_dim = mob._dimension_rank(rows, repo, "industry", 5)
    assert got_dim == dim_expected, "fallback _dimension_rank diverged"

    # Reference directly still matches — proves the `_py_*` haven't drifted
    # from the frozen capture.
    direct_top = mob._py_top_rows(rows, "change_pct", True, 8)
    assert direct_top == top_expected, "_py_top_rows reference drifted"

    direct_pct = mob._py_pct_band_rows(values)
    assert direct_pct == pct_expected, "_py_pct_band_rows reference drifted"

    direct_dim = mob._py_dimension_rank(rows, repo, "industry", 5)
    assert direct_dim == dim_expected, "_py_dimension_rank reference drifted"


# =====================================================================
# 3) Graceful no-op on 3 empty-input cases
# =====================================================================

def test_graceful_no_op_empty_rows():
    """Empty rows → `{"leading": [], "lagging": []}` without raising.

    Both the adapter and the reference must agree (Spec 0003 §"Seams" #3).
    The adapter short-circuits on empty rows before the kernel call, so this
    test passes whether or not the Rust wheel is installed.
    """
    mob = _load_market_overview_module()
    repo = _RepoStub(FIX_DIR)

    got = mob._dimension_rank([], repo, "industry", 5)
    assert got == {"leading": [], "lagging": []}, (
        f"empty rows should yield empty dict; got {got}"
    )

    direct = mob._py_dimension_rank([], repo, "industry", 5)
    assert direct == {"leading": [], "lagging": []}, (
        f"_py_ empty rows should yield empty dict; got {direct}"
    )


def test_graceful_no_op_no_ext_rows():
    """`data_dir` with no `ext_data/` directory → adapter returns empty dict.

    Per Spec §"Seams" #3: graceful no-op when no ext rows are found.
    `_py_dimension_rank` already handles this (store.load_all() returns [],
    groups stays empty, items stays empty). The new Rust adapter must mirror
    this behavior because it shares the same pre-aggregate body.
    """
    mob = _load_market_overview_module()
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        empty_repo = _RepoStub(Path(tmpdir))  # no ext_data subdir at all
        rows = [{"symbol": "000001.SZ", "change_pct": 0.01, "amount": 1.0}]
        got = mob._dimension_rank(rows, empty_repo, "industry", 5)
    assert got == {"leading": [], "lagging": []}, (
        f"empty ext dir should yield empty dict; got {got}"
    )

    # And the Python reference does the same.
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        empty_repo = _RepoStub(Path(tmpdir))
        direct = mob._py_dimension_rank(rows, empty_repo, "industry", 5)
    assert direct == {"leading": [], "lagging": []}, (
        f"_py_ empty ext dir should yield empty dict; got {direct}"
    )


def test_graceful_no_op_top_rows_empty():
    """Empty-input no-op for `_top_rows` and `_pct_band_rows`.

    `_top_rows([])` → `[]` (filter on empty list = no rows).
    `_pct_band_rows([])` → 8 zero-count buckets (the 8 buckets are *fixed*
    by Spec §"pct_band_rows_kernel signature", so empty input still produces
    the 8-row schema with count=0 / pct=0.0). Matches `_py_*` parity.
    """
    mob = _load_market_overview_module()

    assert mob._top_rows([], "change_pct", True, 8) == []
    assert mob._py_top_rows([], "change_pct", True, 8) == []

    # pct_band_rows on empty input: 8 fixed buckets, all count=0, pct=0.0
    got_pct = mob._pct_band_rows([])
    assert len(got_pct) == 8, f"empty input: 8 buckets, got {len(got_pct)}"
    for entry in got_pct:
        assert entry["count"] == 0
        assert entry["pct"] == 0.0
    assert mob._py_pct_band_rows([]) == got_pct


# =====================================================================
# 4) Dtype preservation when the kernel runs
# =====================================================================

@pytestmark_rust_only
def test_dtype_preservation_with_kernel():
    """Verify the Rust kernel returns DataFrames with the documented schemas
    (NaN-aware, matches Spec 0003 §"Rust kernel 输出 shape").

    All 3 kernels tested:
      - top_rows_kernel: 7 columns (symbol str, name Option<str>, close
        Option<f64>, change_pct Option<f64>, amount Option<f64>,
        turnover_rate Option<f64>, board str)
      - pct_band_rows_kernel: 3 columns (label str, count u32, pct f64),
        always exactly 8 rows
      - dimension_rank_kernel: 10 columns (name str, count u32, avg_pct f64,
        up_count u32, down_count u32, amount f64, source_field Option<str>,
        leader_symbol str, leader_name Option<str>, leader_change_pct
        Option<f64>)
    """
    seam = _require_seam()
    rows, dim_expected, top_expected, pct_expected = _load_fixture()

    # top_rows_kernel
    out = seam.top_rows_kernel(rows, "change_pct", True, 8)
    assert out.width == 7, f"top_rows_kernel: expected 7 cols, got {out.width}"
    assert set(out.columns) == {
        "symbol", "name", "close", "change_pct", "amount", "turnover_rate", "board"
    }, f"top_rows_kernel: unexpected columns {out.columns}"
    assert out["symbol"].dtype == pl.Utf8
    assert out["name"].dtype == pl.Utf8  # polars treats Option<str> as Utf8 nullable
    assert out["close"].dtype == pl.Float64
    assert out["change_pct"].dtype == pl.Float64
    assert out["amount"].dtype == pl.Float64
    assert out["turnover_rate"].dtype == pl.Float64
    assert out["board"].dtype == pl.Utf8
    assert out.height == 8, f"top_rows_kernel: expected 8 rows, got {out.height}"

    # pct_band_rows_kernel — always 8 rows even with empty input
    out_pct = seam.pct_band_rows_kernel([0.01, -0.02, 0.03, 0.0])
    assert out_pct.width == 3, f"pct_band_rows_kernel: expected 3 cols, got {out_pct.width}"
    assert set(out_pct.columns) == {"label", "count", "pct"}
    assert out_pct["label"].dtype == pl.Utf8
    assert out_pct["count"].dtype == pl.UInt32
    assert out_pct["pct"].dtype == pl.Float64
    assert out_pct.height == 8, f"pct_band_rows_kernel: always 8 rows, got {out_pct.height}"

    # Empty input → 8 zero-row buckets (matches _py_)
    out_pct_empty = seam.pct_band_rows_kernel([])
    assert out_pct_empty.height == 8
    counts = out_pct_empty["count"].to_list()
    assert all(c == 0 for c in counts), f"empty input should yield count=0 for all bands"

    # dimension_rank_kernel — exercise with a small synthetic group
    groups = [
        ("GroupA", [
            {"symbol": "X", "change_pct": 0.01, "amount": 100.0},
            {"symbol": "Y", "change_pct": -0.005, "amount": 50.0},
        ]),
        ("GroupB", [{"symbol": "Z", "change_pct": 0.0, "amount": 200.0}]),
    ]
    out_dim = seam.dimension_rank_kernel(groups, 5, None)
    assert out_dim.width == 10, f"dimension_rank_kernel: expected 10 cols, got {out_dim.width}"
    assert set(out_dim.columns) == {
        "name", "count", "avg_pct", "up_count", "down_count",
        "amount", "source_field", "leader_symbol", "leader_name", "leader_change_pct",
    }, f"dimension_rank_kernel: unexpected columns {out_dim.columns}"
    assert out_dim["name"].dtype == pl.Utf8
    assert out_dim["count"].dtype == pl.UInt32
    assert out_dim["avg_pct"].dtype == pl.Float64
    assert out_dim["up_count"].dtype == pl.UInt32
    assert out_dim["down_count"].dtype == pl.UInt32
    assert out_dim["amount"].dtype == pl.Float64
    assert out_dim["leader_symbol"].dtype == pl.Utf8
    assert out_dim["leader_name"].dtype == pl.Utf8  # nullable str
    assert out_dim["leader_change_pct"].dtype == pl.Float64  # nullable float


# =====================================================================
# 5) Repeat-call parity (kernel + adapter are stateless)
# =====================================================================

def test_repeated_call_returns_structurally_equal_output():
    """Two consecutive calls with the same input must return structurally
    equal output (no state leakage, no cache that drifts).

    Per ADR-0006 §"5 段": the kernel is **stateless**; the adapter doesn't
    cache either (cache is owned by the caller). Two independent calls must
    produce equal dicts.
    """
    mob = _load_market_overview_module()
    rows, dim_expected, top_expected, pct_expected = _load_fixture()
    repo = _RepoStub(FIX_DIR)

    # First call
    got1_top = mob._top_rows(rows, "change_pct", True, 8)
    got1_pct = mob._pct_band_rows([r["change_pct"] for r in rows if r.get("change_pct") is not None])
    got1_dim = mob._dimension_rank(rows, repo, "industry", 5)

    # Second call (independent, no shared state)
    got2_top = mob._top_rows(rows, "change_pct", True, 8)
    got2_pct = mob._pct_band_rows([r["change_pct"] for r in rows if r.get("change_pct") is not None])
    got2_dim = mob._dimension_rank(rows, repo, "industry", 5)

    assert got1_top == got2_top, "repeated _top_rows calls diverged"
    assert got1_pct == got2_pct, "repeated _pct_band_rows calls diverged"
    assert got1_dim == got2_dim, "repeated _dimension_rank calls diverged"

    # And both match the frozen reference (no drift over time).
    assert got1_top == top_expected
    assert got1_pct == pct_expected
    assert got1_dim == dim_expected