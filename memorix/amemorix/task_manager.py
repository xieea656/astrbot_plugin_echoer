"""Background task manager for import/summary and maintenance loops."""

from __future__ import annotations

import asyncio
import datetime
import json
import uuid
from typing import Any, Dict, List, Optional, Tuple

from astrbot.api import logger

from .context import AppContext
from .services import (
    ImportService,
    MemoryService,
    PersonProfileApiService,
    SummaryService,
)

TASK_STATUS_QUEUED = "queued"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_SUCCEEDED = "succeeded"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_CANCELED = "canceled"


class TaskManager:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        self.import_service = ImportService(ctx)
        self.summary_service = SummaryService(ctx)
        self.memory_service = MemoryService(ctx)
        self.person_service = PersonProfileApiService(ctx)

        queue_maxsize = int(self.ctx.get_config("tasks.queue_maxsize", 1024))
        self.import_queue: asyncio.Queue[Tuple[str, Dict[str, Any]]] = asyncio.Queue(maxsize=max(1, queue_maxsize))
        self.summary_queue: asyncio.Queue[Tuple[str, Dict[str, Any]]] = asyncio.Queue(maxsize=max(1, queue_maxsize))
        self._workers: List[asyncio.Task] = []
        self._stopping = False
        self._bulk_summary_lock = asyncio.Lock()
        self._pending_auto_summary_sessions: set[str] = set()

    async def start(self) -> None:
        self._stopping = False
        import_workers = max(1, int(self.ctx.get_config("tasks.import_workers", 1)))
        summary_workers = max(1, int(self.ctx.get_config("tasks.summary_workers", 1)))

        for idx in range(import_workers):
            self._workers.append(asyncio.create_task(self._import_worker(idx), name=f"import-worker-{idx}"))
        for idx in range(summary_workers):
            self._workers.append(asyncio.create_task(self._summary_worker(idx), name=f"summary-worker-{idx}"))

        if bool(self.ctx.get_config("summarization.enabled", True)) and bool(self.ctx.get_config("schedule.enabled", True)):
            self._workers.append(asyncio.create_task(self._scheduled_summary_loop(), name="scheduled-summary-loop"))
        self._workers.append(asyncio.create_task(self._auto_save_loop(), name="auto-save-loop"))
        self._workers.append(asyncio.create_task(self._memory_maintenance_loop(), name="memory-maint-loop"))
        self._workers.append(asyncio.create_task(self._person_profile_refresh_loop(), name="person-profile-loop"))
        logger.info("TaskManager started with %s workers", len(self._workers))

    async def stop(self) -> None:
        self._stopping = True
        for task in self._workers:
            task.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        logger.info("TaskManager stopped")

    async def enqueue_import_task(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        task_id = uuid.uuid4().hex
        task = self.ctx.metadata_store.create_async_task(task_id=task_id, task_type="import", payload=payload)
        await self.import_queue.put((task_id, payload))
        return task

    async def enqueue_summary_task(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        task_id = uuid.uuid4().hex
        task = self.ctx.metadata_store.create_async_task(task_id=task_id, task_type="summary", payload=payload)
        await self.summary_queue.put((task_id, payload))
        return task

    def _auto_summary_enabled(self) -> bool:
        return bool(self.ctx.get_config("summarization.enabled", True)) and bool(
            self.ctx.get_config("summarization.auto_import.enabled", True)
        )

    def _auto_summary_context_length(self) -> int:
        return max(1, int(self.ctx.get_config("summarization.context_length", 50) or 50))

    async def _count_new_transcript_messages(
        self,
        *,
        session_id: str,
        last_message_created_at: Optional[float],
    ) -> Tuple[int, Optional[float]]:
        conn = self.ctx.metadata_store._conn
        if conn is None or not session_id:
            return 0, None

        async with self.ctx.metadata_store._async_lock:
            cursor = conn.cursor()
            if last_message_created_at is None:
                cursor.execute(
                    """
                    SELECT COUNT(*), MAX(created_at)
                    FROM transcript_messages
                    WHERE session_id = ?
                    """,
                    (str(session_id),),
                )
            else:
                cursor.execute(
                    """
                    SELECT COUNT(*), MAX(created_at)
                    FROM transcript_messages
                    WHERE session_id = ? AND created_at > ?
                    """,
                    (str(session_id), float(last_message_created_at)),
                )
            row = cursor.fetchone() or (0, None)
            return int(row[0] or 0), (float(row[1]) if row[1] is not None else None)

    async def maybe_enqueue_auto_summary(self, *, session_id: str) -> Dict[str, Any]:
        sid = str(session_id or "").strip()
        if not sid:
            return {"queued": False, "reason": "empty_session"}
        if not self._auto_summary_enabled():
            return {"queued": False, "reason": "disabled"}
        if sid in self._pending_auto_summary_sessions:
            return {"queued": False, "reason": "already_pending"}

        transcript_session = self.ctx.metadata_store.get_transcript_session(sid)
        if not transcript_session:
            return {"queued": False, "reason": "session_not_found"}

        session_meta = transcript_session.get("metadata") if isinstance(transcript_session, dict) else {}
        if not isinstance(session_meta, dict):
            session_meta = {}

        group_id = str(session_meta.get("group_id", "") or "").strip() or None
        user_id = str(session_meta.get("user_id", "") or "").strip() or None
        if not self.ctx.is_chat_enabled(stream_id=sid, group_id=group_id, user_id=user_id):
            return {"queued": False, "reason": "chat_filtered"}

        summary_state = self.ctx.metadata_store.get_transcript_summary_state(sid) or {}
        cooldown_minutes = float(self.ctx.get_config("summarization.auto_import.cooldown_minutes", 30) or 30)
        cooldown_seconds = max(0.0, cooldown_minutes * 60.0)
        last_summary_at = summary_state.get("last_summary_at")
        now_ts = datetime.datetime.now().timestamp()
        if last_summary_at is not None:
            try:
                if (now_ts - float(last_summary_at)) < cooldown_seconds:
                    return {"queued": False, "reason": "cooldown"}
            except (TypeError, ValueError):
                pass

        new_message_count, last_message_created_at = await self._count_new_transcript_messages(
            session_id=sid,
            last_message_created_at=summary_state.get("last_message_created_at"),
        )
        min_new_messages = max(1, int(self.ctx.get_config("summarization.auto_import.min_new_messages", 12) or 12))
        if new_message_count < min_new_messages:
            return {"queued": False, "reason": "insufficient_new_messages", "new_message_count": new_message_count}

        payload = {
            "session_id": sid,
            "messages": [],
            "source": f"chat_summary:{sid}",
            "context_length": self._auto_summary_context_length(),
            "persist_messages": False,
            "auto_import": True,
            "last_message_created_at": last_message_created_at,
        }
        task = await self.enqueue_summary_task(payload)
        self._pending_auto_summary_sessions.add(sid)
        return {
            "queued": True,
            "task_id": str(task.get("task_id", "") or ""),
            "new_message_count": new_message_count,
        }

    async def run_bulk_summary_import(
        self,
        *,
        limit: Optional[int] = None,
        context_length: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Run bulk summarization for transcript sessions.

        This shares the same code path as the scheduled import loop, but can be
        triggered from commands / external callers.
        """
        async with self._bulk_summary_lock:
            return await self._perform_bulk_summary_import(limit=limit, context_length=context_length)

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        return self.ctx.metadata_store.get_async_task(task_id)

    async def _import_worker(self, worker_idx: int) -> None:
        while not self._stopping:
            task_id = ""
            dequeued = False
            try:
                task_id, payload = await self.import_queue.get()
                dequeued = True
                existing = self.ctx.metadata_store.get_async_task(task_id)
                if existing and existing.get("cancel_requested"):
                    self.ctx.metadata_store.update_async_task(
                        task_id=task_id,
                        status=TASK_STATUS_CANCELED,
                        finished_at=datetime.datetime.now().timestamp(),
                    )
                    continue

                now = datetime.datetime.now().timestamp()
                self.ctx.metadata_store.update_async_task(task_id=task_id, status=TASK_STATUS_RUNNING, started_at=now)
                mode = str(payload.get("mode", "text"))
                body = payload.get("payload")
                options = payload.get("options") if isinstance(payload.get("options"), dict) else {}
                result = await self.import_service.run_import(mode=mode, payload=body, options=options)
                self.ctx.metadata_store.update_async_task(
                    task_id=task_id,
                    status=TASK_STATUS_SUCCEEDED,
                    result=result,
                    finished_at=datetime.datetime.now().timestamp(),
                )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if task_id:
                    self.ctx.metadata_store.update_async_task(
                        task_id=task_id,
                        status=TASK_STATUS_FAILED,
                        error_message=str(exc),
                        finished_at=datetime.datetime.now().timestamp(),
                    )
                logger.error("Import worker %s failed task %s: %s", worker_idx, task_id, exc, exc_info=True)
            finally:
                if dequeued:
                    self.import_queue.task_done()

    async def _summary_worker(self, worker_idx: int) -> None:
        while not self._stopping:
            task_id = ""
            dequeued = False
            payload: Dict[str, Any] = {}
            try:
                task_id, payload = await self.summary_queue.get()
                dequeued = True
                existing = self.ctx.metadata_store.get_async_task(task_id)
                if existing and existing.get("cancel_requested"):
                    self.ctx.metadata_store.update_async_task(
                        task_id=task_id,
                        status=TASK_STATUS_CANCELED,
                        finished_at=datetime.datetime.now().timestamp(),
                    )
                    continue

                self.ctx.metadata_store.update_async_task(
                    task_id=task_id,
                    status=TASK_STATUS_RUNNING,
                    started_at=datetime.datetime.now().timestamp(),
                )
                session_id = str(payload.get("session_id", "")).strip() or uuid.uuid4().hex
                messages = payload.get("messages")
                if not isinstance(messages, list):
                    messages = []
                source = str(payload.get("source", f"chat_summary:{session_id}"))
                context_length = int(payload.get("context_length", self.ctx.get_config("summarization.context_length", 50)))
                persist_messages = bool(payload.get("persist_messages", False))
                result = await self.summary_service.import_from_transcript(
                    session_id=session_id,
                    messages=messages,
                    source=source,
                    context_length=context_length,
                    persist_messages=persist_messages,
                )
                status = TASK_STATUS_SUCCEEDED if result.get("success") else TASK_STATUS_FAILED
                self.ctx.metadata_store.update_async_task(
                    task_id=task_id,
                    status=status,
                    result=result,
                    error_message="" if result.get("success") else str(result.get("message", "")),
                    finished_at=datetime.datetime.now().timestamp(),
                )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if task_id:
                    self.ctx.metadata_store.update_async_task(
                        task_id=task_id,
                        status=TASK_STATUS_FAILED,
                        error_message=str(exc),
                        finished_at=datetime.datetime.now().timestamp(),
                    )
                logger.error("Summary worker %s failed task %s: %s", worker_idx, task_id, exc, exc_info=True)
            finally:
                session_id = str(payload.get("session_id", "") or "").strip()
                if session_id and bool(payload.get("auto_import", False)):
                    self._pending_auto_summary_sessions.discard(session_id)
                if dequeued:
                    self.summary_queue.task_done()

    async def _auto_save_loop(self) -> None:
        while not self._stopping:
            try:
                interval_min = float(self.ctx.get_config("advanced.auto_save_interval_minutes", 5))
                await asyncio.sleep(max(30.0, interval_min * 60.0))
                if bool(self.ctx.get_config("advanced.enable_auto_save", True)):
                    await self.ctx.save_all()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Auto-save loop error: %s", exc)

    async def _scheduled_summary_loop(self) -> None:
        logger.info("Scheduled summary loop started")
        last_check = datetime.datetime.now()
        while not self._stopping:
            try:
                await asyncio.sleep(60)
                if not bool(self.ctx.get_config("summarization.enabled", True)):
                    last_check = datetime.datetime.now()
                    continue
                if not bool(self.ctx.get_config("schedule.enabled", True)):
                    last_check = datetime.datetime.now()
                    continue

                now = datetime.datetime.now()
                import_times = self.ctx.get_config("schedule.import_times", ["04:00"])
                if not isinstance(import_times, list):
                    import_times = ["04:00"]

                for t_str in import_times:
                    text = str(t_str or "").strip()
                    if not text:
                        continue
                    try:
                        hour, minute = map(int, text.split(":", 1))
                        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                        if last_check < target <= now:
                            logger.info("trigger scheduled summary import at %s", text)
                            await self.run_bulk_summary_import()
                    except Exception as exc:
                        logger.warning("invalid schedule.import_times value: %s (%s)", text, exc)

                last_check = now
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Scheduled summary loop error: %s", exc, exc_info=True)
                await asyncio.sleep(60)

    async def _perform_bulk_summary_import(
        self,
        *,
        limit: Optional[int] = None,
        context_length: Optional[int] = None,
    ) -> Dict[str, Any]:
        conn = self.ctx.metadata_store._conn
        if conn is None:
            return {"success": 0, "skipped": 0, "failed": 0, "candidates": 0, "message": "metadata store not ready"}

        resolved_context_length = (
            max(1, int(context_length))
            if context_length is not None
            else max(1, int(self.ctx.get_config("summarization.context_length", 50) or 50))
        )
        source_mode = str(self.ctx.get_config("summarization.source_mode", "hybrid") or "hybrid").strip().lower()
        resolved_limit = max(1, int(limit)) if limit is not None else 500
        cursor = conn.cursor()
        cursor.execute(
            """
            WITH last_msgs AS (
                SELECT session_id, MAX(created_at) AS last_msg_created_at
                FROM transcript_messages
                GROUP BY session_id
            )
            SELECT s.session_id, s.metadata, lm.last_msg_created_at, st.last_message_created_at
            FROM transcript_sessions s
            JOIN last_msgs lm ON lm.session_id = s.session_id
            LEFT JOIN transcript_summary_state st ON st.session_id = s.session_id
            WHERE st.last_message_created_at IS NULL OR lm.last_msg_created_at > st.last_message_created_at
            ORDER BY lm.last_msg_created_at DESC
            LIMIT ?
            """,
            (resolved_limit,),
        )
        rows = cursor.fetchall()
        if not rows:
            logger.debug("scheduled summary skipped: no transcript sessions")
            return {"success": 0, "skipped": 0, "failed": 0, "candidates": 0}

        success_count = 0
        skipped_count = 0
        fail_count = 0

        for row in rows:
            session_id = str(row[0] or "").strip()
            if not session_id:
                skipped_count += 1
                continue

            metadata: Dict[str, Any] = {}
            raw_meta = row[1]
            if raw_meta:
                try:
                    metadata = json.loads(raw_meta) if isinstance(raw_meta, str) else {}
                except Exception:
                    metadata = {}

            group_id = str(metadata.get("group_id", "") or "").strip() or None
            user_id = str(metadata.get("user_id", "") or "").strip() or None
            if not self.ctx.is_chat_enabled(stream_id=session_id, group_id=group_id, user_id=user_id):
                skipped_count += 1
                continue

            transcript_messages = self.ctx.metadata_store.get_transcript_messages(
                session_id,
                limit=resolved_context_length,
            )
            if source_mode == "transcript" and not transcript_messages:
                skipped_count += 1
                continue
            messages = transcript_messages if transcript_messages else []

            try:
                result = await self.summary_service.import_from_transcript(
                    session_id=session_id,
                    messages=messages,
                    source=f"chat_summary:{session_id}",
                    context_length=resolved_context_length,
                )
                if bool(result.get("success")):
                    success_count += 1
                else:
                    fail_count += 1
            except Exception:
                fail_count += 1
                logger.warning("scheduled summary import failed: session=%s", session_id, exc_info=True)

        logger.info(
            "scheduled summary finished: success=%s skipped=%s failed=%s candidates=%s",
            success_count,
            skipped_count,
            fail_count,
            len(rows),
        )
        return {
            "success": success_count,
            "skipped": skipped_count,
            "failed": fail_count,
            "candidates": len(rows),
        }

    async def _process_reinforce_batch(self, hashes: List[str]) -> None:
        if not hashes:
            return
        status_map = self.ctx.metadata_store.get_relation_status_batch(hashes)
        if not status_map:
            return

        now = datetime.datetime.now().timestamp()
        cooldown = float(self.ctx.get_config("memory.reinforce_cooldown_hours", 1.0) or 1.0) * 3600.0
        max_weight = float(self.ctx.get_config("memory.max_weight", 10.0) or 10.0)
        revive_boost = float(self.ctx.get_config("memory.revive_boost_weight", 0.5) or 0.5)
        auto_protect = float(self.ctx.get_config("memory.auto_protect_ttl_hours", 24.0) or 24.0) * 3600.0

        async with self.ctx.metadata_store._async_lock:
            cursor = self.ctx.metadata_store._conn.cursor()
            placeholders = ",".join(["?"] * len(hashes))
            cursor.execute(f"SELECT hash, subject, object FROM relations WHERE hash IN ({placeholders})", hashes)
            relation_info = {str(row[0]): (str(row[1]), str(row[2])) for row in cursor.fetchall()}

        hashes_to_reinforce: List[str] = []
        hashes_to_revive: List[str] = []

        for hash_value in hashes:
            status = status_map.get(hash_value)
            info = relation_info.get(hash_value)
            if not status or not info:
                continue
            if not bool(status.get("is_inactive")):
                last_reinforced = float(status.get("last_reinforced") or 0.0)
                if (now - last_reinforced) < cooldown:
                    continue

            src, tgt = info
            current_weight = float(status.get("weight") or 0.0)
            delta = 1.0 * (1.0 - (current_weight / max(0.1, max_weight)))
            delta = max(0.0, delta)
            self.ctx.graph_store.update_edge_weight(src, tgt, delta, max_weight=max_weight)

            if bool(status.get("is_inactive")):
                hashes_to_revive.append(hash_value)
            else:
                hashes_to_reinforce.append(hash_value)

        if hashes_to_revive:
            self.ctx.metadata_store.mark_relations_active(hashes_to_revive, boost_weight=revive_boost)
            self.ctx.metadata_store.update_relations_protection(
                hashes_to_revive,
                protected_until=now + auto_protect,
                last_reinforced=now,
            )
            logger.info("memory reinforce revived relations: %s", len(hashes_to_revive))

        if hashes_to_reinforce:
            self.ctx.metadata_store.update_relations_protection(
                hashes_to_reinforce,
                protected_until=now + auto_protect,
                last_reinforced=now,
            )

    async def _process_freeze_and_prune(self) -> None:
        now = datetime.datetime.now().timestamp()
        prune_threshold = float(self.ctx.get_config("memory.prune_threshold", 0.1) or 0.1)
        freeze_duration = float(self.ctx.get_config("memory.freeze_duration_hours", 24.0) or 24.0) * 3600.0

        low_edges = self.ctx.graph_store.get_low_weight_edges(prune_threshold)
        hashes_to_freeze: List[str] = []
        edges_to_deactivate: List[Tuple[str, str]] = []

        for src, tgt in low_edges:
            src_canon = self.ctx.graph_store._canonicalize(src)  # noqa: SLF001
            tgt_canon = self.ctx.graph_store._canonicalize(tgt)  # noqa: SLF001
            if src_canon not in self.ctx.graph_store._node_to_idx:  # noqa: SLF001
                continue
            if tgt_canon not in self.ctx.graph_store._node_to_idx:  # noqa: SLF001
                continue
            src_idx = self.ctx.graph_store._node_to_idx[src_canon]  # noqa: SLF001
            tgt_idx = self.ctx.graph_store._node_to_idx[tgt_canon]  # noqa: SLF001
            edge_hashes = list(self.ctx.graph_store._edge_hash_map.get((src_idx, tgt_idx), set()))  # noqa: SLF001
            if not edge_hashes:
                continue

            statuses = self.ctx.metadata_store.get_relation_status_batch(edge_hashes)
            is_protected = False
            current_hashes: List[str] = []
            for hash_value, status in statuses.items():
                if bool(status.get("is_pinned")) or float(status.get("protected_until") or 0.0) > now:
                    is_protected = True
                    break
                if not bool(status.get("is_inactive")):
                    current_hashes.append(hash_value)
            if not is_protected and current_hashes:
                hashes_to_freeze.extend(current_hashes)
                edges_to_deactivate.append((src, tgt))

        if hashes_to_freeze:
            self.ctx.metadata_store.mark_relations_inactive(hashes_to_freeze, inactive_since=now)
            self.ctx.graph_store.deactivate_edges(edges_to_deactivate)
            logger.info(
                "memory freeze complete: relations=%s edges=%s",
                len(hashes_to_freeze),
                len(edges_to_deactivate),
            )

        cutoff = now - max(1.0, freeze_duration)
        expired_hashes = self.ctx.metadata_store.get_prune_candidates(cutoff)
        if expired_hashes:
            cursor = self.ctx.metadata_store._conn.cursor()
            placeholders = ",".join(["?"] * len(expired_hashes))
            cursor.execute(
                f"SELECT hash, subject, object FROM relations WHERE hash IN ({placeholders})",
                expired_hashes,
            )
            ops = [(str(row[1]), str(row[2]), str(row[0])) for row in cursor.fetchall()]
            if ops:
                self.ctx.graph_store.prune_relation_hashes(ops)
            deleted = self.ctx.metadata_store.backup_and_delete_relations(expired_hashes)
            logger.info("memory prune complete: relations=%s", deleted)

        self.ctx.graph_store.save()

    async def _orphan_gc_phase(self) -> None:
        orphan_cfg = self.ctx.get_config("memory.orphan", {}) or {}
        if not isinstance(orphan_cfg, dict):
            orphan_cfg = {}
        if not bool(orphan_cfg.get("enable_soft_delete", True)):
            return

        entity_retention = float(orphan_cfg.get("entity_retention_days", 7.0) or 7.0) * 86400.0
        para_retention = float(orphan_cfg.get("paragraph_retention_days", 7.0) or 7.0) * 86400.0
        grace = float(orphan_cfg.get("sweep_grace_hours", 24.0) or 24.0) * 3600.0

        isolated = self.ctx.graph_store.get_isolated_nodes(include_inactive=True)
        if isolated:
            entity_candidates = self.ctx.metadata_store.get_entity_gc_candidates(isolated, entity_retention)
            if entity_candidates:
                marked = self.ctx.metadata_store.mark_as_deleted(entity_candidates, "entity")
                if marked > 0:
                    logger.info("orphan gc mark entities: %s", marked)

        para_candidates = self.ctx.metadata_store.get_paragraph_gc_candidates(para_retention)
        if para_candidates:
            marked = self.ctx.metadata_store.mark_as_deleted(para_candidates, "paragraph")
            if marked > 0:
                logger.info("orphan gc mark paragraphs: %s", marked)

        dead_paras = self.ctx.metadata_store.sweep_deleted_items("paragraph", grace)
        if dead_paras:
            para_hashes = [str(item[0]) for item in dead_paras if item and item[0]]
            deleted = self.ctx.metadata_store.physically_delete_paragraphs(para_hashes)
            if deleted > 0:
                logger.info("orphan gc sweep paragraphs: %s", deleted)

        dead_entities = self.ctx.metadata_store.sweep_deleted_items("entity", grace)
        if dead_entities:
            entity_hashes = [str(item[0]) for item in dead_entities if item and item[0]]
            entity_names = [str(item[1]) for item in dead_entities if item and item[1]]
            if entity_names:
                self.ctx.graph_store.delete_nodes(entity_names)
            deleted = self.ctx.metadata_store.physically_delete_entities(entity_hashes)
            if deleted > 0:
                logger.info("orphan gc sweep entities: %s", deleted)

    async def _memory_maintenance_loop(self) -> None:
        while not self._stopping:
            try:
                interval_h = float(self.ctx.get_config("memory.base_decay_interval_hours", 1.0))
                interval_s = max(60.0, interval_h * 3600.0)
                await asyncio.sleep(interval_s)
                if not bool(self.ctx.get_config("memory.enabled", True)):
                    continue

                async with self.ctx.memory_lock:
                    if bool(self.ctx.get_config("memory.enable_auto_reinforce", True)):
                        buffer_hashes = list(self.ctx.reinforce_buffer)
                        self.ctx.reinforce_buffer.clear()
                        if buffer_hashes:
                            await self._process_reinforce_batch(buffer_hashes)

                    half_life = float(self.ctx.get_config("memory.half_life_hours", 24.0))
                    if half_life > 0:
                        factor = 0.5 ** (interval_h / half_life)
                        self.ctx.graph_store.decay(factor)

                    await self._process_freeze_and_prune()
                    await self._orphan_gc_phase()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Memory maintenance loop error: %s", exc, exc_info=True)

    async def _person_profile_refresh_loop(self) -> None:
        while not self._stopping:
            try:
                interval_min = int(self.ctx.get_config("person_profile.refresh_interval_minutes", 30))
                await asyncio.sleep(max(60, interval_min * 60))
                if not bool(self.ctx.get_config("person_profile.enabled", True)):
                    continue

                active_window_h = float(self.ctx.get_config("person_profile.active_window_hours", 72.0))
                active_after = datetime.datetime.now().timestamp() - max(1.0, active_window_h * 3600.0)
                limit = int(self.ctx.get_config("person_profile.max_refresh_per_cycle", 50))
                top_k = int(self.ctx.get_config("person_profile.top_k_evidence", 12))
                opt_in_required = bool(self.ctx.get_config("person_profile.opt_in_required", True))
                default_enabled = bool(self.ctx.get_config("person_profile.default_injection_enabled", False))
                global_enabled = bool(self.ctx.get_config("person_profile.global_injection_enabled", False))

                if global_enabled:
                    pids = self.ctx.metadata_store.get_active_person_ids(
                        active_after=active_after,
                        limit=limit,
                    )
                elif opt_in_required:
                    pids = self.ctx.metadata_store.get_active_person_ids_for_enabled_switches(
                        active_after=active_after,
                        limit=limit,
                    )
                elif default_enabled:
                    pids = self.ctx.metadata_store.get_active_person_ids(
                        active_after=active_after,
                        limit=limit,
                    )
                else:
                    pids = []

                for pid in pids:
                    try:
                        await self.person_service.query(
                            person_id=pid,
                            top_k=top_k,
                            force_refresh=False,
                            source_note="task:person_profile_refresh",
                        )
                    except Exception:
                        continue
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Person profile refresh loop error: %s", exc)
