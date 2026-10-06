"""Dynamic, reload-safe registration for JianerAI tool plugins.

Tool plugins are intentionally small: a module/object only needs a stable
``plugin_id`` and a ``provide_tools`` (or ``tools``) callable.  The manager
keeps ownership tokens so unloading a plugin cannot remove tools registered by
another plugin with the same registry.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import uuid
from collections.abc import Iterable, Mapping
from types import ModuleType
from typing import Any

from plugins.JianerAI.tools.contracts import (
    ToolPluginRegistration,
    ToolRegistration,
    ToolSpec,
)

_PLUGIN_ID_LIMIT = 128


class ToolPluginError(RuntimeError):
    """Raised when a tool plugin does not satisfy the plugin contract."""


class StaticToolPlugin:
    """Provider used to expose built-in tool factories as plugins."""

    def __init__(self, plugin_id: str, specs: Iterable[ToolSpec]) -> None:
        self.plugin_id = str(plugin_id).strip()
        self._specs = tuple(specs)

    def provide_tools(
        self, context: Mapping[str, Any] | None = None
    ) -> tuple[ToolSpec, ...]:
        del context
        return self._specs


class ToolPluginManager:
    """Load and unload ToolPlugin providers for one ``ToolRegistry``."""

    def __init__(self, registry: Any) -> None:
        self.registry = registry
        if not hasattr(registry, "_plugin_providers"):
            registry._plugin_providers = {}

    def load(
        self,
        plugin: Any,
        *,
        context: Mapping[str, Any] | None = None,
        plugin_id: str | None = None,
    ) -> ToolPluginRegistration:
        provider = _resolve_provider(plugin)
        resolved_id = _normalise_plugin_id(
            plugin_id
            or getattr(provider, "plugin_id", None)
            or getattr(provider, "__tool_plugin_id__", None)
            or getattr(plugin, "__name__", None)
        )
        if any(
            item.plugin_id == resolved_id
            for item in self.registry._plugins.values()
        ):
            raise ToolPluginError(f"tool plugin already loaded: {resolved_id}")
        specs = _provide_specs(provider, context)
        registrations: list[ToolRegistration] = []
        try:
            for spec in specs:
                registrations.append(self.registry.register(spec))
        except Exception:
            for registration in registrations:
                self.registry.unregister(registration)
            raise
        handle = ToolPluginRegistration(
            token=uuid.uuid4().hex,
            plugin_id=resolved_id,
            tool_registrations=tuple(registrations),
        )
        self.registry._plugins[handle.token] = handle
        self.registry._plugin_providers[handle.token] = provider
        return handle

    async def load_async(
        self,
        plugin: Any,
        *,
        context: Mapping[str, Any] | None = None,
        plugin_id: str | None = None,
    ) -> ToolPluginRegistration:
        """Async counterpart for providers that build tools asynchronously."""

        provider = _resolve_provider(plugin)
        resolved_id = _normalise_plugin_id(
            plugin_id
            or getattr(provider, "plugin_id", None)
            or getattr(provider, "__tool_plugin_id__", None)
            or getattr(plugin, "__name__", None)
        )
        if any(
            item.plugin_id == resolved_id
            for item in self.registry._plugins.values()
        ):
            raise ToolPluginError(f"tool plugin already loaded: {resolved_id}")
        specs = await _provide_specs_async(provider, context)
        registrations: list[ToolRegistration] = []
        try:
            for spec in specs:
                registrations.append(self.registry.register(spec))
        except Exception:
            for registration in registrations:
                self.registry.unregister(registration)
            raise
        handle = ToolPluginRegistration(
            token=uuid.uuid4().hex,
            plugin_id=resolved_id,
            tool_registrations=tuple(registrations),
        )
        self.registry._plugins[handle.token] = handle
        self.registry._plugin_providers[handle.token] = provider
        return handle

    def unload(self, plugin: ToolPluginRegistration | str) -> bool:
        token = str(getattr(plugin, "token", plugin) or "")
        handle = self.registry._plugins.pop(token, None)
        if handle is None:
            # Accept a plugin ID for admin/reload callers.
            for candidate_token, candidate in tuple(self.registry._plugins.items()):
                if candidate.plugin_id == token:
                    token, handle = (
                        candidate_token,
                        self.registry._plugins.pop(candidate_token),
                    )
                    break
        if handle is None:
            return False
        for registration in handle.tool_registrations:
            self.registry.unregister(registration)
        provider = self.registry._plugin_providers.pop(token, None)
        _shutdown_provider(provider)
        return True

    async def unload_async(self, plugin: ToolPluginRegistration | str) -> bool:
        token = str(getattr(plugin, "token", plugin) or "")
        handle = self.registry._plugins.get(token)
        if handle is None:
            for candidate_token, candidate in self.registry._plugins.items():
                if candidate.plugin_id == token:
                    token, handle = candidate_token, candidate
                    break
        if handle is None:
            return False
        self.registry._plugins.pop(token, None)
        for registration in handle.tool_registrations:
            self.registry.unregister(registration)
        provider = self.registry._plugin_providers.pop(token, None)
        await _shutdown_provider_async(provider)
        return True

    def loaded(self) -> tuple[str, ...]:
        return tuple(sorted(item.plugin_id for item in self.registry._plugins.values()))


def load_tool_plugin(
    registry: Any,
    plugin: Any,
    *,
    context: Mapping[str, Any] | None = None,
    plugin_id: str | None = None,
) -> ToolPluginRegistration:
    """Convenience API for loading a single plugin into a registry."""

    return ToolPluginManager(registry).load(
        plugin, context=context, plugin_id=plugin_id
    )


def _resolve_provider(plugin: Any) -> Any:
    if isinstance(plugin, str):
        plugin = importlib.import_module(plugin)
    if isinstance(plugin, ModuleType):
        # Modules can export a provider object/factory, or just provide_tools.
        factory = getattr(plugin, "create_tool_plugin", None)
        if callable(factory):
            plugin = factory()
        elif not callable(getattr(plugin, "provide_tools", None)) and callable(
            getattr(plugin, "tools", None)
        ):
            plugin = plugin.tools
    elif inspect.isclass(plugin):
        plugin = plugin()
    elif callable(plugin) and not callable(getattr(plugin, "provide_tools", None)):
        plugin = plugin()
    if not callable(getattr(plugin, "provide_tools", None)) and not callable(
        getattr(plugin, "tools", None)
    ):
        raise ToolPluginError(
            "tool plugin must expose provide_tools(context) or tools(context)"
        )
    return plugin


def _normalise_plugin_id(value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ToolPluginError("tool plugin must define plugin_id")
    if len(result) > _PLUGIN_ID_LIMIT:
        raise ToolPluginError("tool plugin ID is too long")
    return result


def _provide_specs(
    provider: Any, context: Mapping[str, Any] | None
) -> tuple[ToolSpec, ...]:
    callback = getattr(provider, "provide_tools", None) or getattr(
        provider, "tools", None
    )
    try:
        value = callback(context)
    except TypeError:
        value = callback()
    if inspect.isawaitable(value):
        raise ToolPluginError("async provider requires ToolPluginManager.load_async")
    if value is None:
        return ()
    if isinstance(value, ToolSpec):
        return (value,)
    try:
        return tuple(value)
    except TypeError as exc:
        raise ToolPluginError(
            "tool plugin provider must return ToolSpec objects"
        ) from exc


async def _provide_specs_async(
    provider: Any, context: Mapping[str, Any] | None
) -> tuple[ToolSpec, ...]:
    callback = getattr(provider, "provide_tools", None) or getattr(
        provider, "tools", None
    )
    try:
        value = callback(context)
    except TypeError:
        value = callback()
    if inspect.isawaitable(value):
        value = await value
    if value is None:
        return ()
    if isinstance(value, ToolSpec):
        return (value,)
    try:
        return tuple(value)
    except TypeError as exc:
        raise ToolPluginError(
            "tool plugin provider must return ToolSpec objects"
        ) from exc


def _shutdown_provider(provider: Any) -> None:
    callback = getattr(provider, "shutdown", None)
    if not callable(callback):
        return
    value = callback()
    if inspect.isawaitable(value):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(value)
        else:
            loop.create_task(value)


async def _shutdown_provider_async(provider: Any) -> None:
    callback = getattr(provider, "shutdown", None)
    if not callable(callback):
        return
    value = callback()
    if inspect.isawaitable(value):
        await value


__all__ = [
    "StaticToolPlugin",
    "ToolPluginError",
    "ToolPluginManager",
    "load_tool_plugin",
]
