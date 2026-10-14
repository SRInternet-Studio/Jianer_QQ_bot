"""At-rest protection for LXNS OAuth tokens."""

from __future__ import annotations

import base64
import ctypes
import os
from ctypes import wintypes


DPAPI_PREFIX = "dpapi:v1:"
PLAIN_PREFIX = "plain:v1:"
_ENTROPY = b"jianerbot-plugin-maimaidx/oauth-token/v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x01


class TokenProtectionError(RuntimeError):
    """Raised when an encrypted token cannot be protected or recovered."""


if os.name == "nt":
    class _DataBlob(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
        ]


    def _blob(value: bytes) -> tuple[_DataBlob, ctypes.Array]:
        buffer = ctypes.create_string_buffer(value)
        return (
            _DataBlob(
                len(value),
                ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
            ),
            buffer,
        )


    def _dpapi(value: bytes, *, decrypt: bool) -> bytes:
        source, source_buffer = _blob(value)
        entropy, entropy_buffer = _blob(_ENTROPY)
        output = _DataBlob()
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
        if decrypt:
            ok = function(
                ctypes.byref(source),
                None,
                ctypes.byref(entropy),
                None,
                None,
                _CRYPTPROTECT_UI_FORBIDDEN,
                ctypes.byref(output),
            )
        else:
            ok = function(
                ctypes.byref(source),
                None,
                ctypes.byref(entropy),
                None,
                None,
                _CRYPTPROTECT_UI_FORBIDDEN,
                ctypes.byref(output),
            )
        # Keep input buffers alive until the native call has completed.
        _ = source_buffer, entropy_buffer
        if not ok:
            raise TokenProtectionError("Windows DPAPI operation failed")
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            kernel32.LocalFree(output.pbData)


def protect_token(value: str | None) -> str | None:
    if value is None:
        return None
    if value.startswith((DPAPI_PREFIX, PLAIN_PREFIX)):
        return value
    raw = value.encode("utf-8")
    if os.name == "nt":
        protected = _dpapi(raw, decrypt=False)
        return DPAPI_PREFIX + base64.urlsafe_b64encode(protected).decode("ascii")
    return PLAIN_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii")


def unprotect_token(value: str | None) -> str | None:
    if value is None:
        return None
    if value.startswith(DPAPI_PREFIX):
        if os.name != "nt":
            raise TokenProtectionError("DPAPI token cannot be decrypted on this platform")
        encoded = value[len(DPAPI_PREFIX) :]
        try:
            protected = base64.urlsafe_b64decode(encoded.encode("ascii"))
        except (ValueError, UnicodeError) as exc:
            raise TokenProtectionError("invalid DPAPI token encoding") from exc
        return _dpapi(protected, decrypt=True).decode("utf-8")
    if value.startswith(PLAIN_PREFIX):
        encoded = value[len(PLAIN_PREFIX) :]
        try:
            return base64.urlsafe_b64decode(encoded.encode("ascii")).decode("utf-8")
        except (ValueError, UnicodeError) as exc:
            raise TokenProtectionError("invalid token encoding") from exc
    return value


def token_needs_migration(value: str | None) -> bool:
    if value is None:
        return False
    expected = DPAPI_PREFIX if os.name == "nt" else PLAIN_PREFIX
    return not value.startswith(expected)
