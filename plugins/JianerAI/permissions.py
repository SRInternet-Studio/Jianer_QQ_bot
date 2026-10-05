"""Actor privileges for model-callable platform capabilities.

A single agent turn runs on behalf of one user.  Whether that user may make the
bot call a mutating platform API depends on two independent facts:

* whether the user is a bot administrator (``root_users`` / ``super_users`` /
  ``manage_users`` / ``admins`` from the runtime snapshot), and
* whether the user is an owner or administrator of the group the turn happens
  in (``event.sender.role`` when available, otherwise an adapter lookup).

The resolved :class:`Actor` is attached to the tool context so that the tool
registry and individual handlers enforce the same policy.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

GROUP_ADMIN_ROLES = frozenset({"owner", "admin"})
_BOT_ADMIN_KEYS = ("root_users", "super_users", "manage_users", "admins")


@dataclass(frozen=True, slots=True)
class Actor:
    user_id: str = ""
    is_bot_admin: bool = False
    is_group_owner: bool = False
    is_group_admin: bool = False
    role: str = ""

    @property
    def is_group_manager(self) -> bool:
        return bool(self.is_group_owner or self.is_group_admin)

    @property
    def is_privileged(self) -> bool:
        """Group owner/admin or bot administrator."""

        return bool(self.is_bot_admin or self.is_group_manager)


def bot_admin_ids(runtime: Mapping[str, Any] | None) -> frozenset[str]:
    if not isinstance(runtime, Mapping):
        return frozenset()
    output: set[str] = set()
    for key in _BOT_ADMIN_KEYS:
        for item in runtime.get(key) or ():
            value = str(item or "").strip()
            if value:
                output.add(value)
    return frozenset(output)


def _normalize_role(value: Any) -> str:
    return str(value or "").strip().casefold()


def _role_from_sender(event: Any) -> str:
    sender = getattr(event, "sender", None)
    if sender is None:
        return ""
    if isinstance(sender, Mapping):
        raw = sender.get("role")
    else:
        raw = getattr(sender, "role", None)
    return _normalize_role(raw)


def _role_from_response(response: Any) -> str:
    for candidate in (getattr(response, "data", None), response):
        if candidate is None:
            continue
        if isinstance(candidate, Mapping):
            raw = candidate.get("role")
        else:
            raw = getattr(candidate, "role", None)
        role = _normalize_role(raw)
        if role:
            return role
    return ""


class ActorResolver:
    """Resolves actors, caching group member roles for a short window."""

    def __init__(
        self,
        *,
        cache_seconds: float = 60.0,
        clock: Any = time.monotonic,
    ) -> None:
        self._cache_seconds = max(0.0, float(cache_seconds))
        self._clock = clock
        self._roles: dict[tuple[str, str], tuple[float, str]] = {}

    def clear(self) -> None:
        self._roles.clear()

    async def resolve(
        self,
        event: Any,
        actions: Any,
        runtime: Mapping[str, Any] | None,
    ) -> Actor:
        user_id = str(getattr(event, "user_id", "") or "")
        group_id = getattr(event, "group_id", None)
        role = _role_from_sender(event)
        if not role and group_id is not None and user_id:
            role = await self._group_role(actions, str(group_id), user_id)
        return Actor(
            user_id=user_id,
            is_bot_admin=user_id in bot_admin_ids(runtime),
            is_group_owner=role == "owner",
            is_group_admin=role in GROUP_ADMIN_ROLES,
            role=role,
        )

    async def _group_role(
        self,
        actions: Any,
        group_id: str,
        user_id: str,
    ) -> str:
        key = (group_id, user_id)
        now = self._clock()
        cached = self._roles.get(key)
        if cached is not None and cached[0] > now:
            return cached[1]
        getter = getattr(actions, "get_group_member_info", None)
        if not callable(getter):
            return ""
        try:
            response = await getter(group_id=group_id, user_id=user_id)
        except Exception:
            return ""
        role = _role_from_response(response)
        if role:
            self._roles[key] = (now + self._cache_seconds, role)
        return role


__all__ = [
    "Actor",
    "ActorResolver",
    "GROUP_ADMIN_ROLES",
    "bot_admin_ids",
]
