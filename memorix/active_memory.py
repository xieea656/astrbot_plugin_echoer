"""AI autonomous memory — LLM tool that allows the AI to actively decide what to remember.

Uses AstrBot's StarTools.register_llm_tool() API to register a `memorize` tool
that the AI can call autonomously when it identifies information worth retaining.
"""

from __future__ import annotations

from typing import Any, Dict

from astrbot.api import logger

MEMORIZE_TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "memorize",
        "description": (
            "当对话中出现值得长期记住的重要信息时调用此工具。包括但不限于："
            "用户的个人信息（姓名、年龄、职业、位置）、偏好（喜欢/不喜欢）、"
            "计划（即将要做的事）、关键事实、重要决策、关系信息等。"
            "不要记住琐碎的闲聊或一次性信息。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "要记住的信息内容。用自然语言描述，尽量包含主语、谓语、宾语等完整语义。",
                },
                "knowledge_type": {
                    "type": "string",
                    "enum": ["factual", "narrative", "structured", "auto"],
                    "description": "知识类型：factual=事实型事实，narrative=叙事型描述，structured=结构化信息，auto=自动判断。默认 auto。",
                },
                "importance": {
                    "type": "number",
                    "description": "重要性 1-10。10 表示极其重要（如用户姓名、核心偏好），5 表示一般重要（如临时计划），1 表示低优先级。默认 5。",
                },
            },
            "required": ["content"],
        },
    },
}


def make_memorize_handler(scope_key_getter, runtime_manager: Any, event_getter):
    """Build the async handler for the memorize tool.

    Returns an async callable suitable for register_llm_tool().
    """

    async def handler(_event, content: str, knowledge_type: str = "auto", importance: float = 5.0) -> Dict[str, Any]:
        text = str(content or "").strip()
        if not text:
            return {"success": False, "message": "内容为空，无法记忆"}

        ktype = str(knowledge_type or "auto").strip().lower()
        if ktype not in {"factual", "narrative", "structured", "mixed", "auto"}:
            ktype = "auto"

        importance_val = max(1.0, min(10.0, float(importance or 5.0)))

        scope_key = scope_key_getter()
        event = event_getter()

        from .amemorix.services.import_service import ImportService

        runtime = await runtime_manager.get_runtime(scope_key)
        ctx = runtime.context

        source = "active_memory"
        if event is not None:
            try:
                from .adapters.astrbot_event_adapter import AstrbotEventAdapter

                adapted = AstrbotEventAdapter.from_event(event, scope_key)
                source = f"active_memory:{adapted.platform}:{adapted.session_id}"
            except Exception:
                pass

        try:
            import_service = ImportService(ctx)
            result = await import_service.run_import(
                mode="text",
                payload={"text": text, "name": f"mem_{_short_hash(text)}"},
                options={
                    "knowledge_type": ktype,
                    "source": source,
                    "importance": importance_val,
                    "auto_detect_entities": True,
                },
            )

            if result.get("success"):
                count = 0
                for key in ("paragraphs", "relations", "entities"):
                    val = result.get(key, 0)
                    count += val if isinstance(val, int) else len(val) if isinstance(val, (list, tuple)) else 0
                logger.info(
                    "[echoer] active_memory: wrote %s items to scope=%s importance=%.0f type=%s",
                    count, scope_key, importance_val, ktype,
                )
                return {"success": True, "message": f"已记住 {count} 条信息", "items_written": count}
            else:
                return {"success": False, "message": str(result.get("message", "写入失败"))}
        except Exception as exc:
            logger.error("[echoer] active_memory failed: %s", exc, exc_info=True)
            return {"success": False, "message": f"记忆写入异常: {exc}"}

    return handler


def _short_hash(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
