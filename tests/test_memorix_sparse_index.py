import sqlite3

from plugins.JianerAI.memorix.core.retrieval.sparse_bm25 import (
    SparseBM25Config,
    SparseBM25Index,
)
from plugins.JianerAI.memorix.core.storage.metadata_store import MetadataStore


def test_sparse_index_load_backfills_paragraph_ngrams(tmp_path):
    store = MetadataStore(tmp_path / "metadata")
    store.connect()
    index = SparseBM25Index(
        store,
        SparseBM25Config(
            enable_tokenized_shadow_index=False,
            enable_relation_sparse_fallback=False,
        ),
    )
    try:
        paragraph_hash = store.add_paragraph("用于验证中文段落 ngram 启动回填。")

        assert index.ensure_loaded()

        with sqlite3.connect(store.get_db_path()) as conn:
            metadata = dict(
                conn.execute(
                    "SELECT key, value FROM paragraph_ngram_meta"
                ).fetchall()
            )
            indexed_terms = conn.execute(
                "SELECT COUNT(*) FROM paragraph_ngrams WHERE paragraph_hash = ?",
                (paragraph_hash,),
            ).fetchone()[0]

        assert metadata["ngram_n"] == "2"
        assert metadata["paragraph_count"] == "1"
        assert indexed_terms > 0
    finally:
        index.unload()
        store.close()
