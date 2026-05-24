"""Standalone operation APIs (/v1/*)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from ..services import (
    DeleteService,
    MemoryService,
    PersonProfileApiService,
    QueryService,
)

router = APIRouter(prefix="/v1", tags=["v1"])


class ImportTaskCreateRequest(BaseModel):
    mode: str = Field(default="text")
    payload: Any
    options: Dict[str, Any] = Field(default_factory=dict)


class ImportPasteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str
    name: Optional[str] = None
    knowledge_type: Optional[str] = None


class ImportRawScanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alias: Optional[str] = "raw"
    relative_path: Optional[str] = ""
    glob: Optional[str] = "*"
    recursive: Optional[bool] = True
    knowledge_type: Optional[str] = None


class ImportRetryRequest(BaseModel):
    input_mode: Optional[str] = None
    glob: Optional[str] = None
    recursive: Optional[bool] = None
    knowledge_type: Optional[str] = None


class ImportPathResolveRequest(BaseModel):
    alias: str
    relative_path: Optional[str] = ""
    must_exist: Optional[bool] = True


class SummaryTaskCreateRequest(BaseModel):
    session_id: Optional[str] = None
    source: str = ""
    messages: list[dict[str, Any]] = Field(default_factory=list)
    context_length: int = 50


class QuerySearchRequest(BaseModel):
    query: str
    top_k: Optional[int] = None


class QueryTimeRequest(BaseModel):
    query: str = ""
    time_from: Optional[str] = None
    time_to: Optional[str] = None
    person: Optional[str] = None
    source: Optional[str] = None
    top_k: Optional[int] = None


class QueryEntityRequest(BaseModel):
    entity_name: str


class QueryRelationRequest(BaseModel):
    subject: str = ""
    predicate: str = ""
    object: str = ""


class DeleteParagraphRequest(BaseModel):
    paragraph_hash: str


class DeleteEntityRequest(BaseModel):
    entity_name: str


class DeleteRelationRequest(BaseModel):
    relation: str


class MemoryStatusRequest(BaseModel):
    pass


class MemoryProtectRequest(BaseModel):
    id: str
    hours: float = 24.0


class MemoryReinforceRequest(BaseModel):
    id: str


class MemoryRestoreRequest(BaseModel):
    hash: str
    type: str = "relation"


class PersonQueryRequest(BaseModel):
    person_id: str = ""
    person_keyword: str = ""
    top_k: int = 12
    force_refresh: bool = False


class PersonOverrideRequest(BaseModel):
    person_id: str
    override_text: str
    updated_by: str = "v1"


class PersonOverrideDeleteRequest(BaseModel):
    person_id: str


class PersonRegistryUpsertRequest(BaseModel):
    person_id: str
    person_name: str = ""
    nickname: str = ""
    user_id: str = ""
    platform: str = ""
    group_nick_name: Any = None
    memory_points: Any = None
    last_know: Optional[float] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


def _ctx(request: Request):
    return request.app.state.context


def _task_manager(request: Request):
    manager = getattr(request.app.state, "task_manager", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="Task manager not initialized")
    return manager


def _import_manager(request: Request):
    manager = getattr(request.app.state, "import_task_manager", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="Import task manager not initialized")
    return manager


def _import_enabled(request: Request) -> bool:
    return bool(_ctx(request).get_config("web.import.enabled", False))


def _ensure_import_enabled(request: Request) -> None:
    if not _import_enabled(request):
        raise HTTPException(status_code=404, detail="导入功能未启用")


def _task_or_404(task):
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


def _load_import_guide_text() -> Dict[str, Any]:
    guide_path = (Path(__file__).resolve().parents[2] / "IMPORT_GUIDE.md").resolve()
    if not guide_path.exists():
        raise HTTPException(status_code=404, detail=f"导入文档不存在: {guide_path}")
    text = guide_path.read_text(encoding="utf-8")
    return {
        "source": "local",
        "url": "",
        "path": str(guide_path),
        "content": text,
    }


@router.post("/import/tasks")
async def create_import_task(request: Request, body: ImportTaskCreateRequest):
    manager = _task_manager(request)
    task = await manager.enqueue_import_task(body.model_dump())
    return {
        "task_id": task.get("task_id"),
        "status": task.get("status"),
        "created_at": task.get("created_at"),
    }


@router.post("/import/tasks/upload")
async def create_import_task_upload(
    request: Request,
    files: Optional[list[UploadFile]] = File(default=None),
    files_array: Optional[list[UploadFile]] = File(default=None, alias="files[]"),
    payload: str = Form("{}"),
):
    _ensure_import_enabled(request)
    merged_files = list(files or []) + list(files_array or [])
    if not merged_files:
        raise HTTPException(status_code=400, detail="至少需要上传一个文件")
    try:
        payload_obj = json.loads(payload or "{}")
    except Exception as exc:
        raise HTTPException(status_code=400, detail="payload 必须为合法 JSON") from exc
    if not isinstance(payload_obj, dict):
        raise HTTPException(status_code=400, detail="payload 必须为 JSON 对象")
    manager = _import_manager(request)
    try:
        task = await manager.create_upload_task(merged_files, payload_obj)
        return {"success": True, "task": task}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/import/tasks/paste")
async def create_import_task_paste(request: Request, body: ImportPasteRequest):
    _ensure_import_enabled(request)
    manager = _import_manager(request)
    try:
        task = await manager.create_paste_task(body.model_dump())
        return {"success": True, "task": task}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/import/tasks/raw_scan")
async def create_import_task_raw_scan(request: Request, body: ImportRawScanRequest):
    _ensure_import_enabled(request)
    manager = _import_manager(request)
    try:
        task = await manager.create_raw_scan_task(body.model_dump())
        return {"success": True, "task": task}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/import/tasks")
async def list_import_tasks(request: Request, limit: int = Query(50, ge=1, le=200)):
    _ensure_import_enabled(request)
    manager = _import_manager(request)
    items = await manager.list_tasks(limit=limit)
    return {"success": True, "items": items}


@router.get("/import/tasks/{task_id}")
async def get_import_task(request: Request, task_id: str, include_chunks: bool = Query(False)):
    if _import_enabled(request):
        manager = _import_manager(request)
        task = await manager.get_task(task_id, include_chunks=include_chunks)
        if task is not None:
            return {"success": True, "task": task}
    legacy_manager = _task_manager(request)
    return _task_or_404(legacy_manager.get_task(task_id))


@router.get("/import/tasks/{task_id}/files/{file_id}/chunks")
async def get_import_chunks(
    request: Request,
    task_id: str,
    file_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
):
    _ensure_import_enabled(request)
    manager = _import_manager(request)
    data = await manager.get_chunks(task_id, file_id, offset=offset, limit=limit)
    if data is None:
        raise HTTPException(status_code=404, detail="任务或文件不存在")
    return {"success": True, **data}


@router.post("/import/tasks/{task_id}/cancel")
async def cancel_import_task(request: Request, task_id: str):
    _ensure_import_enabled(request)
    manager = _import_manager(request)
    task = await manager.cancel_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"success": True, "task": task}


@router.post("/import/tasks/{task_id}/retry_failed")
async def retry_import_task_failed(
    request: Request,
    task_id: str,
    body: Optional[ImportRetryRequest] = Body(default=None),
):
    _ensure_import_enabled(request)
    manager = _import_manager(request)
    overrides = body.model_dump(exclude_none=True) if body else {}
    try:
        task = await manager.retry_failed(task_id, overrides=overrides)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"success": True, "task": task}


@router.get("/import/path_aliases")
async def get_import_path_aliases(request: Request):
    _ensure_import_enabled(request)
    manager = _import_manager(request)
    aliases = manager.get_path_aliases()
    filtered = {k: v for k, v in aliases.items() if k in {"raw", "plugin_data"}}
    return {"success": True, "items": filtered}


@router.post("/import/path_resolve")
async def resolve_import_path(request: Request, body: ImportPathResolveRequest):
    _ensure_import_enabled(request)
    manager = _import_manager(request)
    try:
        data = await manager.resolve_path_request(body.model_dump())
        return {"success": True, **data}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/import/guide")
async def get_import_guide(request: Request):
    _ensure_import_enabled(request)
    return {"success": True, **_load_import_guide_text()}


@router.post("/query/search")
async def query_search(request: Request, body: QuerySearchRequest):
    service = QueryService(_ctx(request))
    try:
        return await service.search(query=body.query, top_k=body.top_k)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/query/time")
async def query_time(request: Request, body: QueryTimeRequest):
    service = QueryService(_ctx(request))
    try:
        return await service.time_search(
            query=body.query,
            time_from=body.time_from,
            time_to=body.time_to,
            person=body.person,
            source=body.source,
            top_k=body.top_k,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/query/entity")
async def query_entity(request: Request, body: QueryEntityRequest):
    service = QueryService(_ctx(request))
    try:
        return await service.entity(entity_name=body.entity_name)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/query/relation")
async def query_relation(request: Request, body: QueryRelationRequest):
    service = QueryService(_ctx(request))
    try:
        return await service.relation(subject=body.subject, predicate=body.predicate, obj=body.object)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/query/stats")
async def query_stats(request: Request):
    service = QueryService(_ctx(request))
    return await service.stats()


@router.post("/delete/paragraph")
async def delete_paragraph(request: Request, body: DeleteParagraphRequest):
    service = DeleteService(_ctx(request))
    try:
        return await service.paragraph(body.paragraph_hash)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/delete/entity")
async def delete_entity(request: Request, body: DeleteEntityRequest):
    service = DeleteService(_ctx(request))
    try:
        return await service.entity(body.entity_name)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/delete/relation")
async def delete_relation(request: Request, body: DeleteRelationRequest):
    service = DeleteService(_ctx(request))
    try:
        return await service.relation(body.relation)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/delete/clear")
async def delete_clear(request: Request):
    service = DeleteService(_ctx(request))
    try:
        return await service.clear()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/memory/status")
async def memory_status(request: Request, body: MemoryStatusRequest):
    del body
    service = MemoryService(_ctx(request))
    return await service.status()


@router.post("/memory/protect")
async def memory_protect(request: Request, body: MemoryProtectRequest):
    service = MemoryService(_ctx(request))
    return await service.protect(query_or_hash=body.id, hours=body.hours)


@router.post("/memory/reinforce")
async def memory_reinforce(request: Request, body: MemoryReinforceRequest):
    service = MemoryService(_ctx(request))
    return await service.reinforce(query_or_hash=body.id)


@router.post("/memory/restore")
async def memory_restore(request: Request, body: MemoryRestoreRequest):
    service = MemoryService(_ctx(request))
    return await service.restore(hash_value=body.hash, restore_type=body.type)


@router.post("/person/query")
async def person_query(request: Request, body: PersonQueryRequest):
    service = PersonProfileApiService(_ctx(request))
    try:
        return await service.query(
            person_id=body.person_id,
            person_keyword=body.person_keyword,
            top_k=body.top_k,
            force_refresh=body.force_refresh,
            source_note="v1:person_query",
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/person/override")
async def person_override(request: Request, body: PersonOverrideRequest):
    service = PersonProfileApiService(_ctx(request))
    try:
        return await service.set_override(
            person_id=body.person_id,
            override_text=body.override_text,
            updated_by=body.updated_by,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/person/override")
async def person_override_delete(request: Request, body: PersonOverrideDeleteRequest):
    service = PersonProfileApiService(_ctx(request))
    try:
        return await service.delete_override(person_id=body.person_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/person/registry/upsert")
async def person_registry_upsert(request: Request, body: PersonRegistryUpsertRequest):
    service = PersonProfileApiService(_ctx(request))
    try:
        data = await service.upsert_registry(body.model_dump())
        return {"success": True, "item": data}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/person/registry/list")
async def person_registry_list(
    request: Request,
    keyword: str = Query("", description="keyword"),
    page: int = Query(1, ge=1),
    page_size: Optional[int] = Query(None, ge=1, le=200),
):
    service = PersonProfileApiService(_ctx(request))
    return await service.list_registry(keyword=keyword, page=page, page_size=page_size)


@router.post("/summary/tasks")
async def create_summary_task(request: Request, body: SummaryTaskCreateRequest):
    manager = _task_manager(request)
    task = await manager.enqueue_summary_task(body.model_dump())
    return {
        "task_id": task.get("task_id"),
        "status": task.get("status"),
        "created_at": task.get("created_at"),
    }


@router.get("/summary/tasks/{task_id}")
async def get_summary_task(request: Request, task_id: str):
    manager = _task_manager(request)
    return _task_or_404(manager.get_task(task_id))
