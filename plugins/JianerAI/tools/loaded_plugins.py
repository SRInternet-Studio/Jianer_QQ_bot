"""Opt-in bridge from active JianerCore plugins to JianerAI ToolSpecs."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import re
from collections.abc import Iterable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from typing import Any, Callable

from plugins.JianerAI.tools.contracts import ToolContext, ToolExecutionError, ToolSpec
from plugins.JianerAI.tools.plugin_registry import ToolPluginManager


_PLUGIN_TOOL_ID = "jianerbot-host-plugin-"
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_-]+")


class LoadedPluginToolProvider:
    """Wrap explicitly exported tools with namespacing and generation leases."""

    def __init__(
        self,
        plugin_id: str,
        manager: Any,
        specs: Iterable[ToolSpec],
        *,
        source_provider: Any = None,
        lease_factory: Callable[[Any], AbstractAsyncContextManager[Any]] | None = None,
    ) -> None:
        self.host_plugin_id = str(plugin_id)
        self.plugin_id = _tool_plugin_id(self.host_plugin_id)
        self.manager = manager
        self.source_provider = source_provider
        self._lease_factory = lease_factory or _plugin_manager_lease
        original_specs = tuple(specs)
        self._cleanup_callbacks = tuple(
            spec.shutdown for spec in original_specs if spec.shutdown is not None
        )
        self._specs = tuple(
            _namespace_spec(
                self.host_plugin_id,
                manager,
                spec,
                self._lease_factory,
            )
            for spec in original_specs
        )

    def provide_tools(self, context: Mapping[str, Any] | None = None) -> tuple[ToolSpec, ...]:
        del context
        return self._specs

    async def shutdown(self) -> None:
        seen: set[tuple[int, int]] = set()
        for callback in self._cleanup_callbacks:
            identity = (id(getattr(callback, "__self__", None)), id(getattr(callback, "__func__", callback)))
            if identity in seen:
                continue
            seen.add(identity)
            await _invoke_cleanup(callback)
        callback = getattr(self.source_provider, "shutdown", None)
        if callable(callback):
            await _invoke_cleanup(callback)


async def load_active_plugin_tools(
    registry: Any,
    manager: Any,
    *,
    runtime: Mapping[str, Any],
    service: Any,
    logger: Any = None,
    lease_factory: Callable[[Any], AbstractAsyncContextManager[Any]] | None = None,
) -> tuple[str, ...]:
    """Load tools only from modules that export an explicit provider hook."""

    plugins = getattr(manager, "plugins", None)
    if not isinstance(plugins, Mapping):
        return ()
    loaded: list[str] = []
    tool_manager = ToolPluginManager(registry)
    for plugin_id, record in sorted(plugins.items(), key=lambda item: str(item[0])):
        module = getattr(record, "module", None)
        if module is None or str(plugin_id) == "jianerbot-plugin-jianer-ai":
            continue
        context = {
            "plugin_id": str(plugin_id),
            "runtime": runtime,
            "service": service,
        }
        try:
            specs, source_provider = await _read_plugin_specs(module, context)
            if not specs:
                continue
            provider = LoadedPluginToolProvider(
                str(plugin_id),
                manager,
                specs,
                source_provider=source_provider,
                lease_factory=lease_factory,
            )
            await tool_manager.load_async(provider)
            loaded.append(str(plugin_id))
        except Exception as exc:
            _log_provider_error(logger, str(plugin_id), exc)
    return tuple(loaded)


async def _read_plugin_specs(
    module: Any,
    context: Mapping[str, Any],
) -> tuple[tuple[ToolSpec, ...], Any]:
    factory = getattr(module, "create_agent_tool_provider", None)
    if callable(factory):
        source = await _call_with_context(factory, context)
        callback = getattr(source, "provide_tools", None) or getattr(source, "tools", None)
        if not callable(callback):
            raise TypeError("create_agent_tool_provider must return a tool provider")
        value = await _call_with_context(callback, context)
        return _normalize_specs(value), source

    callback = getattr(module, "provide_agent_tools", None)
    if callable(callback):
        value = await _call_with_context(callback, context)
        return _normalize_specs(value), None
    return (), None


async def _call_with_context(callback: Callable[..., Any], context: Mapping[str, Any]) -> Any:
    try:
        signature = inspect.signature(callback)
        signature.bind(context)
    except (TypeError, ValueError):
        value = callback()
    else:
        value = callback(context)
    if inspect.isawaitable(value):
        return await value
    return value


def _normalize_specs(value: Any) -> tuple[ToolSpec, ...]:
    if value is None:
        return ()
    if isinstance(value, ToolSpec):
        return (value,)
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes, Mapping)):
        raise TypeError("agent plugin tools must be ToolSpec objects")
    specs = tuple(value)
    if any(not isinstance(spec, ToolSpec) for spec in specs):
        raise TypeError("agent plugin tools must contain only ToolSpec objects")
    return specs


def _namespace_spec(
    plugin_id: str,
    manager: Any,
    spec: ToolSpec,
    lease_factory: Callable[[Any], AbstractAsyncContextManager[Any]],
) -> ToolSpec:
    name = _namespaced_tool_name(plugin_id, spec.name)
    original_handler = spec.handler

    async def handler(context: ToolContext, arguments: Mapping[str, Any]) -> Any:
        lease_stack = AsyncExitStack()
        try:
            await lease_stack.enter_async_context(lease_factory(manager))
        except RuntimeError as exc:
            raise ToolExecutionError(
                "plugin_generation_unavailable",
                "插件已重新加载，当前工具调用已取消。",
            ) from exc
        try:
            if inspect.iscoroutinefunction(original_handler):
                return await original_handler(context, arguments)
            value = await asyncio.to_thread(original_handler, context, arguments)
            if inspect.isawaitable(value):
                return await value
            return value
        finally:
            await lease_stack.aclose()

    return ToolSpec(
        name=name,
        description=f"[{plugin_id}] {spec.description}",
        input_schema=spec.input_schema,
        handler=handler,
        risk=spec.risk,
        timeout_seconds=spec.timeout_seconds,
        max_output_chars=spec.max_output_chars,
        supported_protocols=spec.supported_protocols,
        required_capabilities=spec.required_capabilities,
        required_privilege=spec.required_privilege,
        shutdown=None,
    )


def _namespaced_tool_name(plugin_id: str, tool_name: str) -> str:
    safe_plugin = _SAFE_NAME.sub("_", plugin_id).strip("_-") or "plugin"
    raw = f"plugin_{safe_plugin}_{tool_name}"
    if len(raw) <= 64:
        return raw
    suffix = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return f"{raw[:55]}_{suffix}"


def _tool_plugin_id(plugin_id: str) -> str:
    raw = _PLUGIN_TOOL_ID + _SAFE_NAME.sub("-", plugin_id).strip("-_")
    return raw[:128]


def _plugin_manager_lease(manager: Any):
    from bot.plugin_state import plugin_manager_lease

    return plugin_manager_lease(manager)


async def _invoke_cleanup(callback: Callable[..., Any]) -> None:
    value = callback()
    if inspect.isawaitable(value):
        await value


def _log_provider_error(logger: Any, plugin_id: str, exc: Exception) -> None:
    callback = getattr(logger, "warning", None)
    if callable(callback):
        callback(
            "JianerAI skipped plugin Agent tools for %s (%s)",
            plugin_id,
            type(exc).__name__,
        )


__all__ = ["LoadedPluginToolProvider", "load_active_plugin_tools"]
