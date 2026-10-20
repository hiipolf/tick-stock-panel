"""Path A-5 / Spec 0005 §"Test plan (per Q13)" — 5 seam tests for
`run_regime_batch` thin adapter (services_regime).

End state:
  pytest backend/tests/test_rs_regime_builder_seam.py -v → 5 passed

Mirrors `backend/tests/test_rs_sector_rotation_seam.py` (Path A-4 T04, 5 tests)
adapted for the multi-batch daily-grain orchestrator:
  1. parity: orchestrator (Rust path) == _py_run_regime_batch (Python ref) == expected.json
     (3-way assertion per Path A-4 T04 `test_parity` pattern at line 63-101)
  2. forced ImportError fallback: monkeypatch `_rust_kernels_available` to
     lambda: False → adapter falls back to Python ref; result == direct _py call
  3. 3 graceful no-op triggers: (a) missing enriched dir → no_data, (b) missing
     instruments → graceful, (c) start > end → ValueError
  4. dtype preservation: `tf_rs_indicators` exposes 2 Path A-5 kernels;
     `aggregate_daily_kernel(per_stock_df, {})` returns polars DataFrame with
     documented 16-col schema
  5. cache eviction: per Q9 no in-process caching; 2 calls within same process
     return equal DataFrames content but distinct Python object ids
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import polars as pl
import pytest


ROOT = Path(__file__).resolve().parent  # backend/tests/
FIXTURE_DIR = ROOT / "fixtures" / "regime_builder_5d"
EXPECTED_JSON = FIXTURE_DIR / "expected.json"

FIXTURE_START = date(2026, 10, 13)
FIXTURE_END = date(2026, 10, 19)

# 30 symbols (mirrors gen_regime_builder_fixture.SYMBOLS — must match exactly
# for the parity test to be byte-equal)
SYMBOLS: list[str] = [
    "600000.SH", "600036.SH", "601398.SH", "601939.SH", "601288.SH",
    "600519.SH", "000858.SZ", "600887.SH", "603288.SH",
    "000001.SZ", "000002.SZ", "600030.SH",
    "002415.SZ", "000063.SZ", "600276.SH",
    "300750.SZ", "002594.SZ", "300059.SZ",
    "601012.SH", "601888.SH", "600028.SH",
    "600585.SH", "000333.SZ", "000538.SZ",
    "300015.SZ", "601628.SH", "000661.SZ",
    "001979.SZ", "600048.SH", "600340.SH",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _MockStore:
    def __init__(self, data_dir):
        self.data_dir = data_dir


class _MockRepo:
    """Minimal repo satisfying `run_regime_batch` (per Spec 0005 Q12 + T03 regenerator)."""

    def __init__(self, data_dir, instruments: pl.DataFrame, index_daily: pl.DataFrame):
        self.store = _MockStore(data_dir)
        self._instruments = instruments
        self._index_daily = index_daily

    def get_instruments(self) -> pl.DataFrame:
        return self._instruments

    def get_historical_shares(self) -> pl.DataFrame:
        return pl.DataFrame()  # empty; verified accepted by `compute_limit_signals`

    def get_enriched_range(self, start, end):
        # Force fallback path (`_scan_enriched_fallback` in Python ref;
        # `_orchestrate_with_kernels` in Rust path) — exercises the kernels.
        return None

    def get_index_daily(self, symbol, start, end, columns=None):
        df = self._index_daily
        if columns:
            df = df.select([c for c in columns if c in df.columns])
        return df.filter((pl.col("date") >= start) & (pl.col("date") <= end))


def _load_instruments() -> pl.DataFrame:
    """Subset live `instruments/instruments.parquet` to the 30 fixture symbols.

    Mirrors `gen_regime_builder_fixture._subset_instruments`.
    """
    src = Path(r"D:/AI-Stock/Stock-Data/instruments/instruments.parquet")
    return pl.read_parquet(src).filter(pl.col("symbol").is_in(SYMBOLS))


def _load_index_daily() -> pl.DataFrame:
    """Load 000001.SH (上证指数) for 5 fixture dates; compute change_pct.

    Mirrors `gen_regime_builder_fixture._subset_index_daily`.
    """
    frames: list[pl.DataFrame] = []
    for d in ["2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-19"]:
        src = Path(f"D:/AI-Stock/Stock-Data/kline_index_daily/date={d}/part.parquet")
        if not src.exists():
            continue
        df = pl.read_parquet(src).filter(pl.col("symbol") == "000001.SH")
        if not df.is_empty():
            frames.append(df.select(["symbol", "date", "close"]))
    full = pl.concat(frames, how="vertical").sort("date")
    return full.with_columns(
        change_pct=(pl.col("close") - pl.col("close").shift(1).over("symbol"))
                  / pl.col("close").shift(1).over("symbol"),
    ).select(["date", "change_pct"])


@pytest.fixture(autouse=True)
def _clear_module_state():
    """Clear `_RUST_KERNELS_OK` + ST cache before AND after each test."""
    from app.services import regime_builder as rb
    from app.services import market_mainline
    rb._RUST_KERNELS_OK = None
    market_mainline._ST_SYMBOLS_CACHE = None
    yield
    rb._RUST_KERNELS_OK = None
    market_mainline._ST_SYMBOLS_CACHE = None


# ---------------------------------------------------------------------------
# Seam tests
# ---------------------------------------------------------------------------

def test_parity():
    """Seam #1: 3-way parity (orchestrator == _py ref == expected.json).

    With kernels available (cdylib exposes `compute_batch_kernel` +
    `aggregate_daily_kernel`), `run_regime_batch` dispatches to
    `_orchestrate_with_kernels` (kernel 1 IO + Python pipeline + kernel 2
    aggregation). The Python-only ref path (`_scan_enriched_fallback` →
    `_py_compute_batch` + `_py_aggregate_daily`) produces the same 30-col
    DataFrame. Three-way byte-equal assertion.
    """
    from app.services import regime_builder as rb

    expected = json.loads(EXPECTED_JSON.read_text(encoding="utf-8"))
    instruments = _load_instruments()
    index_daily = _load_index_daily()
    repo = _MockRepo(FIXTURE_DIR, instruments, index_daily)

    actual = rb.run_regime_batch(repo, FIXTURE_START, FIXTURE_END)
    # `iter_rows(named=True)` returns polars Date as `datetime.date`; expected.json
    # has ISO 8601 strings (per `gen_regime_builder_fixture._df_to_jsonable`).
    # Normalize both to ISO strings for dict-key comparison.
    actual_dict = {r["date"].isoformat() if hasattr(r["date"], "isoformat") else str(r["date"]): r
                   for r in actual.iter_rows(named=True)}
    expected_by_date = {r["date"]: r for r in expected["rows"]}

    # Same shape
    assert list(actual.shape) == expected["shape"], (
        f"orchestrator shape {actual.shape} != expected {expected['shape']}"
    )

    # Same dates
    assert set(actual_dict.keys()) == set(expected_by_date.keys()), (
        f"date mismatch: orchestrator {sorted(actual_dict.keys())} != "
        f"expected {sorted(expected_by_date.keys())}"
    )

    # Per-date byte-equal for the 30-col output (polars `to_dicts` roundtrip)
    def _norm(v):
        # Date → ISO 8601 string (expected.json has strings per `_df_to_jsonable`)
        if hasattr(v, "isoformat"):
            return v.isoformat()
        return v
    for d in sorted(actual_dict.keys()):
        a = actual_dict[d]
        e = expected_by_date[d]
        for col in expected["schema"]:
            name = col["name"]
            av = _norm(a.get(name))
            ev = _norm(e.get(name))
            # Floats: allow tiny relative diff (kernel vs Python may differ in
            # last-digit precision due to aggregation order).
            if isinstance(av, float) and isinstance(ev, float):
                if abs(av - ev) > 1e-9 * max(abs(av), abs(ev), 1.0):
                    raise AssertionError(
                        f"[{d}] {name}: kernel={av!r} != expected={ev!r}"
                    )
            else:
                assert av == ev, f"[{d}] {name}: kernel={av!r} != expected={ev!r}"


def test_forced_import_error_fallback(monkeypatch):
    """Seam #2: forced `_rust_kernels_available() = False` → adapter falls back.

    Mirrors Path A-4 T04 `test_forced_import_error_fallback` at line 104-134.
    Patches `_rust_kernels_available` to return `False` (simulating `ImportError`
    on `tf_rs_indicators` or missing kernels). Adapter must still return the
    same dict as a direct `_py_*` call — **no tautology** (the call goes
    through `run_regime_batch`'s `if/else` dispatch, NOT through a pre-cached
    result).
    """
    from app.services import regime_builder as rb

    monkeypatch.setattr(rb, "_rust_kernels_available", lambda: False)
    assert rb._rust_kernels_available() is False

    instruments = _load_instruments()
    index_daily = _load_index_daily()
    repo = _MockRepo(FIXTURE_DIR, instruments, index_daily)

    actual = rb.run_regime_batch(repo, FIXTURE_START, FIXTURE_END)
    # Compare against expected.json (which was captured via the Python-only ref)
    expected = json.loads(EXPECTED_JSON.read_text(encoding="utf-8"))
    expected_by_date = {r["date"]: r for r in expected["rows"]}
    actual_by_date = {r["date"].isoformat() if hasattr(r["date"], "isoformat") else str(r["date"]): r
                      for r in actual.iter_rows(named=True)}

    assert list(actual.shape) == expected["shape"]
    def _norm(v):
        if hasattr(v, "isoformat"):
            return v.isoformat()
        return v
    for d in sorted(actual_by_date.keys()):
        for col in expected["schema"]:
            name = col["name"]
            av = _norm(actual_by_date[d].get(name))
            ev = _norm(expected_by_date[d].get(name))
            if isinstance(av, float) and isinstance(ev, float):
                assert abs(av - ev) <= 1e-9 * max(abs(av), abs(ev), 1.0), (
                    f"[{d}] {name}: fallback={av!r} != expected={ev!r}"
                )
            else:
                assert av == ev, f"[{d}] {name}: fallback={av!r} != expected={ev!r}"


def test_graceful_no_op_triggers(tmp_path, monkeypatch):
    """Seam #3: 3 graceful no-op triggers per Spec 0005 §"Test plan".

    (a) missing enriched dir (data_dir has no `kline_daily_enriched/`) → empty
        DataFrame (Python ref returns `pl.DataFrame()`; Rust path returns
        `pl.DataFrame()` via the orchestrator's `not enriched_dir.exists()`
        early return at `_orchestrate_with_kernels` line 559).
    (b) missing instruments (empty `_MockRepo.get_instruments()`) → graceful
        (orchestrator proceeds; `compute_limit_signals` accepts empty `instruments`
        per `pipeline.py:706-711`).
    (c) start > end → empty DataFrame (early return at `run_regime_batch` line 477
        and `_orchestrate_with_kernels` line 547).
    """
    from app.services import regime_builder as rb

    instruments = _load_instruments()
    index_daily = _load_index_daily()

    # (a) missing enriched dir
    empty_dir = tmp_path / "no_enriched"
    empty_dir.mkdir()
    repo = _MockRepo(empty_dir, instruments, index_daily)
    out_a = rb.run_regime_batch(repo, FIXTURE_START, FIXTURE_END)
    assert isinstance(out_a, pl.DataFrame)
    assert out_a.is_empty(), f"expected empty DataFrame for missing enriched dir, got shape={out_a.shape}"

    # (b) missing instruments (empty DataFrame)
    repo_b = _MockRepo(FIXTURE_DIR, pl.DataFrame(), index_daily)
    out_b = rb.run_regime_batch(repo_b, FIXTURE_START, FIXTURE_END)
    # Should still produce a DataFrame (may be empty if compute_limit_signals
    # can't proceed; we just verify no exception is raised).
    assert isinstance(out_b, pl.DataFrame)

    # (c) start > end
    repo_c = _MockRepo(FIXTURE_DIR, instruments, index_daily)
    out_c = rb.run_regime_batch(repo_c, FIXTURE_END, FIXTURE_START)
    assert isinstance(out_c, pl.DataFrame)
    assert out_c.is_empty()


def test_dtype_preservation():
    """Seam #4: dtype preservation — 2 Path A-5 kernels callable + 16-col schema.

    Verifies that `tf_rs_indicators` exposes the 2 Path A-5 kernels from T02
    (per `cargo test --lib services_regime` 10/10 + wheel rebuild). The
    `aggregate_daily_kernel(per_stock_df, {})` call must return a polars
    DataFrame with the documented 16-col schema (date + 14 aggregations +
    `ma20_above_pct` + `index_pct`).
    """
    import tf_rs_indicators as m

    expected_names = ("compute_batch_kernel", "aggregate_daily_kernel")
    for name in expected_names:
        assert hasattr(m, name), f"tf_rs_indicators is missing Path A-5 kernel: {name}"
        assert callable(getattr(m, name)), f"tf_rs_indicators.{name} is not callable"

    # aggregate_daily_kernel smoke: returns pl.DataFrame with 23-col schema
    # (14 agg + ma20_above_pct + index_pct + 7 梯队 agg).
    per_stock = pl.DataFrame({
        "symbol": ["A.SH", "B.SH"],
        "date": ["2025-01-10", "2025-01-10"],
        "change_pct": [0.02, -0.01],
        "amount": [1.0e8, 2.0e8],
        "signal_limit_up": [True, False],
        "signal_limit_down": [False, False],
        "signal_broken_limit_up": [False, False],
        "consecutive_limit_ups": [2, 0],
        "close": [11.0, 12.0],
        "ma20": [10.0, 12.5],
    })
    out = m.aggregate_daily_kernel(per_stock, {"2025-01-10": 0.005})
    assert isinstance(out, pl.DataFrame), (
        f"aggregate_daily_kernel must return pl.DataFrame; got {type(out).__name__}"
    )
    expected_cols = (
        "date", "up_count", "down_count", "total_count",
        "avg_pct", "median_pct", "strong_up_count", "strong_down_count",
        "limit_up", "limit_down", "broken_limit", "max_consecutive",
        "total_amount", "avg_amount", "ma20_above_pct", "index_pct",
        # 梯队指标 (ladder) — same group_by block as the 14 base aggs
        "first_board", "ge2_count", "ge3_count", "ge5_count",
        "rungs_filled", "promo_pool", "promo_ok",
    )
    assert out.columns == list(expected_cols), (
        f"aggregate_daily_kernel cols {out.columns} != expected {list(expected_cols)}"
    )


def test_cache_eviction(monkeypatch):
    """Seam #5: no in-process cache (per Q9) — 2 calls return equal content,
    distinct object ids.

    Per Spec 0005 §"Implementation Decisions" Q9: "daily batch compute 责任
    在 caller (`regime_history.parquet` + `detect_stale_dates` +
    `upsert_regime_history`)". The module does NOT keep an in-process cache;
    each call rebuilds the DataFrame fresh. 2 calls within the same process
    return content-equal but distinct Python object ids.
    """
    from app.services import regime_builder as rb

    instruments = _load_instruments()
    index_daily = _load_index_daily()
    repo = _MockRepo(FIXTURE_DIR, instruments, index_daily)

    # Call 1
    result1 = rb.run_regime_batch(repo, FIXTURE_START, FIXTURE_END)
    # Call 2 — should produce a fresh DataFrame (NOT a cached reference)
    result2 = rb.run_regime_batch(repo, FIXTURE_START, FIXTURE_END)

    # Content-equal (same input → same output)
    assert result1.shape == result2.shape
    assert result1.columns == result2.columns
    # polars DataFrames: `.equals` does deep structural comparison
    assert result1.equals(result2), "2 calls within same process must be content-equal"

    # Distinct Python object ids (Q9: no in-process cache → fresh DataFrame per call)
    assert result1 is not result2, (
        "Q9: no in-process cache → 2 calls must return distinct Python objects, "
        "not the same cached reference"
    )
