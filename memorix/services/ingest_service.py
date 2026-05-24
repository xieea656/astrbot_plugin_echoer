"""Ingest orchestration service."""

from __future__ import annotations

from typing import Any, Dict, Optional

from ..app_context import ScopeRuntimeManager


class IngestService:
    def __init__(self, runtime_manager: ScopeRuntimeManager, plugin_config: Dict[str, Any]):
        self.runtime_manager = runtime_manager
        self.plugin_config = plugin_config or {}

    async def ingest_message(
        self,
        *,
        scope_key: str,
        session_id: str,
        role: str,
        content: str,
        source: str,
        user_id: str = "",
        group_id: str = "",
        platform: str = "",
        unified_msg_origin: str = "",
        sender_name: str = "",
        message_id: str = "",
        role_origin: str = "",
        timestamp: float = 0,
        time_meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        text = str(content or "").strip()
        if not text and bool(self.plugin_config.get("ingest", {}).get("skip_empty_text", True)):
            return {"success": True, "skipped": True, "reason": "empty"}

        runtime = await self.runtime_manager.get_runtime(scope_key)
        ctx = runtime.context
        session = str(session_id or "").strip() or f"scope:{scope_key}"

        # 仅将原始消息存入 transcript（聊天记录表），不做向量化。
        # 向量库和知识图谱仅由 LLM 总结提炼后写入，避免低质量碎片污染检索空间。
        ctx.metadata_store.upsert_transcript_session(
            session_id=session,
            source=source,
            metadata={
                "scope_key": scope_key,
                "user_id": str(user_id or "").strip(),
                "group_id": str(group_id or "").strip(),
                "platform": str(platform or "").strip(),
                "unified_msg_origin": str(unified_msg_origin or "").strip(),
            },
        )
        msg_record = {"role": str(role or "user"), "content": text}
        ts_val = float(timestamp) if timestamp else None
        if ts_val:
            msg_record["timestamp"] = ts_val
        msg_meta = {
            "sender_name": str(sender_name or "").strip(),
            "sender_id": str(user_id or "").strip(),
            "group_id": str(group_id or "").strip(),
            "platform": str(platform or "").strip(),
            "session_id": session,
            "unified_msg_origin": str(unified_msg_origin or "").strip(),
            "message_id": str(message_id or "").strip(),
            "role_origin": str(role_origin or role or "").strip(),
        }
        filtered_meta = {key: value for key, value in msg_meta.items() if value}
        if filtered_meta:
            msg_record["metadata"] = filtered_meta
        ctx.metadata_store.append_transcript_messages(
            session_id=session,
            messages=[msg_record],
        )

        return {"success": True, "skipped": False, "result": {"mode": "transcript_only"}}
