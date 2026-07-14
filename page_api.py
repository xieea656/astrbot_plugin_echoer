# -*- coding: utf-8 -*-
"""Echoer 内嵌页面 API — 通过 AstrBot register_web_api 暴露记忆管理接口。"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger
from quart import request

PAGE_API_PREFIX = "/astrbot_plugin_echoer"


def _ok(data: Any = None) -> dict:
    return {"success": True, "data": data}


def _err(msg: str) -> dict:
    return {"success": False, "message": msg}


def _int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


class EchoerPageApi:
    def __init__(self, plugin):
        self.plugin = plugin

    def register_routes(self):
        reg = self.plugin.context.register_web_api
        prefix = PAGE_API_PREFIX
        routes = [
            (f"{prefix}/overview", self.overview, ["GET"], "Echoer overview"),
            (f"{prefix}/memories", self.memories, ["GET"], "List/search memories"),
            (f"{prefix}/memory", self.memory_detail, ["GET"], "Memory detail"),
            (f"{prefix}/memory/protect", self.memory_protect, ["POST"], "Protect memory"),
            (f"{prefix}/memory/delete", self.memory_delete, ["POST"], "Delete memory"),
            (f"{prefix}/entities", self.entities, ["GET"], "List entities"),
            (f"{prefix}/graph", self.graph, ["GET"], "Graph relations"),
            (f"{prefix}/persons", self.persons, ["GET"], "Person profiles"),
            (f"{prefix}/scopes", self.scopes, ["GET"], "Scope info"),
        ]
        for path, handler, methods, desc in routes:
            reg(path, handler, methods, desc)
        logger.info(f"[echoer] page API registered: {len(routes)} routes")

    # ---- handlers ----

    async def overview(self) -> dict:
        try:
            scopes = self.plugin.runtime_manager.get_known_scopes()
            info = []
            for sk in scopes:
                try:
                    runtime = await self.plugin.runtime_manager.get_runtime(sk)
                    ms = runtime.context.metadata_store
                    gs = runtime.context.graph_store
                    vs = runtime.context.vector_store
                    info.append({
                        "scope": sk,
                        "data_dir": str(runtime.settings.data_dir),
                        "nodes": getattr(gs, "num_nodes", 0),
                        "vectors": getattr(vs, "num_vectors", 0),
                    })
                except Exception:
                    info.append({"scope": sk, "error": "failed to load"})
            return _ok({
                "scopes": info,
                "known_scopes": scopes,
                "mode": self.plugin.scope_router.mode,
            })
        except Exception as e:
            logger.error(f"[echoer] overview error: {e}", exc_info=True)
            return _err(str(e))

    async def memories(self) -> dict:
        try:
            scope = str(request.args.get("scope", "")).strip()
            query = str(request.args.get("q", "")).strip()
            limit = _int(request.args.get("limit"), 50)
            if not scope:
                scopes = self.plugin.runtime_manager.get_known_scopes()
                scope = scopes[0] if scopes else "default"
            runtime = await self.plugin.runtime_manager.get_runtime(scope)
            from .memorix.amemorix.services.query_service import QueryService
            svc = runtime.get_service(QueryService)
            if query:
                result = await svc.search(query=query, top_k=limit)
                items = result.get("results", [])
            else:
                try:
                    items = runtime.context.metadata_store.list_paragraphs(limit=limit)
                except Exception:
                    items = []
            items_safe = []
            for item in items[:limit]:
                items_safe.append({
                    "hash": str(item.get("hash", "")),
                    "content": str(item.get("content", ""))[:500],
                    "type": str(item.get("type", "paragraph")),
                    "score": float(item.get("score", 0)),
                    "source": str(item.get("source", "")),
                })
            return _ok({"items": items_safe, "total": len(items_safe), "scope": scope})
        except Exception as e:
            logger.error(f"[echoer] memories error: {e}", exc_info=True)
            return _err(str(e))

    async def memory_detail(self) -> dict:
        try:
            scope = str(request.args.get("scope", "")).strip()
            hash_val = str(request.args.get("hash", "")).strip()
            if not hash_val:
                return _err("missing hash")
            if not scope:
                scopes = self.plugin.runtime_manager.get_known_scopes()
                scope = scopes[0] if scopes else "default"
            runtime = await self.plugin.runtime_manager.get_runtime(scope)
            para = runtime.context.metadata_store.get_paragraph(hash_val)
            if not para:
                return _err("not found")
            relations = runtime.context.metadata_store.get_relations_for_paragraph(hash_val)
            return _ok({
                "paragraph": {
                    "hash": str(para.get("hash", "")),
                    "content": str(para.get("content", "")),
                    "source": str(para.get("source", "")),
                },
                "relations": [{
                    "hash": str(r.get("hash", "")),
                    "type": str(r.get("type", "")),
                    "content": str(r.get("content", ""))[:300],
                } for r in (relations or [])],
            })
        except Exception as e:
            return _err(str(e))

    async def memory_protect(self) -> dict:
        try:
            body = await request.get_json(silent=True) or {}
            scope = str(body.get("scope", "")).strip()
            hash_val = str(body.get("hash", "")).strip()
            hours = float(body.get("hours", 24))
            if not hash_val:
                return _err("missing hash")
            if not scope:
                scopes = self.plugin.runtime_manager.get_known_scopes()
                scope = scopes[0] if scopes else "default"
            result = await self.plugin.memory_service.protect(
                scope_key=scope, query_or_hash=hash_val, hours=hours)
            return _ok(result)
        except Exception as e:
            return _err(str(e))

    async def memory_delete(self) -> dict:
        try:
            body = await request.get_json(silent=True) or {}
            scope = str(body.get("scope", "")).strip()
            hash_val = str(body.get("hash", "")).strip()
            if not hash_val:
                return _err("missing hash")
            if not scope:
                scopes = self.plugin.runtime_manager.get_known_scopes()
                scope = scopes[0] if scopes else "default"
            runtime = await self.plugin.runtime_manager.get_runtime(scope)
            from .memorix.amemorix.services.delete_service import DeleteService
            result = await DeleteService(runtime.context).paragraph(hash_val)
            return _ok(result)
        except Exception as e:
            return _err(str(e))

    async def entities(self) -> dict:
        try:
            scope = str(request.args.get("scope", "")).strip()
            limit = _int(request.args.get("limit"), 50)
            if not scope:
                scopes = self.plugin.runtime_manager.get_known_scopes()
                scope = scopes[0] if scopes else "default"
            runtime = await self.plugin.runtime_manager.get_runtime(scope)
            try:
                entities = runtime.context.graph_store.list_nodes(limit=limit)
            except Exception:
                entities = []
            return _ok({"items": entities[:limit] if entities else [], "total": len(entities) if entities else 0})
        except Exception as e:
            return _err(str(e))

    async def graph(self) -> dict:
        try:
            scope = str(request.args.get("scope", "")).strip()
            entity = str(request.args.get("entity", "")).strip()
            limit = _int(request.args.get("limit"), 30)
            if not scope:
                scopes = self.plugin.runtime_manager.get_known_scopes()
                scope = scopes[0] if scopes else "default"
            runtime = await self.plugin.runtime_manager.get_runtime(scope)
            if entity:
                triples = runtime.context.metadata_store.get_triples_for_entity(entity, limit=limit)
            else:
                try:
                    triples = runtime.context.metadata_store.get_all_triples()
                    triples = triples[:limit]
                except Exception:
                    triples = []
            return _ok({"triples": triples[:limit], "total": len(triples)})
        except Exception as e:
            return _err(str(e))

    async def persons(self) -> dict:
        try:
            scope = str(request.args.get("scope", "")).strip()
            limit = _int(request.args.get("limit"), 20)
            if not scope:
                scopes = self.plugin.runtime_manager.get_known_scopes()
                scope = scopes[0] if scopes else "default"
            data = await self.plugin.profile_manager.list_registry(
                scope_key=scope, page=1, page_size=limit)
            return _ok(data)
        except Exception as e:
            return _err(str(e))

    async def scopes(self) -> dict:
        try:
            known = self.plugin.runtime_manager.get_known_scopes()
            return _ok({
                "scopes": known,
                "mode": self.plugin.scope_router.mode,
            })
        except Exception as e:
            return _err(str(e))
