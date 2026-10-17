"""JianerCore port of ``nonebot-plugin-rollpig-plus`` ("今天是什么小猪 Plus").

The plugin is written directly against the JianerCore plugin API: Alconna
``Command`` registrations in ``rollpig/handlers/``, and ``host/`` bindings for
logging, data paths, configuration, the OneBot action facade and the job
scheduler.  Upstream business logic (store, renderers, flows, texts) is reused
under ``rollpig/``.
"""

from __future__ import annotations

from pathlib import Path

UPSTREAM_REPOSITORY = "https://github.com/Felis2026/nonebot-plugin-rollpig-plus"
UPSTREAM_VERSION = "0.14.4"
UPSTREAM_COMMIT = "bd8681f62bc3d24153af52b0090bde3e35c5a791"
__version__ = UPSTREAM_VERSION

PACKAGE_DIR = Path(__file__).resolve().parent

__all__ = [
    "PACKAGE_DIR",
    "UPSTREAM_COMMIT",
    "UPSTREAM_REPOSITORY",
    "UPSTREAM_VERSION",
    "__version__",
]
