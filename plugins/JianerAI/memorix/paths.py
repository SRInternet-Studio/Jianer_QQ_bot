from __future__ import annotations

from pathlib import Path

A_MEMORIX_SYSTEM_ID = "a_memorix"


def package_root() -> Path:
    return Path(__file__).resolve().parent


def src_root() -> Path:
    """Return the plugin source root for compatibility with upstream helpers."""

    return package_root().parent


def repo_root() -> Path:
    """Find the Jianer repository root instead of an external ``src`` root."""

    current = package_root()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists() or (candidate / "main.py").exists():
            return candidate
    # The normal checkout is ``<repo>/plugins/JianerAI/memorix``.
    return package_root().parents[3]


def config_path() -> Path:
    return repo_root() / "aiconfig" / f"{A_MEMORIX_SYSTEM_ID}.toml"


def default_data_dir() -> Path:
    return repo_root() / "data" / "jianer_ai_memorix"


def artifacts_root() -> Path:
    return default_data_dir() / "artifacts"


def schema_path() -> Path:
    return package_root() / "config_schema.json"


def web_root() -> Path:
    return package_root() / "web"


def scripts_root() -> Path:
    return package_root() / "scripts"


def resolve_repo_path(raw_path: str | Path | None, *, fallback: Path | None = None) -> Path:
    if raw_path is None:
        return (fallback or default_data_dir()).resolve()

    raw_value = str(raw_path).strip()
    if not raw_value:
        return (fallback or default_data_dir()).resolve()

    candidate = Path(raw_value).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()

    return (repo_root() / candidate).resolve()
