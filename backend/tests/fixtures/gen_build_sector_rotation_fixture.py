"""Path A-4 / Spec 0004 fixture regenerator for `build_sector_rotation`.

Status: 2026-10-19 fixture generation (Path A-4 T03).

Produces `backend/tests/fixtures/build_sector_rotation_5d/`:

  kline_minute/
    .gitkeep                 # intentionally empty — captures the
                             # `_compute` short-circuit path at line 408-409
                             # of app/services/sector_rotation.py
  expected.json              # byte-equal to the `_py_*` Python reference
                             # output for the same fixture args

Regenerator invariants:
  - No RNG anywhere (no np.random.*, random.*, os.urandom(), time.time()).
  - No `tf_rs_indicators` import (T03 is Python-ref only; wheel parity is
    T04's job).
  - Hardcoded fixture args (kind="concept", bucket_minutes=5, sort_by="activity",
    etc. — fixed by the test author of the T04 parity test).
  - `TICK_STOCK_PANEL_WORKTREE` env override (per Path A-3 T05 amendment —
    lets CI / dev rewrite the source tree path).
  - End-of-script determinism guard: 2 independent `_compute` calls + sort_keys
    serialization → SHA-256 must match.

**Empirical divergence flag (T05 closure to amend Spec 0004)**:
`D:/AI-Stock/Stock-Data/kline_minute/` is empty in the current environment
(MinuteRefreshService hasn't run to populate minute parquet partitions).
Therefore the spec's "5 trading days × 30 symbols = ~36K minute bars"
fixture plan cannot be replicated in this worktree. T03 captures the
short-circuit path of `_compute` instead. The Rust adapter (T04) must
also return the same short-circuit dict, validating parity for that
specific path. T05 will amend Spec 0004 §"Fixture (per Q9=b)" wording
to acknowledge this divergence.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
FIXTURE_DIR = THIS_DIR / "build_sector_rotation_5d"
EXPECTED_JSON = FIXTURE_DIR / "expected.json"
KLINE_MINUTE_DIR = FIXTURE_DIR / "kline_minute"
GITKEEP = KLINE_MINUTE_DIR / ".gitkeep"

WORKTREE = Path(os.environ.get("TICK_STOCK_PANEL_WORKTREE", "D:/AI-Stock/Stock-Data"))
REPO_ROOT = THIS_DIR.parents[3]  # .../backend/tests/fixtures  → backend  → repo root

# Mirror Path A-3 `gen_market_overview_dim_fixture.py` hard-coded argument shape
# (no RNG; fixture args are an explicit test contract, NOT user-supplied).
FIXTURE_KIND = "concept"
FIXTURE_FLOW_FIELD = None
FIXTURE_TOP = 30
FIXTURE_BUCKET_MINUTES = 5
FIXTURE_SERIES_NAMES: list[str] | None = None
FIXTURE_EXCLUDE_SECTORS: tuple[str, ...] = ()
FIXTURE_AUTO_ROWS = 10
FIXTURE_SORT_BY = "activity"


class _MockStore:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir


class _MockRepo:
    def __init__(self, data_dir: Path):
        self.store = _MockStore(data_dir)


def _ensure_skeleton() -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    KLINE_MINUTE_DIR.mkdir(exist_ok=True)
    GITKEEP.write_text(
        "intentionally empty — T03 fixture captures build_sector_rotation's\n"
        "`minute_missing` short-circuit. _compute returns\n"
        '{"status": "no_data", "reason": "minute_missing", "kind": ...}\n'
        "when no `date=*/part.parquet` partitions exist under kline_minute/.\n"
        "See Spec 0004 §\"Fixture (per Q9=b)\" T05 closure amendment note.\n",
        encoding="utf-8",
    )


def _capture_expected() -> dict:
    sys.path.insert(0, str(REPO_ROOT / "backend"))
    os.environ.setdefault("TICK_STOCK_PANEL_WORKTREE", str(WORKTREE))
    from app.services.sector_rotation import _compute, _cache  # type: ignore
    _cache.clear()
    repo = _MockRepo(FIXTURE_DIR)
    return _compute(
        repo,
        FIXTURE_DIR,
        kind=FIXTURE_KIND,
        flow_field=FIXTURE_FLOW_FIELD,
        top=FIXTURE_TOP,
        bucket_minutes=FIXTURE_BUCKET_MINUTES,
        series_names=FIXTURE_SERIES_NAMES,
        exclude_sectors=FIXTURE_EXCLUDE_SECTORS,
        auto_rows=FIXTURE_AUTO_ROWS,
        sort_by=FIXTURE_SORT_BY,
    )


def _write_expected(payload: dict) -> None:
    EXPECTED_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def main() -> int:
    _ensure_skeleton()
    # Pass 1: capture + write expected.json.
    payload_a = _capture_expected()
    _write_expected(payload_a)
    # Pass 2 (independent re-capture): same hard-coded args, fresh dict ordering.
    payload_b = _capture_expected()

    body_a = json.dumps(payload_a, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    body_b = json.dumps(payload_b, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    body_c = json.dumps(_capture_expected(), ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"

    sha_a = _sha256_bytes(body_a)
    sha_b = _sha256_bytes(body_b)
    sha_c = _sha256_bytes(body_c)
    file_sha = _sha256_bytes(EXPECTED_JSON.read_bytes())
    print(f"[T03-regen] FIXTURE_DIR = {FIXTURE_DIR}")
    print(f"[T03-regen] payload A (re-serialised)    SHA-256 = {sha_a}")
    print(f"[T03-regen] payload B (independent re-run) SHA-256 = {sha_b}")
    print(f"[T03-regen] payload C (third re-run)    SHA-256 = {sha_c}")
    print(f"[T03-regen] expected.json (file)        SHA-256 = {file_sha}  (CRLF on Windows is fine; git handles)")

    # Determinism guard: 3 captures must all serialize identically.
    if not (sha_a == sha_b == sha_c):
        print(f"[T03-regen] ERROR: SHA mismatch across captures")
        return 2

    # File roundtrip guard: file bytes (whatever line ending git chose) hash differently
    # than LF-only serialized bytes; that's expected on Windows CRLF. Verify the file
    # parses back to the same dict (regardless of line endings).
    import json as _json
    file_dict = _json.loads(EXPECTED_JSON.read_text(encoding="utf-8"))
    if file_dict != payload_a:
        print(f"[T03-regen] ERROR: file roundtrip != payload_a")
        print(f"  file:    {file_dict}")
        print(f"  payload: {payload_a}")
        return 4

    print("[T03-regen] OK — fixture regenerated, deterministic across 3 captures, file roundtrip OK")
    print(EXPECTED_JSON.read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
