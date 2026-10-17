from __future__ import annotations

from ..config import plugin_config
from .base import RollpigStore


def build_store() -> RollpigStore:
    """按配置创建存储后端；导入具体实现时保持延迟，避免本地/云端互相牵连。"""

    backend = (plugin_config.rollpig_storage_backend or "local").strip().lower()
    if backend == "cloud":
        from .cloud import CloudStore

        return CloudStore()

    from ..data_manager import get_data_manager
    from .local_json import LocalJsonStore

    return LocalJsonStore(get_data_manager)


store = build_store()

__all__ = ["build_store", "store"]
