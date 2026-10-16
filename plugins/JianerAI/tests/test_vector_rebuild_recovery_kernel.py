from __future__ import annotations

import asyncio
import json
from pathlib import Path

import numpy as np
import pytest

from plugins.JianerAI.memorix.core.runtime import sdk_memory_kernel as kernel_module

_DIMENSION = 8
_FINGERPRINT = {
    "version": 1,
    "hash": "sha256:fake-model",
    "model": "fake-embed",
    "provider": "fake",
    "dimension": _DIMENSION,
    "dimension_request_mode": "explicit",
    "source": "observed",
}


class _FakeEmbedding:
    default_dimension = _DIMENSION

    def __init__(self) -> None:
        self.encode_calls = 0

    def get_requested_dimension(self) -> int:
        return _DIMENSION

    def get_embedding_fingerprint(self, *, dimension: int | None = None) -> dict:
        return dict(_FINGERPRINT, dimension=int(dimension or _DIMENSION))

    async def _detect_dimension(self, *args, **kwargs) -> int:
        return _DIMENSION

    async def encode(self, texts):
        self.encode_calls += 1
        single = isinstance(texts, str)
        count = 1 if single else len(texts)
        vectors = np.ones((count, _DIMENSION), dtype=np.float32)
        return vectors[0] if single else vectors

    async def encode_batch(self, texts, batch_size: int | None = None):
        self.encode_calls += 1
        return np.ones((len(texts), _DIMENSION), dtype=np.float32)


def _config(data_dir: Path) -> dict:
    return {
        "storage": {"data_dir": str(data_dir)},
        "embedding": {
            "dimension": _DIMENSION,
            "batch_size": 2,
            "model_name": "fake-embed",
            "provider": "fake",
        },
        "retrieval": {
            "vector_pools": {"mode": "dual"},
            "relation_vectorization": {"enabled": False},
        },
    }


async def _ingest_one_paragraph(data_dir: Path) -> None:
    kernel = kernel_module.SDKMemoryKernel(plugin_root=data_dir, config=_config(data_dir))
    await kernel.initialize()
    try:
        assert kernel._runtime_capabilities["vector_read"] is True
        await kernel.ingest_text(
            external_id="doc-1",
            source_type="chat",
            text="今天天气不错，我们去公园散步。",
            chat_id="c1",
        )
        store = kernel._paragraph_store()
        assert store is not None and store.num_vectors == 1
    finally:
        await kernel.shutdown()


async def _recover_without_fingerprint(data_dir: Path) -> tuple[dict, dict, int]:
    meta_path = data_dir / "vectors" / "paragraph" / "vectors_metadata.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta.pop("embedding_fingerprint", None)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    kernel = kernel_module.SDKMemoryKernel(plugin_root=data_dir, config=_config(data_dir))
    await kernel.initialize()
    try:
        degraded = dict(kernel._vector_health)
        for _ in range(200):
            if kernel._runtime_capabilities.get("vector_read"):
                break
            await asyncio.sleep(0.05)
        recovered = dict(kernel._vector_health)
        store = kernel._paragraph_store()
        count = int(store.num_vectors) if store is not None else 0
        return degraded, recovered, count
    finally:
        await kernel.shutdown()


def test_missing_fingerprint_is_rebuilt_automatically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("faiss")
    monkeypatch.setattr(
        kernel_module,
        "create_embedding_api_adapter",
        lambda **kwargs: _FakeEmbedding(),
    )
    monkeypatch.setattr(kernel_module, "VECTOR_REBUILD_RECOVERY_POLL_SECONDS", 0.05)
    monkeypatch.setattr(kernel_module, "VECTOR_REBUILD_RECOVERY_INITIAL_DELAY_SECONDS", 0.05)
    monkeypatch.setattr(kernel_module, "VECTOR_REBUILD_RECOVERY_RETRY_SECONDS", 0.1)

    asyncio.run(_ingest_one_paragraph(tmp_path))
    degraded, recovered, vector_count = asyncio.run(_recover_without_fingerprint(tmp_path))

    assert degraded["error_code"] == "v2_fingerprint_missing"
    assert degraded["recovery_stage"] == "rebuild_required"
    assert recovered["state"] == "healthy"
    assert vector_count == 1

    rebuilt_meta = json.loads(
        (tmp_path / "vectors" / "paragraph" / "vectors_metadata.json").read_text(encoding="utf-8")
    )
    assert rebuilt_meta["embedding_fingerprint"]["hash"] == _FINGERPRINT["hash"]
