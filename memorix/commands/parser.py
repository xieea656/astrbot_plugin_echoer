"""Command parsing utilities extracted from main.py."""

from __future__ import annotations

import shlex
from typing import Any


def parse_tail(raw_text: str, sub_cmd: str) -> str:
    text = str(raw_text or "").strip()
    if text.startswith("/"):
        text = text[1:].lstrip()
    for prefix in ("mem", "astrbot_plugin_echoer"):
        matched, text = _consume_head_token(text, prefix)
        if matched:
            break
    matched, text = _consume_head_token(text, sub_cmd)
    return text if matched else ""


def parse_tail_tokens(raw_text: str, sub_cmd: str) -> list[str]:
    tail = parse_tail(raw_text, sub_cmd)
    if not tail:
        return []
    try:
        return [str(token).strip() for token in shlex.split(tail) if str(token).strip()]
    except ValueError:
        return [token for token in tail.split() if token]


def parse_direct_command_tokens(raw_text: str, command: str) -> list[str]:
    text = str(raw_text or "").strip()
    if text.startswith("/"):
        text = text[1:].lstrip()
    matched, tail = _consume_head_token(text, command)
    if not matched:
        return []
    if not tail:
        return []
    try:
        return [str(token).strip() for token in shlex.split(tail) if str(token).strip()]
    except ValueError:
        return [token for token in tail.split() if token]


def _consume_head_token(text: str, token: str) -> tuple[bool, str]:
    raw = str(text or "").strip()
    name = str(token or "").strip()
    if not raw or not name:
        return False, raw
    if raw == name:
        return True, ""
    if not raw.startswith(name):
        return False, raw
    next_char = raw[len(name): len(name) + 1]
    if next_char and not next_char.isspace():
        return False, raw
    return True, raw[len(name):].strip()


def to_int(raw: Any, default: int, min_value: int = 1) -> int:
    try:
        return max(int(raw), min_value)
    except (TypeError, ValueError):
        return default


def to_float(raw: Any, default: float, min_value: float = 0.0) -> float:
    try:
        return max(float(raw), min_value)
    except (TypeError, ValueError):
        return default


def bool_cfg(config: dict[str, Any], key: str, default: bool) -> bool:
    current: Any = config
    for part in key.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return default
    return bool(current)


def normalize_command_prefixes(raw: Any) -> list[str]:
    values: list[str] = []
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, (list, tuple, set)):
        values = [str(item or "") for item in raw]

    normalized: list[str] = []
    seen = set()
    for item in values:
        prefix = str(item or "").strip()
        if not prefix or prefix in seen:
            continue
        seen.add(prefix)
        normalized.append(prefix)
    return normalized or ["/"]


def strip_leading_mentions(text: str) -> str:
    current = str(text or "").lstrip()
    while True:
        changed = False
        if current.startswith("@"):
            parts = current.split(maxsplit=1)
            if len(parts) == 2:
                current = parts[1].lstrip()
                changed = True
        elif current.startswith("[CQ:at,"):
            idx = current.find("]")
            if idx > 0:
                current = current[idx + 1:].lstrip()
                changed = True
        if not changed:
            break
    return current


def is_command_message(text: str, ingest_cfg: dict[str, Any], prefixes_raw: Any = None) -> bool:
    if prefixes_raw is None:
        prefixes_raw = ingest_cfg.get("command_prefixes", ["/"])
    if "command_prefixes" not in ingest_cfg and "command_prefix" in ingest_cfg:
        prefixes_raw = ingest_cfg.get("command_prefix")
    prefixes = normalize_command_prefixes(prefixes_raw)
    content = str(text or "").lstrip()
    if not content:
        return False
    candidates = [content]
    mention_stripped = strip_leading_mentions(content)
    if mention_stripped and mention_stripped != content:
        candidates.append(mention_stripped)

    for candidate in candidates:
        for prefix in prefixes:
            if not candidate.startswith(prefix):
                continue
            if len(candidate) == len(prefix):
                return True
            if prefix[-1].isalnum():
                next_char = candidate[len(prefix): len(prefix) + 1]
                if next_char and (next_char.isalnum() or next_char == "_"):
                    continue
            return True
    return False
