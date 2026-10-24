"""Shared, protocol-neutral models for the JianerAI Agent runtime."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

SceneType = Literal["group", "private", "system"]
RoleName = Literal["main", "sub", "system"]
PermissionGroup = Literal["member", "whitelist", "any_admin", "bot_owner"]

PERMISSION_LEVEL: dict[PermissionGroup, int] = {
    "member": 0,
    "whitelist": 1,
    "any_admin": 2,
    "bot_owner": 3,
}


class AgentRole(str, Enum):
    MAIN = "main"
    SUB = "sub"
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class SessionKey:
    """Stable context identity across protocols and Bot accounts."""

    scene_type: SceneType
    scene_id: str
    protocol: str = "unknown"
    self_id: str = "unknown"

    def __post_init__(self) -> None:
        scene_type = str(self.scene_type or "").strip().casefold()
        if scene_type not in {"group", "private", "system"}:
            raise ValueError("scene_type must be group, private, or system")
        object.__setattr__(self, "scene_type", scene_type)
        object.__setattr__(self, "scene_id", _required_id(self.scene_id, "scene_id"))
        object.__setattr__(
            self,
            "protocol",
            _optional_id(self.protocol, "unknown"),
        )
        object.__setattr__(self, "self_id", _optional_id(self.self_id, "unknown"))

    @property
    def value(self) -> str:
        return (
            f"{self.protocol}:{self.self_id}:"
            f"{self.scene_type}:{self.scene_id}"
        )

    @property
    def lock_id(self) -> str:
        return self.value

    @property
    def directory_name(self) -> str:
        slug = re.sub(
            r"[^A-Za-z0-9_.-]+",
            "-",
            self.value,
        ).strip("-")[:72]
        digest = hashlib.sha256(self.value.encode("utf-8")).hexdigest()[:16]
        return f"{slug or self.scene_type}-{digest}"

    @classmethod
    def system(cls) -> "SessionKey":
        return cls(scene_type="system", scene_id="0")


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """A serializable event delivered to an Agent context."""

    type: str
    scene_type: SceneType
    scene_id: str
    source: str = ""
    payload: Any = None
    protocol: str = ""
    self_id: str = ""
    timestamp: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "scene_type": self.scene_type,
            "scene_id": str(self.scene_id),
            "source": self.source,
            "payload": self.payload,
            "protocol": self.protocol,
            "self_id": str(self.self_id),
            "timestamp": int(self.timestamp),
        }


@dataclass(frozen=True, slots=True)
class SystemRequest:
    request_id: str
    operation: str
    source: SessionKey
    payload: dict[str, Any] = field(default_factory=dict)
    principal_id: str = ""
    self_id: str = ""
    reply_message_id: str = ""


def permission_level(value: str) -> int:
    return PERMISSION_LEVEL.get(str(value or "member"), 0)  # type: ignore[arg-type]


def _required_id(value: Any, field_name: str) -> str:
    text = str(value if value is not None else "").strip()
    if not text:
        raise ValueError(f"{field_name} must be a non-empty identifier")
    return text


def _optional_id(value: Any, fallback: str) -> str:
    text = str(value if value is not None else "").strip()
    return text or fallback
