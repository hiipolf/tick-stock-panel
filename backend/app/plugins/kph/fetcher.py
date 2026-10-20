"""开盘啦 (KPH) 批量落盘 CLI.

v0.2 重写: 之前的 v0.1 是"扩展数据 fetcher", 自带一套**猜测的** endpoint
(https://www.kphhq.com/api/...) 和重复的解析逻辑。v0.2 改成:

- endpoint 换成真实值 (apphwshhq / apphis.kaipanhong.com), 见 client.py
- 解析逻辑全部复用 Provider (provider.py), 本文件只做「取数 → 落 parquet」
- 正式接入路径是 Provider 插件 (plugin.yaml, datasets: [limit_board]),
  本 CLI 只是给「盘后批量落盘 / 不想起框架」的场景用的便捷脚本

用法:
    uv run python scripts/kph_fetcher.py --date 2026-08-14
    uv run python scripts/kph_fetcher.py --date 2026-08-14 --board-type all
    uv run python scripts/kph_fetcher.py --date 2026-08-14 --data-root data/extensions/kph

输出 (默认落在框架的 data_dir 下, 规范见 backend/app/config.py §data_dir):
    开发/Docker : <项目根>/data/extensions/kph/YYYY-MM-DD/limit_board.parquet
    桌面版     : <exe 同级>/data/extensions/kph/YYYY-MM-DD/limit_board.parquet
    (另有 .duckdb; 环境变量 DATA_DIR 可覆盖根目录)
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Any


def _import_provider() -> Any:
    """三种部署形态下都能 import 到 Provider。

    1. 包内相对导入 (app.plugins.kph.fetcher / kph.fetcher)
    2. vendor 脚本位置 (backend/scripts/kph_fetcher.py) → app.plugins.kph.provider
    3. 暂存区独立运行 (plugins/kph/fetcher.py) → kph.provider
    """
    try:
        from .provider import KphProvider  # type: ignore

        return KphProvider
    except ImportError:
        pass
    try:
        from app.plugins.kph.provider import KphProvider  # type: ignore

        return KphProvider
    except ImportError:
        pass
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from kph.provider import KphProvider  # type: ignore

    return KphProvider


KphProvider = _import_provider()

LOG = logging.getLogger("kph_fetcher")


def _project_root() -> Path:
    """项目根 (backend 的上一级)。相对路径按它解析, 不按 CWD。"""
    # 暂存区: plugins/kph/fetcher.py → parents[2] = 项目根
    # TSP   : backend/app/plugins/kph/fetcher.py → parents[4] = 项目根
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "backend" / "pyproject.toml").exists():
            return parent
        if (parent / "plugins").is_dir() and (parent / "docs").is_dir():
            return parent
    return here.parents[2]


def _default_data_root() -> Path:
    """默认落盘根 = 框架 data_dir 下的 extensions/kph。

    ⚠️ **不能用相对路径** —— 规范 (backend/app/config.py §data_dir):
        - 非 frozen (开发/Docker): <项目根>/data/
        - frozen (桌面版):           <exe 同级>/data/
        - 环境变量 DATA_DIR 可覆盖
      相对路径会按 **CWD** 解析, 在 backend/ 下跑就落到 backend/data/,
      与规范的项目根 data/ 不一致 → 必须取 settings.data_dir。
    """
    try:
        from app.config import settings  # type: ignore

        return settings.data_dir / "extensions" / "kph"
    except Exception:  # 脱离框架独立跑 (暂存区)
        return _project_root() / "data" / "extensions" / "kph"


DEFAULT_DATA_ROOT = _default_data_root()


def run(
    trade_date: date,
    board_type: str = "up",
    data_root: Path = DEFAULT_DATA_ROOT,
) -> dict[str, Path]:
    """跑一次抓取并落盘。返回 {artifact_name: path}。"""
    # 规范: 相对路径基于项目根解析, 不基于 CWD
    if not data_root.is_absolute():
        data_root = _project_root() / data_root
    out_dir = data_root / trade_date.isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)

    provider = KphProvider()
    try:
        frame = provider.get_limit_board(
            trade_date=trade_date.isoformat(), board_type=board_type
        )
    finally:
        provider.close()

    paths: dict[str, Path] = {}
    if frame.is_empty():
        LOG.warning("kph board_type=%s 返回空, 不落盘", board_type)
        return paths

    parquet_path = out_dir / "limit_board.parquet"
    frame.write_parquet(parquet_path)
    paths["limit_board"] = parquet_path
    LOG.info("kph %s → %s (%d rows)", board_type, parquet_path, frame.height)

    duckdb_path = out_dir / "limit_board.duckdb"
    try:
        import duckdb

        conn = duckdb.connect(str(duckdb_path))
        try:
            conn.execute("CREATE OR REPLACE TABLE limit_board AS SELECT * FROM read_parquet(?)", [str(parquet_path)])
        finally:
            conn.close()
        paths["limit_board_duckdb"] = duckdb_path
        LOG.info("kph duckdb → %s", duckdb_path)
    except Exception as exc:
        LOG.warning("kph duckdb 落盘失败 (可忽略): %s", exc)

    return paths


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="KPH 开盘啦数据批量落盘")
    parser.add_argument("--date", required=True, help="交易日 YYYY-MM-DD")
    parser.add_argument(
        "--board-type",
        default="up",
        help="up / down / wind_vane / ladder / emotion / sector / resumption / all (默认 up)",
    )
    parser.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT), type=Path)
    args = parser.parse_args(argv)

    trade_date = date.fromisoformat(args.date)
    paths = run(trade_date, board_type=args.board_type, data_root=args.data_root)
    LOG.info("done. %d 个产物", len(paths))
    return 0 if paths else 1


if __name__ == "__main__":
    sys.exit(main())
