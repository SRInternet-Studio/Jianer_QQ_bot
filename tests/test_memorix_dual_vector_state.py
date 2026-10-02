import json

import numpy as np

from plugins.JianerAI.memorix.core.runtime.sdk_memory_kernel import SDKMemoryKernel


class _ObservedEmbeddingManager:
    default_dimension = 4

    @staticmethod
    def get_requested_dimension():
        return 4

    @staticmethod
    def get_embedding_fingerprint(*, dimension):
        return {
            "hash": f"test-embedding-{dimension}",
            "source": "observed",
            "dimension": dimension,
        }


def _make_kernel(tmp_path, *, targets=None):
    data_dir = tmp_path / "memory"
    kernel = SDKMemoryKernel(
        plugin_root=tmp_path,
        config={
            "storage": {"data_dir": str(data_dir)},
            "embedding": {"dimension": 4},
            "retrieval": {"vector_pools": {"mode": "dual"}},
        },
    )
    kernel.embedding_manager = _ObservedEmbeddingManager()
    kernel.metadata_store = object()
    target_counts = targets or {"paragraphs": 0, "entities": 0, "relations": 0}
    kernel._count_vector_rebuild_targets = lambda: dict(target_counts)
    kernel.vector_store = kernel._make_vector_store(kernel._vectors_root(), dimension=4)
    kernel.paragraph_vector_store = kernel._make_vector_store(
        kernel._paragraph_vector_dir(), dimension=4
    )
    kernel.graph_vector_store = kernel._make_vector_store(
        kernel._graph_vector_dir(), dimension=4
    )
    return kernel


def test_empty_single_pool_directory_initializes_dual_pools_and_updates_manifest(tmp_path):
    kernel = _make_kernel(tmp_path)

    assert kernel._reload_dual_vector_stores_from_disk()
    assert kernel._dual_vector_pools_enabled()
    manifest = json.loads(kernel._dual_vector_ready_manifest_path().read_text(encoding="utf-8"))
    assert manifest["generation_reason"] == "empty_initialization"
    assert manifest["paragraph_vectors"] == 0
    assert manifest["graph_vectors"] == 0

    kernel.paragraph_vector_store.add(np.ones((1, 4), dtype=np.float32), ["paragraph-1"])
    kernel.graph_vector_store.add(np.ones((1, 4), dtype=np.float32), ["entity-1"])
    kernel._persist(force_vectors=True)

    manifest = json.loads(kernel._dual_vector_ready_manifest_path().read_text(encoding="utf-8"))
    assert manifest["paragraph_vectors"] == 1
    assert manifest["graph_vectors"] == 1
    assert "generation_reason" not in manifest


def test_empty_dual_initialization_is_skipped_when_memory_rows_exist(tmp_path):
    kernel = _make_kernel(tmp_path, targets={"paragraphs": 1, "entities": 0, "relations": 0})

    assert not kernel._reload_dual_vector_stores_from_disk()
    assert not kernel._dual_vector_pools_enabled()
    assert not kernel._dual_vector_ready_manifest_path().exists()


def test_empty_dual_initialization_is_skipped_when_legacy_vectors_exist(tmp_path):
    kernel = _make_kernel(tmp_path)
    kernel.vector_store.add(np.ones((1, 4), dtype=np.float32), ["legacy-vector"])

    assert not kernel._reload_dual_vector_stores_from_disk()
    assert not kernel._dual_vector_pools_enabled()
    assert not kernel._dual_vector_ready_manifest_path().exists()
