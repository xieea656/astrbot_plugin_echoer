"""Global write guard for import runtime."""

from __future__ import annotations

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
IMPORT_WRITE_PREFIX = "/v1/import"


class ImportWriteGuardMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        method = request.method.upper()
        if method not in WRITE_METHODS:
            return await call_next(request)

        path = request.url.path
        if path.startswith(IMPORT_WRITE_PREFIX):
            return await call_next(request)

        manager = getattr(request.app.state, "import_task_manager", None)
        if manager is not None and manager.is_write_blocked():
            return JSONResponse(
                status_code=409,
                content={"detail": "导入任务运行中，写操作已临时禁用"},
            )

        return await call_next(request)

