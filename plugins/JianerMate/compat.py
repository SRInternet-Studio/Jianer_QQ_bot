"""Compatibility helper: reuse JianerAI modules even when it is disabled.

JianerMate imports JianerAI's provider layer, tool implementations and memory
kernel as *modules*.  The host disable convention renames a plugin folder to
``d_<name>``, which would normally make ``plugins.JianerAI`` unimportable.
``ensure_jianer_ai()`` registers an import alias pointing at the renamed
folder, so operators can switch the plugin off without losing those modules.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import types
from pathlib import Path


DISABLED_FOLDER = "d_JianerAI"
PACKAGE_NAME = "plugins.JianerAI"
_ALIASED = False


def repository_root() -> Path:
    """The plugin lives at ``<root>/plugins/JianerMate``."""

    return Path(__file__).resolve().parents[2]


def ensure_jianer_ai(
    *,
    repository: Path | None = None,
    force: bool = False,
) -> bool:
    """Return True when ``plugins.JianerAI.*`` importable (aliasing if needed)."""

    global _ALIASED
    if _ALIASED and not force:
        return True
    if not force:
        try:
            if importlib.util.find_spec(PACKAGE_NAME) is not None:
                return True
        except (ImportError, ValueError, AttributeError):
            pass
    root = Path(repository) if repository is not None else repository_root()
    candidate = root / "plugins" / DISABLED_FOLDER
    if not candidate.is_dir():
        return False
    try:
        module = types.ModuleType(PACKAGE_NAME)
        module.__path__ = [str(candidate)]  # type: ignore[attr-defined]
        module.__package__ = PACKAGE_NAME
        module.__file__ = str(candidate)
        sys.modules.setdefault(PACKAGE_NAME, module)
    except Exception:  # noqa: BLE001 - alias is best effort
        return False
    _ALIASED = True
    return True


__all__ = ["DISABLED_FOLDER", "ensure_jianer_ai", "repository_root"]
