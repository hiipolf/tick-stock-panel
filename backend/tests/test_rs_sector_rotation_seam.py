"""Path A-4 / Spec 0004 §"Testing Decisions" — 5 seam tests for
`build_sector_rotation` thin adapter (services_sector_rotation_adapter).

End state:
  pytest backend/tests/test_rs_sector_rotation_seam.py -v → 5 passed

Mirrors `backend/tests/test_rs_market_overview_seam.py` (Path A-3 T04, 7 tests)
shrunk to 5 tests per Spec 0004 §"Test plan (per Q13=b)":
  1. parity: orchestrator == _py_compute == expected.json
  2. forced ImportError fallback: monkeypatch tf_rs_indicators import → adapter
     falls back to _py_compute
  3. 3 graceful no-op triggers: empty kline_minute / out-of-range bucket_minutes
     / invalid kind
  4. dtype preservation: 8 kernels each callable + dtype smoke (bare_kernel
     returns pl.Series)
  5. cache eviction: TTL=0 → 3rd call produces new dict object

Empirical context (2026-10-19): `D:/AI-Stock/Stock-Data/kline_minute/` is empty
in this worktree. T03 fixture `backend/tests/fixtures/build_sector_rotation_5d/`
captures the `_compute` `minute_missing` short-circuit path; both `_py_compute`
and `_orchestrate_with_kernels` return the same `{"status": "no_data", ...}`
dict for that fixture, so the parity + fallback + no-op + cache tests all
exercise identical code paths in both implementations.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent  # backend/tests/
FIXTURE_DIR = ROOT / "fixtures" / "build_sector_rotation_5d"
EXPECTED_JSON = FIXTURE_DIR / "expected.json"


class _MockStore:
    def __init__(self, data_dir):
        self.data_dir = data_dir


class _MockRepo:
    def __init__(self, data_dir):
        self.store = _MockStore(data_dir)


@pytest.fixture(autouse=True)
def _clear_module_state():
    """Clear `_cache` + `_cache_ts` + `_RUST_KERNELS_OK` before AND after each test."""
    from app.services import sector_rotation as sr
    sr._cache.clear()
    sr._cache_ts.clear()
    sr._RUST_KERNELS_OK = None  # force re-eval of kernel availability
    yield
    sr._cache.clear()
    sr._cache_ts.clear()
    sr._RUST_KERNELS_OK = None


def test_parity():
    """Seam #1: bit-identical 5-day parity (short-circuit path).

    Empty kline_minute dir triggers `_compute` line 408-409 `{"status":
    "no_data", "reason": "minute_missing", "kind": "concept"}`. Both
    `_py_compute` (Python ref) and `_orchestrate_with_kernels` (Rust path)
    return the same dict. Three-way assertion: orchestrator == _py_compute
    == expected.json.
    """
    from app.services import sector_rotation as sr

    repo = _MockRepo(FIXTURE_DIR)
    expected = json.loads(EXPECTED_JSON.read_text(encoding="utf-8"))

    actual = sr.build_sector_rotation(repo, kind="concept", bucket_minutes=5, sort_by="activity")
    py_ref = sr._py_compute(
        repo, FIXTURE_DIR,
        kind="concept", flow_field=None, top=30, bucket_minutes=5,
        series_names=None, exclude_sectors=(), auto_rows=10, sort_by="activity",
    )

    assert actual == expected, (
        f"orchestrator (Rust path) != expected.json:\n"
        f"  orchestrator: {actual}\n"
        f"  expected:     {expected}"
    )
    assert py_ref == expected, (
        f"_py_compute (Python ref) != expected.json:\n"
        f"  py_ref:  {py_ref}\n"
        f"  expected: {expected}"
    )
    assert actual == py_ref, (
        f"orchestrator (Rust path) != _py_compute (Python ref):\n"
        f"  orchestrator: {actual}\n"
        f"  py_ref:       {py_ref}"
    )
    assert actual["status"] == "no_data"
    assert actual["reason"] == "minute_missing"
    assert actual["kind"] == "concept"


def test_forced_import_error_fallback(monkeypatch):
    """Seam #2: forced `ImportError` fallback (Path A-3 T05 expansion).

    Simulates `tf_rs_indicators` being unavailable by patching
    `_rust_kernels_available` to return `False`. Adapter must still return the
    same dict as a direct `_py_compute` call — **no tautology** (the call goes
    through `build_sector_rotation`'s `if/else` dispatch, NOT through a
    pre-cached result).
    """
    from app.services import sector_rotation as sr

    # Patch the probe to simulate ImportError outcome (the actual
    # `_rust_kernels_available` body is a 4-line try/except that's covered by
    # inspection; this test verifies the adapter's `if/else` dispatch path).
    monkeypatch.setattr(sr, "_rust_kernels_available", lambda: False)
    assert sr._rust_kernels_available() is False

    repo = _MockRepo(FIXTURE_DIR)
    actual = sr.build_sector_rotation(repo, kind="concept", bucket_minutes=5, sort_by="activity")
    py_ref = sr._py_compute(
        repo, FIXTURE_DIR,
        kind="concept", flow_field=None, top=30, bucket_minutes=5,
        series_names=None, exclude_sectors=(), auto_rows=10, sort_by="activity",
    )

    assert actual == py_ref, (
        f"fallback path should still match _py_compute:\n"
        f"  actual: {actual}\n"
        f"  py_ref: {py_ref}"
    )
    assert actual["status"] == "no_data"


def test_graceful_no_op_triggers():
    """Seam #3: 3 graceful no-op triggers (per Spec 0004 §"Test plan").

    (a) empty kline_minute dir + valid bucket_minutes → `{"status": "no_data",
        "reason": "minute_missing", "kind": ...}` short-circuit.
    (b) out-of-range bucket_minutes (e.g. 999) → `ValueError` raised by the
        parameter validator at line 368-369 (`_ALLOWED_BUCKETS = (1, 5, 15)`).
    (c) invalid kind (e.g. "bogus") → `ValueError` raised by the kind
        validator at line 363-364 (`kind in ("concept", "industry")`).
    """
    from app.services import sector_rotation as sr

    repo = _MockRepo(FIXTURE_DIR)

    # (a) short-circuit
    out_a = sr.build_sector_rotation(repo, kind="concept", bucket_minutes=5)
    assert out_a["status"] == "no_data"
    assert out_a["reason"] == "minute_missing"
    assert out_a["kind"] == "concept"

    # (b) out-of-range bucket
    with pytest.raises(ValueError, match="不支持的分钟桶"):
        sr.build_sector_rotation(repo, kind="concept", bucket_minutes=999)

    # (c) invalid kind
    with pytest.raises(ValueError, match="不支持的板块维度"):
        sr.build_sector_rotation(repo, kind="bogus", bucket_minutes=5)


def test_dtype_preservation():
    """Seam #4: dtype preservation — 8 kernels each callable + bare_kernel smoke.

    Verifies that `tf_rs_indicators` exposes the 8 Path A-4 kernels from T02
    (per `cargo test --lib services_sector_rotation` 22/22 + wheel rebuild
    25.5 MB). Per-kernel dtype is verified in the Rust unit tests; here we
    verify the Python-facing symbols are present + `bare_kernel` returns
    `pl.Series` from a smoke call (no parquet needed).
    """
    import polars as pl
    import tf_rs_indicators as m

    expected_names = (
        "bare_kernel",
        "prev_daily_close_kernel",
        "minute_pcts_kernel",
        "sector_flow_kernel",
        "per_bucket_reduce_kernel",
        "timeline_kernel",
        "cross_events_kernel",
        "rank_for_mode_kernel",
    )
    for name in expected_names:
        assert hasattr(m, name), f"tf_rs_indicators is missing Path A-4 kernel: {name}"
        assert callable(getattr(m, name)), f"tf_rs_indicators.{name} is not callable"

    # bare_kernel smoke: returns pl.Series (declared output type per Spec 0004).
    s = m.bare_kernel("symbol")
    assert isinstance(s, pl.Series), (
        f"bare_kernel must return pl.Series per Spec 0004 §\"8 Rust kernels\".1; "
        f"got {type(s).__name__}"
    )


def test_cache_eviction(monkeypatch):
    """Seam #5: cache eviction semantics.

    Call 1 + 2 within TTL → same dict object (cache hit).
    Force TTL expiry (set `_CACHE_TTL = 0.0` + back-date `_cache_ts`) → call 3
    produces a fresh dict object. Both old + new dicts are content-equal but
    different objects.
    """
    from app.services import sector_rotation as sr

    repo = _MockRepo(FIXTURE_DIR)

    # Call 1: cache miss → compute → store
    result1 = sr.build_sector_rotation(repo, kind="concept", bucket_minutes=5, sort_by="activity")
    assert len(sr._cache) == 1, f"expected 1 cache entry after call 1, got {len(sr._cache)}"
    cache_key = next(iter(sr._cache.keys()))
    assert sr._cache[cache_key] is result1, (
        "call 1 should populate the cache with the returned dict"
    )

    # Call 2: cache hit (within TTL) → same dict object
    result2 = sr.build_sector_rotation(repo, kind="concept", bucket_minutes=5, sort_by="activity")
    assert result2 is result1, (
        "call 2 (within TTL) must hit the cache + return the same object"
    )

    # Force expiry: set _CACHE_TTL=0 AND back-date _cache_ts so the
    # (now - ts) >= TTL condition triggers a fresh compute.
    monkeypatch.setattr(sr, "_CACHE_TTL", 0.0)
    sr._cache_ts[cache_key] = time.monotonic() - 100.0  # way in the past

    # Call 3: cache miss → fresh compute → new dict object
    result3 = sr.build_sector_rotation(repo, kind="concept", bucket_minutes=5, sort_by="activity")
    assert result3 is not result1, (
        "after forced TTL expiry, call 3 must produce a fresh dict object "
        "(not the cached one)"
    )
    assert result3 == result1, (
        "the fresh dict must be content-equal to the original cached dict "
        "(orchestrator's no_data short-circuit return is value-deterministic)"
    )
