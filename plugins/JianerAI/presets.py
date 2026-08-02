from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any


DEFAULT_PRESET_ID = "XingYu"
_PRESET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_PLACEHOLDERS = {
    "{self.bot_name}": "bot_name",
    "{self.bot_name_en}": "bot_name_en",
    "{self.event_user}": "event_user",
    "{self.event_user_id}": "event_user_id",
    "{agent_tools}": "agent_tools",
    "{agent_tools_info}": "agent_tools_info",
}


class PresetError(RuntimeError):
    pass


class UnknownPresetError(PresetError):
    pass


@dataclass(frozen=True, slots=True)
class Preset:
    key: str
    name: str
    info: str
    template: str
    path: str
    legacy_user_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_preset_id(self.key)
        object.__setattr__(self, "name", str(self.name or self.key))
        object.__setattr__(self, "info", str(self.info or ""))
        object.__setattr__(self, "template", str(self.template or ""))
        object.__setattr__(self, "path", str(self.path or f"{self.key}.txt"))
        object.__setattr__(
            self,
            "legacy_user_ids",
            tuple(str(value) for value in self.legacy_user_ids),
        )

    @property
    def id(self) -> str:
        return self.key

    def render(
        self,
        *,
        bot_name: str,
        bot_name_en: str,
        event_user: str,
        event_user_id: str,
        agent_tools: str = "无",
        agent_tools_info: str = "无",
    ) -> str:
        values = {
            "bot_name": str(bot_name),
            "bot_name_en": str(bot_name_en),
            "event_user": str(event_user),
            "event_user_id": str(event_user_id),
            "agent_tools": str(agent_tools),
            "agent_tools_info": str(agent_tools_info),
        }
        rendered = self.template
        for placeholder, key in _PLACEHOLDERS.items():
            rendered = rendered.replace(placeholder, values[key])
        return rendered


class PresetStore:
    """Reads and updates the existing ``prerequisites/current.json`` format."""

    def __init__(
        self,
        config_path: str | Path,
        preset_dir: str | Path | None = None,
        *,
        default_key: str = DEFAULT_PRESET_ID,
    ) -> None:
        self.config_path = Path(config_path)
        self.preset_dir = (
            Path(preset_dir)
            if preset_dir is not None
            else self.config_path.parent
        )
        self.default_key = _validate_preset_id(default_key)
        self._lock = RLock()
        self._presets: dict[str, Preset] = {}
        self.reload()

    def reload(self) -> Mapping[str, Preset]:
        with self._lock:
            if not self.config_path.exists():
                self._presets = {}
                return {}
            try:
                with self.config_path.open("r", encoding="utf-8") as stream:
                    raw = json.load(stream)
            except json.JSONDecodeError as exc:
                raise PresetError(
                    f"invalid preset JSON at line {exc.lineno}, column {exc.colno}"
                ) from exc
            except OSError as exc:
                raise PresetError("unable to read preset metadata") from exc
            if not isinstance(raw, dict):
                raise PresetError("preset metadata must contain a JSON object")
            loaded: dict[str, Preset] = {}
            for key, value in raw.items():
                if not isinstance(value, dict):
                    raise PresetError(f"preset {key!r} metadata must be an object")
                normalized_key = _validate_preset_id(key)
                relative_path = str(value.get("path") or f"{normalized_key}.txt")
                template_path = self._resolve_template_path(relative_path)
                try:
                    template = template_path.read_text(encoding="utf-8")
                except OSError as exc:
                    raise PresetError(
                        f"unable to read template for preset {normalized_key!r}"
                    ) from exc
                legacy_users = value.get("uid", [])
                if legacy_users is None:
                    legacy_users = []
                if not isinstance(legacy_users, list):
                    raise PresetError(
                        f"preset {normalized_key!r} uid must be a list"
                    )
                loaded[normalized_key] = Preset(
                    key=normalized_key,
                    name=value.get("name") or normalized_key,
                    info=value.get("info") or "",
                    template=template,
                    path=relative_path,
                    legacy_user_ids=tuple(str(item) for item in legacy_users),
                )
            self._presets = loaded
            return dict(loaded)

    def list_presets(self) -> tuple[Preset, ...]:
        with self._lock:
            return tuple(self._presets.values())

    def list_choices(self) -> Mapping[str, str]:
        with self._lock:
            return {key: preset.name for key, preset in self._presets.items()}

    def get(self, key_or_name: str) -> Preset:
        lookup = str(key_or_name or "").strip()
        with self._lock:
            if lookup in self._presets:
                return self._presets[lookup]
            matches = [
                preset
                for preset in self._presets.values()
                if preset.name == lookup
            ]
        if len(matches) == 1:
            return matches[0]
        raise UnknownPresetError(f"unknown preset: {lookup}")

    def get_default(self) -> Preset:
        return self.get(self.default_key)

    def find_legacy_assignment(self, user_id: Any) -> Preset:
        normalized_user_id = str(user_id)
        candidates = {normalized_user_id}
        if normalized_user_id.startswith("qq:"):
            candidates.add(normalized_user_id.removeprefix("qq:"))
        with self._lock:
            for preset in self._presets.values():
                if candidates.intersection(preset.legacy_user_ids):
                    return preset
        return self.get_default()

    def render(
        self,
        key_or_name: str,
        *,
        bot_name: str,
        bot_name_en: str,
        event_user: str,
        event_user_id: Any,
        agent_tools: str = "无",
        agent_tools_info: str = "无",
    ) -> str:
        return self.get(key_or_name).render(
            bot_name=bot_name,
            bot_name_en=bot_name_en,
            event_user=event_user,
            event_user_id=str(event_user_id),
            agent_tools=agent_tools,
            agent_tools_info=agent_tools_info,
        )

    def upsert(
        self,
        *,
        key: str,
        name: str,
        info: str,
        template: str,
        legacy_user_ids: tuple[str, ...] = (),
    ) -> Preset:
        normalized_key = _validate_preset_id(key)
        with self._lock:
            existing = self._presets.get(normalized_key)
            relative_path = (
                existing.path if existing is not None else f"{normalized_key}.txt"
            )
            target = self._resolve_template_path(relative_path)
            preset = Preset(
                key=normalized_key,
                name=name,
                info=info,
                template=template,
                path=relative_path,
                legacy_user_ids=tuple(str(item) for item in legacy_user_ids),
            )
            self.preset_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(target, preset.template)
            updated = dict(self._presets)
            updated[normalized_key] = preset
            self._write_metadata(updated)
            self._presets = updated
            return preset

    def delete(self, key: str) -> bool:
        normalized_key = _validate_preset_id(key)
        if normalized_key == self.default_key:
            raise PresetError("the default preset cannot be deleted")
        with self._lock:
            preset = self._presets.get(normalized_key)
            if preset is None:
                return False
            updated = dict(self._presets)
            del updated[normalized_key]
            self._write_metadata(updated)
            self._presets = updated
            template_path = self._resolve_template_path(preset.path)
            try:
                template_path.unlink(missing_ok=True)
            except OSError as exc:
                raise PresetError("preset metadata was saved but template removal failed") from exc
            return True

    def _write_metadata(self, presets: Mapping[str, Preset]) -> None:
        raw: dict[str, dict[str, Any]] = {}
        for key, preset in presets.items():
            raw[key] = {
                "name": preset.name,
                "uid": list(preset.legacy_user_ids),
                "info": preset.info,
                "path": preset.path,
            }
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(
            self.config_path,
            json.dumps(raw, indent=4, ensure_ascii=False),
        )

    def _resolve_template_path(self, relative_path: str) -> Path:
        path = Path(relative_path)
        if path.is_absolute() or path.name != str(path):
            raise PresetError("preset template path must be a plain filename")
        root = self.preset_dir.resolve()
        target = (root / path).resolve()
        if target.parent != root:
            raise PresetError("preset template escapes the preset directory")
        return target


def _validate_preset_id(value: Any) -> str:
    normalized = str(value or "").strip()
    if not _PRESET_ID_RE.fullmatch(normalized):
        raise PresetError(f"invalid preset ID: {normalized!r}")
    return normalized


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
