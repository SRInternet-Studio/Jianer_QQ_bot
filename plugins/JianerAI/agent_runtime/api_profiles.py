"""HypeR-style active API profile facade over aiconfig model files."""

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
    ) -> None:
        self.providers = providers
        self._active = ""
        preferred = str(active_model or default_model or "").strip()
        models = self.providers.list_models()
        if preferred in models and self.providers.supports_tools(preferred):
            self._active = preferred
        elif models:
            self._active = next(
                (
                    key
                    for key in models
                    if self.providers.supports_tools(key)
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
        try:
            self.providers.get(normalized)
        except UnknownModelError as exc:
            raise ModelConfigError(f"unknown Agent model: {normalized}") from exc
        if not self.providers.supports_tools(normalized):
            raise ModelConfigError(
                f"Agent model {normalized!r} does not support tools"
            )
        self._active = normalized
        return normalized

    def reload(self) -> Mapping[str, str]:
        models = self.providers.reload()
        if self._active not in models:
            self._active = (
                next(iter(models))
                if models
                else ""
            )
        return models

    def redacted_status(self) -> dict[str, Any]:
        current = self._active
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
            "supports_tools": self.providers.supports_tools(current),
            "available": sorted(self.providers.list_models()),
        }
