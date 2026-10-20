"""Pytest conftest — 加 backend/app 到 sys.path。

`app.custom.research` 在 `backend_pack/app/custom/research/` 下, 需要把
`backend_pack/` (包含 app/) 加进 path 才能 `import app.custom.research.*`。

净室重写: 不依赖任何 easy-stock / vendor 代码。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# backend_pack/app 在 path 里 → app.* 可被 import
BACKEND_ROOT = Path(__file__).resolve().parents[3]  # _tests_/conftest.py → custom/research/_tests_/conftest.py → backend_pack/
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


@pytest.fixture(scope="session")
def backend_root() -> Path:
    """暴露 backend_pack/ 根路径给测试。"""
    return BACKEND_ROOT