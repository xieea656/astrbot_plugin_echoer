"""Summary service facade with caching via ScopeRuntime.get_service()."""

from __future__ import annotations

from typing import Any, Dict

from ..amemorix.services.summary_service import SummaryService as BaseSummaryService
from ..app_context import ScopeRuntimeManager


class SummaryService:
    def __init__(self, runtime_manager: ScopeRuntimeManager):
        self.runtime_manager = runtime_manager

    async def _get(self, scope_key: str) -> BaseSummaryService:
        runtime = await self.runtime_manager.get_runtime(scope_key)
        return runtime.get_service(BaseSummaryService)

    async def maybe_enqueue_auto_summary(
        self, *, scope_key: str, session_id: str,
    ) -> Dict[str, Any]:
        runtime = await self.runtime_manager.get_runtime(scope_key)
        return await runtime.task_manager.maybe_enqueue_auto_summary(session_id=session_id)

    async def summarize_session(
        self, *, scope_key: str, session_id: str, source: str, context_length: int = 50,
    ) -> Dict[str, Any]:
        return await (await self._get(scope_key)).import_from_transcript(
            session_id=session_id, messages=[], source=source, context_length=context_length,
        )
