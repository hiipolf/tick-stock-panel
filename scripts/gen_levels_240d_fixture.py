"""One-shot fixture generator for the `_support_resistance` parity test.

Run once, commit the two outputs (parquet + JSON), delete this script if
you want; the test loads the committed fixture, not this generator.

Output:
  backend/tests/fixtures/levels_240d.parquet        -- 240-row synthetic OHLCV
  backend/tests/fixtures/levels_240d.expected.json  -- expected list[dict] from
                                                       the pure-Python reference

Deterministic via `random.seed(42)`. Re-runs produce identical bytes; any
deviation means the seed was changed (or the reference impl was — which
should fail parity first, this is the secondary check).
"""
from __future__ import annotations

import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path

import polars as pl

# Repo root = scripts/.. = the rust-kernel worktree
ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "backend" / "tests" / "fixtures"
FIX.mkdir(parents=True, exist_ok=True)
PARQUET = FIX / "levels_240d.parquet"
JSON_OUT = FIX / "levels_240d.expected.json"

# Use the pure-Python reference (post Ticket 03 rename) to compute expected.
sys.path.insert(0, str(ROOT / "backend"))
from app.indicators.levels import _py_support_resistance  # noqa: E402

N = 240
SEED = 42
DRIFT = 0.02          # price drift per day
VOL = 0.15            # price std per day
PRICE0 = 10.0
BIN_COUNT = 40

rng = random.Random(SEED)

# 240 consecutive calendar days, ending today. The Python implementation
# doesn't care about weekends; this is a 1-D time series.
end = date.today()
dates = [end - timedelta(days=N - 1 - i) for i in range(N)]

# Random walk: log-returns ≈ Normal(drift, vol).
prices = [PRICE0]
for _ in range(N - 1):
    z = rng.gauss(DRIFT, VOL)
    prices.append(prices[-1] * (1.0 + z))

# Build OHLCV. Open = prev close; close = last tick; high = max; low = min.
# Volume in [1e5, 1e7]; turnover_rate in [0, 5] percent.
rows = []
prev_close = prices[0]
for i, p in enumerate(prices):
    op = prev_close
    cl = p
    hi = max(op, cl) * (1.0 + abs(rng.gauss(0.0, 0.005)))
    lo = min(op, cl) * (1.0 - abs(rng.gauss(0.0, 0.005)))
    vol = rng.uniform(1e5, 1e7)
    turn = rng.uniform(0.0, 5.0)
    rows.append({
        "date": dates[i],
        "open": op,
        "high": hi,
        "low": lo,
        "close": cl,
        "volume": vol,
        "turnover_rate": turn,
    })
    prev_close = cl

df = pl.DataFrame(rows)

# Compute expected on the pure-Python reference.
expected = _py_support_resistance(df, BIN_COUNT)

# Save.
df.write_parquet(PARQUET)
JSON_OUT.write_text(json.dumps(expected, indent=2, ensure_ascii=False), encoding="utf-8")

print(f"wrote {PARQUET.relative_to(ROOT)} ({PARQUET.stat().st_size} bytes)")
print(f"wrote {JSON_OUT.relative_to(ROOT)} ({JSON_OUT.stat().st_size} bytes, {len(expected)} dicts)")
for d in expected:
    print(f"  {d}")
