"""Scope routing strategy."""

from __future__ import annotations

import re
from dataclasses import dataclass

_SCOPE_PATTERN = re.compile(r"[^0-9A-Za-z:._-]+")


@dataclass(slots=True)
class ScopeRouter:
    mode: str = "group_global"

    def resolve(self, event) -> str:
        mode = str(self.mode or "group_global").strip().lower()
        platform = self._safe_str(getattr(event, "get_platform_name", lambda: "unknown")()) or "unknown"
        sender = self._safe_str(getattr(event, "get_sender_id", lambda: "unknown")()) or "unknown"
        group = self._safe_str(getattr(event, "get_group_id", lambda: "")())
        umo = self._safe_str(getattr(event, "unified_msg_origin", ""))

        if mode == "umo":
            return self._sanitize(umo or f"{platform}:{sender}")
        if mode == "user_global":
            return self._sanitize(f"{platform}:user:{sender}")
        if mode == "group_global":
            if group:
                return self._sanitize(f"{platform}:group:{group}")
            return self._sanitize(f"{platform}:user:{sender}")
        return self._sanitize(platform)

    @staticmethod
    def _safe_str(value) -> str:
        return str(value or "").strip()

    @staticmethod
    def _sanitize(raw: str) -> str:
        text = str(raw or "default").strip()
        text = text.replace("/", "_").replace("\\", "_")
        text = re.sub(r"\s+", "_", text)
        text = _SCOPE_PATTERN.sub("_", text)
        text = text.strip("._")
        if ".." in text:
            text = text.replace("..", "_")
        if text in {"", ".", ".."}:
            return "default"
        return text or "default"
