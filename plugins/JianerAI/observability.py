from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote, quote_plus


_REDACTED = "[REDACTED]"
_SENSITIVE_KEY_MARKERS = (
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "passwd",
    "password",
    "private_key",
    "secret",
    "token",
)
_BEARER_PATTERN = re.compile(
    r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]+"
)
_COMMON_TOKEN_PATTERN = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{12,}|ghp_[A-Za-z0-9]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|AIza[A-Za-z0-9_-]{20,})\b"
)


def sanitize_log_data(
    value: Any,
    *,
    sensitive_values: Sequence[str] | set[str] = (),
    tool_name: str = "",
) -> Any:
    secrets = tuple(
        sorted(
            {str(item) for item in sensitive_values if str(item)},
            key=len,
            reverse=True,
        )
    )
    return _sanitize(value, secrets=secrets, tool_name=str(tool_name))


def format_log_data(
    value: Any,
    *,
    sensitive_values: Sequence[str] | set[str] = (),
    tool_name: str = "",
    max_chars: int = 6000,
) -> str:
    sanitized = sanitize_log_data(
        value,
        sensitive_values=sensitive_values,
        tool_name=tool_name,
    )
    try:
        output = json.dumps(
            sanitized,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
    except (TypeError, ValueError):
        output = json.dumps(str(sanitized), ensure_ascii=False)
    limit = max(256, int(max_chars))
    if len(output) <= limit:
        return output
    omitted = len(output) - limit
    return f"{output[:limit]}…<truncated {omitted} chars>"


def safe_log_info(logger: Any, message: str) -> None:
    method = getattr(logger, "info", None)
    if not callable(method):
        return
    try:
        method(str(message))
    except Exception:
        # Logging must never interrupt a conversation or tool execution.
        return


def _sanitize(value: Any, *, secrets: tuple[str, ...], tool_name: str) -> Any:
    if isinstance(value, Mapping):
        source = dict(value)
        if (
            tool_name == "web_browser"
            and str(source.get("action") or "").casefold() == "fill"
            and "value" in source
        ):
            source["value"] = _REDACTED
        output: dict[str, Any] = {}
        for key, item in source.items():
            key_text = str(key)
            if _is_sensitive_key(key_text):
                output[key_text] = _REDACTED
            else:
                output[key_text] = _sanitize(
                    item,
                    secrets=secrets,
                    tool_name=tool_name,
                )
        return output
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return [
            _sanitize(item, secrets=secrets, tool_name=tool_name)
            for item in value
        ]
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    if isinstance(value, str):
        decoded = _decode_json_container(value)
        if decoded is not None:
            return _sanitize(decoded, secrets=secrets, tool_name=tool_name)
        output = value
        for secret in secrets:
            for variant in {secret, quote(secret, safe=""), quote_plus(secret)}:
                if variant:
                    output = output.replace(variant, _REDACTED)
        output = _BEARER_PATTERN.sub(r"\1[REDACTED]", output)
        return _COMMON_TOKEN_PATTERN.sub(_REDACTED, output)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def _decode_json_container(value: str) -> Any | None:
    stripped = value.strip()
    if not stripped or stripped[0] not in "[{":
        return None
    try:
        decoded = json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        return None
    return decoded if isinstance(decoded, (Mapping, list)) else None


def _is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(key).casefold()).strip("_")
    return any(marker in normalized for marker in _SENSITIVE_KEY_MARKERS)
