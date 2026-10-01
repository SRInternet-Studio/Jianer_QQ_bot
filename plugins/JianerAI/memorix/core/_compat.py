"""Compatibility exports for the vendored Jianer Memory core.

Core modules use a three-dot relative import (``..._compat``), which resolves
to this package.  Keep the provider and embedding shims in the package root,
and re-export them here so every core subsystem sees the same host adapter.
"""

from __future__ import annotations

from .._compat import (
    APIProvider,
    EmbeddingRequest,
    ModelInfo,
    NetworkConnectionError,
    client_registry,
    config_manager,
    get_logger,
)

__all__ = [
    "APIProvider",
    "EmbeddingRequest",
    "ModelInfo",
    "NetworkConnectionError",
    "client_registry",
    "config_manager",
    "get_logger",
]
