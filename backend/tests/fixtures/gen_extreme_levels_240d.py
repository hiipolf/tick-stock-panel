"""Generate the 240-day extreme_levels fixture + expected output.

Deterministic via random.seed(42). Reuses the existing 240-day parquet
from Spec 0001 (random.seed(42) was already used to build it, so we get
bit-identical K-line data → bit-identical extreme_levels output).

Run:
  python gen_extreme_levels_240d.py
"""
import json
import sys
from pathlib import Path

import polars as pl

WORKTREE = Path(r"D:\AI-Data\路线分析\tmp_cmp\tick-stock-panel-rust-kernel")
sys.path.insert(0, str(WORKTREE / "backend"))

# Reuse the Spec 0001 240-day parquet (deterministic via random.seed(42)).
FIXTURE_IN = WORKTREE / "backend" / "tests" / "fixtures" / "levels_240d.parquet"

# Output:
#   - extreme_levels_240d.parquet: same K-line data as levels_240d.parquet
#     (separate file so future Spec changes don't ripple). For Path A-2 we
#     can just symlink to levels_240d.parquet to keep the test fixtures
#     in sync; if/when Spec 0002 wants a distinct extreme_levels-only
#     fixture, generate it here with a different random seed.
#   - extreme_levels_240d.expected.json: the _py_extreme_levels output on
#     the fixture (captured once, frozen forever).
FIXTURE_OUT = WORKTREE / "backend" / "tests" / "fixtures" / "extreme_levels_240d.parquet"
EXPECTED_OUT = WORKTREE / "backend" / "tests" / "fixtures" / "extreme_levels_240d.expected.json"

df = pl.read_parquet(FIXTURE_IN)
print(f"loaded fixture: {FIXTURE_IN.name} shape={df.shape}")

# Mirror the file (regen stage: spec 0002 fixture == spec 0001 fixture).
df.write_parquet(FIXTURE_OUT)
print(f"wrote fixture:  {FIXTURE_OUT.name}")

# Compute the pure-Python reference output (this is the "frozen" baseline).
from app.indicators.levels import _py_extreme_levels
out = _py_extreme_levels(df)
print(f"reference output: {len(out)} dicts")
for d in out:
    print(f"  {d}")

# Persist as JSON (the test compares the Rust-via-adapter output against this).
EXPECTED_OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"wrote expected: {EXPECTED_OUT.name}")