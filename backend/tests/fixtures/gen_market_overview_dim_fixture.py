"""Generate the market_overview_dim_240d fixture + 3 expected.json baselines.

Path A-3 / Ticket 03 fixture. Subsets the live `D:/AI-Stock/Stock-Data` tree
into a self-contained directory under `backend/tests/fixtures/` and captures
`_py_*` outputs as bit-identical expected JSON for the Rust kernel parity test.

**Deterministic** by construction — there is no randomness in this regenerator:
- 30 symbols hard-coded below (diverse sectors; all present in ext_hy_ths,
  ext_gn_ths, and kline_daily_enriched)
- Last 240 trading days of kline (sorted desc, take 240 from end)
- Ext parquets filtered to the 30 symbols (preserves all 5 columns)
- ext config.json copied verbatim (config schema frozen)
- expected.json captured by running `_py_*` on the fixture's rows

**Run once** to (re)generate; fixture then committed and frozen forever.
The Rust-kernel test (Ticket 04) compares adapter output against this expected.json.

Run:
    python gen_market_overview_dim_fixture.py

This overwrites:
    backend/tests/fixtures/market_overview_dim_240d/ext_data/{ext_hy_ths,ext_gn_ths}/{config.json,part.parquet}
    backend/tests/fixtures/market_overview_dim_240d/kline_240d.parquet
    backend/tests/fixtures/market_overview_dim_240d/rows_snapshot.json
    backend/tests/fixtures/market_overview_dim_240d/{dimension_rank,top_rows,pct_band_rows}.expected.json

Path A-1/2 used `random.seed(42)` because their fixtures needed a *synthetic*
kline generator; Path A-3 subsets *real* kline data so determinism comes from
the hard-coded symbol list + the immutable source tree.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import polars as pl

WORKTREE = Path(
    os.environ.get("TICK_STOCK_PANEL_WORKTREE", r"D:\AI-Data\路线分析\tmp_cmp\tick-stock-panel-rust-kernel")
)
sys.path.insert(0, str(WORKTREE / "backend"))

LIVE_DATA_DIR = Path(r"D:/AI-Stock/Stock-Data")
FIXTURE_DIR = WORKTREE / "backend" / "tests" / "fixtures" / "market_overview_dim_240d"
EXT_HY_FIXTURE = FIXTURE_DIR / "ext_data" / "ext_hy_ths"
EXT_GN_FIXTURE = FIXTURE_DIR / "ext_data" / "ext_gn_ths"
KLINE_FIXTURE = FIXTURE_DIR / "kline_240d.parquet"
ROWS_SNAPSHOT = FIXTURE_DIR / "rows_snapshot.json"
DIM_RANK_EXPECTED = FIXTURE_DIR / "dimension_rank.expected.json"
TOP_ROWS_EXPECTED = FIXTURE_DIR / "top_rows.expected.json"
PCT_BAND_EXPECTED = FIXTURE_DIR / "pct_band_rows.expected.json"

# Hard-coded 38-symbol subset — diverse sectors with some industries
# having 2-3 stocks so the per-group reduce (avg_pct, up_count, down_count,
# leader selection) is meaningfully exercised. All present in ext_hy_ths,
# ext_gn_ths, and kline_daily_enriched at the regenerator's last-write time.
# Validated by hand-pick from the live data (5,562 symbols common to all 3 sources).
SYMBOLS: list[str] = [
    # Banks (6) — exercises avg_pct with multiple stocks per group
    "600000.SH", "600036.SH", "601398.SH", "601939.SH", "601288.SH", "601988.SH",
    # Consumer staples (5) — 白色家电 has 2, 食品 has 2, 乳品 has 1
    "600519.SH", "000858.SZ", "600887.SH", "603288.SH", "000651.SZ",
    # Finance (4)
    "000001.SZ", "000002.SZ", "600030.SH", "601318.SH",
    # Tech / pharma (4)
    "002415.SZ", "000063.SZ", "600276.SH", "600196.SH",
    # 新能源 / tech (4)
    "300750.SZ", "002594.SZ", "300059.SZ", "000725.SZ",
    # 中字头 / industrial (4)
    "601012.SH", "601888.SH", "600028.SH", "600050.SH",
    # Manufacturing (4)
    "600585.SH", "000333.SZ", "600690.SH", "000538.SZ",
    # Healthcare / insurance (4) — 医药 has 3
    "300015.SZ", "601628.SH", "000661.SZ", "600436.SH",
    # Real estate (3) — 房地产 has 3
    "001979.SZ", "600048.SH", "600340.SH",
]

N_DAYS = 240


def _subset_kline() -> pl.DataFrame:
    """Concatenate the last 240 trading days of kline for the 30 symbols."""
    date_dirs = sorted((LIVE_DATA_DIR / "kline_daily_enriched").glob("date=*"))[-N_DAYS:]
    print(f"[regen] reading {len(date_dirs)} trading days of kline...")
    frames: list[pl.DataFrame] = []
    for d in date_dirs:
        f = d / "part.parquet"
        df = pl.read_parquet(f).filter(pl.col("symbol").is_in(SYMBOLS))
        frames.append(df)
    full = pl.concat(frames, how="vertical")
    full = full.sort(["symbol", "date"])
    print(f"[regen] kline shape: {full.shape}, dates: {full['date'].min()}..{full['date'].max()}")
    return full


def _subset_ext_hy() -> pl.DataFrame:
    """Subset ext_hy_ths to 30 symbols; preserve the GBK-encoded Chinese columns."""
    df = pl.read_parquet(LIVE_DATA_DIR / "ext_data" / "ext_hy_ths" / "part.parquet")
    sub = df.filter(pl.col("symbol").is_in(SYMBOLS))
    print(f"[regen] ext_hy_ths: {df.shape} -> {sub.shape}")
    return sub


def _subset_ext_gn() -> pl.DataFrame:
    """Subset ext_gn_ths to 30 symbols."""
    df = pl.read_parquet(LIVE_DATA_DIR / "ext_data" / "ext_gn_ths" / "part.parquet")
    sub = df.filter(pl.col("symbol").is_in(SYMBOLS))
    print(f"[regen] ext_gn_ths: {df.shape} -> {sub.shape}")
    return sub


def _copy_config(src: Path, dst: Path) -> None:
    """Copy config.json verbatim — config schema is frozen by upstream."""
    shutil.copy2(src, dst / "config.json")
    print(f"[regen] copied config: {dst / 'config.json'}")


def _build_rows_snapshot(kline: pl.DataFrame) -> list[dict]:
    """Build the `rows: list[dict]` snapshot from the latest kline date for the 30 symbols.

    Mirrors `build_market_overview`'s row shape — `{symbol, name, close, change_pct, amount,
    turnover_rate, ...}`. The caller (test in Ticket 04) feeds this to the kernel.

    `change_pct` is computed as `(close_today - close_yesterday) / close_yesterday`,
    not from `raw_close` (which is the close *before* adjustment factors, not the
    previous trading day's close).
    """
    # Compute per-symbol prev_close via window.
    enriched = kline.sort(["symbol", "date"]).with_columns(
        prev_close=pl.col("close").shift(1).over("symbol")
    )
    latest_date = enriched["date"].max()
    latest_df = enriched.filter(pl.col("date") == latest_date)
    rows = []
    for row in latest_df.iter_rows(named=True):
        close = row.get("close")
        prev_close = row.get("prev_close")
        if close is None or prev_close is None or prev_close == 0:
            change_pct = None
        else:
            change_pct = (close - prev_close) / prev_close
        rows.append({
            "symbol": row.get("symbol"),
            "name": None,
            "close": close,
            "change_pct": change_pct,
            "amount": row.get("amount"),
            "turnover_rate": row.get("turnover_rate"),
        })
    rows.sort(key=lambda r: r["symbol"] or "")
    print(f"[regen] rows_snapshot: {len(rows)} rows (date={latest_date})")
    return rows


def _capture_expected(rows: list[dict], fixture_dir: Path) -> None:
    """Run `_py_*` references on the fixture's rows; write bit-identical expected.json.

    Mirrors what `build_market_overview._py_*` would produce on this fixture.
    """
    # `repo` is a stub — `_py_dimension_rank` calls `ExtConfigStore(repo.store.data_dir)`
    # + `_read_ext_rows(...)`. We provide a real `Path` for `data_dir`.
    class _Repo:
        class _Store:
            data_dir: Path = fixture_dir
        store = _Store()
    repo = _Repo()

    # Lazy import (the regex + helpers load a lot of vendor code).
    # Use the **current** Python names (`_dimension_rank` etc.). Ticket 04 will
    # rename these to `_py_*` when wiring the adapter; the captured expected.json
    # is bit-identical either way (the output dict shape is what matters).
    from app.services.market_overview_builder import (
        _dimension_rank,
        _top_rows,
        _pct_band_rows,
    )

    dim = _dimension_rank(rows, repo, "industry", 5, level=None)
    DIM_RANK_EXPECTED.write_text(
        json.dumps(dim, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(f"[regen] wrote {DIM_RANK_EXPECTED.name}: leading={len(dim['leading'])} lagging={len(dim['lagging'])}")

    top = _top_rows(rows, "change_pct", True, 8)
    TOP_ROWS_EXPECTED.write_text(
        json.dumps(top, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(f"[regen] wrote {TOP_ROWS_EXPECTED.name}: {len(top)} rows")

    values = [row["change_pct"] for row in rows if row.get("change_pct") is not None]
    pct = _pct_band_rows(values)
    PCT_BAND_EXPECTED.write_text(
        json.dumps(pct, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(f"[regen] wrote {PCT_BAND_EXPECTED.name}: {len(pct)} bands (n_values={len(values)})")


def main() -> None:
    # 1. Subset kline + ext_parquets + copy configs.
    kline = _subset_kline()
    kline.write_parquet(KLINE_FIXTURE)
    print(f"[regen] wrote {KLINE_FIXTURE}")

    _copy_config(LIVE_DATA_DIR / "ext_data" / "ext_hy_ths" / "config.json", EXT_HY_FIXTURE)
    _copy_config(LIVE_DATA_DIR / "ext_data" / "ext_gn_ths" / "config.json", EXT_GN_FIXTURE)

    hy = _subset_ext_hy()
    hy.write_parquet(EXT_HY_FIXTURE / "part.parquet")
    print(f"[regen] wrote {EXT_HY_FIXTURE / 'part.parquet'}")

    gn = _subset_ext_gn()
    gn.write_parquet(EXT_GN_FIXTURE / "part.parquet")
    print(f"[regen] wrote {EXT_GN_FIXTURE / 'part.parquet'}")

    # 2. Build rows snapshot + capture expected.json.
    rows = _build_rows_snapshot(kline)
    ROWS_SNAPSHOT.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[regen] wrote {ROWS_SNAPSHOT}")

    _capture_expected(rows, FIXTURE_DIR)
    print("[regen] done.")


if __name__ == "__main__":
    main()