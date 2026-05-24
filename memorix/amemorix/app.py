"""FastAPI app factory for standalone runtime."""

from __future__ import annotations

from astrbot.api import logger
from fastapi import FastAPI

from ..webui.routes_compat import MemorixServer
from .auth import BearerAuthMiddleware
from .bootstrap import build_context
from .common import register_lifecycle_handler
from .common.logging import setup_logging
from .import_write_guard import ImportWriteGuardMiddleware
from .routers.v1_router import router as v1_router
from .services.import_task_manager import ImportTaskManager
from .settings import AppSettings
from .task_manager import TaskManager


def create_app(*, settings: AppSettings) -> FastAPI:
    setup_logging("DEBUG" if bool(settings.get("advanced.debug", False)) else "INFO")

    context = build_context(settings)
    compat = MemorixServer(
        plugin_instance=context,
        host=settings.host,
        port=settings.port,
    )
    app = compat.app
    app.state.context = context
    app.state.task_manager = TaskManager(context)
    app.state.import_task_manager = ImportTaskManager(context)

    # 身份验证做的是全局核验，用默认值
    app.add_middleware(BearerAuthMiddleware)
    app.add_middleware(ImportWriteGuardMiddleware)
    app.include_router(v1_router)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz():
        ctx = app.state.context
        return {
            "ready": all(
                [
                    ctx.metadata_store is not None,
                    ctx.graph_store is not None,
                    ctx.vector_store is not None,
                    ctx.retriever is not None,
                ]
            )
        }

    async def _shutdown():
        try:
            await app.state.import_task_manager.stop()
        except Exception as exc:
            logger.warning("Import task manager stop failed: %s", exc)
        try:
            await app.state.task_manager.stop()
        except Exception as exc:
            logger.warning("Task manager stop failed: %s", exc)
        try:
            await app.state.context.close()
        except Exception as exc:
            logger.warning("Shutdown close failed: %s", exc)

    async def _startup():
        try:
            await app.state.import_task_manager.start()
        except Exception as exc:
            logger.warning("Import task manager start failed: %s", exc)
        try:
            await app.state.task_manager.start()
        except Exception as exc:
            logger.warning("Task manager start failed: %s", exc)

    register_lifecycle_handler(app, "shutdown", _shutdown)
    register_lifecycle_handler(app, "startup", _startup)

    return app
