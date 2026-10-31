"""JianerAI active API profile facade over aiconfig model files."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from plugins.JianerAI.providers import (
    ModelConfigError,
    ProviderRegistry,
    UnknownModelError,
)


class ApiModelManager:
    """Select one global Agent model from ``aiconfig/*.ai.json``."""

    def __init__(
        self,
        providers: ProviderRegistry,
        *,
        default_model: str = "",
        active_model: str = "",
    require_tools: bool = True,
    allow_empty: bool = False,
    ) -> None:
        self.providers = providers
        self.require_tools = bool(require_tools)
        self.allow_empty = bool(allow_empty)
        self._active = ""
        preferred = str(active_model or default_model or "").strip()
        models = self.providers.list_models()
        if preferred in models and (
            not self.require_tools or self.providers.supports_tools(preferred)
        ):
            self._active = preferred
        elif self.allow_empty and not preferred:
            self._active = ""
        elif models:
            self._active = next(
                (
                    key
                    for key in models
                    if not self.require_tools
                    or self.providers.supports_tools(key)
                ),
                next(iter(models)),
            )
        elif preferred:
            self._active = preferred

    @property
    def active(self) -> str:
        return self._active

    def list_models(self) -> Mapping[str, str]:
        return self.providers.list_models()

    def activate(self, model: str) -> str:
        normalized = str(model or "").strip()
        if not normalized and self.allow_empty:
            self._active = ""
            return ""
        try:
            self.providers.get(normalized)
        except UnknownModelError as exc:
            raise ModelConfigError(f"unknown Agent model: {normalized}") from exc
        if self.require_tools and not self.providers.supports_tools(normalized):
            raise ModelConfigError(
                f"Agent model {normalized!r} does not support tools"
            )
        self._active = normalized
        return normalized

    def reload(self) -> Mapping[str, str]:
        models = self.providers.reload()
        if self._active not in models:
            self._active = ""
            if models and not self.allow_empty:
                self._active = (
                    next(
                        (
                            key
                            for key in models
                            if not self.require_tools
                            or self.providers.supports_tools(key)
                        ),
                        "",
                    )
                )
        return models

    def redacted_status(self) -> dict[str, Any]:
        current = self._active
        if not current and self.allow_empty:
            return {
                "model": "",
                "status": "disabled",
                "supports_tools": False,
                "available": sorted(self.providers.list_models()),
            }
        try:
            config = self.providers.get(current)
        except Exception:
            return {
                "model": current,
                "available": sorted(self.providers.list_models()),
                "status": "unavailable",
            }
        return {
            "model": config.key,
            "friendly_name": config.friendly_name,
            "provider": config.provider,
            "model_id": config.model,
            "base_url": config.base_url,
            "supports_tools": (
                self.providers.supports_tools(current)
                if self.require_tools
                else None
            ),
            "available": sorted(self.providers.list_models()),
        }
