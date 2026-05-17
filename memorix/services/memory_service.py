"""Memory service facade with caching via ScopeRuntime.get_service()."""

from __future__ import annotations

from typing import Any, Dict

from ..amemorix.services.memory_service import MemoryService as BaseMemoryService
from ..app_context import ScopeRuntimeManager


class MemoryService:
    def __init__(self, runtime_manager: ScopeRuntimeManager):
        self.runtime_manager = runtime_manager

    async def _get(self, scope_key: str) -> BaseMemoryService:
        runtime = await self.runtime_manager.get_runtime(scope_key)
        return runtime.get_service(BaseMemoryService)

    async def status(self, *, scope_key: str) -> Dict[str, Any]:
        return await (await self._get(scope_key)).status()

    async def protect(self, *, scope_key: str, query_or_hash: str, hours: float = 24.0) -> Dict[str, Any]:
        return await (await self._get(scope_key)).protect(query_or_hash=query_or_hash, hours=hours)

    async def reinforce(self, *, scope_key: str, query_or_hash: str) -> Dict[str, Any]:
        return await (await self._get(scope_key)).reinforce(query_or_hash=query_or_hash)

    async def restore(self, *, scope_key: str, hash_value: str, restore_type: str = "relation") -> Dict[str, Any]:
        return await (await self._get(scope_key)).restore(hash_value=hash_value, restore_type=restore_type)
