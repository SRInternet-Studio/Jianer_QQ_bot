"""Plugin data/cache locations for the RollPigPlus modules."""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_BASE_DIR = Path("data") / "rollpig"


class _LocalStore:
    """Drop-in replacement for the path helpers the ported modules expect."""

    def base_dir(self) -> Path:
        configured = os.getenv("ROLLPIG_PATH", "").strip()
        if configured:
            return Path(configured).expanduser()
        return DEFAULT_BASE_DIR

    @staticmethod
    def _ensure(path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        return path

    def get_plugin_data_dir(self, plugin: str | None = None) -> Path:
        return self._ensure(self.base_dir() / "data")

    def get_plugin_data_file(self, filename: str, plugin: str | None = None) -> Path:
        return self.get_plugin_data_dir(plugin) / filename

    def get_plugin_cache_dir(self, plugin: str | None = None) -> Path:
        return self._ensure(self.base_dir() / "cache")

    def get_plugin_cache_file(self, filename: str, plugin: str | None = None) -> Path:
        return self.get_plugin_cache_dir(plugin) / filename

    def get_plugin_config_dir(self, plugin: str | None = None) -> Path:
        return self._ensure(self.base_dir() / "config")

    def get_plugin_config_file(self, filename: str, plugin: str | None = None) -> Path:
        return self.get_plugin_config_dir(plugin) / filename


localstore = _LocalStore()

__all__ = ["DEFAULT_BASE_DIR", "localstore"]
