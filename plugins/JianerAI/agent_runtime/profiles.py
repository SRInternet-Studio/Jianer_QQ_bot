"""Global HypeR-style persona profiles backed by raw persona templates."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import RLock


class ProfileError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AgentProfile:
    prompt: str
    inject_master: bool = True
    display_name: str = ""

    def __post_init__(self) -> None:
        prompt = str(self.prompt or "").strip()
        if not prompt:
            raise ProfileError("profile prompt cannot be empty")
        object.__setattr__(self, "prompt", prompt)
        object.__setattr__(self, "inject_master", bool(self.inject_master))
        object.__setattr__(self, "display_name", str(self.display_name or "").strip())


class ProfileStore:
    """Read/write ``profiles.json`` and bootstrap it from legacy raw templates."""

    def __init__(
        self,
        path: str | Path,
        *,
        legacy_metadata: str | Path | None = None,
        active_name: str = "",
    ) -> None:
        self.path = Path(path)
        self.legacy_metadata = (
            Path(legacy_metadata) if legacy_metadata is not None else None
        )
        self.active_name = str(active_name or "").strip()
        self._lock = RLock()
        self._profiles: dict[str, AgentProfile] = {}
        self._legacy_display_names = self._load_legacy_display_names()
        self.reload()
        if not self._profiles:
            self.bootstrap_legacy()

    def reload(self) -> Mapping[str, AgentProfile]:
        with self._lock:
            self._legacy_display_names = self._load_legacy_display_names()
            try:
                with self.path.open("r", encoding="utf-8") as stream:
                    raw = json.load(stream)
            except FileNotFoundError:
                # A legacy bootstrap may still be held in memory before the
                # first explicit save. Do not discard it on a reload.
                return dict(self._profiles)
            except (OSError, json.JSONDecodeError) as exc:
                raise ProfileError("unable to read profiles.json") from exc
            if not isinstance(raw, Mapping):
                raise ProfileError("profiles.json must contain an object")
            profiles: dict[str, AgentProfile] = {}
            for name, value in raw.items():
                normalized = str(name or "").strip()
                if not normalized:
                    continue
                if isinstance(value, str):
                    try:
                        profiles[normalized] = AgentProfile(
                            value,
                            display_name=self._legacy_display_names.get(
                                normalized,
                                normalized,
                            ),
                        )
                    except ProfileError:
                        continue
                elif isinstance(value, Mapping):
                    display_name = str(value.get("name") or "").strip()
                    if not display_name:
                        display_name = self._legacy_display_names.get(
                            normalized,
                            normalized,
                        )
                    inject_master = value.get("inject_master", True)
                    try:
                        profiles[normalized] = AgentProfile(
                            str(value.get("prompt") or ""),
                            (
                                inject_master
                                if isinstance(inject_master, bool)
                                else True
                            ),
                            display_name,
                        )
                    except ProfileError:
                        continue
            self._profiles = profiles
            self.active_name = self._normalize_active_name_locked(
                self.active_name,
                profiles,
            )
            return dict(profiles)

    def bootstrap_legacy(self) -> Mapping[str, AgentProfile]:
        """Import every raw template referenced by legacy metadata."""

        metadata = self.legacy_metadata
        if metadata is None or not metadata.is_file():
            return {}
        try:
            with metadata.open("r", encoding="utf-8") as stream:
                raw = json.load(stream)
        except (OSError, json.JSONDecodeError) as exc:
            raise ProfileError("unable to read legacy persona metadata") from exc
        if not isinstance(raw, Mapping):
            return {}
        imported: dict[str, AgentProfile] = {}
        for name, value in raw.items():
            if not isinstance(value, Mapping):
                continue
            relative = str(value.get("path") or f"{name}.txt")
            template = metadata.parent / relative
            try:
                prompt = template.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if prompt:
                imported[str(name)] = AgentProfile(
                    prompt,
                    display_name=str(value.get("name") or name),
                )
        if imported:
            self._profiles = imported
            self.active_name = self._normalize_active_name_locked(
                self.active_name,
                imported,
            )
        return dict(imported)

    def save(self) -> None:
        payload = {
            name: {
                "prompt": profile.prompt,
                "inject_master": profile.inject_master,
            }
            for name, profile in self._profiles.items()
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f"{self.path.name}.tmp-{os.getpid()}")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)

    def list(self) -> Mapping[str, AgentProfile]:
        with self._lock:
            return dict(self._profiles)

    def get(self, name: str | None = None) -> AgentProfile:
        with self._lock:
            wanted = str(name if name is not None else self.active_name or "").strip()
            if wanted:
                key = self._resolve_key_locked(wanted)
                return self._profiles[key]
            if self._profiles:
                return self._profiles[next(iter(self._profiles))]
        raise ProfileError("no Agent profile is configured")

    def current(self) -> AgentProfile:
        self.reload()
        return self.get(self.active_name)

    def switch(self, name: str) -> AgentProfile:
        self.reload()
        with self._lock:
            key = self._resolve_key_locked(name)
            self.active_name = key
            return self._profiles[key]

    def resolve_key(self, name: str) -> str:
        with self._lock:
            return self._resolve_key_locked(name)

    def upsert(
        self,
        name: str,
        prompt: str,
        *,
        inject_master: bool = True,
        display_name: str = "",
    ) -> AgentProfile:
        normalized = str(name or "").strip()
        if not normalized:
            raise ProfileError("profile name cannot be empty")
        self.reload()
        with self._lock:
            key = normalized
            if normalized not in self._profiles:
                matches = [
                    candidate
                    for candidate, profile in self._profiles.items()
                    if profile.display_name.casefold() == normalized.casefold()
                ]
                if len(matches) > 1:
                    raise ProfileError(
                        "profile display name is ambiguous: "
                        f"{normalized}; use its key"
                    )
                if matches:
                    key = matches[0]
            existing = self._profiles.get(key)
            resolved_display_name = (
                str(display_name or "").strip()
                or (existing.display_name if existing is not None else "")
                or normalized
            )
            profile = AgentProfile(
                prompt,
                inject_master,
                resolved_display_name,
            )
            self._profiles[key] = profile
            if not self.active_name:
                self.active_name = key
            self.save()
        return profile

    def remove(self, name: str) -> bool:
        self.reload()
        with self._lock:
            normalized = self._resolve_key_locked(name)
            if normalized == self.active_name:
                raise ProfileError("cannot delete the active profile")
            removed = self._profiles.pop(normalized, None) is not None
            if removed:
                self.save()
            return removed

    def display_name(self, key: str) -> str:
        with self._lock:
            profile = self._profiles.get(str(key or "").strip())
            if profile is None:
                return str(key or "")
            return profile.display_name or str(key)

    def label(self, key: str) -> str:
        name = self.display_name(key)
        return name if name == key else f"{name} ({key})"

    def active_label(self) -> str:
        return self.label(self.active_name)

    def labels(self) -> Mapping[str, str]:
        self.reload()
        with self._lock:
            return {
                key: self.label(key)
                for key in self._profiles
            }

    def _resolve_key_locked(self, value: str) -> str:
        wanted = str(value or "").strip()
        if wanted in self._profiles:
            return wanted
        matches = [
            key
            for key, profile in self._profiles.items()
            if profile.display_name.casefold() == wanted.casefold()
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ProfileError(
                f"profile display name is ambiguous: {wanted}; use its key"
            )
        raise ProfileError(f"unknown profile: {wanted}")

    def _load_legacy_display_names(self) -> dict[str, str]:
        metadata = self.legacy_metadata
        if metadata is None or not metadata.is_file():
            return {}
        try:
            with metadata.open("r", encoding="utf-8") as stream:
                raw = json.load(stream)
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, Mapping):
            return {}
        return {
            str(key): str(value.get("name") or key)
            for key, value in raw.items()
            if isinstance(value, Mapping) and str(value.get("name") or "").strip()
        }

    @staticmethod
    def _normalize_active_name_locked(
        active_name: str,
        profiles: Mapping[str, AgentProfile],
    ) -> str:
        wanted = str(active_name or "").strip()
        if not profiles:
            return ""
        if wanted in profiles:
            return wanted
        matches = [
            key
            for key, profile in profiles.items()
            if profile.display_name.casefold() == wanted.casefold()
        ]
        if len(matches) == 1:
            return matches[0]
        return next(iter(profiles))
