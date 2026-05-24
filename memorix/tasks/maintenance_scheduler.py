"""Maintenance scheduler facade backed by migrated TaskManager loops."""

from __future__ import annotations

from dataclasses import dataclass

from ..app_context import ScopeRuntimeManager


@dataclass(slots=True)
class MaintenanceScheduler:
    """Lifecycle facade around per-scope TaskManager background loops."""

    runtime_manager: ScopeRuntimeManager
    name: str = "memorix-task-manager"

    async def status(self, scope_key: str) -> dict:
        runtime = await self.runtime_manager.get_runtime(scope_key)
        task_manager = runtime.task_manager
        workers = list(getattr(task_manager, "_workers", []))
        return {
            "scope_key": scope_key,
            "running": bool(workers),
            "worker_count": len(workers),
            "stopping": bool(getattr(task_manager, "_stopping", False)),
        }

    async def restart(self, scope_key: str) -> dict:
        runtime = await self.runtime_manager.get_runtime(scope_key)
        await runtime.task_manager.stop()
        await runtime.task_manager.start()
        return await self.status(scope_key)
