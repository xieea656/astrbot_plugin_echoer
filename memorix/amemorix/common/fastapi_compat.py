"""FastAPI compatibility helpers."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

AsyncHandler = Callable[[], Awaitable[None]]


def register_lifecycle_handler(app: Any, event: str, handler: AsyncHandler) -> None:
    """Register startup/shutdown handlers across FastAPI/Starlette variants."""
    event_name = str(event or "").strip().lower()
    if event_name not in {"startup", "shutdown"}:
        raise ValueError(f"unsupported lifecycle event: {event}")

    add_event_handler = getattr(app, "add_event_handler", None)
    if callable(add_event_handler):
        add_event_handler(event_name, handler)
        return

    router = getattr(app, "router", None)
    router_add_event_handler = getattr(router, "add_event_handler", None)
    if callable(router_add_event_handler):
        router_add_event_handler(event_name, handler)
        return

    on_event = getattr(app, "on_event", None)
    if callable(on_event):
        on_event(event_name)(handler)
        return

    router_on_event = getattr(router, "on_event", None)
    if callable(router_on_event):
        router_on_event(event_name)(handler)
        return

    raise AttributeError(f"application does not support lifecycle event registration: {type(app)!r}")
