"""KPH 批量落盘 CLI 测试.

CLI 只是 Provider 的薄封装, 所以这里只测「取数 → 落盘」这一层:
- 有数据 → 写 parquet (duckdb 产物可选)
- 空数据 → 不落盘, 不留空文件
- main() 退出码: 有产物 0, 无产物 1

解析与单位口径的测试在 test_kph_provider.py.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl

try:  # 装进 TSP 仓库后走正式包路径
    from app.plugins.kph import fetcher as kph_fetcher
    from app.plugins.kph import provider as kph_provider
except ModuleNotFoundError:  # 暂存区: 插件还在 plugins/ 下
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from kph import fetcher as kph_fetcher
    from kph import provider as kph_provider
LIMIT_BOARD_SCHEMA = kph_provider.LIMIT_BOARD_SCHEMA


class _FakeProvider:
    """假 Provider: 返回预置帧, 记录调用."""

    def __init__(self, frame: pl.DataFrame) -> None:
        self.frame = frame
        self.calls: list[tuple[str, str]] = []
        self.closed = False

    def get_limit_board(
        self, trade_date: str | None = None, board_type: str = "up", **_: Any
    ) -> pl.DataFrame:
        self.calls.append((trade_date or "", board_type))
        return self.frame

    def close(self) -> None:
        self.closed = True


def _frame(rows: int = 2) -> pl.DataFrame:
    data = [
        {
            "symbol": f"60051{i}.SH",
            "name": f"股票{i}",
            "trade_date": date(2026, 8, 14),
            "board_type": "up",
        }
        for i in range(rows)
    ]
    frame = pl.DataFrame(data, strict=False)
    # 补齐 canonical schema 的其余列 (缺列 → None), 与 provider._to_frame 行为一致
    for column, dtype in LIMIT_BOARD_SCHEMA.items():
        if column not in frame.columns:
            frame = frame.with_columns(pl.lit(None, dtype=dtype).alias(column))
    return frame.select(list(LIMIT_BOARD_SCHEMA.keys())).cast(LIMIT_BOARD_SCHEMA)


def test_run_writes_parquet(monkeypatch, tmp_path):
    """有数据 → 写出 limit_board.parquet."""
    fake = _FakeProvider(_frame())
    monkeypatch.setattr(kph_fetcher, "KphProvider", lambda *a, **kw: fake)

    paths = kph_fetcher.run(date(2026, 8, 14), board_type="up", data_root=tmp_path)

    assert "limit_board" in paths
    out = paths["limit_board"]
    assert out.exists()
    assert out.name == "limit_board.parquet"
    assert out.parent.name == "2026-08-14"
    assert fake.calls == [("2026-08-14", "up")]
    assert fake.closed is True

    # 落盘内容可读回, 且行数一致
    back = pl.read_parquet(out)
    assert back.height == 2
    assert back.columns == list(LIMIT_BOARD_SCHEMA.keys())


def test_run_skips_empty_result(monkeypatch, tmp_path):
    """空数据 → 不落盘, 不留空文件."""
    fake = _FakeProvider(pl.DataFrame(schema=LIMIT_BOARD_SCHEMA))
    monkeypatch.setattr(kph_fetcher, "KphProvider", lambda *a, **kw: fake)

    paths = kph_fetcher.run(date(2026, 8, 14), board_type="up", data_root=tmp_path)

    assert paths == {}
    assert not (tmp_path / "2026-08-14" / "limit_board.parquet").exists()


def test_main_exit_codes(monkeypatch, tmp_path):
    """main(): 有产物 → 0; 无产物 → 1."""
    fake = _FakeProvider(_frame())
    monkeypatch.setattr(kph_fetcher, "KphProvider", lambda *a, **kw: fake)
    monkeypatch.setattr(sys, "argv", ["kph_fetcher.py", "--date", "2026-08-14", "--data-root", str(tmp_path)])
    assert kph_fetcher.main() == 0

    empty = _FakeProvider(pl.DataFrame(schema=LIMIT_BOARD_SCHEMA))
    monkeypatch.setattr(kph_fetcher, "KphProvider", lambda *a, **kw: empty)
    assert kph_fetcher.main() == 1
