"""Local secret-file permission hardening for MaimaiDX state."""

from __future__ import annotations

import csv
import os
import stat
import subprocess
from pathlib import Path


class SecretProtectionError(RuntimeError):
    """Raised when a credential-bearing path cannot be made private."""


def _windows_current_user_sid() -> str:
    completed = subprocess.run(
        ["whoami.exe", "/user", "/fo", "csv", "/nh"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    rows = list(csv.reader(completed.stdout.splitlines()))
    if not rows or len(rows[0]) < 2 or not rows[0][1].startswith("S-1-"):
        raise SecretProtectionError("cannot determine the current Windows user SID")
    return rows[0][1]


def _harden_windows(path: Path, *, directory: bool) -> None:
    sid = _windows_current_user_sid()
    permission = "(OI)(CI)F" if directory else "(F)"
    reset = subprocess.run(
        ["icacls.exe", str(path), "/reset"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )
    if reset.returncode != 0:
        raise SecretProtectionError(
            f"icacls reset failed for {path.name} with exit code {reset.returncode}"
        )
    completed = subprocess.run(
        [
            "icacls.exe",
            str(path),
            "/inheritance:r",
            "/grant:r",
            f"*{sid}:{permission}",
            f"*S-1-5-18:{permission}",
            f"*S-1-5-32-544:{permission}",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )
    if completed.returncode != 0:
        raise SecretProtectionError(
            f"icacls failed for {path.name} with exit code {completed.returncode}"
        )


def harden_private_path(path: str | Path, *, directory: bool | None = None) -> None:
    """Restrict one exact file or directory to the service account.

    This function never recurses and never follows an unresolved glob. Directory
    inheritance is configured so SQLite journals created later remain private.
    """

    resolved = Path(path).expanduser().resolve(strict=False)
    if directory is None:
        directory = resolved.is_dir()
    if directory:
        resolved.mkdir(parents=True, exist_ok=True)
    elif not resolved.exists():
        return

    try:
        if os.name == "nt":
            _harden_windows(resolved, directory=directory)
        else:
            resolved.chmod(stat.S_IRUSR | stat.S_IWUSR | (stat.S_IXUSR if directory else 0))
    except (OSError, subprocess.SubprocessError) as exc:
        raise SecretProtectionError(
            f"failed to protect local secret path {resolved.name}"
        ) from exc
