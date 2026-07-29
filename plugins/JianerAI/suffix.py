from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from threading import RLock
from typing import Any


_PUNCTUATION = frozenset("，。；？！,.;?!")


class SuffixConfigError(RuntimeError):
    pass


class SuffixStore:
    """Persistent legacy suffix settings.

    The only transformation method is named :meth:`apply_ai_reply` so host
    command/help/system messages are never modified implicitly.
    """

    def __init__(self, config_path: str | Path) -> None:
        self.config_path = Path(config_path)
        self._lock = RLock()
        self._global_suffix = ""
        self._identity_suffixes: dict[str, str] = {}
        self.reload()

    def reload(self) -> None:
        with self._lock:
            if not self.config_path.exists():
                return
            try:
                with self.config_path.open("r", encoding="utf-8") as stream:
                    raw = json.load(stream)
            except json.JSONDecodeError as exc:
                raise SuffixConfigError(
                    f"invalid suffix JSON at line {exc.lineno}, column {exc.colno}"
                ) from exc
            except OSError as exc:
                raise SuffixConfigError("unable to read suffix settings") from exc
            if not isinstance(raw, dict):
                raise SuffixConfigError("suffix settings must be a JSON object")
            per_identity = raw.get("user_suffixes") or {}
            if not isinstance(per_identity, dict):
                raise SuffixConfigError("user_suffixes must be a JSON object")
            self._global_suffix = str(raw.get("global_suffix") or "")
            normalized_suffixes: dict[str, str] = {}
            for identity, suffix in per_identity.items():
                key = _normalize_identity_key(identity)
                if key not in normalized_suffixes or str(identity).startswith(
                    "qq:"
                ):
                    normalized_suffixes[key] = str(suffix)
            self._identity_suffixes = normalized_suffixes

    def get(self, identity: Any) -> str:
        normalized_identity = _normalize_identity_key(identity)
        with self._lock:
            return self._identity_suffixes.get(
                normalized_identity, self._global_suffix
            )

    def set_global(self, suffix: str) -> None:
        with self._lock:
            self._global_suffix = str(suffix or "")
            self._save()

    def clear_global(self) -> None:
        self.set_global("")

    def set_for_identity(self, identity: Any, suffix: str) -> None:
        normalized_identity = _normalize_identity_key(identity)
        with self._lock:
            self._identity_suffixes[normalized_identity] = str(suffix or "")
            self._save()

    def clear_for_identity(self, identity: Any) -> bool:
        normalized_identity = _normalize_identity_key(identity)
        with self._lock:
            if normalized_identity not in self._identity_suffixes:
                return False
            del self._identity_suffixes[normalized_identity]
            self._save()
            return True

    # Compatibility names used by the previous command layer.
    set_global_suffix = set_global
    remove_global_suffix = clear_global
    set_user_suffix = set_for_identity
    remove_user_suffix = clear_for_identity
    get_suffix = get

    def apply_ai_reply(self, text: str, identity: Any) -> str:
        """Apply the configured suffix to one completed AI reply only."""

        value = str(text or "")
        suffix = self.get(identity)
        if not value or not suffix:
            return value
        if _already_suffixed(value, suffix):
            return value
        output: list[str] = []
        for index, char in enumerate(value):
            if char in _PUNCTUATION:
                prefix = "".join(output)
                if not prefix.endswith(suffix):
                    output.append(suffix)
                output.append(char)
            else:
                output.append(char)
        processed = "".join(output)
        if value[-1] not in _PUNCTUATION and not processed.endswith(suffix):
            processed += suffix
        return processed

    def _save(self) -> None:
        raw = {
            "global_suffix": self._global_suffix,
            "user_suffixes": dict(self._identity_suffixes),
        }
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(raw, indent=4, ensure_ascii=False)
        handle, temporary_name = tempfile.mkstemp(
            prefix=f".{self.config_path.name}.",
            suffix=".tmp",
            dir=str(self.config_path.parent),
            text=True,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.config_path)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise


def _already_suffixed(text: str, suffix: str) -> bool:
    if text.endswith(suffix):
        return True
    for punctuation in _PUNCTUATION:
        if text.endswith(suffix + punctuation):
            return True
    return False


def _normalize_identity_key(identity: Any) -> str:
    value = str(identity)
    if value.isdigit():
        return f"qq:{value}"
    return value
