"""Optional MCP client provider for JianerAI's tool registry.

The MCP SDK is imported only when a real server connection is opened, so the
core plugin remains usable without the optional dependency installed.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from contextlib import AsyncExitStack, asynccontextmanager
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Callable

from plugins.JianerAI.tools.contracts import (
    ToolContext,
    ToolExecutionError,
    ToolRisk,
    ToolSpec,
)


_SAFE_NAME = re.compile(r"[^A-Za-z0-9_-]+")
_SUPPORTED_SCHEMA_KEYS = {
    "type",
    "description",
    "properties",
    "required",
    "additionalProperties",
    "enum",
    "anyOf",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
    "items",
    "minItems",
    "maxItems",
    "default",
}
_IGNORED_SCHEMA_METADATA = {"title", "$schema", "$id", "examples", "example"}


@dataclass(frozen=True, slots=True)
class MCPServerConfig:
    """Connection settings for one MCP server.

    ``transport`` accepts ``stdio`` or ``streamable_http`` (``http`` is an
    alias). Mutating tools are identified by either their server tool name or
    the generated JianerAI tool name; every other tool is read-only.
    """

    name: str
    transport: str
    command: str = ""
    args: tuple[str, ...] = ()
    env: Mapping[str, str] | None = None
    url: str = ""
    headers: Mapping[str, str] | None = None
    mutating_tools: frozenset[str] = frozenset()
    timeout_seconds: float = 30.0
    max_output_chars: int = 8192

    def __post_init__(self) -> None:
        normalized_transport = str(self.transport).strip().casefold()
        if normalized_transport == "http":
            normalized_transport = "streamable_http"
        if normalized_transport not in {"stdio", "streamable_http"}:
            raise ValueError("MCP transport must be stdio or streamable_http")
        object.__setattr__(self, "transport", normalized_transport)
        if not str(self.name).strip():
            raise ValueError("MCP server name is required")
        if normalized_transport == "stdio" and not str(self.command).strip():
            raise ValueError(f"MCP server {self.name!r} requires a command")
        if normalized_transport == "streamable_http" and not str(self.url).strip():
            raise ValueError(f"MCP server {self.name!r} requires a URL")
        if self.timeout_seconds <= 0:
            raise ValueError("MCP timeout_seconds must be positive")
        if self.max_output_chars < 256:
            raise ValueError("MCP max_output_chars must be at least 256")

    @classmethod
    def from_value(cls, value: MCPServerConfig | Mapping[str, Any]) -> MCPServerConfig:
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("MCP server configuration must be an object")
        name = str(value.get("name", "")).strip()
        transport = str(value.get("transport", "")).strip().casefold()
        if transport == "http":
            transport = "streamable_http"
        if not name:
            raise ValueError("MCP server name is required")
        if transport not in {"stdio", "streamable_http"}:
            raise ValueError("MCP transport must be stdio or streamable_http")
        command = str(value.get("command", "")).strip()
        url = str(value.get("url", "")).strip()
        if transport == "stdio" and not command:
            raise ValueError(f"MCP server {name!r} requires a command")
        if transport == "streamable_http" and not url:
            raise ValueError(f"MCP server {name!r} requires a URL")
        args_value = value.get("args", ())
        if not isinstance(args_value, Sequence) or isinstance(args_value, (str, bytes)):
            raise ValueError(f"MCP server {name!r} args must be an array")
        env = value.get("env")
        headers = value.get("headers")
        if env is not None and not isinstance(env, Mapping):
            raise ValueError(f"MCP server {name!r} env must be an object")
        if headers is not None and not isinstance(headers, Mapping):
            raise ValueError(f"MCP server {name!r} headers must be an object")
        mutating = value.get("mutating_tools", ())
        if not isinstance(mutating, Sequence) or isinstance(mutating, (str, bytes)):
            raise ValueError(f"MCP server {name!r} mutating_tools must be an array")
        timeout = float(value.get("timeout_seconds", 30.0))
        output_limit = int(value.get("max_output_chars", 8192))
        if timeout <= 0:
            raise ValueError("MCP timeout_seconds must be positive")
        if output_limit < 256:
            raise ValueError("MCP max_output_chars must be at least 256")
        return cls(
            name=name,
            transport=transport,
            command=command,
            args=tuple(str(item) for item in args_value),
            env={str(key): str(item) for key, item in env.items()} if env else None,
            url=url,
            headers={str(key): str(item) for key, item in headers.items()}
            if headers
            else None,
            mutating_tools=frozenset(str(item) for item in mutating),
            timeout_seconds=timeout,
            max_output_chars=output_limit,
        )


SessionConnector = Callable[[MCPServerConfig], Any]


class MCPToolPlugin:
    """Expose configured MCP tools as ordinary JianerAI ``ToolSpec`` objects.

    A custom ``session_connector`` is useful for tests and alternate MCP
    transports. It receives an ``MCPServerConfig`` and may return an
    ``AsyncContextManager`` yielding a session, an awaitable of either, or a
    session object with ``initialize``, ``list_tools`` and ``call_tool``.
    """

    plugin_id = "jianer-ai-mcp"

    def __init__(
        self,
        servers: Sequence[MCPServerConfig | Mapping[str, Any]],
        *,
        session_connector: SessionConnector | None = None,
        plugin_id: str | None = None,
    ) -> None:
        self.servers = tuple(MCPServerConfig.from_value(item) for item in servers)
        self.plugin_id = str(plugin_id or self.plugin_id)
        self._session_connector = session_connector or _connect_mcp_server
        self._exit_stack: AsyncExitStack | None = None
        self._specs: tuple[ToolSpec, ...] | None = None
        self.errors: list[str] = []
        self._closed = False

    async def provide_tools(
        self, context: Mapping[str, Any] | None = None
    ) -> tuple[ToolSpec, ...]:
        del context
        if self._closed:
            return ()
        if self._specs is not None:
            return self._specs

        stack = AsyncExitStack()
        specs: list[ToolSpec] = []
        names: set[str] = set()
        self.errors.clear()
        try:
            for config in self.servers:
                server_stack = AsyncExitStack()
                try:
                    session = await self._open_session(server_stack, config)
                    await _maybe_await(session.initialize())
                    remote_tools = await _list_all_tools(session)
                    stack.push_async_callback(server_stack.aclose)
                    for remote_tool in remote_tools:
                        try:
                            spec = self._make_spec(config, session, remote_tool)
                            if spec.name in names:
                                raise ValueError("generated tool name collides")
                            names.add(spec.name)
                            specs.append(spec)
                        except (TypeError, ValueError) as exc:
                            self.errors.append(
                                f"{config.name}: skipped MCP tool: {str(exc)[:240]}"
                            )
                except Exception as exc:
                    await server_stack.aclose()
                    self.errors.append(
                        f"{config.name}: MCP connection/listing failed "
                        f"({type(exc).__name__})"
                    )
            self._exit_stack = stack
            self._specs = tuple(specs)
            return self._specs
        except BaseException:
            await stack.aclose()
            raise

    async def _open_session(
        self, stack: AsyncExitStack, config: MCPServerConfig
    ) -> Any:
        value = self._session_connector(config)
        value = await _maybe_await(value)
        if hasattr(value, "__aenter__") and hasattr(value, "__aexit__"):
            return await stack.enter_async_context(value)
        close = getattr(value, "aclose", None) or getattr(value, "close", None)
        if callable(close):
            stack.push_async_callback(_close_session, close)
        return value

    def _make_spec(
        self, config: MCPServerConfig, session: Any, remote_tool: Any
    ) -> ToolSpec:
        remote_name = str(_get(remote_tool, "name", "")).strip()
        if not remote_name:
            raise ValueError("tool has no name")
        schema = normalize_input_schema(_get(remote_tool, "inputSchema", None))
        public_name = _namespaced_name(config.name, remote_name)
        description = str(_get(remote_tool, "description", "")).strip()
        if not description:
            description = f"MCP server {config.name} tool {remote_name}"
        mutating = remote_name in config.mutating_tools or public_name in config.mutating_tools

        async def invoke(
            tool_context: ToolContext,
            arguments: Mapping[str, Any],
            *,
            _session: Any = session,
            _name: str = remote_name,
            _limit: int = config.max_output_chars,
        ) -> Any:
            del tool_context
            try:
                result = await _maybe_await(
                    _session.call_tool(name=_name, arguments=dict(arguments))
                )
            except ToolExecutionError:
                raise
            except Exception as exc:
                raise ToolExecutionError(
                    "mcp_call_failed", "MCP 工具调用失败。"
                ) from exc
            payload = _result_payload(result)
            if bool(_get(result, "isError", False)):
                message = _payload_text(payload)[:400] or "MCP 工具返回错误。"
                raise ToolExecutionError("mcp_tool_error", message)
            return _cap_payload(payload, _limit)

        return ToolSpec(
            name=public_name,
            description=description[:1000],
            input_schema=schema,
            handler=invoke,
            risk=ToolRisk.MUTATING if mutating else ToolRisk.READ_ONLY,
            timeout_seconds=config.timeout_seconds,
            max_output_chars=config.max_output_chars,
        )

    async def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._exit_stack is not None:
            stack, self._exit_stack = self._exit_stack, None
            await stack.aclose()
        self._specs = None


def normalize_input_schema(value: Any) -> dict[str, Any]:
    """Normalize a remote JSON Schema to ToolRegistry's supported subset."""

    if not isinstance(value, Mapping):
        raise ValueError("inputSchema must be a JSON object")
    normalized = _normalize_schema_node(value, "$", root=True)
    if normalized.get("type") != "object":
        raise ValueError("inputSchema root type must be object")
    return normalized


def _normalize_schema_node(value: Any, path: str, *, root: bool = False) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} schema must be an object")
    unknown = set(value) - _SUPPORTED_SCHEMA_KEYS - _IGNORED_SCHEMA_METADATA
    if unknown:
        raise ValueError(f"{path} has unsupported schema keywords: {sorted(unknown)}")
    result: dict[str, Any] = {
        key: item for key, item in value.items() if key in _SUPPORTED_SCHEMA_KEYS
    }
    schema_type = result.get("type")
    if isinstance(schema_type, list):
        types = [item for item in schema_type if isinstance(item, str)]
        if not types or len(types) != len(schema_type):
            raise ValueError(f"{path}.type must contain JSON Schema type names")
        alternatives = []
        for item in types:
            branch = dict(result)
            branch["type"] = item
            alternatives.append(_normalize_schema_node(branch, path))
        result = {"anyOf": alternatives}
        if "description" in value:
            result["description"] = value["description"]
    else:
        if schema_type is not None and schema_type not in {
            "object",
            "array",
            "string",
            "integer",
            "number",
            "boolean",
            "null",
        }:
            raise ValueError(f"{path} has unsupported type: {schema_type}")
        properties = result.get("properties")
        if properties is not None:
            if not isinstance(properties, Mapping):
                raise ValueError(f"{path}.properties must be an object")
            result["properties"] = {
                str(name): _normalize_schema_node(child, f"{path}.{name}")
                for name, child in properties.items()
            }
        if "items" in result:
            result["items"] = _normalize_schema_node(result["items"], f"{path}[]")
        if "anyOf" in result:
            alternatives = result["anyOf"]
            if not isinstance(alternatives, Sequence) or isinstance(
                alternatives, (str, bytes)
            ):
                raise ValueError(f"{path}.anyOf must be an array")
            result["anyOf"] = [
                _normalize_schema_node(child, f"{path}.anyOf[{index}]")
                for index, child in enumerate(alternatives)
            ]
        required = result.get("required", ())
        if not isinstance(required, Sequence) or isinstance(required, (str, bytes)):
            raise ValueError(f"{path}.required must be an array")
        if any(not isinstance(item, str) for item in required):
            raise ValueError(f"{path}.required entries must be strings")
        if properties is not None and set(required) - set(properties):
            raise ValueError(f"{path}.required references unknown properties")
        if properties is None and required:
            raise ValueError(f"{path}.required needs properties")
        if "enum" in result and (
            not isinstance(result["enum"], Sequence)
            or isinstance(result["enum"], (str, bytes))
        ):
            raise ValueError(f"{path}.enum must be an array")
        if "additionalProperties" in result and not isinstance(
            result["additionalProperties"], bool
        ):
            raise ValueError(f"{path}.additionalProperties must be a boolean")
        for key in ("minimum", "maximum"):
            if key in result and (
                isinstance(result[key], bool)
                or not isinstance(result[key], (int, float))
            ):
                raise ValueError(f"{path}.{key} must be a number")
        for key in ("minLength", "maxLength", "minItems", "maxItems"):
            if key in result and (
                isinstance(result[key], bool)
                or not isinstance(result[key], int)
                or result[key] < 0
            ):
                raise ValueError(f"{path}.{key} must be a non-negative integer")
        if "description" in result and not isinstance(result["description"], str):
            raise ValueError(f"{path}.description must be a string")
    if root and result.get("type") != "object":
        raise ValueError("inputSchema root type must be object")
    return result


def _namespaced_name(server_name: str, tool_name: str) -> str:
    raw = f"mcp_{_SAFE_NAME.sub('_', server_name).strip('_')}_{_SAFE_NAME.sub('_', tool_name).strip('_')}"
    raw = raw or "mcp_tool"
    if len(raw) <= 64:
        return raw
    suffix = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return f"{raw[:55]}_{suffix}"


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _close_session(close: Callable[[], Any]) -> None:
    await _maybe_await(close())


async def _list_all_tools(session: Any) -> tuple[Any, ...]:
    tools: list[Any] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    for _ in range(100):
        page = await _maybe_await(
            session.list_tools(cursor=cursor) if cursor else session.list_tools()
        )
        tools.extend(_get(page, "tools", ()) or ())
        next_cursor = _get(page, "nextCursor", None)
        if next_cursor is None:
            return tuple(tools)
        cursor = str(next_cursor)
        if not cursor or cursor in seen_cursors:
            raise ValueError("MCP tool listing returned a repeated pagination cursor")
        seen_cursors.add(cursor)
    raise ValueError("MCP tool listing exceeded the pagination limit")


def _result_payload(result: Any) -> Any:
    structured = _get(result, "structuredContent", None)
    if structured is not None:
        return {"structuredContent": _json_safe(structured)}
    content = []
    for item in _get(result, "content", ()) or ():
        kind = str(_get(item, "type", ""))
        if kind == "text":
            content.append({"type": "text", "text": str(_get(item, "text", ""))})
        elif kind in {"image", "audio", "resource_link"}:
            content.append(
                {
                    "type": kind,
                    "mimeType": _get(item, "mimeType"),
                    "uri": _get(item, "uri"),
                }
            )
        else:
            content.append({"type": kind or "unknown"})
    return {"content": content}


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_safe(item) for item in value]
    return str(value)


def _payload_text(payload: Any) -> str:
    if isinstance(payload, Mapping):
        content = payload.get("content", ())
        return " ".join(
            str(item.get("text", ""))
            for item in content
            if isinstance(item, Mapping) and item.get("type") == "text"
        )
    return str(payload)


def _cap_payload(payload: Any, max_chars: int) -> Any:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) <= max_chars:
        return payload
    budget = max(32, max_chars - 64)
    return {"truncated": True, "content": encoded[:budget]}


@asynccontextmanager
async def _connect_mcp_server(config: MCPServerConfig) -> AsyncIterator[Any]:
    """Create a session using the official MCP Python SDK."""

    try:
        from mcp import ClientSession
        if config.transport == "stdio":
            from mcp import StdioServerParameters
            from mcp.client.stdio import stdio_client

            params = StdioServerParameters(
                command=config.command,
                args=list(config.args),
                env=dict(config.env) if config.env is not None else None,
            )
            async with stdio_client(params) as streams:
                read_stream, write_stream = streams[0], streams[1]
                async with ClientSession(read_stream, write_stream) as session:
                    yield session
            return
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(
            config.url,
            headers=dict(config.headers) if config.headers is not None else None,
        ) as streams:
            read_stream, write_stream = streams[0], streams[1]
            async with ClientSession(read_stream, write_stream) as session:
                yield session
    except ImportError as exc:
        raise RuntimeError(
            "MCP support requires the optional 'mcp' Python package"
        ) from exc


__all__ = [
    "MCPServerConfig",
    "MCPToolPlugin",
    "normalize_input_schema",
]
