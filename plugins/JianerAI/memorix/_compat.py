"""Small host compatibility layer used by the vendored Jianer Memory core.

The upstream project runs inside HostRuntime and imports ``src.common.logger``.
JianerAI has its own runtime, so the memory package keeps this dependency
local and intentionally exposes only the stable pieces that the storage and
retrieval layers need.  Optional HostRuntime integrations remain lazy in their
call sites.
"""

from __future__ import annotations

import logging
import json
import os
import urllib.request
from dataclasses import dataclass, field
from typing import Any


def get_logger(name: str | None = None, *args: Any, **kwargs: Any) -> logging.Logger:
    """Return a standard-library logger with the upstream call signature."""

    del args, kwargs
    return logging.getLogger(str(name or "jianer_ai.memorix"))


@dataclass(frozen=True)
class APIProvider:
    """Minimal provider descriptor used when HostRuntime is not installed."""

    name: str = ""
    client_type: str = "openai"
    base_url: str = ""
    api_key: str = ""


@dataclass(frozen=True)
class ModelInfo:
    """Minimal embedding model descriptor for the local host boundary."""

    name: str = ""
    api_provider: str = ""
    extra_params: dict[str, Any] = field(default_factory=dict)
    model: str = ""


@dataclass(frozen=True)
class EmbeddingRequest:
    model_info: Any
    embedding_input: str
    extra_params: dict[str, Any] = field(default_factory=dict)


class NetworkConnectionError(ConnectionError):
    """Compatible exception name for retry handling."""


class _FallbackEmbeddingTask:
    model_list: list[str] = []


class _FallbackTasks:
    embedding = _FallbackEmbeddingTask()


class _FallbackModelConfig:
    models: list[ModelInfo] = []
    api_providers: list[APIProvider] = []
    model_task_config = _FallbackTasks()


class _EmbeddingResponse:
    def __init__(self, embedding: list[float]):
        self.embedding = embedding


class _JianerEmbeddingClient:
    def __init__(self, provider: APIProvider):
        self.provider = provider

    async def get_embedding(self, request: EmbeddingRequest) -> _EmbeddingResponse:
        model_info = request.model_info
        provider = self.provider
        model = str(getattr(model_info, "model", "") or getattr(model_info, "name", ""))
        params = dict(getattr(request, "extra_params", {}) or {})
        if str(provider.client_type).lower() in {"gemini", "google"}:
            url = f"{provider.base_url.rstrip('/')}/v1beta/models/{model}:embedContent"
            payload = {"model": f"models/{model}", "content": {"parts": [{"text": request.embedding_input}]}}
            if "output_dimensionality" in params:
                payload["outputDimensionality"] = int(params["output_dimensionality"])
            headers = {"Content-Type": "application/json", "x-goog-api-key": provider.api_key}
            response_key = ("embedding", "values")
        else:
            url = f"{provider.base_url.rstrip('/')}/embeddings"
            payload = {"model": model, "input": request.embedding_input}
            payload.update(params)
            headers = {"Content-Type": "application/json", "Authorization": f"Bearer {provider.api_key}"}
            response_key = ()
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=60) as response:
            data = json.loads(response.read().decode("utf-8"))
        if response_key:
            values: Any = data
            for key in response_key:
                values = values.get(key, {}) if isinstance(values, dict) else {}
            values = values if isinstance(values, list) else []
        else:
            values = ((data.get("data") or [{}])[0]).get("embedding", []) if isinstance(data, dict) else []
        return _EmbeddingResponse([float(item) for item in values])


class _FallbackConfigManager:
    def get_model_config(self) -> _FallbackModelConfig:
        paths = [
            os.environ.get("JIANER_AI_EMBEDDING_CONFIG", ""),
            os.path.join(os.getcwd(), "aiconfig", "embedding.json"),
            os.path.join(os.getcwd(), "aiconfig", "embedding.json.example"),
        ]
        raw: dict[str, Any] = {}
        for path in paths:
            if not path:
                continue
            try:
                with open(path, encoding="utf-8") as stream:
                    loaded = json.load(stream)
                if isinstance(loaded, dict):
                    raw = loaded
                    break
            except (OSError, json.JSONDecodeError):
                continue
        provider_name = str(raw.get("Provider", raw.get("provider", "openai"))).strip() or "openai"
        provider = APIProvider(
            name=provider_name,
            client_type=provider_name,
            base_url=str(raw.get("BaseUrl", raw.get("base_url", "https://api.openai.com/v1"))).strip().rstrip("/"),
            api_key=str(raw.get("ApiKey", raw.get("api_key", ""))).strip(),
        )
        model_name = str(raw.get("Model", raw.get("model", ""))).strip()
        dimension = raw.get("Dimension", raw.get("dimension"))
        params: dict[str, Any] = {}
        if dimension:
            params["dimensions"] = int(dimension)
        model = ModelInfo(
            name=model_name,
            api_provider=provider_name,
            extra_params=params,
            model=model_name,
        )
        config = _FallbackModelConfig()
        config.models = [model] if model_name else []
        config.api_providers = [provider]
        config.model_task_config.embedding.model_list = [model_name] if model_name else []
        return config


class _FallbackClientRegistry:
    def get_client_class_instance(self, provider: Any) -> Any:
        if not getattr(provider, "api_key", ""):
            raise RuntimeError("JianerAI embedding API key is not configured")
        return _JianerEmbeddingClient(provider)


config_manager = _FallbackConfigManager()
client_registry = _FallbackClientRegistry()


__all__ = [
    "APIProvider",
    "EmbeddingRequest",
    "ModelInfo",
    "NetworkConnectionError",
    "client_registry",
    "config_manager",
    "get_logger",
]
