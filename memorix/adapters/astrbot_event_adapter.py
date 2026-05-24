"""AstrBot event adapter."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class MemorixEvent:
    scope_key: str
    platform: str
    unified_msg_origin: str
    session_id: str
    sender_id: str
    sender_name: str
    group_id: str
    message_id: str
    message_text: str
    timestamp: int


class AstrbotEventAdapter:
    @staticmethod
    def from_event(event, scope_key: str) -> MemorixEvent:
        platform = str(getattr(event, "get_platform_name", lambda: "unknown")() or "unknown")
        unified_msg_origin = str(getattr(event, "unified_msg_origin", "") or "")
        message_obj = getattr(event, "message_obj", None)
        session_id = str(getattr(message_obj, "session_id", "") or unified_msg_origin)
        sender_id = str(getattr(event, "get_sender_id", lambda: "")() or "")
        sender_name = str(getattr(event, "get_sender_name", lambda: "")() or "")
        group_id = str(getattr(event, "get_group_id", lambda: "")() or "")
        message_id = str(getattr(message_obj, "message_id", "") or "")
        message_text = str(getattr(event, "message_str", "") or "").strip()
        timestamp = int(getattr(message_obj, "timestamp", 0) or 0)
        return MemorixEvent(
            scope_key=scope_key,
            platform=platform,
            unified_msg_origin=unified_msg_origin,
            session_id=session_id,
            sender_id=sender_id,
            sender_name=sender_name,
            group_id=group_id,
            message_id=message_id,
            message_text=message_text,
            timestamp=timestamp,
        )
