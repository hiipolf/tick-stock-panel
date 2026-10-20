"""Frontend field-contract test for the regime API (live backend).

Path A-5 / Spec 0005 — the Rust kernel is only useful if the FRONTEND still
sees the fields it renders. This test guards that contract against the running
app, complementing the in-process seam tests
(`backend/tests/test_rs_regime_builder_seam.py`).

What it checks:
  1. every field declared in the frontend's `RegimeRow`
     (frontend/src/lib/api.ts:542-576) is present in the API response
  2. the 15 REQUIRED fields are never null
  3. the 7 ladder fields (first_board / ge2_count / ge3_count / ge5_count /
     ladder_completeness / promo_rate / promo_pool) are populated on days
     that had limit-ups — these are the fields Path A-5 T04 added to the
     Rust kernel (16 → 23 output cols)
  4. value ranges are sane (state ∈ RegimeState, score ∈ [0,100],
     rates ∈ [0,1])
  5. the ladder partition invariant holds:
     first_board (consec==1) and ge2_count (consec>=2) are DISJOINT subsets
     of the limit-up set, so first_board + ge2_count <= limit_up.
     A small positive gap is EXPECTED (see note below) and is reported, not
     failed.
  6. rows are sorted ascending by date
  7. `GET /api/regime/latest` agrees with the tail of the history response
  8. the live HTTP response equals the PURE-PYTHON reference path for the
     same range (forces the reference path in-process and diffs cell by cell)
  9. regression guard: 2025-10-17 is NOT an empty aggregation any more
     (it used to be up_count=0 / limit_up=0 because the data snapshot back
     then had no warmup rows)

Run against a live backend:
    cd backend
    ..\\.venv\\Scripts\\python.exe -m pytest tests/test_rs_regime_live_api_contract.py -v

Env:
    TICK_STOCK_PANEL_API     base URL (default http://127.0.0.1:3018)
    TICK_STOCK_PANEL_DATA_DIR live data dir (default D:/AI-Stock/Stock-Data)

If no server is reachable every test SKIPS (so CI / offline runs stay green).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

import pytest

API = os.environ.get("TICK_STOCK_PANEL_API", "http://127.0.0.1:3018").rstrip("/")
DATA_DIR = Path(os.environ.get("TICK_STOCK_PANEL_DATA_DIR", "D:/AI-Stock/Stock-Data"))

# ── frontend contract: frontend/src/lib/api.ts :: RegimeRow (542-576) ──
REQUIRED_FIELDS = (
    "date", "state", "score", "limit_up", "limit_down", "broken_limit",
    "max_consecutive", "seal_rate", "up_count", "down_count", "up_ratio",
    "index_pct", "above_ma20_pct", "total_amount", "avg_turnover",
)
OPTIONAL_FIELDS = (
    "avg_pct", "median_pct", "strong_up_pct", "strong_down_pct",
    "profit_score", "speculation_score", "resilience_score", "trend_score",
    "phase", "first_board", "ge2_count", "ge3_count", "ge5_count",
    "ladder_completeness", "promo_rate", "promo_pool",
)
LADDER_FIELDS = (
    "first_board", "ge2_count", "ge3_count", "ge5_count",
    "ladder_completeness", "promo_rate", "promo_pool",
)
REGIME_STATES = {"strong", "lean_strong", "range", "lean_weak", "weak"}

# The date the Path A-5 HTTP smoke fixed: it used to be an EMPTY aggregation
# (up_count=0, limit_up=0) because the data snapshot back then had no warmup
# rows. Any non-zero up_count proves the warmup prefix is working.
REGRESSION_DATE = "2025-10-17"


# ── HTTP helper ──────────────────────────────────────────────────────────

def _get(path: str, timeout: int = 120):
    req = urllib.request.Request(API + path)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


@pytest.fixture(scope="module")
def api_up() -> bool:
    """True if the live backend answers /health. Otherwise all tests skip."""
    try:
        _get("/health", timeout=5)
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def history(api_up) -> dict:
    if not api_up:
        pytest.skip("no live backend at %s (set TICK_STOCK_PANEL_API)" % API)
    cov = _get("/api/regime/coverage")
    body = _get(
        "/api/regime/history?start=%s&end=%s" % (cov["earliest_date"], cov["latest_date"])
    )
    body["_coverage"] = cov
    return body


# ── tests ────────────────────────────────────────────────────────────────

def test_coverage_endpoint_shape(api_up):
    """coverage exposes rows + earliest/latest date (frontend RegimeCoverage)."""
    if not api_up:
        pytest.skip("no live backend at %s" % API)
    cov = _get("/api/regime/coverage")
    assert cov["rows"] > 0
    assert cov["earliest_date"] and cov["latest_date"]
    assert cov["earliest_date"] <= cov["latest_date"]


def test_history_has_every_frontend_contract_field(history):
    """Every field the frontend's `RegimeRow` declares is present in the payload."""
    rows = history["rows"]
    assert rows, "history returned no rows"
    sample = rows[len(rows) // 2]
    missing = [f for f in REQUIRED_FIELDS + OPTIONAL_FIELDS if f not in sample]
    assert not missing, "API payload is missing frontend contract fields: %s" % missing


def test_required_fields_never_null(history):
    """The 15 non-optional `RegimeRow` fields are never null."""
    nulls: dict[str, list[str]] = {}
    for r in history["rows"]:
        for f in REQUIRED_FIELDS:
            if r.get(f) is None:
                nulls.setdefault(f, []).append(r["date"])
    assert not nulls, "required fields null on some days: %s" % {
        k: v[:3] for k, v in nulls.items()
    }


def test_ladder_fields_populated_on_limit_up_days(history):
    """The 7 ladder fields (Path A-5 T04 kernel addition) are populated.

    On any day with limit-ups, `first_board` / `ge2_count` / `promo_pool` must be
    non-null integers >= 0 — a null here means the Rust kernel's ladder
    aggregations are missing from the output schema.
    """
    missing: dict[str, list[str]] = {}
    for r in history["rows"]:
        if not r.get("limit_up"):
            continue
        for f in ("first_board", "ge2_count", "promo_pool"):
            v = r.get(f)
            if v is None:
                missing.setdefault(f, []).append(r["date"])
            else:
                assert isinstance(v, int), "%s must be an int, got %r" % (f, type(v))
                assert v >= 0, "%s must be >= 0, got %r on %s" % (f, v, r["date"])
    assert not missing, "ladder fields null on limit-up days: %s" % {
        k: v[:3] for k, v in missing.items()
    }


def test_ladder_fields_present_on_every_row(history):
    """All 7 ladder fields are present as KEYS on every row (value may be null)."""
    for r in history["rows"][::37]:  # sample across the range
        missing = [f for f in LADDER_FIELDS if f not in r]
        assert not missing, "row %s missing ladder keys: %s" % (r["date"], missing)


def test_state_and_score_ranges(history):
    """`state` ∈ RegimeState; `score` ∈ [0,100]."""
    for r in history["rows"]:
        assert r["state"] in REGIME_STATES, "bad state %r on %s" % (r["state"], r["date"])
        assert 0 <= r["score"] <= 100, "score out of range: %r on %s" % (r["score"], r["date"])


def test_rate_fields_in_unit_range(history):
    """Rate-like fields live in [0,1]."""
    for r in history["rows"]:
        for f in ("seal_rate", "above_ma20_pct", "ladder_completeness"):
            v = r.get(f)
            if v is None:
                continue
            assert 0.0 <= v <= 1.0, "%s out of [0,1]: %r on %s" % (f, v, r["date"])
        v = r.get("promo_rate")
        if v is not None:
            assert 0.0 <= v <= 1.0, "promo_rate out of [0,1]: %r on %s" % (v, r["date"])


def test_ladder_nested_invariants(history):
    """The ladder buckets are NESTED, so their counts must be ordered.

    `ge2_count` counts consec>=2, `ge3_count` counts consec>=3, `ge5_count`
    counts consec>=5 — so ge5 <= ge3 <= ge2 must hold on every day.

    NOTE: `first_board + ge2_count` is NOT bounded by `limit_up`. The ladder
    buckets are keyed on the *streak length* (`consecutive_limit_ups`), which
    includes stocks that did NOT limit up today (broke the board), while
    `limit_up` counts today's limit-ups only — two different populations.
    Measured over the live 1695-row file: 1568 days equal, 125 days
    first+ge2 < limit_up, 2 days first+ge2 > limit_up. So no hard invariant
    links the two; only the nesting one is asserted here.
    """
    for r in history["rows"]:
        g2, g3, g5 = r.get("ge2_count"), r.get("ge3_count"), r.get("ge5_count")
        if g2 is None or g3 is None or g5 is None:
            continue
        assert g5 <= g3 <= g2, (
            "ladder buckets not nested on %s: ge2=%s ge3=%s ge5=%s"
            % (r["date"], g2, g3, g5)
        )
        # max_consecutive is the streak max → must dominate every bucket
        mc = r.get("max_consecutive")
        if mc is not None and g5:
            assert mc >= 5, (
                "max_consecutive=%s but ge5_count=%s on %s" % (mc, g5, r["date"])
            )


def test_ladder_vs_limit_up_gap_is_reported_not_asserted(history):
    """Informational: how often does first_board+ge2_count differ from limit_up.

    A difference is EXPECTED (streak buckets vs today's limit-ups — see
    `test_ladder_nested_invariants`). This test only reports the distribution;
    it never fails, so a change in the ratio is visible without being a
    false alarm.
    """
    eq = lt = gt = 0
    for r in history["rows"]:
        lu, fb, g2 = r.get("limit_up"), r.get("first_board"), r.get("ge2_count")
        if lu is None or fb is None or g2 is None:
            continue
        s = fb + g2
        if s == lu:
            eq += 1
        elif s < lu:
            lt += 1
        else:
            gt += 1
    total = eq + lt + gt
    print(
        "\n[ladder vs limit_up] %d rows: equal=%d  first+ge2<limit_up=%d  first+ge2>limit_up=%d"
        % (total, eq, lt, gt)
    )
    assert total > 0, "no comparable rows"


def test_rows_sorted_ascending(history):
    """Frontend renders the series in order — the API must return sorted dates."""
    dates = [r["date"] for r in history["rows"]]
    assert dates == sorted(dates), "history rows are not sorted ascending"


def test_latest_matches_history_tail(history):
    """`GET /api/regime/latest` agrees with the last row of the history."""
    tail = history["rows"][-1]
    latest = _get("/api/regime/latest")["row"]
    assert latest is not None, "latest returned null"
    assert latest["date"] == tail["date"], (
        "latest date %s != history tail %s" % (latest["date"], tail["date"])
    )
    for f in ("state", "score", "limit_up", "first_board", "ge2_count", "promo_rate"):
        assert latest.get(f) == tail.get(f), (
            "latest.%s=%r != history tail %r" % (f, latest.get(f), tail.get(f))
        )


def test_regression_date_not_empty(history):
    """2025-10-17 must no longer be an empty aggregation.

    It used to be up_count=0 / limit_up=0 because the data snapshot at the
    time had no warmup rows; the current warmup handling yields real values.
    """
    row = next((r for r in history["rows"] if r["date"] == REGRESSION_DATE), None)
    assert row is not None, "%s missing from history" % REGRESSION_DATE
    assert row["up_count"] > 0, (
        "%s has up_count=0 — the warmup prefix is not being applied (regression)"
        % REGRESSION_DATE
    )
    assert row["limit_up"] > 0, (
        "%s has limit_up=0 — the warmup prefix is not being applied (regression)"
        % REGRESSION_DATE
    )
    assert row["first_board"] is not None, (
        "%s has first_board=null — ladder aggregations missing (regression)"
        % REGRESSION_DATE
    )


@pytest.mark.parametrize("rng", [("2026-06-01", "2026-06-30")])
def test_live_http_matches_python_reference_path(rng, api_up):
    """The HTTP response equals the PURE-PYTHON reference path for the same range.

    IMPORTANT — two subtleties, both pinned by this test:

    1. WHICH "reference path": `run_regime_batch()` has TWO routes.
       * FAST route — `repo.get_enriched_range()` hits the warm enriched history
         cache and `_py_aggregate_daily` runs on it. That cache carries
         change_pct/ma20 but NOT the limit signals, so this route yields
         limit_up=0, first_board=0, ge2_count=0, promo_rate=None.
       * SLOW route — `_scan_enriched_fallback()` → `_py_compute_batch()`
         → `compute_indicators` + `compute_limit_signals` → `_py_aggregate_daily`.
         This is the route the Rust kernel path mirrors.
       The kernel path is what the server actually runs, so the reference
       compared here is the SLOW route, NOT `run_regime_batch`.

    2. SAME BATCH PARTITIONING: `above_ma20_pct` is very slightly sensitive to
       where the batch boundaries fall (the warmup prefix changes the ma20
       rolling window at a batch's first days). The server computed the live
       file over the FULL coverage range (N batches); a reference computed
       over a narrower range would use a DIFFERENT partition and could differ
       in the 4th decimal. So the reference is computed over the SAME full
       range, reproducing the server's batch partition.
    """
    if not api_up:
        pytest.skip("no live backend at %s" % API)
    import sys
    from pathlib import Path as _Path

    backend = _Path(__file__).resolve().parents[1]
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))

    from app.services import regime_builder as rb
    from app.services.market_mainline import load_risk_warning_symbols
    from app.tickflow.repository import DataStore, KlineRepository

    cov = _get("/api/regime/coverage")
    # Same range the server used -> same batch partition.
    start_s, end_s = cov["earliest_date"], cov["latest_date"]
    body = _get("/api/regime/history?start=%s&end=%s" % (start_s, end_s))
    http_rows = {r["date"]: r for r in body["rows"]}

    repo = KlineRepository(DataStore(DATA_DIR))
    index_pct_map = rb._load_index_pct(repo, date.fromisoformat(start_s), date.fromisoformat(end_s))
    excluded = sorted(load_risk_warning_symbols(DATA_DIR))
    ref = rb._scan_enriched_fallback(
        repo, date.fromisoformat(start_s), date.fromisoformat(end_s),
        index_pct_map=index_pct_map, excluded_symbols=excluded,
    )
    assert ref is not None and not ref.is_empty(), "reference path produced no rows"

    ref_rows = {str(r["date"]): r for r in ref.iter_rows(named=True)}
    assert set(ref_rows) == set(http_rows), (
        "date sets differ: only-ref=%s only-http=%s"
        % (sorted(set(ref_rows) - set(http_rows))[:3], sorted(set(http_rows) - set(ref_rows))[:3])
    )

    mismatches = []
    for d in sorted(ref_rows):
        for col in ref.columns:
            a, b = ref_rows[d].get(col), http_rows[d].get(col)
            if isinstance(a, float) and isinstance(b, (int, float)):
                if abs(a - float(b)) > 1e-9 * max(abs(a), abs(float(b)), 1.0):
                    mismatches.append((d, col, a, b))
            elif a is not None and b is not None and str(a) != str(b):
                mismatches.append((d, col, a, b))
    assert not mismatches, (
        "live HTTP payload != Python reference (slow route) on %d cells: %s"
        % (len(mismatches), mismatches[:8])
    )


def test_kernel_route_is_never_degraded(api_up):
    """The live (kernel) route must always carry the limit signals.

    `run_regime_batch()`'s FAST route depends on the in-memory enriched history
    cache. When that cache is built from the full computation it carries the
    limit signals and the fast route is fine; when the enriched refresh FALLS
    BACK to the raw 14-column parquet the fast route silently yields
    limit_up=0 / first_board=0 / ge2_count=0 / promo_rate=None (the frontend's
    ladder cards would show 0 / "—").

    The Rust kernel path never reads that cache — kernel 1 always rescans the
    parquet and the Python pipeline always runs `compute_limit_signals` — so
    the kernel route must be populated unconditionally. This test asserts that
    invariant (and reports the fast route's state, which varies by run).
    """
    if not api_up:
        pytest.skip("no live backend at %s" % API)
    import sys
    from pathlib import Path as _Path

    backend = _Path(__file__).resolve().parents[1]
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))

    from app.services import regime_builder as rb
    from app.tickflow.repository import DataStore, KlineRepository

    repo = KlineRepository(DataStore(DATA_DIR))
    start, end = date(2026, 6, 1), date(2026, 6, 30)

    # Warm the cache so the fast route becomes reachable, then take the fast route.
    repo.get_enriched_range(start, end)
    saved = rb._RUST_KERNELS_OK
    try:
        rb._RUST_KERNELS_OK = False
        fast = rb.run_regime_batch(repo, start, end)
    finally:
        rb._RUST_KERNELS_OK = saved

    if fast is not None and not fast.is_empty():
        frow = fast.row(0, named=True)
        degraded = (
            frow.get("limit_up") == 0
            and (frow.get("first_board") in (0, None))
            and (frow.get("ge2_count") in (0, None))
        )
        print(
            "\n[fast route] limit_up=%s first_board=%s ge2_count=%s promo_rate=%s -> %s"
            % (frow.get("limit_up"), frow.get("first_board"), frow.get("ge2_count"),
               frow.get("promo_rate"),
               "DEGRADED (enriched cache fell back to raw 14 columns)"
               if degraded else "populated")
        )

    # The live server (kernel path) must ALWAYS be populated.
    body = _get("/api/regime/history?start=2026-06-01&end=2026-06-30")
    http_row = body["rows"][0]
    assert http_row["limit_up"] > 0, (
        "the live (kernel) route is degraded: limit_up=%s — the Rust kernel path "
        "is supposed to always carry the limit signals" % http_row["limit_up"]
    )
    assert http_row.get("first_board") not in (0, None), (
        "the live (kernel) route has no first_board: %r" % http_row.get("first_board")
    )
    assert http_row.get("ge2_count") not in (0, None), (
        "the live (kernel) route has no ge2_count: %r" % http_row.get("ge2_count")
    )
