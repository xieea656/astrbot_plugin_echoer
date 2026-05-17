"""Query service facade with caching via ScopeRuntime.get_service()."""

from __future__ import annotations

from typing import Any, Dict, Optional

from ..amemorix.services.query_service import QueryService as BaseQueryService
from ..app_context import ScopeRuntimeManager


class QueryService:
    def __init__(self, runtime_manager: ScopeRuntimeManager):
        self.runtime_manager = runtime_manager

    async def _get(self, scope_key: str) -> BaseQueryService:
        runtime = await self.runtime_manager.get_runtime(scope_key)
        return runtime.get_service(BaseQueryService)

    async def search(
        self,
        *,
        scope_key: str,
        query: str,
        top_k: Optional[int] = None,
        stream_id: Optional[str] = None,
        group_id: Optional[str] = None,
        user_id: Optional[str] = None,
        source: Optional[str] = None,
        strict_source: bool = False,
        enforce_chat_filter: bool = False,
    ) -> Dict[str, Any]:
        return await (await self._get(scope_key)).search(
            query=query, top_k=top_k, stream_id=stream_id, group_id=group_id,
            user_id=user_id, source=source, strict_source=strict_source,
            enforce_chat_filter=enforce_chat_filter,
        )

    async def time_search(
        self,
        *,
        scope_key: str,
        query: str = "",
        time_from: Optional[str] = None,
        time_to: Optional[str] = None,
        person: Optional[str] = None,
        source: Optional[str] = None,
        top_k: Optional[int] = None,
        stream_id: Optional[str] = None,
        group_id: Optional[str] = None,
        user_id: Optional[str] = None,
        enforce_chat_filter: bool = False,
    ) -> Dict[str, Any]:
        return await (await self._get(scope_key)).time_search(
            query=query, time_from=time_from, time_to=time_to, person=person,
            source=source, top_k=top_k, stream_id=stream_id, group_id=group_id,
            user_id=user_id, enforce_chat_filter=enforce_chat_filter,
        )

    async def auto_search(
        self,
        *,
        scope_key: str,
        query: str,
        top_k: Optional[int] = None,
        stream_id: Optional[str] = None,
        group_id: Optional[str] = None,
        user_id: Optional[str] = None,
        source: Optional[str] = None,
        strict_source: bool = False,
        enforce_chat_filter: bool = False,
    ) -> Dict[str, Any]:
        return await (await self._get(scope_key)).auto_search(
            query=query, top_k=top_k, stream_id=stream_id, group_id=group_id,
            user_id=user_id, source=source, strict_source=strict_source,
            enforce_chat_filter=enforce_chat_filter,
        )

    async def stats(self, *, scope_key: str) -> Dict[str, Any]:
        return await (await self._get(scope_key)).stats()
