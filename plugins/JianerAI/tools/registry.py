from __future__ import annotations

import asyncio
import inspect
import json
import math
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from plugins.JianerAI.tools.contracts import (
    ToolCall,
    ToolContext,
    ToolExecutionError,
    ToolRegistration,
    ToolResult,
    ToolRisk,
    ToolSpec,
)


class ToolRegistry:
    """A reload-generation-scoped registry for model-callable tools."""

    def __init__(
        self,
        *,
        allowed_risks: frozenset[ToolRisk] = frozenset({ToolRisk.READ_ONLY}),
        allowed_mutating_tools: frozenset[str] | None = None,
    ) -> None:
        self._allowed_risks = frozenset(allowed_risks)
        self._allowed_mutating_tools = (
            None
            if allowed_mutating_tools is None
            else frozenset(str(name) for name in allowed_mutating_tools)
        )
        self._tools: dict[str, tuple[str, ToolSpec]] = {}
        self._active_tasks: set[asyncio.Task[Any]] = set()
        self._closed = False

    def register(self, spec: ToolSpec) -> ToolRegistration:
        if self._closed:
            raise RuntimeError("tool registry is closed")
        if not isinstance(spec, ToolSpec):
            spec = _coerce_spec(spec)
        _validate_schema_definition(spec.input_schema)
        if spec.name in self._tools:
            raise ValueError(f"tool already registered: {spec.name}")
        token = uuid.uuid4().hex
        self._tools[spec.name] = (token, spec)
        return ToolRegistration(token=token, name=spec.name)

    def unregister(self, registration: ToolRegistration | str) -> bool:
        token_value = getattr(registration, "token", registration)
        token = str(token_value or "")
        for name, (current_token, _) in tuple(self._tools.items()):
            if current_token == token:
                del self._tools[name]
                return True
        return False

    def available(self, context: ToolContext) -> tuple[ToolSpec, ...]:
        if self._closed:
            return ()
        protocol = str(context.conversation.protocol).casefold()
        capabilities = frozenset(getattr(context.actions, "capabilities", ()))
        output: list[ToolSpec] = []
        for _, spec in sorted(self._tools.values(), key=lambda item: item[1].name):
            if spec.risk not in self._allowed_risks:
                continue
            if (
                spec.risk is ToolRisk.MUTATING
                and self._allowed_mutating_tools is not None
                and spec.name not in self._allowed_mutating_tools
            ):
                continue
            if spec.supported_protocols and protocol not in spec.supported_protocols:
                continue
            if not spec.required_capabilities.issubset(capabilities):
                continue
            output.append(spec)
        return tuple(output)

    async def execute(self, call: ToolCall, context: ToolContext) -> ToolResult:
        if self._closed:
            return _error_result(call, "registry_closed", "工具系统正在关闭。")
        stored = self._tools.get(str(call.name))
        if stored is None:
            return _error_result(call, "unknown_tool", "请求的工具不存在或不可用。")
        spec = stored[1]
        if spec not in self.available(context):
            return _error_result(call, "tool_not_allowed", "当前上下文不允许使用该工具。")
        try:
            arguments = _normalize_arguments(call.arguments)
            _validate_value(arguments, spec.input_schema, path="$", definition=True)
        except (TypeError, ValueError) as exc:
            return _error_result(call, "invalid_arguments", str(exc)[:500])
        try:
            async with asyncio.timeout(spec.timeout_seconds):
                task = asyncio.create_task(
                    _invoke_handler(spec, context, arguments),
                    name=f"jianer-ai-tool-{spec.name}",
                )
                self._active_tasks.add(task)
                try:
                    value = await task
                finally:
                    self._active_tasks.discard(task)
        except TimeoutError:
            return _error_result(call, "tool_timeout", "工具执行超时。")
        except asyncio.CancelledError:
            raise
        except ToolExecutionError as exc:
            return _error_result(call, exc.code, exc.safe_message)
        except Exception:
            return _error_result(call, "tool_failed", "工具执行失败。")
        payload = {"ok": True, "data": _json_safe(value)}
        content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(content) > spec.max_output_chars:
            content = json.dumps(
                {
                    "ok": True,
                    "data": content[: spec.max_output_chars - 100],
                    "truncated": True,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        return ToolResult(
            call_id=str(call.id),
            name=spec.name,
            ok=True,
            content=content,
        )

    def close(self) -> None:
        self._closed = True
        self._tools.clear()
        for task in tuple(self._active_tasks):
            task.cancel()

    async def shutdown(self) -> None:
        callbacks = tuple(
            spec.shutdown
            for _, spec in self._tools.values()
            if spec.shutdown is not None
        )
        self.close()
        pending = tuple(self._active_tasks)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._active_tasks.clear()
        seen: set[tuple[int, int]] = set()
        for callback in callbacks:
            identity = (
                id(getattr(callback, "__self__", None)),
                id(getattr(callback, "__func__", callback)),
            )
            if identity in seen:
                continue
            seen.add(identity)
            try:
                await _invoke_shutdown(callback)
            except Exception:
                continue


def _normalize_arguments(arguments: Any) -> Mapping[str, Any]:
    if isinstance(arguments, Mapping):
        return dict(arguments)
    if isinstance(arguments, str):
        try:
            decoded = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise ValueError("工具参数不是有效 JSON。") from exc
        if isinstance(decoded, Mapping):
            return dict(decoded)
    raise TypeError("工具参数必须是 JSON 对象。")


async def _invoke_handler(
    spec: ToolSpec,
    context: ToolContext,
    arguments: Mapping[str, Any],
) -> Any:
    if inspect.iscoroutinefunction(spec.handler):
        return await spec.handler(context, arguments)
    value = await asyncio.to_thread(spec.handler, context, arguments)
    if inspect.isawaitable(value):
        return await value
    return value


async def _invoke_shutdown(callback: Any) -> None:
    if inspect.iscoroutinefunction(callback):
        await callback()
        return
    value = callback()
    if inspect.isawaitable(value):
        await value


def _coerce_spec(value: Any) -> ToolSpec:
    try:
        risk_value = getattr(value, "risk", ToolRisk.READ_ONLY)
        risk = ToolRisk(getattr(risk_value, "value", risk_value))
        return ToolSpec(
            name=getattr(value, "name"),
            description=getattr(value, "description"),
            input_schema=getattr(value, "input_schema"),
            handler=getattr(value, "handler"),
            risk=risk,
            timeout_seconds=float(getattr(value, "timeout_seconds", 10.0)),
            max_output_chars=int(getattr(value, "max_output_chars", 8192)),
            supported_protocols=frozenset(
                getattr(value, "supported_protocols", ())
            ),
            required_capabilities=frozenset(
                getattr(value, "required_capabilities", ())
            ),
            shutdown=getattr(value, "shutdown", None),
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise TypeError("spec must provide the ToolSpec contract") from exc


def _error_result(call: ToolCall, code: str, message: str) -> ToolResult:
    content = json.dumps(
        {"ok": False, "error_code": code, "message": str(message)},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return ToolResult(
        call_id=str(call.id),
        name=str(call.name),
        ok=False,
        content=content,
        error_code=code,
    )


def duplicate_call_result(call: ToolCall) -> ToolResult:
    return _error_result(
        call,
        "duplicate_tool_call_id",
        "模型重复使用了同一个工具调用 ID。",
    )


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "__dict__"):
        return {
            str(key): _json_safe(item)
            for key, item in vars(value).items()
            if not str(key).startswith("_")
        }
    return str(value)


def _validate_schema_definition(schema: Mapping[str, Any]) -> None:
    _validate_value({}, schema, path="$schema", definition=True, check_required=False)


def _validate_value(
    value: Any,
    schema: Mapping[str, Any],
    *,
    path: str,
    definition: bool,
    check_required: bool = True,
) -> None:
    if not isinstance(schema, Mapping):
        raise TypeError(f"{path} schema must be an object")
    allowed_keywords = {
        "type",
        "description",
        "properties",
        "required",
        "additionalProperties",
        "enum",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "items",
        "minItems",
        "maxItems",
        "default",
    }
    unsupported = set(schema) - allowed_keywords
    if unsupported:
        raise ValueError(f"{path} uses unsupported schema keywords: {sorted(unsupported)}")
    expected = schema.get("type")
    if expected not in {"object", "array", "string", "integer", "number", "boolean", None}:
        raise ValueError(f"{path} has unsupported type: {expected}")
    if definition:
        properties = schema.get("properties", {})
        if properties is not None and not isinstance(properties, Mapping):
            raise TypeError(f"{path}.properties must be an object")
        for name, child in (properties or {}).items():
            _validate_value(None, child, path=f"{path}.{name}", definition=True, check_required=False)
        items = schema.get("items")
        if items is not None:
            _validate_value(None, items, path=f"{path}[]", definition=True, check_required=False)
        required = schema.get("required", ())
        if not isinstance(required, Sequence) or isinstance(required, (str, bytes)):
            raise TypeError(f"{path}.required must be an array")
        unknown_required = set(required) - set(properties or {})
        if unknown_required:
            raise ValueError(f"{path} requires unknown properties: {sorted(unknown_required)}")
        if value is None or not check_required:
            return
    if expected == "object":
        if not isinstance(value, Mapping):
            raise TypeError(f"{path} must be an object")
        properties = schema.get("properties", {}) or {}
        required = schema.get("required", ()) or ()
        if check_required:
            missing = [name for name in required if name not in value]
            if missing:
                raise ValueError(f"{path} is missing required properties: {missing}")
        if schema.get("additionalProperties", True) is False:
            unexpected = set(value) - set(properties)
            if unexpected:
                raise ValueError(f"{path} has unexpected properties: {sorted(unexpected)}")
        for name, item in value.items():
            child = properties.get(name)
            if child is not None:
                _validate_value(item, child, path=f"{path}.{name}", definition=False)
    elif expected == "array":
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
            raise TypeError(f"{path} must be an array")
        _check_numeric_bound(len(value), schema, path, "minItems", "maxItems")
        if schema.get("items") is not None:
            for index, item in enumerate(value):
                _validate_value(item, schema["items"], path=f"{path}[{index}]", definition=False)
    elif expected == "string":
        if not isinstance(value, str):
            raise TypeError(f"{path} must be a string")
        _check_numeric_bound(len(value), schema, path, "minLength", "maxLength")
    elif expected == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{path} must be an integer")
        _check_numeric_bound(value, schema, path, "minimum", "maximum")
    elif expected == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{path} must be a number")
        _check_numeric_bound(value, schema, path, "minimum", "maximum")
    elif expected == "boolean" and not isinstance(value, bool):
        raise TypeError(f"{path} must be a boolean")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} is not one of the allowed values")


def _check_numeric_bound(
    value: float,
    schema: Mapping[str, Any],
    path: str,
    minimum_key: str,
    maximum_key: str,
) -> None:
    if minimum_key in schema and value < schema[minimum_key]:
        raise ValueError(f"{path} is below {minimum_key}")
    if maximum_key in schema and value > schema[maximum_key]:
        raise ValueError(f"{path} exceeds {maximum_key}")
