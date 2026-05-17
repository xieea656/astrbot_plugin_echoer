"""Scope runtime management for memorix plugin."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np
from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .amemorix.bootstrap import build_context
from .amemorix.settings import DEFAULT_CONFIG, AppSettings
from .amemorix.task_manager import TaskManager
from .providers import AstrBotProviderBridge

_SCOPE_DIR_PATTERN = re.compile(r"[^0-9A-Za-z._-]+")


def _deep_merge(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class LocalEmbeddingAdapter:
    """Deterministic local embedding fallback for offline mode."""

    def __init__(self, dimension: int):
        self.dimension = int(max(1, dimension))

    async def _detect_dimension(self) -> int:
        return self.dimension

    async def encode(self, texts, **kwargs):
        del kwargs
        if isinstance(texts, str):
            return self._embed_text(texts)

        vectors = [self._embed_text(str(text)) for text in list(texts)]
        if not vectors:
            return np.zeros((0, self.dimension), dtype=np.float32)
        return np.vstack(vectors).astype(np.float32)

    def get_embedding_dimension(self) -> int:
        return self.dimension

    def _embed_text(self, text: str) -> np.ndarray:
        payload = str(text or "")
        digest = hashlib.sha256(payload.encode("utf-8")).digest()
        seed = int.from_bytes(digest[:8], byteorder="big", signed=False)
        rng = np.random.default_rng(seed)
        vec = rng.standard_normal(self.dimension, dtype=np.float32)
        norm = float(np.linalg.norm(vec))
        if norm > 0:
            vec = vec / norm
        return vec.astype(np.float32)


@dataclass
class ScopeRuntime:
    scope_key: str
    settings: AppSettings
    context: Any
    task_manager: TaskManager
    _service_cache: dict = None  # type: ignore

    def __post_init__(self):
        if self._service_cache is None:
            object.__setattr__(self, "_service_cache", {})

    def get_service(self, svc_class: type):
        """Lazy-create and cache an amemorix service instance on this runtime."""
        name = svc_class.__name__
        cached = self._service_cache.get(name)
        if cached is None:
            cached = svc_class(self.context)
            self._service_cache[name] = cached
        return cached

class ScopeRuntimeManager:
    def __init__(
        self,
        *,
        plugin_name: str,
        plugin_config: Dict[str, Any],
        astrbot_context: Any = None,
    ):
        self.plugin_name = plugin_name
        self.plugin_config = dict(plugin_config or {})
        self.astrbot_context = astrbot_context
        self._runtimes: Dict[str, ScopeRuntime] = {}
        self._lock = asyncio.Lock()

    def _scope_dir(self, scope_key: str) -> Path:
        data_root_raw = get_astrbot_data_path()
        data_root = Path(data_root_raw) if data_root_raw else (Path.cwd() / "data")
        base = data_root / "plugin_data" / self.plugin_name / "scopes"
        base.mkdir(parents=True, exist_ok=True)
        base_resolved = base.resolve()
        safe_scope = self._sanitize_scope_dirname(scope_key)
        target = (base_resolved / safe_scope).resolve()
        try:
            target.relative_to(base_resolved)
        except ValueError:
            fallback = f"scope_{hashlib.sha256(str(scope_key or '').encode('utf-8')).hexdigest()[:16]}"
            logger.warning(
                "unsafe scope dir detected, fallback applied: scope=%s fallback=%s",
                scope_key,
                fallback,
            )
            target = base_resolved / fallback
        target.mkdir(parents=True, exist_ok=True)
        logger.debug("resolved scope dir: scope=%s dir=%s", target.name, target)
        return target

    @staticmethod
    def _sanitize_scope_dirname(scope_key: str) -> str:
        text = str(scope_key or "default").strip()
        text = text.replace("/", "_").replace("\\", "_").replace(":", "_")
        text = re.sub(r"\s+", "_", text)
        text = _SCOPE_DIR_PATTERN.sub("_", text)
        text = text.strip("._")
        if ".." in text:
            text = text.replace("..", "_")
        if text in {"", ".", ".."}:
            return "default"
        return text[:128] or "default"

    def _build_scope_config(self, scope_key: str) -> Dict[str, Any]:
        cfg = _deep_merge(DEFAULT_CONFIG, self.plugin_config)
        cfg.setdefault("storage", {})
        cfg["storage"]["data_dir"] = str(self._scope_dir(scope_key))

        webui_cfg = cfg.get("webui", {}) if isinstance(cfg.get("webui"), dict) else {}
        auth_cfg = webui_cfg.get("auth", {}) if isinstance(webui_cfg.get("auth"), dict) else {}
        cfg.setdefault("auth", {})
        cfg["auth"]["enabled"] = bool(auth_cfg.get("enabled", False))
        cfg["auth"]["write_tokens"] = list(auth_cfg.get("write_tokens", []))
        cfg["auth"]["read_tokens"] = list(auth_cfg.get("read_tokens", []))
        cfg["auth"]["protect_read_endpoints"] = bool(auth_cfg.get("protect_read_endpoints", False))

        return cfg

    def _build_provider_bridge(self) -> Optional[AstrBotProviderBridge]:
        if self.astrbot_context is None:
            return None
        provider_cfg = self.plugin_config.get("provider", {})
        if not isinstance(provider_cfg, dict):
            provider_cfg = {}
        return AstrBotProviderBridge(
            astrbot_context=self.astrbot_context,
            # 聊天模型可选 AstrBot 中已定义 provider（配置优先）。
            chat_provider_id=str(provider_cfg.get("chat_provider_id", "") or ""),
            embedding_provider_id="",
        )

    def _patch_local_embedding(self, runtime: ScopeRuntime) -> None:
        dimension = int(runtime.settings.get("embedding.dimension", 1024) or 1024)
        adapter = LocalEmbeddingAdapter(dimension=dimension)
        runtime.context.embedding_manager = adapter
        runtime.context.retriever.embedding_manager = adapter
        if hasattr(runtime.context, "person_profile_service"):
            runtime.context.person_profile_service.embedding_manager = adapter
        logger.info("local embedding fallback enabled: scope=%s dim=%s", runtime.scope_key, dimension)

    async def get_runtime(self, scope_key: str) -> ScopeRuntime:
        key = str(scope_key or "default")
        existing = self._runtimes.get(key)
        if existing is not None:
            logger.debug("runtime cache hit: scope=%s", key)
            return existing

        async with self._lock:
            existing = self._runtimes.get(key)
            if existing is not None:
                logger.debug("runtime cache hit after lock: scope=%s", key)
                return existing

            cfg = self._build_scope_config(key)
            settings = AppSettings(config=cfg)
            logger.info("create runtime: scope=%s data_dir=%s", key, settings.data_dir)
            ctx = build_context(settings)
            ctx.astrbot_context = self.astrbot_context
            provider_bridge = self._build_provider_bridge()
            ctx.provider_bridge = provider_bridge
            task_manager = TaskManager(ctx)
            runtime = ScopeRuntime(scope_key=key, settings=settings, context=ctx, task_manager=task_manager)

            embedding_enabled = bool(settings.get("embedding.enabled", False))
            if embedding_enabled:
                endpoint_cfg = settings.get_openapi_endpoint_config()
                logger.info(
                    "openapi embedding enabled: scope=%s base_url=%s model=%s",
                    key,
                    str(endpoint_cfg.get("base_url", "") or "<default>"),
                    str(endpoint_cfg.get("model", "") or "<default>"),
                )
            else:
                self._patch_local_embedding(runtime)

            await task_manager.start()
            self._runtimes[key] = runtime
            logger.info(
                "runtime ready: scope=%s embedding_mode=%s",
                key,
                "openapi" if embedding_enabled else "local-fallback",
            )
            return runtime

    def get_known_scopes(self) -> list[str]:
        return list(self._runtimes.keys())

    def get_person_profile_policy(self) -> Dict[str, Any]:
        person_cfg = self.plugin_config.get("person_profile", {})
        if not isinstance(person_cfg, dict):
            person_cfg = {}
        return {
            "enabled": bool(person_cfg.get("enabled", True)),
            "opt_in_required": bool(person_cfg.get("opt_in_required", True)),
            "default_injection_enabled": bool(person_cfg.get("default_injection_enabled", False)),
            "global_injection_enabled": bool(person_cfg.get("global_injection_enabled", False)),
            "known_scopes": list(self._runtimes.keys()),
        }

    async def apply_person_profile_policy(
        self,
        *,
        global_injection_enabled: Optional[bool] = None,
    ) -> Dict[str, Any]:
        def _apply(cfg: Dict[str, Any]) -> None:
            person_cfg = cfg.get("person_profile")
            if not isinstance(person_cfg, dict):
                person_cfg = {}
                cfg["person_profile"] = person_cfg
            if global_injection_enabled is not None:
                person_cfg["global_injection_enabled"] = bool(global_injection_enabled)

        async with self._lock:
            _apply(self.plugin_config)
            for runtime in self._runtimes.values():
                _apply(runtime.settings.config)
                runtime.context.config = runtime.settings.config

        policy = self.get_person_profile_policy()
        logger.info(
            "person profile policy updated: enabled=%s opt_in_required=%s default_injection_enabled=%s global_injection_enabled=%s scopes=%s",
            policy["enabled"],
            policy["opt_in_required"],
            policy["default_injection_enabled"],
            policy["global_injection_enabled"],
            len(policy["known_scopes"]),
        )
        return policy

    async def close_all(self) -> None:
        async with self._lock:
            runtimes = list(self._runtimes.values())
            self._runtimes.clear()

        logger.info("close all runtimes: count=%s", len(runtimes))
        for runtime in runtimes:
            try:
                await runtime.task_manager.stop()
            except Exception:
                logger.warning("stop task manager failed: scope=%s", runtime.scope_key, exc_info=True)
            try:
                await runtime.context.close()
            except Exception:
                logger.warning("close context failed: scope=%s", runtime.scope_key, exc_info=True)
        logger.info("all runtimes closed")
