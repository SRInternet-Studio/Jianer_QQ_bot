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

    def __post_init__(self) -> None:
        prompt = str(self.prompt or "").strip()
        if not prompt:
            raise ProfileError("profile prompt cannot be empty")
        object.__setattr__(self, "prompt", prompt)
        object.__setattr__(self, "inject_master", bool(self.inject_master))


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
        self.reload()
        if not self._profiles:
            self.bootstrap_legacy()

    def reload(self) -> Mapping[str, AgentProfile]:
        with self._lock:
            try:
                with self.path.open("r", encoding="utf-8") as stream:
                    raw = json.load(stream)
            except FileNotFoundError:
                self._profiles = {}
                return {}
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
                    profiles[normalized] = AgentProfile(value)
                elif isinstance(value, Mapping):
                    profiles[normalized] = AgentProfile(
                        str(value.get("prompt") or ""),
                        bool(value.get("inject_master", True)),
                    )
            self._profiles = profiles
            if not self.active_name and profiles:
                self.active_name = next(iter(profiles))
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
                imported[str(name)] = AgentProfile(prompt)
        if imported:
            self._profiles = imported
            if not self.active_name or self.active_name not in imported:
                self.active_name = next(iter(imported))
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
            wanted = str(name or self.active_name or "").strip()
            if wanted and wanted in self._profiles:
                return self._profiles[wanted]
            if self._profiles:
                return next(iter(self._profiles.values()))
        raise ProfileError("no Agent profile is configured")

    def current(self) -> AgentProfile:
        return self.get(self.active_name)

    def switch(self, name: str) -> AgentProfile:
        with self._lock:
            normalized = str(name or "").strip()
            profile = self._profiles.get(normalized)
            if profile is None:
                raise ProfileError(f"unknown profile: {normalized}")
            self.active_name = normalized
            return profile

    def upsert(
        self,
        name: str,
        prompt: str,
        *,
        inject_master: bool = True,
    ) -> AgentProfile:
        normalized = str(name or "").strip()
        if not normalized:
            raise ProfileError("profile name cannot be empty")
        profile = AgentProfile(prompt, inject_master)
        with self._lock:
            self._profiles[normalized] = profile
            if not self.active_name:
                self.active_name = normalized
            self.save()
        return profile

    def remove(self, name: str) -> bool:
        normalized = str(name or "").strip()
        with self._lock:
            if normalized == self.active_name:
                raise ProfileError("cannot delete the active profile")
            removed = self._profiles.pop(normalized, None) is not None
            if removed:
                self.save()
            return removed
