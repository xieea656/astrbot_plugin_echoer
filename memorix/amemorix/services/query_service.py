"""Query orchestration service."""

from __future__ import annotations

from typing import Any, Dict, Optional

from astrbot.api import logger

from ...core.utils.search_execution_service import (
    SearchExecutionRequest,
    SearchExecutionService,
)
from ...core.utils.time_parser import extract_query_time_intent, parse_query_time_range
from ..context import AppContext


class QueryService:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx

    def _plugin_config(self) -> Dict[str, Any]:
        cfg = dict(self.ctx.config)
        cfg["plugin_instance"] = self.ctx
        cfg["graph_store"] = self.ctx.graph_store
        cfg["metadata_store"] = self.ctx.metadata_store
        return cfg

    async def _execute_request(
        self,
        *,
        caller: str,
        query_type: str,
        query: str,
        top_k: Optional[int] = None,
        time_from: Optional[str] = None,
        time_to: Optional[str] = None,
        person: Optional[str] = None,
        stream_id: Optional[str] = None,
        group_id: Optional[str] = None,
        user_id: Optional[str] = None,
        source: Optional[str] = None,
        strict_source: bool = False,
        enforce_chat_filter: bool = False,
        route_reason: str = "",
        matched_time: str = "",
    ) -> Dict[str, Any]:
        req = SearchExecutionRequest(
            caller=caller,
            stream_id=stream_id,
            group_id=group_id,
            user_id=user_id,
            query_type=query_type,
            query=str(query or ""),
            top_k=top_k,
            time_from=time_from,
            time_to=time_to,
            person=person,
            source=source,
            strict_source=bool(strict_source),
            use_threshold=True,
            enable_ppr=bool(self.ctx.get_config("retrieval.enable_ppr", True)),
        )
        result = await SearchExecutionService.execute(
            retriever=self.ctx.retriever,
            threshold_filter=self.ctx.threshold_filter,
            plugin_config=self._plugin_config(),
            request=req,
            enforce_chat_filter=bool(enforce_chat_filter),
            reinforce_access=True,
        )
        if not result.success:
            raise ValueError(result.error)
        payload = {
            "query_type": result.query_type,
            "query": result.query,
            "top_k": result.top_k,
            "count": result.count,
            "elapsed_ms": result.elapsed_ms,
            "results": SearchExecutionService.to_serializable_results(result.results),
        }
        if result.query_type in {"time", "hybrid"}:
            payload["time_from"] = result.time_from
            payload["time_to"] = result.time_to
        if route_reason:
            payload["route_reason"] = route_reason
        if matched_time:
            payload["matched_time"] = matched_time
        return payload

    async def search(
        self,
        *,
        query: str,
        top_k: Optional[int] = None,
        stream_id: Optional[str] = None,
        group_id: Optional[str] = None,
        user_id: Optional[str] = None,
        source: Optional[str] = None,
        strict_source: bool = False,
        enforce_chat_filter: bool = False,
    ) -> Dict[str, Any]:
        return await self._execute_request(
            caller="v1.search",
            query_type="search",
            query=query,
            top_k=top_k,
            stream_id=stream_id,
            group_id=group_id,
            user_id=user_id,
            source=source,
            strict_source=strict_source,
            enforce_chat_filter=enforce_chat_filter,
        )

    async def time_search(
        self,
        *,
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
        # Validate format eagerly to provide deterministic error text.
        parse_query_time_range(time_from, time_to)

        return await self._execute_request(
            caller="v1.time",
            query_type="time",
            query=query,
            top_k=top_k,
            time_from=time_from,
            time_to=time_to,
            person=person,
            stream_id=stream_id,
            group_id=group_id,
            user_id=user_id,
            source=source,
            enforce_chat_filter=enforce_chat_filter,
        )

    async def auto_search(
        self,
        *,
        query: str,
        top_k: Optional[int] = None,
        stream_id: Optional[str] = None,
        group_id: Optional[str] = None,
        user_id: Optional[str] = None,
        source: Optional[str] = None,
        strict_source: bool = False,
        enforce_chat_filter: bool = False,
    ) -> Dict[str, Any]:
        text = str(query or "").strip()
        if not text:
            raise ValueError("query is empty")

        if not bool(self.ctx.get_config("retrieval.auto_route.enabled", True)):
            return await self.search(
                query=text,
                top_k=top_k,
                stream_id=stream_id,
                group_id=group_id,
                user_id=user_id,
                source=source,
                strict_source=strict_source,
                enforce_chat_filter=enforce_chat_filter,
            )

        if bool(self.ctx.get_config("retrieval.auto_route.enable_time_intent", True)):
            intent = extract_query_time_intent(text)
            if intent is not None:
                routed_query = intent.cleaned_query if intent.query_type == "hybrid" else ""
                try:
                    return await self._execute_request(
                        caller="v1.auto_search",
                        query_type=intent.query_type,
                        query=routed_query,
                        top_k=top_k,
                        time_from=intent.time_from,
                        time_to=intent.time_to,
                        stream_id=stream_id,
                        group_id=group_id,
                        user_id=user_id,
                        source=source,
                        strict_source=strict_source,
                        enforce_chat_filter=enforce_chat_filter,
                        route_reason="time_intent",
                        matched_time=intent.matched_text,
                    )
                except ValueError as exc:
                    logger.debug("auto search time intent fallback to semantic search: %s", exc)

        return await self.search(
            query=text,
            top_k=top_k,
            stream_id=stream_id,
            group_id=group_id,
            user_id=user_id,
            source=source,
            strict_source=strict_source,
            enforce_chat_filter=enforce_chat_filter,
        )

    async def entity(self, *, entity_name: str) -> Dict[str, Any]:
        target = str(entity_name or "").strip()
        if not target:
            raise ValueError("entity_name is empty")
        if not self.ctx.graph_store.has_node(target):
            raise ValueError(f"entity not found: {target}")
        neighbors = self.ctx.graph_store.get_neighbors(target)
        paragraphs = self.ctx.metadata_store.get_paragraphs_by_entity(target)
        relations = (
            self.ctx.metadata_store.get_relations(subject=target)
            + self.ctx.metadata_store.get_relations(object=target)
        )
        return {
            "entity_name": target,
            "neighbors": neighbors,
            "paragraphs": paragraphs,
            "relations": relations,
        }

    async def relation(self, *, subject: str = "", predicate: str = "", obj: str = "") -> Dict[str, Any]:
        rels = self.ctx.metadata_store.get_relations(
            subject=subject or None,
            predicate=predicate or None,
            object=obj or None,
        )
        return {
            "subject": subject,
            "predicate": predicate,
            "object": obj,
            "count": len(rels),
            "relations": rels,
        }

    async def stats(self) -> Dict[str, Any]:
        vector_stats = {"num_vectors": self.ctx.vector_store.num_vectors, "dimension": self.ctx.vector_store.dimension}
        graph_stats = {"num_nodes": self.ctx.graph_store.num_nodes, "num_edges": self.ctx.graph_store.num_edges}
        metadata_stats = self.ctx.metadata_store.get_statistics()
        return {
            "vector_store": vector_stats,
            "graph_store": graph_stats,
            "metadata_store": metadata_stats,
            "retriever": self.ctx.retriever.get_statistics(),
            "sparse": self.ctx.sparse_index.stats() if self.ctx.sparse_index is not None else None,
        }
