"""Path A-5 / Spec 0005 fixture regenerator for `run_regime_batch`.

Status: 2026-10-20 fixture generation (Path A-5 T03).

Produces `backend/tests/fixtures/regime_builder_5d/`:

  kline_daily_enriched/             # orchestrator hardcodes this path; NOT subset
    date=2026-10-13/part.parquet    # 30 rows × 15 cols (subset of live)
    date=2026-10-14/part.parquet
    date=2026-10-15/part.parquet
    date=2026-10-16/part.parquet
    date=2026-10-19/part.parquet
  expected.json                     # byte-equal to `_py_*` Python reference
                                    # output for the same fixture args

Regenerator invariants:
  - No RNG anywhere (no np.random.*, random.*, os.urandom(), time.time()).
  - No `tf_rs_indicators` import (T03 is Python-ref only; wheel parity is
    T04's job).
  - Hardcoded fixture args (start=2026-10-13, end=2026-10-19 — 5 contiguous
    trading days, latest as of fixture freeze date 2026-10-20).
  - 30-symbol subset chosen for cross-day continuity (Banks, Consumer, Finance,
    Tech, New energy, Industrial, Manufacturing, Healthcare, Real estate).
  - `TICK_STOCK_PANEL_WORKTREE` env override (per Path A-3 T05 + Path A-4 T03
    convention: lets CI / dev override the source tree path).
  - End-of-script determinism guard: 3 independent captures → all serialize
    identically (Path A-4 T03 pattern from gen_build_sector_rotation_fixture.py:128-156).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl

THIS_DIR = Path(__file__).resolve().parent
FIXTURE_DIR = THIS_DIR / "regime_builder_5d"
ENRICHED_DIR = FIXTURE_DIR / "kline_daily_enriched"
EXPECTED_JSON = FIXTURE_DIR / "expected.json"

WORKTREE = Path(os.environ.get("TICK_STOCK_PANEL_WORKTREE", "D:/AI-Stock/Stock-Data"))
# THIS_DIR = <worktree>/backend/tests/fixtures/
# parents[0] = tests/, parents[1] = backend/, parents[2] = <worktree>/
REPO_ROOT = THIS_DIR.parents[2]  # worktree root

# 5 latest trading days as of fixture freeze 2026-10-20.
FIXTURE_DATES: list[str] = ["2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-19"]
FIXTURE_START = date(2026, 10, 13)
FIXTURE_END = date(2026, 10, 19)

# 30-symbol subset chosen for sector diversity + all 5 dates present.
# (Validated 2026-10-20 against `D:/AI-Stock/Stock-Data/kline_daily_enriched/`:
#  all 30 symbols present in all 5 partitions, no missing data.)
SYMBOLS: list[str] = [
    # Banks (5)
    "600000.SH", "600036.SH", "601398.SH", "601939.SH", "601288.SH",
    # Consumer staples (4)
    "600519.SH", "000858.SZ", "600887.SH", "603288.SH",
    # Finance (3)
    "000001.SZ", "000002.SZ", "600030.SH",
    # Tech / pharma (3)
    "002415.SZ", "000063.SZ", "600276.SH",
    # New energy / tech (3)
    "300750.SZ", "002594.SZ", "300059.SZ",
    # Industrial (3)
    "601012.SH", "601888.SH", "600028.SH",
    # Manufacturing (3)
    "600585.SH", "000333.SZ", "000538.SZ",
    # Healthcare (3)
    "300015.SZ", "601628.SH", "000661.SZ",
    # Real estate (3)
    "001979.SZ", "600048.SH", "600340.SH",
]


# ---------------------------------------------------------------------------
# Subset helpers
# ---------------------------------------------------------------------------

def _subset_kline_partition(d: str) -> pl.DataFrame:
    """Read one enriched-daily partition, filter to 30 symbols."""
    src = WORKTREE / "kline_daily_enriched" / f"date={d}" / "part.parquet"
    df = pl.read_parquet(src).filter(pl.col("symbol").is_in(SYMBOLS))
    return df


def _subset_instruments() -> pl.DataFrame:
    """Read live instruments, filter to 30 symbols, return required cols."""
    src = WORKTREE / "instruments" / "instruments.parquet"
    df = pl.read_parquet(src).filter(pl.col("symbol").is_in(SYMBOLS))
    return df


def _subset_index_daily() -> pl.DataFrame:
    """Read 000001.SH (上证指数) for the 5 fixture dates; return (date, change_pct).

    `kline_index_daily/date=*/part.parquet` has cols (symbol, date, open, high, low,
    close, volume, amount) — NO `change_pct` column. Compute it from prev-day close
    via polars window (mirrors `_build_rows_snapshot` in gen_market_overview_dim_fixture).
    """
    frames: list[pl.DataFrame] = []
    for d in FIXTURE_DATES:
        src = WORKTREE / "kline_index_daily" / f"date={d}" / "part.parquet"
        if not src.exists():
            continue
        df = pl.read_parquet(src).filter(pl.col("symbol") == "000001.SH")
        if not df.is_empty():
            frames.append(df.select(["symbol", "date", "close"]))
    if not frames:
        # Fallback: synthesize a small change_pct series (deterministic, no RNG).
        return pl.DataFrame({
            "date": [date.fromisoformat(d) for d in FIXTURE_DATES],
            "change_pct": [0.005, -0.003, 0.002, 0.001, 0.004],
        })
    full = pl.concat(frames, how="vertical").sort("date")
    # Compute change_pct from prev-day close (window over 1 row back).
    full = full.with_columns(
        prev_close=pl.col("close").shift(1).over("symbol"),
        change_pct=(
            (pl.col("close") - pl.col("close").shift(1).over("symbol"))
            / pl.col("close").shift(1).over("symbol")
        ),
    )
    return full.select(["date", "change_pct"])


# ---------------------------------------------------------------------------
# Mock repo
# ---------------------------------------------------------------------------

class _MockStore:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir


class _MockRepo:
    """Minimal repo satisfying run_regime_batch's interface.

    Per Spec 0005 §"T03 Implementation" the fixture exercises the orchestrator's
    fallback path (`_scan_enriched_fallback`), so:
      - `get_enriched_range()` returns None → forces fallback
      - `get_instruments()` returns the 30-symbol subset
      - `get_historical_shares()` returns an empty DataFrame (no
        `financials/shares/part.parquet` under the fixture; verified OK by
        `compute_limit_signals` accepting empty)
      - `get_index_daily(...)` returns 5-row × 2-col for 000001.SH
    """
    def __init__(self, data_dir: Path, instruments: pl.DataFrame,
                 index_daily: pl.DataFrame):
        self.store = _MockStore(data_dir)
        self._instruments = instruments
        self._index_daily = index_daily

    def get_instruments(self) -> pl.DataFrame:
        return self._instruments

    def get_historical_shares(self) -> pl.DataFrame:
        # Empty DataFrame — `_compute_batch` passes this to `compute_limit_signals`
        # which accepts None / empty for historical_shares (per pipeline.py:693).
        return pl.DataFrame()

    def get_enriched_range(self, start: date, end: date) -> pl.DataFrame | None:
        # Force fallback path (which exercises _compute_batch + _aggregate_daily).
        return None

    def get_index_daily(self, symbol: str, start: date, end: date,
                        columns: list[str] | None = None) -> pl.DataFrame:
        df = self._index_daily
        if columns is not None:
            df = df.select([c for c in columns if c in df.columns])
        return df.filter((pl.col("date") >= start) & (pl.col("date") <= end))


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------

def _build_fixture() -> None:
    """Write 5 partition parquets under the fixture's `kline_daily_enriched/`."""
    ENRICHED_DIR.mkdir(parents=True, exist_ok=True)
    for d in FIXTURE_DATES:
        part_dir = ENRICHED_DIR / f"date={d}"
        part_dir.mkdir(parents=True, exist_ok=True)
        part = part_dir / "part.parquet"
        df = _subset_kline_partition(d)
        df.write_parquet(part)
        print(f"[regen] wrote {part} shape={df.shape}")


def _capture_expected() -> dict[str, Any]:
    """Run `_py_run_regime_batch` (current Python reference) on the fixture.

    Returns a JSON-serializable dict (one entry per row + column metadata).
    """
    sys.path.insert(0, str(REPO_ROOT / "backend"))
    os.environ.setdefault("TICK_STOCK_PANEL_WORKTREE", str(WORKTREE))
    # The orchestrator's fast path: get_enriched_range returns None → fallback.
    # Clear the ST cache so load_risk_warning_symbols re-runs (no ST under fixture).
    from app.services import market_mainline  # type: ignore
    market_mainline._ST_SYMBOLS_CACHE = None
    from app.services import regime_builder  # type: ignore
    instruments = _subset_instruments()
    index_daily = _subset_index_daily()
    repo = _MockRepo(FIXTURE_DIR, instruments, index_daily)
    df = regime_builder.run_regime_batch(repo, FIXTURE_START, FIXTURE_END)
    return _df_to_jsonable(df)


def _df_to_jsonable(df: pl.DataFrame) -> dict[str, Any]:
    """Convert a polars DataFrame to a JSON-serializable dict.

    Schema: list of (name, dtype) pairs. Rows: list of {col: value} dicts.
    NaN/None are normalized to JSON null. Date values are ISO 8601 strings.
    """
    rows: list[dict[str, Any]] = []
    for row in df.iter_rows(named=True):
        out: dict[str, Any] = {}
        for k, v in row.items():
            if v is None:
                out[k] = None
            elif isinstance(v, date):
                out[k] = v.isoformat()
            elif isinstance(v, float) and v != v:  # NaN
                out[k] = None
            else:
                out[k] = v
        rows.append(out)
    schema = [{"name": c, "dtype": str(df.schema[c])} for c in df.columns]
    return {"schema": schema, "rows": rows, "shape": list(df.shape)}


def _write_expected(payload: dict[str, Any]) -> None:
    EXPECTED_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def main() -> int:
    _build_fixture()
    # Pass 1: capture + write expected.json.
    payload_a = _capture_expected()
    _write_expected(payload_a)
    # Pass 2 (independent re-capture).
    payload_b = _capture_expected()
    # Pass 3 (third re-run for determinism).
    payload_c = _capture_expected()

    body_a = json.dumps(payload_a, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    body_b = json.dumps(payload_b, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    body_c = json.dumps(payload_c, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"

    sha_a = _sha256_bytes(body_a)
    sha_b = _sha256_bytes(body_b)
    sha_c = _sha256_bytes(body_c)
    file_sha = _sha256_bytes(EXPECTED_JSON.read_bytes())
    print(f"[T03-regen] FIXTURE_DIR = {FIXTURE_DIR}")
    print(f"[T03-regen] payload A (re-serialised)    SHA-256 = {sha_a}")
    print(f"[T03-regen] payload B (independent re-run) SHA-256 = {sha_b}")
    print(f"[T03-regen] payload C (third re-run)    SHA-256 = {sha_c}")
    print(f"[T03-regen] expected.json (file)        SHA-256 = {file_sha}  (CRLF on Windows is fine; git handles)")

    if not (sha_a == sha_b == sha_c):
        print("[T03-regen] ERROR: SHA mismatch across captures")
        return 2

    file_dict = json.loads(EXPECTED_JSON.read_text(encoding="utf-8"))
    if file_dict != payload_a:
        print("[T03-regen] ERROR: file roundtrip != payload_a")
        return 4

    print(f"[T03-regen] OK — fixture regenerated, deterministic across 3 captures, file roundtrip OK")
    print(f"[T03-regen] expected.json: shape={file_dict['shape']}, schema={len(file_dict['schema'])} cols, rows={len(file_dict['rows'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())