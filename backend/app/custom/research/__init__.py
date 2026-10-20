"""IRP 后端扩展 — 短线连板 + 趋势题材。

按 TSP 通道 1 标准实现（参考 shy3130/tick-stock-panel docs/secondary-development.md）。
删除本目录 + 重启后端 = 完全卸载。
"""
from app.extensions import (
    BACKEND_EXTENSION_API_VERSION,
    BackendExtensionRegistrar,
    ExtensionContext,
)

EXTENSION_ID = "research.studio"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION


def setup(registrar: BackendExtensionRegistrar) -> None:
    """注册路由。可选挂载工具到 AI 助手。"""
    from app.custom.research.routes import build_router
    registrar.include_router(build_router())


def startup(context: ExtensionContext) -> None:
    """启动期初始化。失败不影响主程序。"""
    _ = context  # P0 hello-world 不做初始化
