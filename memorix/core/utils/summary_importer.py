"""
聊天总结与知识导入工具（独立版）。
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Tuple

from astrbot.api import logger

from ...amemorix.llm_client import LLMClient
from ..embedding.api_adapter import EmbeddingAPIAdapter
from ..storage import (
    GraphStore,
    KnowledgeType,
    MetadataStore,
    VectorStore,
    get_knowledge_type_from_string,
)

SUMMARY_PROMPT_TEMPLATE = """
你是 {bot_name}。{personality_context}
现在你需要对以下一段聊天记录进行总结，并提取其中的重要知识。

聊天记录内容：
{chat_history}

请完成以下任务：
1. **生成总结**：以第三人称或机器人的视角，简洁明了地总结这段对话的主要内容、发生的事件或讨论的主题。
2. **提取实体与关系**：识别并提取对话中提到的重要实体（人名、地名、事物等）以及它们之间的关系。

请严格以 JSON 格式输出，格式如下：
{{
  "summary": "总结文本内容",
  "entities": ["张三", "李四"],
  "relations": [
    {{"subject": "张三", "predicate": "认识", "object": "李四"}}
  ]
}}

注意：
1. 总结应具有叙事性，能够作为长程记忆的一部分。
2. 直接使用实体的实际名称，不要使用 e1/e2 等代号。
3. 实体与关系尽量使用原文措辞。
4. 如果没有明确的关系，relations 返回空数组。
5. 不要把“用户/user/assistant/AI助手/机器人/我/你”等角色称谓或代词当作实体。
"""


_GENERIC_ENTITY_STOPWORDS = {
    # Role / speaker placeholders
    "user",
    "assistant",
    "system",
    "tool",
    "bot",
    # Chinese placeholders
    "用户",
    "助手",
    "ai助手",
    "ai助理",
    "机器人",
    # Common pronouns (tends to create a giant hub node)
    "我",
    "你",
    "他",
    "她",
    "它",
    "我们",
    "你们",
    "他们",
    "她们",
    "它们",
    "大家",
}


def _canon_entity_token(text: Any) -> str:
    """Normalize entity token for stopword checks."""
    if text is None:
        return ""
    return "".join(str(text).strip().lower().split())


def _is_generic_entity(name: str) -> bool:
    return _canon_entity_token(name) in _GENERIC_ENTITY_STOPWORDS


class SummaryImporter:
    def __init__(
        self,
        vector_store: VectorStore,
        graph_store: GraphStore,
        metadata_store: MetadataStore,
        embedding_manager: EmbeddingAPIAdapter,
        plugin_config: dict,
        llm_client: Optional[LLMClient] = None,
        astrbot_context: Optional[Any] = None,
    ):
        self.vector_store = vector_store
        self.graph_store = graph_store
        self.metadata_store = metadata_store
        self.embedding_manager = embedding_manager
        self.plugin_config = plugin_config or {}
        self.llm_client = llm_client
        self._astrbot_context = astrbot_context

    def _cfg(self, key: str, default: Any = None) -> Any:
        current: Any = self.plugin_config if isinstance(self.plugin_config, dict) else {}
        for part in key.split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return default
        return current

    def _build_chat_text(self, messages: List[Dict[str, Any]], bot_name: str = "") -> str:
        lines: List[str] = []
        now = time.time()
        for item in messages:
            role = str(item.get("role", "user") or "user")
            content = str(item.get("content", "") or "").strip()
            if not content:
                continue
            meta = item.get("metadata") or {}
            sender_name = str(meta.get("sender_name", "") or "").strip()
            sender_id = str(meta.get("sender_id", "") or "").strip()
            if role == "assistant":
                display_name = bot_name or sender_name or "AI助手"
            elif sender_name:
                display_name = sender_name
            elif sender_id:
                display_name = sender_id
            else:
                display_name = role
            ts = item.get("timestamp") or item.get("created_at")
            time_str = self._format_relative_time(ts, now) if ts else ""
            if time_str:
                lines.append(f"{time_str}, {display_name}: {content}")
            else:
                lines.append(f"{display_name}: {content}")
        return "\n".join(lines)

    @staticmethod
    def _format_relative_time(ts: Any, now: float) -> str:
        try:
            diff = now - float(ts)
            if diff < 0:
                diff = 0
            if diff < 60:
                return "刚刚"
            if diff < 3600:
                return f"{int(diff // 60)}分钟前"
            if diff < 86400:
                return f"{int(diff // 3600)}小时前"
            if diff < 604800:
                return f"{int(diff // 86400)}天前"
            from datetime import datetime
            return datetime.fromtimestamp(float(ts)).strftime("%m月%d日")
        except (TypeError, ValueError):
            return ""

    def _fallback_summary(self, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
        merged = " ".join(str(m.get("content", "") or "").strip() for m in messages if str(m.get("content", "")).strip())
        merged = merged[:500]
        return {"summary": merged or "暂无可总结内容", "entities": [], "relations": []}

    async def _resolve_persona(self) -> Tuple[str, str]:
        """从 AstrBot persona_manager 获取 bot 名称和人格描述。"""
        ctx = self._astrbot_context
        if ctx is None:
            return "AI助手", ""
        try:
            persona_mgr = getattr(ctx, "persona_manager", None)
            if persona_mgr is None:
                return "AI助手", ""
            get_default = getattr(persona_mgr, "get_default_persona_v3", None)
            if callable(get_default):
                import inspect
                personality = get_default(umo=None)
                if inspect.isawaitable(personality):
                    personality = await personality
                if isinstance(personality, dict):
                    bot_name = str(personality.get("name", "") or "").strip() or "AI助手"
                    persona_text = str(personality.get("prompt", "") or "").strip()
                    return bot_name, persona_text
        except Exception as exc:
            logger.debug("resolve AstrBot persona failed: %s", exc)
        return "AI助手", ""

    async def _generate_summary_payload(self, messages: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not messages:
            return self._fallback_summary(messages)

        # 从 AstrBot 人格系统获取 bot 名称和人设
        bot_name, persona_text = await self._resolve_persona()

        history = self._build_chat_text(messages, bot_name=bot_name)

        if persona_text:
            personality_context = f"你的人设信息如下：{persona_text}"
        else:
            personality_context = ""

        prompt = SUMMARY_PROMPT_TEMPLATE.format(
            bot_name=bot_name,
            personality_context=personality_context,
            chat_history=history,
        )

        if self.llm_client is None:
            logger.error(
                "LLM client is None — cannot generate summary, skipping import.",
            )
            return None

        try:
            ok, payload, raw = await self.llm_client.complete_json(prompt, temperature=0.2, max_tokens=1200)
            if ok and isinstance(payload, dict):
                return payload
            logger.warning("Summary LLM returned non-JSON, fallback parser used. raw=%s", raw[:200] if raw else "(empty)")
            if raw:
                start = raw.find("{")
                end = raw.rfind("}")
                if start >= 0 and end > start:
                    try:
                        parsed = json.loads(raw[start : end + 1])
                        if isinstance(parsed, dict):
                            return parsed
                    except json.JSONDecodeError:
                        pass
        except Exception as exc:
            logger.error("Summary LLM call failed, skipping import: %s", exc, exc_info=True)
            return None

        logger.error("Summary LLM returned unparseable response, skipping import.")
        return None

    async def import_from_transcript(
        self,
        *,
        session_id: str,
        messages: List[Dict[str, Any]],
        source: str = "",
        context_length: Optional[int] = None,
        persist_messages: bool = False,
    ) -> Tuple[bool, str]:
        try:
            existing_session = self.metadata_store.get_transcript_session(session_id)
            existing_metadata: Dict[str, Any] = {}
            existing_source = ""
            if isinstance(existing_session, dict):
                existing_meta_obj = existing_session.get("metadata")
                if isinstance(existing_meta_obj, dict):
                    existing_metadata = dict(existing_meta_obj)
                existing_source = str(existing_session.get("source", "") or "").strip()

            session_metadata = dict(existing_metadata)
            session_metadata["imported_at"] = time.time()
            session = self.metadata_store.upsert_transcript_session(
                session_id=session_id,
                source=source or existing_source or f"transcript:{session_id}",
                metadata=session_metadata,
            )

            limit = int(context_length) if context_length is not None else int(self._cfg("summarization.context_length", 50))
            normalized_messages: List[Dict[str, Any]] = []
            for item in messages or []:
                if not isinstance(item, dict):
                    continue
                role = str(item.get("role", "user") or "user").strip() or "user"
                content = str(item.get("content", "") or "").strip()
                if not content:
                    continue
                normalized_messages.append(
                    {
                        "role": role,
                        "content": content,
                        "timestamp": item.get("timestamp"),
                        "metadata": item.get("metadata", {}),
                        "created_at": item.get("created_at"),
                    }
                )

            if persist_messages and normalized_messages:
                self.metadata_store.append_transcript_messages(
                    session_id=session["session_id"],
                    messages=normalized_messages,
                )

            if normalized_messages:
                transcript_messages = normalized_messages[-max(1, limit) :]
            else:
                transcript_messages = self.metadata_store.get_transcript_messages(
                    session["session_id"],
                    limit=max(1, limit),
                )

            if not transcript_messages:
                return False, "未找到可总结的聊天记录"

            payload = await self._generate_summary_payload(transcript_messages)
            if payload is None:
                return False, "LLM 调用失败，跳过本次导入"

            summary = str(payload.get("summary", "") or "").strip()
            entities = payload.get("entities", [])
            relations = payload.get("relations", [])
            if not summary:
                return False, "总结为空"

            filtered_entities: List[str] = []
            seen_entities = set()
            if isinstance(entities, list):
                for item in entities:
                    name = str(item or "").strip()
                    if not name:
                        continue
                    if _is_generic_entity(name):
                        continue
                    canon = _canon_entity_token(name)
                    if canon in seen_entities:
                        continue
                    seen_entities.add(canon)
                    filtered_entities.append(name)

            filtered_relations: List[Dict[str, str]] = []
            if isinstance(relations, list):
                for rel in relations:
                    if not isinstance(rel, dict):
                        continue
                    s = str(rel.get("subject", "") or "").strip()
                    p = str(rel.get("predicate", "") or "").strip()
                    o = str(rel.get("object", "") or "").strip()
                    if not (s and p and o):
                        continue
                    if _is_generic_entity(s) or _is_generic_entity(o):
                        continue
                    if s == o:
                        continue
                    filtered_relations.append({"subject": s, "predicate": p, "object": o})

            await self._execute_import(
                summary=summary,
                entities=filtered_entities,
                relations=filtered_relations,
                stream_id=session["session_id"],
            )

            self.vector_store.save()
            self.graph_store.save()

            try:
                last_msg_created_at: Optional[float] = None
                for item in transcript_messages:
                    if not isinstance(item, dict):
                        continue
                    ts_raw = item.get("created_at")
                    if ts_raw is None:
                        continue
                    try:
                        ts_val = float(ts_raw)
                    except (TypeError, ValueError):
                        continue
                    if last_msg_created_at is None or ts_val > last_msg_created_at:
                        last_msg_created_at = ts_val

                if last_msg_created_at is None:
                    tail = self.metadata_store.get_transcript_messages(session["session_id"], limit=1)
                    if tail and isinstance(tail[-1], dict):
                        ts_raw = tail[-1].get("created_at")
                        try:
                            last_msg_created_at = float(ts_raw) if ts_raw is not None else None
                        except (TypeError, ValueError):
                            last_msg_created_at = None

                self.metadata_store.upsert_transcript_summary_state(
                    session_id=session["session_id"],
                    last_summary_at=time.time(),
                    last_message_created_at=last_msg_created_at,
                )
            except Exception:
                logger.debug("update transcript summary state failed: session=%s", session["session_id"], exc_info=True)

            return True, f"总结导入成功: session={session['session_id']}"
        except Exception as exc:
            logger.error("Summary transcript import failed: %s", exc, exc_info=True)
            return False, str(exc)

    async def import_from_stream(
        self,
        stream_id: str,
        context_length: Optional[int] = None,
        include_personality: Optional[bool] = None,
    ) -> Tuple[bool, str]:
        del include_personality
        limit = int(context_length) if context_length is not None else int(self._cfg("summarization.context_length", 50))
        messages = self.metadata_store.get_transcript_messages(stream_id, limit=max(1, limit))
        if not messages:
            return False, "未找到可总结的聊天记录（请先写入 transcript）"
        return await self.import_from_transcript(
            session_id=stream_id,
            messages=messages,
            source=f"chat_summary:{stream_id}",
            context_length=limit,
        )

    async def _execute_import(
        self,
        summary: str,
        entities: List[str],
        relations: List[Dict[str, str]],
        stream_id: str,
        time_meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not entities and not relations:
            logger.warning(
                "Summary for session=%s produced 0 entities and 0 relations. "
                "Knowledge graph will NOT grow from this import. "
                "This usually means the LLM call failed or returned poor results.",
                stream_id,
            )

        type_str = self._cfg("summarization.default_knowledge_type", "narrative")
        knowledge_type = get_knowledge_type_from_string(type_str) or KnowledgeType.NARRATIVE

        hash_value = self.metadata_store.add_paragraph(
            content=summary,
            source=f"chat_summary:{stream_id}",
            knowledge_type=knowledge_type.value,
            time_meta=time_meta,
        )

        embedding = await self.embedding_manager.encode(summary)
        self.vector_store.add(vectors=embedding.reshape(1, -1), ids=[hash_value])

        if entities:
            self.graph_store.add_nodes([str(e) for e in entities if str(e).strip()])

        for rel in relations:
            s = str(rel.get("subject", "")).strip()
            p = str(rel.get("predicate", "")).strip()
            o = str(rel.get("object", "")).strip()
            if not (s and p and o):
                continue
            rel_hash = self.metadata_store.add_relation(
                subject=s,
                predicate=p,
                obj=o,
                confidence=1.0,
                source_paragraph=hash_value,
            )
            self.graph_store.add_edges([(s, o)], relation_hashes=[rel_hash])

        logger.info("Summary imported: %s", hash_value[:8])
