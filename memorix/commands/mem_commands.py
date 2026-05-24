"""Utilities for command output formatting."""

from __future__ import annotations

import json
from typing import Any


def to_pretty_text(payload: Any) -> str:
    try:
        return json.dumps(payload, ensure_ascii=False, indent=2)
    except Exception:
        return str(payload)

