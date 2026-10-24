"""HypeR-style class/decorator tool declarations.

Existing ToolSpec providers remain first-class.  New built-ins can use
``AgentToolBase`` and ``@tool`` while the compatibility registry presents
both forms through one schema and dispatch path.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, get_type_hints

from plugins.JianerAI.tools.contracts import (
    ToolContext,
    ToolRisk,
    ToolSpec,
)


@dataclass(frozen=True, slots=True)
class ToolDeclaration:
    name: str
    description: str
    perm: str
    scenes: frozenset[str]
    group: str
    main_visible: bool
    sub_visible: bool
    system_visible: bool
    release: bool
    preserve: bool
    risk: ToolRisk


def tool(
    *,
    name: str | None = None,
    desc: str = "",
    perm: str = "member",
    scenes: tuple[str, ...] = ("group", "private", "system"),
    group: str = "general",
    main_visible: bool = True,
    sub_visible: bool = True,
    system_visible: bool = False,
    release: bool = False,
    preserve: bool = False,
    risk: ToolRisk = ToolRisk.READ_ONLY,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(method: Callable[..., Any]) -> Callable[..., Any]:
        setattr(
            method,
            "__jianer_agent_tool__",
            ToolDeclaration(
                name=str(name or method.__name__),
                description=str(desc or inspect.getdoc(method) or "").strip(),
                perm=str(perm or "member"),
                scenes=frozenset(scenes),
                group=str(group or "general"),
                main_visible=bool(main_visible),
                sub_visible=bool(sub_visible),
                system_visible=bool(system_visible),
                release=bool(release),
                preserve=bool(preserve),
                risk=risk,
            ),
        )
        return method

    return decorator


class AgentToolBase:
    """Collect decorated methods for registration on one ToolRegistry."""

    @classmethod
    def tool_declarations(cls) -> tuple[tuple[str, ToolDeclaration], ...]:
        output: list[tuple[str, ToolDeclaration]] = []
        for name, member in inspect.getmembers(cls):
            declaration = getattr(member, "__jianer_agent_tool__", None)
            if isinstance(declaration, ToolDeclaration):
                output.append((name, declaration))
        return tuple(output)


def bind_decorated_tools(instance: AgentToolBase) -> tuple[ToolSpec, ...]:
    specs: list[ToolSpec] = []
    for method_name, declaration in type(instance).tool_declarations():
        method = getattr(instance, method_name)
        specs.append(
            ToolSpec(
                name=declaration.name,
                description=declaration.description,
                input_schema=_input_schema(method),
                handler=_handler(method),
                risk=declaration.risk,
                perm=declaration.perm,
                scenes=declaration.scenes,
                group=declaration.group,
                main_visible=declaration.main_visible,
                sub_visible=declaration.sub_visible,
                system_visible=declaration.system_visible,
                release=declaration.release,
                preserve=declaration.preserve,
            )
        )
    return tuple(specs)


def _handler(method: Callable[..., Any]) -> Callable[[ToolContext, Mapping[str, Any]], Any]:
    async def invoke(
        context: ToolContext,
        arguments: Mapping[str, Any],
    ) -> Any:
        value = method(context, **dict(arguments))
        if inspect.isawaitable(value):
            return await value
        return value

    return invoke


def _input_schema(method: Callable[..., Any]) -> dict[str, Any]:
    hints = get_type_hints(method)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, parameter in inspect.signature(method).parameters.items():
        if name in {"self", "ctx", "context"}:
            continue
        annotation = hints.get(name, str)
        schema = _annotation_schema(annotation)
        default = parameter.default
        if default is not inspect.Parameter.empty:
            schema["default"] = default
        properties[name] = schema
        if default is inspect.Parameter.empty:
            required.append(name)
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _annotation_schema(annotation: Any) -> dict[str, Any]:
    origin = getattr(annotation, "__origin__", None)
    if origin is not None:
        args = getattr(annotation, "__args__", ())
        if origin in {list, tuple, set, frozenset}:
            return {
                "type": "array",
                "items": _annotation_schema(args[0] if args else str),
            }
        if origin is dict:
            return {"type": "object"}
    if annotation is bool:
        return {"type": "boolean"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if annotation is str:
        return {"type": "string"}
    return {"type": "string"}
