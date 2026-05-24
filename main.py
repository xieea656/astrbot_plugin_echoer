from __future__ import annotations

import contextvars
import time
from typing import Any, Dict, Optional

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .memorix.adapters.astrbot_event_adapter import AstrbotEventAdapter
from .memorix.app_context import ScopeRuntimeManager
from .memorix.commands.mem_commands import to_pretty_text
from .memorix.commands.parser import (
    bool_cfg,
    is_command_message,
    parse_direct_command_tokens,
    parse_tail,
    parse_tail_tokens,
    to_float,
    to_int,
)
from .memorix.injection import build_and_inject_memory
from .memorix.profile_manager import ProfileManager
from .memorix.scope_router import ScopeRouter
from .memorix.services import IngestService, MemoryService, QueryService, SummaryService
from .memorix.tasks.maintenance_scheduler import MaintenanceScheduler
from .memorix.webui import EmbeddedWebUIServer


_active_scope_key_var: contextvars.ContextVar[str] = contextvars.ContextVar("active_scope_key", default="")
_active_event_var: contextvars.ContextVar[Any] = contextvars.ContextVar("active_event", default=None)


@register("astrbot_plugin_echoer", "爱丽丝", "Echoer — 为 AstrBot 打造的完整记忆系统：图谱+向量混合检索，记忆生命周期管理，AI 主动记忆，内嵌 WebUI", "1.0.0")
class MemorixPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self._config_obj = config if hasattr(config, "save_config") else None
        self.config = dict(config or {})
        self.scope_router = ScopeRouter(mode=str(self.config.get("scope", {}).get("mode", "group_global")))
        self.runtime_manager = ScopeRuntimeManager(
            plugin_name="astrbot_plugin_echoer",
            plugin_config=self.config,
            astrbot_context=context,
        )

        self.ingest_service = IngestService(self.runtime_manager, self.config)
        self.query_service = QueryService(self.runtime_manager)
        self.memory_service = MemoryService(self.runtime_manager)
        self.profile_manager = ProfileManager(self.runtime_manager)
        self.summary_service = SummaryService(self.runtime_manager)
        self.maintenance_scheduler = MaintenanceScheduler(runtime_manager=self.runtime_manager)
        self.webui_server = EmbeddedWebUIServer(self.runtime_manager, self.config)


    async def initialize(self):
        logger.info("[echoer] initialize start")

        # Register AI autonomous memory tool
        from .memorix.active_memory import MEMORIZE_TOOL_DEFINITION, make_memorize_handler

        plugin_self = self

        handler = make_memorize_handler(
            scope_key_getter=lambda: _active_scope_key_var.get(),
            runtime_manager=plugin_self.runtime_manager,
            event_getter=lambda: _active_event_var.get(),
        )

        try:
            # Prefer register_llm_tool (v4 API, wraps handler into FunctionTool internally)
            from astrbot.api.star import StarTools

            StarTools.register_llm_tool(
                name="memorize",
                func_args=[
                    {"type": "string", "name": "content", "description": "要记住的信息内容。用自然语言描述，尽量包含主语、谓语、宾语等完整语义。"},
                    {"type": "string", "name": "knowledge_type", "description": "知识类型：factual=事实型事实，narrative=叙事型描述，structured=结构化信息，auto=自动判断。默认 auto。"},
                    {"type": "number", "name": "importance", "description": "重要性 1-10。10 表示极其重要，5 表示一般，1 表示低优先级。默认 5。"},
                ],
                desc=MEMORIZE_TOOL_DEFINITION["function"]["description"],
                func_obj=handler,
            )
            logger.info("[echoer] active_memory tool registered: memorize")
        except (ImportError, AttributeError):
            # Fallback: try add_llm_tools on the context (v5+ API)
            try:
                self.context.add_llm_tools(handler)
                logger.info("[echoer] active_memory tool registered: memorize (v5+)")
            except Exception as exc2:
                logger.warning("[echoer] active_memory tool registration failed: %s", exc2)
        except Exception as exc:
            logger.warning("[echoer] active_memory tool registration failed: %s", exc)

        if bool(self.config.get("webui", {}).get("enabled", True)):
            try:
                ui_scope = self._resolve_webui_scope()
                if ui_scope:
                    state = await self.webui_server.start(scope_key=ui_scope)
                    if state.url:
                        logger.info("[echoer] WebUI started at %s (scope=%s)", state.url, state.scope_key)
                else:
                    logger.info("[echoer] WebUI start deferred until a concrete scope becomes active")
            except Exception as exc:
                logger.error("[echoer] WebUI start failed: %s", exc, exc_info=True)
        logger.info("[echoer] initialize done")

    async def terminate(self):
        logger.info("[echoer] terminate start")
        try:
            self.webui_server.stop()
        except Exception:
            pass
        await self.runtime_manager.close_all()
        logger.info("[echoer] terminate done")

    def _resolve_scope(self, event: AstrMessageEvent) -> str:
        return self.scope_router.resolve(event)

    def _resolve_webui_scope(self, event: Optional[AstrMessageEvent] = None) -> str:
        configured = str(self.config.get("webui", {}).get("scope", "auto") or "auto").strip()
        mode = configured.lower()
        if mode not in {"", "auto", "current", "event"}:
            return configured
        if event is not None:
            return self._resolve_scope(event)
        known_scopes = self.runtime_manager.get_known_scopes()
        if known_scopes:
            return str(known_scopes[-1])
        return ""

    async def _ensure_webui_scope_ready(self, scope_key: str) -> None:
        if not bool(self.config.get("webui", {}).get("enabled", True)):
            return

        configured = str(self.config.get("webui", {}).get("scope", "auto") or "auto").strip().lower()
        if configured not in {"", "auto", "current", "event"}:
            return

        target_scope = str(scope_key or "").strip()
        if not target_scope:
            return

        state = self.webui_server.state
        if not state.url:
            await self.webui_server.start(scope_key=target_scope)
            logger.info("[echoer] WebUI auto-started at %s (scope=%s)", self.webui_server.state.url, self.webui_server.state.scope_key)
            return

        known_scopes = self.runtime_manager.get_known_scopes()
        current_scope = str(state.scope_key or "").strip()
        if current_scope in known_scopes:
            return
        if len(known_scopes) != 1 or target_scope not in known_scopes or current_scope == target_scope:
            return

        logger.info("[echoer] auto-switching webui scope: from=%s to=%s", current_scope or "<deferred>", target_scope)
        self.webui_server.stop()
        await self.webui_server.start(scope_key=target_scope)

    @staticmethod
    def _event_ctx_text(event: AstrMessageEvent, scope_key: Optional[str] = None) -> str:
        scope = str(scope_key or "").strip() or "unknown"
        platform = str(getattr(event, "get_platform_name", lambda: "unknown")() or "unknown")
        sender = str(getattr(event, "get_sender_id", lambda: "")() or "")
        group = str(getattr(event, "get_group_id", lambda: "")() or "")
        message_obj = getattr(event, "message_obj", None)
        session = str(getattr(message_obj, "session_id", "") or getattr(event, "unified_msg_origin", ""))
        return (
            f"scope={scope} platform={platform} "
            f"session={session or '-'} sender={sender or '-'} group={group or '-'}"
        )


    def _log_cmd(self, event: AstrMessageEvent, command: str, **fields: Any) -> None:
        scope_key = self._resolve_scope(event)
        ctx = self._event_ctx_text(event, scope_key)
        detail = " ".join(f"{key}={value}" for key, value in fields.items())
        if detail:
            logger.info("[echoer] cmd=%s %s %s", command, ctx, detail)
        else:
            logger.info("[echoer] cmd=%s %s", command, ctx)

    @staticmethod
    def _person_profile_global_usage() -> str:
        return "用法: /person_profile_global on|off|status 或 /mem profile_global on|off|status"

    @staticmethod
    def _person_profile_global_mode_enabled(policy: Dict[str, Any]) -> bool:
        return bool(policy.get("enabled", True) and policy.get("global_injection_enabled", False))

    def _sync_local_person_profile_policy(
        self,
        *,
        global_injection_enabled: Optional[bool] = None,
    ) -> None:
        person_cfg = self.config.get("person_profile")
        if not isinstance(person_cfg, dict):
            person_cfg = {}
            self.config["person_profile"] = person_cfg
        if global_injection_enabled is not None:
            person_cfg["global_injection_enabled"] = bool(global_injection_enabled)

    def _persist_plugin_config(self) -> tuple[bool, str]:
        cfg_obj = self._config_obj
        if cfg_obj is None:
            return False, "当前运行环境不支持插件配置落盘"
        try:
            cfg_obj.save_config(replace_config=dict(self.config))
            return True, "配置已持久化到插件配置文件"
        except Exception as exc:
            logger.error("[echoer] persist plugin config failed: %s", exc, exc_info=True)
            return False, f"配置落盘失败: {exc}"

    async def _handle_person_profile_global_action(self, action: str) -> Dict[str, Any]:
        act = str(action or "").strip().lower() or "status"
        if act not in {"on", "off", "status"}:
            raise ValueError(self._person_profile_global_usage())

        if act == "on":
            self._sync_local_person_profile_policy(
                global_injection_enabled=True,
            )
            policy = await self.runtime_manager.apply_person_profile_policy(
                global_injection_enabled=True,
            )
            policy["message"] = "已开启全局人物画像：将强制注入所有会话画像（仍受 person_profile.enabled 总开关约束）。"
            persisted, persist_message = self._persist_plugin_config()
            policy["persisted"] = persisted
            policy["persist_message"] = persist_message
        elif act == "off":
            self._sync_local_person_profile_policy(
                global_injection_enabled=False,
            )
            policy = await self.runtime_manager.apply_person_profile_policy(
                global_injection_enabled=False,
            )
            policy["message"] = "已关闭全局人物画像：恢复为 opt_in/default 的常规策略。"
            persisted, persist_message = self._persist_plugin_config()
            policy["persisted"] = persisted
            policy["persist_message"] = persist_message
        else:
            policy = self.runtime_manager.get_person_profile_policy()
            policy["message"] = "当前为人物画像全局策略状态。"
            policy["persisted"] = None
            policy["persist_message"] = "status 查询不会修改配置文件"

        policy["global_mode_enabled"] = self._person_profile_global_mode_enabled(policy)
        return policy

    async def _ingest_event_message(self, event: AstrMessageEvent, role: str, text: str) -> None:
        adapted = AstrbotEventAdapter.from_event(event, self._resolve_scope(event))
        self_id = str(getattr(getattr(event, "message_obj", None), "self_id", "") or "")
        if role == "user" and adapted.sender_id and self_id and adapted.sender_id == self_id:
            logger.debug(
                "[echoer] skip self message role=%s %s",
                role,
                self._event_ctx_text(event, adapted.scope_key),
            )
            return

        source = f"chat:{adapted.platform}:{adapted.session_id}"
        await self.profile_manager.upsert_registry_from_event(
            scope_key=adapted.scope_key,
            platform=adapted.platform,
            sender_id=adapted.sender_id,
            sender_name=adapted.sender_name or adapted.sender_id,
            group_id=adapted.group_id,
            session_id=adapted.session_id,
            unified_msg_origin=adapted.unified_msg_origin,
            timestamp=float(adapted.timestamp) if adapted.timestamp else None,
        )
        await self.ingest_service.ingest_message(
            scope_key=adapted.scope_key,
            session_id=adapted.session_id,
            role=role,
            content=text,
            source=source,
            user_id=adapted.sender_id,
            group_id=adapted.group_id,
            platform=adapted.platform,
            unified_msg_origin=adapted.unified_msg_origin,
            sender_name=adapted.sender_name,
            message_id=adapted.message_id,
            role_origin=role,
            timestamp=adapted.timestamp,
            time_meta={"event_time": adapted.timestamp} if adapted.timestamp else None,
        )
        try:
            await self._ensure_webui_scope_ready(adapted.scope_key)
        except Exception as exc:
            logger.warning(
                "[echoer] ensure webui scope ready failed: %s (%s)",
                exc,
                self._event_ctx_text(event, adapted.scope_key),
                exc_info=True,
            )
        logger.debug(
            "[echoer] ingested role=%s chars=%s %s",
            role,
            len(text),
            self._event_ctx_text(event, adapted.scope_key),
        )

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_all_messages(self, event: AstrMessageEvent):
        if not bool_cfg(self.config, "ingest.record_all_events", True):
            logger.debug("[echoer] ingest disabled by config")
            return
        text = str(getattr(event, "message_str", "") or "").strip()
        if not text and bool_cfg(self.config, "ingest.skip_empty_text", True):
            logger.debug("[echoer] skip empty message %s", self._event_ctx_text(event))
            return
        if bool_cfg(self.config, "ingest.skip_command_messages", True) and is_command_message(text, self.config.get("ingest", {})):
            logger.debug("[echoer] skip command message %s", self._event_ctx_text(event))
            return
        try:
            await self._ingest_event_message(event, "user", text)
            if not bool_cfg(self.config, "summarization.auto_import.after_reply_only", True):
                adapted = AstrbotEventAdapter.from_event(event, self._resolve_scope(event))
                await self.summary_service.maybe_enqueue_auto_summary(
                    scope_key=adapted.scope_key,
                    session_id=adapted.session_id,
                )
        except Exception as exc:
            logger.warning(
                "[echoer] ingest user message failed: %s (%s)",
                exc,
                self._event_ctx_text(event),
                exc_info=True,
            )

    @filter.on_llm_request(priority=40)
    async def on_llm_request(self, event: AstrMessageEvent, req):
        scope_key = self._resolve_scope(event)
        _active_scope_key_var.set(scope_key)
        _active_event_var.set(event)
        try:
            await build_and_inject_memory(
                event=event,
                req=req,
                scope_key=scope_key,
                scope_router=self.scope_router,
                config=self.config,
                runtime_manager=self.runtime_manager,
                profile_manager=self.profile_manager,
            )
        except Exception as exc:
            logger.warning(
                "[echoer] llm request hook failed: %s (%s)",
                exc,
                self._event_ctx_text(event, scope_key),
                exc_info=True,
            )

    @filter.command("person_profile")
    async def person_profile_switch(self, event: AstrMessageEvent):
        """开关当前对话的人物画像注入（on/off/status）。"""
        tokens = parse_direct_command_tokens(event.message_str, "person_profile")
        action = str(tokens[0]).strip().lower() if tokens else "status"
        if action not in {"on", "off", "status"}:
            yield event.plain_result("用法: /person_profile on|off|status")
            return

        scope_key = self._resolve_scope(event)
        adapted = AstrbotEventAdapter.from_event(event, scope_key)
        session_id = str(adapted.session_id or "").strip()
        user_id = str(adapted.sender_id or "").strip()
        if not session_id or not user_id:
            yield event.plain_result("无法识别当前会话范围（session_id/user_id）")
            return

        try:
            if action == "status":
                data = await self.profile_manager.get_injection_status(
                    scope_key=scope_key,
                    session_id=session_id,
                    user_id=user_id,
                )
            else:
                data = await self.profile_manager.set_injection_switch(
                    scope_key=scope_key,
                    session_id=session_id,
                    user_id=user_id,
                    enabled=(action == "on"),
                )
            yield event.plain_result(to_pretty_text(data))
        except Exception as exc:
            logger.error("[echoer] person_profile command failed: %s", exc, exc_info=True)
            yield event.plain_result(f"人物画像开关操作失败: {exc}")

    @filter.command("person_profile_global")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def person_profile_global_switch(self, event: AstrMessageEvent):
        """统一设置所有对话的人物画像注入开关。"""
        tokens = parse_direct_command_tokens(event.message_str, "person_profile_global")
        action = str(tokens[0]).strip().lower() if tokens else "status"
        self._log_cmd(event, "person_profile_global", action=action)
        try:
            data = await self._handle_person_profile_global_action(action)
            yield event.plain_result(to_pretty_text(data))
        except ValueError as exc:
            yield event.plain_result(str(exc))
        except Exception as exc:
            logger.error("[echoer] person_profile_global command failed: %s", exc, exc_info=True)
            yield event.plain_result(f"全局人物画像开关操作失败: {exc}")

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp):
        text = str(getattr(resp, "completion_text", "") or "").strip()
        if not text:
            return
        adapted = AstrbotEventAdapter.from_event(event, self._resolve_scope(event))
        try:
            await self._ingest_event_message(event, "assistant", text)
            auto_summary_result = await self.summary_service.maybe_enqueue_auto_summary(
                scope_key=adapted.scope_key,
                session_id=adapted.session_id,
            )
            if auto_summary_result.get("queued"):
                logger.debug(
                    "[echoer] auto summary queued task=%s %s",
                    str(auto_summary_result.get("task_id", "") or ""),
                    self._event_ctx_text(event, adapted.scope_key),
                )
        except Exception as exc:
            logger.warning(
                "[echoer] ingest llm response failed: %s (%s)",
                exc,
                self._event_ctx_text(event),
                exc_info=True,
            )

    @filter.command_group("mem")
    def mem(self):
        """记忆系统：查询、管理和维护记忆数据。"""
        pass

    @mem.command("status")
    async def mem_status(self, event: AstrMessageEvent):
        """查看当前作用域的记忆状态和服务信息。"""
        self._log_cmd(event, "status")
        scope_key = self._resolve_scope(event)
        data = await self.memory_service.status(scope_key=scope_key)
        scheduler = await self.maintenance_scheduler.status(scope_key=scope_key)
        payload = {
            "scope": scope_key,
            "known_scopes": self.runtime_manager.get_known_scopes(),
            "webui": {
                "url": self.webui_server.state.url,
                "scope": self.webui_server.state.scope_key,
            },
            "scheduler": scheduler,
            "memory": data,
        }
        yield event.plain_result(to_pretty_text(payload))

    @mem.command("query")
    async def mem_query(self, event: AstrMessageEvent, query: str = "", top_k: int = 10):
        """语义搜索记忆（支持模糊匹配）。"""
        resolved_top_k = to_int(top_k, 10)
        q = str(query or "").strip()
        tokens = parse_tail_tokens(event.message_str, "query")
        if tokens:
            if len(tokens) > 1:
                maybe_k = to_int(tokens[-1], -1, min_value=1)
                if maybe_k > 0:
                    resolved_top_k = maybe_k
                    tokens = tokens[:-1]
            parsed_query = " ".join(tokens).strip()
            if parsed_query:
                q = parsed_query
        if not q:
            yield event.plain_result("用法: /mem query <关键词> [top_k]")
            return
        self._log_cmd(event, "query", top_k=resolved_top_k, q_len=len(q))
        scope_key = self._resolve_scope(event)
        try:
            data = await self.query_service.search(scope_key=scope_key, query=q, top_k=resolved_top_k)
            yield event.plain_result(to_pretty_text(data))
        except Exception as exc:
            logger.error("[echoer] mem query failed: %s", exc, exc_info=True)
            yield event.plain_result(f"查询失败: {exc}")

    @mem.command("time")
    async def mem_time(
        self,
        event: AstrMessageEvent,
        time_from: str = "",
        time_to: str = "",
        query: str = "",
        top_k: int = 10,
    ):
        """按时间段查找记忆（支持"昨天""上周"等自然语言）。"""
        from_text = str(time_from or "").strip()
        to_text = str(time_to or "").strip()
        query_text = str(query or "").strip()
        resolved_top_k = to_int(top_k, 10)

        tokens = parse_tail_tokens(event.message_str, "time")
        if tokens:
            from_text = str(tokens[0]).strip() or from_text
            if len(tokens) >= 2:
                to_text = str(tokens[1]).strip()
            if len(tokens) >= 3:
                query_text = " ".join(tokens[2:]).strip()

        if not from_text:
            yield event.plain_result("用法: /mem time <time_from> [time_to] [query]")
            return

        self._log_cmd(
            event,
            "time",
            time_from=from_text,
            has_time_to=bool(to_text),
            top_k=resolved_top_k,
            q_len=len(query_text),
        )
        scope_key = self._resolve_scope(event)
        try:
            data = await self.query_service.time_search(
                scope_key=scope_key,
                query=query_text,
                time_from=from_text or None,
                time_to=to_text or None,
                top_k=resolved_top_k,
            )
            yield event.plain_result(to_pretty_text(data))
        except Exception as exc:
            logger.error("[echoer] mem time failed: %s", exc, exc_info=True)
            yield event.plain_result(f"时序查询失败: {exc}")

    @mem.command("protect")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def mem_protect(self, event: AstrMessageEvent, query_or_hash: str = "", hours: float = 24.0):
        """保护记忆不被自动衰减清理（不填时长则永久保护）。"""
        target = str(query_or_hash or "").strip()
        resolved_hours = to_float(hours, 24.0, min_value=0.1)
        tokens = parse_tail_tokens(event.message_str, "protect")
        if tokens:
            maybe_hours = to_float(tokens[-1], -1.0, min_value=0.1)
            if len(tokens) > 1 and maybe_hours > 0:
                resolved_hours = maybe_hours
                tokens = tokens[:-1]
            parsed_target = " ".join(tokens).strip()
            if parsed_target:
                target = parsed_target
        if not target:
            yield event.plain_result("用法: /mem protect <hash_or_query> [hours]")
            return
        self._log_cmd(event, "protect", hours=resolved_hours, target_len=len(target))
        scope_key = self._resolve_scope(event)
        try:
            data = await self.memory_service.protect(scope_key=scope_key, query_or_hash=target, hours=resolved_hours)
            yield event.plain_result(to_pretty_text(data))
        except Exception as exc:
            logger.error("[echoer] mem protect failed: %s", exc, exc_info=True)
            yield event.plain_result(f"保护失败: {exc}")

    @mem.command("reinforce")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def mem_reinforce(self, event: AstrMessageEvent, query_or_hash: str = ""):
        """强化记忆权重并自动保护 24 小时。"""
        target = str(query_or_hash or "").strip()
        parsed_target = parse_tail(event.message_str, "reinforce")
        if parsed_target:
            target = parsed_target
        if not target:
            yield event.plain_result("用法: /mem reinforce <hash_or_query>")
            return
        self._log_cmd(event, "reinforce", target_len=len(target))
        scope_key = self._resolve_scope(event)
        try:
            data = await self.memory_service.reinforce(scope_key=scope_key, query_or_hash=target)
            yield event.plain_result(to_pretty_text(data))
        except Exception as exc:
            logger.error("[echoer] mem reinforce failed: %s", exc, exc_info=True)
            yield event.plain_result(f"强化失败: {exc}")

    @mem.command("restore")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def mem_restore(self, event: AstrMessageEvent, hash_value: str = "", restore_type: str = "relation"):
        """从回收站恢复已被剪枝的记忆。"""
        target = str(hash_value or "").strip()
        rtype = str(restore_type or "relation").strip() or "relation"
        tokens = parse_tail_tokens(event.message_str, "restore")
        if tokens:
            target = str(tokens[0]).strip() or target
            if len(tokens) > 1:
                rtype = str(tokens[1]).strip() or rtype
        if not target:
            yield event.plain_result("用法: /mem restore <hash> [relation|entity]")
            return
        self._log_cmd(event, "restore", restore_type=rtype, hash=target)
        scope_key = self._resolve_scope(event)
        try:
            data = await self.memory_service.restore(scope_key=scope_key, hash_value=target, restore_type=rtype)
            yield event.plain_result(to_pretty_text(data))
        except Exception as exc:
            logger.error("[echoer] mem restore failed: %s", exc, exc_info=True)
            yield event.plain_result(f"恢复失败: {exc}")

    @mem.command("delete_entity")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def mem_delete_entity(self, event: AstrMessageEvent, entity_name: str = ""):
        """删除指定实体及其所有关联关系。"""
        target = str(entity_name or "").strip()
        parsed = parse_tail(event.message_str, "delete_entity")
        if parsed:
            target = parsed
        if not target:
            yield event.plain_result("用法: /mem delete_entity <实体名>")
            return
        self._log_cmd(event, "delete_entity", entity_len=len(target))
        scope_key = self._resolve_scope(event)
        try:
            runtime = await self.runtime_manager.get_runtime(scope_key)
            from .memorix.amemorix.services.delete_service import DeleteService

            data = await DeleteService(runtime.context).entity(target)
            data["scope"] = scope_key
            yield event.plain_result(to_pretty_text(data))
        except Exception as exc:
            logger.error("[echoer] mem delete_entity failed: %s", exc, exc_info=True)
            yield event.plain_result(f"删除实体失败: {exc}")

    @mem.command("profile")
    async def mem_profile(self, event: AstrMessageEvent, person_keyword_or_id: str = "", top_k: int = 12):
        """查看人物画像（自动生成的用户特征摘要）。"""
        scope_key = self._resolve_scope(event)
        keyword = str(person_keyword_or_id or "").strip()
        resolved_top_k = to_int(top_k, 12)
        tokens = parse_tail_tokens(event.message_str, "profile")
        if tokens:
            if len(tokens) > 1:
                maybe_k = to_int(tokens[-1], -1, min_value=1)
                if maybe_k > 0:
                    resolved_top_k = maybe_k
                    tokens = tokens[:-1]
            parsed_keyword = " ".join(tokens).strip()
            if parsed_keyword:
                keyword = parsed_keyword
        if not keyword:
            keyword = str(getattr(event, "get_sender_id", lambda: "")() or "")
        self._log_cmd(event, "profile", top_k=resolved_top_k, keyword_len=len(keyword))
        try:
            data = await self.profile_manager.query(
                scope_key=scope_key,
                person_keyword=keyword,
                top_k=resolved_top_k,
                force_refresh=False,
            )
            yield event.plain_result(to_pretty_text(data))
        except Exception as exc:
            logger.error("[echoer] mem profile failed: %s", exc, exc_info=True)
            yield event.plain_result(f"画像查询失败: {exc}")

    @mem.command("profile_override")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def mem_profile_override(self, event: AstrMessageEvent, person_id: str = "", override_text: str = ""):
        """手动覆盖指定用户的人物画像内容。"""
        pid = str(person_id or "").strip()
        text = str(override_text or "").strip()
        tail = parse_tail(event.message_str, "profile_override")
        if tail:
            parts = tail.split(maxsplit=1)
            if len(parts) == 2:
                pid = str(parts[0]).strip() or pid
                text = str(parts[1]).strip() or text
        if not pid or not text:
            yield event.plain_result("用法: /mem profile_override <person_id> <text>")
            return
        self._log_cmd(event, "profile_override", person_id=pid, text_len=len(text))
        scope_key = self._resolve_scope(event)
        try:
            data = await self.profile_manager.set_override(scope_key=scope_key, person_id=pid, override_text=text)
            yield event.plain_result(to_pretty_text(data))
        except Exception as exc:
            logger.error("[echoer] mem profile_override failed: %s", exc, exc_info=True)
            yield event.plain_result(f"画像覆盖失败: {exc}")

    @mem.command("profile_clear")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def mem_profile_clear(self, event: AstrMessageEvent, person_id: str = ""):
        """清除手动覆盖，恢复自动生成的画像。"""
        pid = str(person_id or "").strip()
        parsed_pid = parse_tail(event.message_str, "profile_clear")
        if parsed_pid:
            pid = parsed_pid
        if not pid:
            yield event.plain_result("用法: /mem profile_clear <person_id>")
            return
        self._log_cmd(event, "profile_clear", person_id=pid)
        scope_key = self._resolve_scope(event)
        try:
            data = await self.profile_manager.delete_override(scope_key=scope_key, person_id=pid)
            yield event.plain_result(to_pretty_text(data))
        except Exception as exc:
            logger.error("[echoer] mem profile_clear failed: %s", exc, exc_info=True)
            yield event.plain_result(f"画像覆盖清除失败: {exc}")

    @mem.command("profile_global")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def mem_profile_global(self, event: AstrMessageEvent, action: str = "status"):
        """全局开关：是否在 LLM 请求中注入人物画像。"""
        tokens = parse_tail_tokens(event.message_str, "profile_global")
        cmd_action = str(action or "").strip().lower()
        if tokens:
            cmd_action = str(tokens[0]).strip().lower()
        if not cmd_action:
            cmd_action = "status"
        self._log_cmd(event, "profile_global", action=cmd_action)
        try:
            data = await self._handle_person_profile_global_action(cmd_action)
            yield event.plain_result(to_pretty_text(data))
        except ValueError as exc:
            yield event.plain_result(str(exc))
        except Exception as exc:
            logger.error("[echoer] mem profile_global failed: %s", exc, exc_info=True)
            yield event.plain_result(f"全局画像策略设置失败: {exc}")

    @mem.command("summary_now")
    async def mem_summary_now(self, event: AstrMessageEvent, context_length: int = 50):
        """立即对当前对话生成 AI 总结并写入记忆。"""
        resolved_context_length = to_int(context_length, 50)
        tokens = parse_tail_tokens(event.message_str, "summary_now")
        if tokens:
            parsed = to_int(tokens[0], -1)
            if parsed > 0:
                resolved_context_length = parsed
        self._log_cmd(event, "summary_now", context_length=resolved_context_length)
        scope_key = self._resolve_scope(event)
        adapted = AstrbotEventAdapter.from_event(event, scope_key)
        try:
            data = await self.summary_service.summarize_session(
                scope_key=scope_key,
                session_id=adapted.session_id,
                source=f"chat_summary:{adapted.session_id}",
                context_length=resolved_context_length,
            )
            yield event.plain_result(to_pretty_text(data))
        except Exception as exc:
            logger.error("[echoer] mem summary_now failed: %s", exc, exc_info=True)
            yield event.plain_result(f"总结失败: {exc}")

    @mem.command("summary_all")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def mem_summary_all(self, event: AstrMessageEvent, context_length: int = 50, limit: int = 500):
        """批量总结当前作用域内所有会话的历史记录。"""
        resolved_context_length = to_int(context_length, 50)
        resolved_limit = to_int(limit, 500)
        tokens = parse_tail_tokens(event.message_str, "summary_all")
        if tokens:
            resolved_context_length = to_int(tokens[0], resolved_context_length)
            if len(tokens) > 1:
                resolved_limit = to_int(tokens[1], resolved_limit)

        self._log_cmd(event, "summary_all", context_length=resolved_context_length, limit=resolved_limit)
        scope_key = self._resolve_scope(event)
        started_at = time.time()
        try:
            runtime = await self.runtime_manager.get_runtime(scope_key)
            data = await runtime.task_manager.run_bulk_summary_import(
                context_length=resolved_context_length,
                limit=resolved_limit,
            )
            data["scope"] = scope_key
            data["elapsed_seconds"] = round(time.time() - started_at, 3)
            yield event.plain_result(to_pretty_text(data))
        except Exception as exc:
            logger.error("[echoer] mem summary_all failed: %s", exc, exc_info=True)
            yield event.plain_result(f"全量总结失败: {exc}")

    @mem.command("ui")
    async def mem_ui(self, event: AstrMessageEvent):
        """获取 WebUI 管理页面的访问地址。"""
        if not bool(self.config.get("webui", {}).get("enabled", True)):
            yield event.plain_result("WebUI 已禁用")
            return
        self._log_cmd(event, "ui")
        try:
            desired_scope = self._resolve_scope(event)
            if self.webui_server.state.url and self.webui_server.state.scope_key != desired_scope:
                logger.info(
                    "[echoer] switching webui scope: from=%s to=%s",
                    self.webui_server.state.scope_key,
                    desired_scope,
                )
                self.webui_server.stop()

            if not self.webui_server.state.url:
                state = await self.webui_server.start(scope_key=desired_scope)
                if not state.url:
                    yield event.plain_result("WebUI 启动失败")
                    return
            yield event.plain_result(
                f"Echoer WebUI: {self.webui_server.state.url} (scope={self.webui_server.state.scope_key})"
            )
        except Exception as exc:
            logger.error("[echoer] mem ui failed: %s", exc, exc_info=True)
            yield event.plain_result(f"WebUI 启动失败: {exc}")
