"""Jianer Memory core exports.

Storage is safe to import during plugin discovery. Embedding, retrieval and
runtime services are intentionally lazy because their providers are configured
by the JianerAI host and may not be installed in a command-only process.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from .storage import (
    GraphStore,
    ImportStrategy,
    KnowledgeType,
    MetadataStore,
    VectorStore,
    detect_knowledge_type,
    get_type_display_name,
    parse_import_strategy,
    resolve_stored_knowledge_type,
    select_import_strategy,
    should_extract_relations,
)

_LAZY_EXPORTS = {
    "EmbeddingAPIAdapter": (".embedding", "EmbeddingAPIAdapter"),
    "create_embedding_api_adapter": (".embedding", "create_embedding_api_adapter"),
    "DualPathRetriever": (".retrieval", "DualPathRetriever"),
    "RetrievalStrategy": (".retrieval", "RetrievalStrategy"),
    "RetrievalResult": (".retrieval", "RetrievalResult"),
    "RetrievalScope": (".retrieval", "RetrievalScope"),
    "DualPathRetrieverConfig": (".retrieval", "DualPathRetrieverConfig"),
    "TemporalQueryOptions": (".retrieval", "TemporalQueryOptions"),
    "FusionConfig": (".retrieval", "FusionConfig"),
    "GraphRelationRecallConfig": (".retrieval", "GraphRelationRecallConfig"),
    "RelationIntentConfig": (".retrieval", "RelationIntentConfig"),
    "PersonalizedPageRank": (".retrieval", "PersonalizedPageRank"),
    "PageRankConfig": (".retrieval", "PageRankConfig"),
    "create_ppr_from_graph": (".retrieval", "create_ppr_from_graph"),
    "DynamicThresholdFilter": (".retrieval", "DynamicThresholdFilter"),
    "ThresholdMethod": (".retrieval", "ThresholdMethod"),
    "ThresholdConfig": (".retrieval", "ThresholdConfig"),
    "SparseBM25Index": (".retrieval", "SparseBM25Index"),
    "SparseBM25Config": (".retrieval", "SparseBM25Config"),
    "RelationWriteService": (".utils", "RelationWriteService"),
    "RelationWriteResult": (".utils", "RelationWriteResult"),
}

__all__ = [
    "VectorStore",
    "GraphStore",
    "MetadataStore",
    "ImportStrategy",
    "KnowledgeType",
    "parse_import_strategy",
    "resolve_stored_knowledge_type",
    "detect_knowledge_type",
    "select_import_strategy",
    "should_extract_relations",
    "get_type_display_name",
    *_LAZY_EXPORTS,
]


def __getattr__(name: str) -> Any:
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute_name = target
    value = getattr(import_module(module_name, __name__), attribute_name)
    globals()[name] = value
    return value
