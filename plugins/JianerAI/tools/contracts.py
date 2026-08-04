from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from jianer.adapters import Capabilities, ConversationKey


_TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class ToolRisk(str, Enum):
    READ_ONLY = "read_only"
    PRESENTATION = "presentation"
    MUTATING = "mutating"
    PRIVILEGED = "privileged"


class ToolExecutionError(RuntimeError):
    """A model-safe tool failure with a stable machine-readable code."""

    def __init__(self, code: str, safe_message: str) -> None:
        normalized_code = str(code or "tool_failed").strip()
        normalized_message = str(safe_message or "工具执行失败。").strip()
        super().__init__(normalized_message)
        self.code = normalized_code[:64]
        self.safe_message = normalized_message[:500]


@dataclass(frozen=True, slots=True)
class ToolContext:
    event: Any
    actions: Any
    conversation: ConversationKey
    canonical_user_id: str
    runtime: Mapping[str, Any]
    memory: Any = field(repr=False)
    history: Sequence[Mapping[str, str]] = field(
        default_factory=tuple,
        repr=False,
        compare=False,
    )
    sensitive_values: set[str] = field(
        default_factory=set,
        repr=False,
        compare=False,
    )


ToolHandler = Callable[
    [ToolContext, Mapping[str, Any]],
    Awaitable[Any] | Any,
]
ToolShutdown = Callable[[], Awaitable[Any] | Any]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: Mapping[str, Any]
    handler: ToolHandler = field(repr=False, compare=False)
    risk: ToolRisk = ToolRisk.READ_ONLY
    timeout_seconds: float = 10.0
    max_output_chars: int = 8192
    supported_protocols: frozenset[str] = frozenset()
    required_capabilities: Capabilities = frozenset()
    shutdown: ToolShutdown | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        name = str(self.name or "").strip()
        if _TOOL_NAME_PATTERN.fullmatch(name) is None:
            raise ValueError(
                "tool name must contain only letters, numbers, underscores, or dashes"
            )
        description = str(self.description or "").strip()
        if not description:
            raise ValueError("tool description cannot be empty")
        if not isinstance(self.input_schema, Mapping):
            raise TypeError("tool input_schema must be a mapping")
        if self.input_schema.get("type") != "object":
            raise ValueError("tool input_schema root type must be object")
        if not callable(self.handler):
            raise TypeError("tool handler must be callable")
        if self.shutdown is not None and not callable(self.shutdown):
            raise TypeError("tool shutdown must be callable")
        if self.timeout_seconds <= 0:
            raise ValueError("tool timeout_seconds must be positive")
        if self.max_output_chars < 256:
            raise ValueError("tool max_output_chars must be at least 256")
        protocols = frozenset(
            str(item).strip().casefold()
            for item in self.supported_protocols
            if str(item).strip()
        )
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "input_schema", dict(self.input_schema))
        object.__setattr__(self, "supported_protocols", protocols)
        object.__setattr__(
            self,
            "required_capabilities",
            frozenset(self.required_capabilities),
        )


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: Any


@dataclass(frozen=True, slots=True)
class ToolResult:
    call_id: str
    name: str
    ok: bool
    content: str
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class ToolRegistration:
    token: str
    name: str
