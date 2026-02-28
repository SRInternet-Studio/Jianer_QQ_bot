import asyncio
import glob
import json
import os
import re
import threading
from typing import Any, Dict, List, Optional, Tuple

import jieba
import networkx as nx
import numpy as np

from Hyper import Configurator

Configurator.cm = Configurator.ConfigManager(Configurator.Config(file="config.json").load_from_file())

TRIGGHT_KEYWORD = "Any"

KNOWLEDGE_DIR = os.path.normpath("data/knowledge")
CHUNK_SIZE = 1400
CHUNK_OVERLAP = 180
MANIFEST_NAME = "manifest.json"
INDEX_CHUNKS_NAME = "chunks.json"
INDEX_EMBEDDINGS_NAME = "embeddings.npy"
INDEX_GRAPH_NAME = "graph.json"

_GREETINGS = {
    "hi",
    "hello",
    "hey",
    "你好",
    "您好",
    "在吗",
    "在不在",
    "哈喽",
    "嗨",
    "早",
    "早上好",
    "中午好",
    "下午好",
    "晚上好",
    "晚安",
    "测试",
    "test",
}

_LOCK = threading.RLock()
_STATE: Dict[str, Any] = {
    "cfg_key": None,
    "building": False,
    "last_error": None,
    "last_manifest": None,
    "index": None,
}


def _coerce_str(value: Any, default: Optional[str]) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _load_hipporag_config() -> Dict[str, Any]:
    others = getattr(Configurator.cm.get_cfg(), "others", {}) or {}
    hipporag = others.get("hipporag") or others.get("HippoRAG") or {}
    if not isinstance(hipporag, dict):
        hipporag = {}

    llm = hipporag.get("llm") or {}
    if not isinstance(llm, dict):
        llm = {}

    embedding = hipporag.get("embedding") or {}
    if not isinstance(embedding, dict):
        embedding = {}

    save_dir = _coerce_str(hipporag.get("save_dir"), "./data/hipporag")
    top_k = _coerce_int(hipporag.get("top_k"), 3)
    if top_k < 1:
        top_k = 1

    seed_k = _coerce_int(hipporag.get("seed_k"), 8)
    if seed_k < 1:
        seed_k = 1
    neighbor_cap = _coerce_int(hipporag.get("neighbor_cap"), 25)
    if neighbor_cap < 1:
        neighbor_cap = 1
    edge_cap_per_node = _coerce_int(hipporag.get("edge_cap_per_node"), 20)
    if edge_cap_per_node < 1:
        edge_cap_per_node = 1

    llm_base_url = _coerce_str(llm.get("base_url"), None)
    emb_base_url = _coerce_str(embedding.get("base_url"), None) or llm_base_url
    llm_api_key = _coerce_str(llm.get("api_key"), None)
    embedding_api_key = _coerce_str(embedding.get("api_key"), None)

    return {
        "save_dir": os.path.normpath(save_dir or "./data/hipporag"),
        "top_k": top_k,
        "seed_k": seed_k,
        "neighbor_cap": neighbor_cap,
        "edge_cap_per_node": edge_cap_per_node,
        "llm": {
            "model": _coerce_str(llm.get("model"), None),
            "base_url": llm_base_url,
            "api_key": llm_api_key,
        },
        "embedding": {
            "base_url": emb_base_url,
            "model": _coerce_str(embedding.get("model"), None),
            "api_key": embedding_api_key,
        },
    }


def _cfg_key(cfg: Dict[str, Any]) -> str:
    return json.dumps(
        {
            "save_dir": cfg.get("save_dir"),
            "top_k": cfg.get("top_k"),
            "seed_k": cfg.get("seed_k"),
            "neighbor_cap": cfg.get("neighbor_cap"),
            "edge_cap_per_node": cfg.get("edge_cap_per_node"),
            "llm": cfg.get("llm"),
            "embedding": cfg.get("embedding"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _is_cfg_ready(cfg: Dict[str, Any]) -> bool:
    emb_model = ((cfg.get("embedding") or {}).get("model") or "").strip()
    emb_base_url = ((cfg.get("embedding") or {}).get("base_url") or "").strip()
    return bool(emb_model and emb_base_url and (cfg.get("save_dir") or "").strip())


def _list_knowledge_files() -> List[str]:
    pattern = os.path.join(KNOWLEDGE_DIR, "*.md")
    return sorted(glob.glob(pattern))

def _file_state(path: str) -> Dict[str, Any]:
    st = os.stat(path)
    return {"mtime_ns": int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))), "size": int(st.st_size)}


def _manifest_path(save_dir: str) -> str:
    return os.path.join(save_dir, MANIFEST_NAME)

def _index_chunks_path(save_dir: str) -> str:
    return os.path.join(save_dir, INDEX_CHUNKS_NAME)

def _index_embeddings_path(save_dir: str) -> str:
    return os.path.join(save_dir, INDEX_EMBEDDINGS_NAME)

def _index_graph_path(save_dir: str) -> str:
    return os.path.join(save_dir, INDEX_GRAPH_NAME)


def _read_manifest(save_dir: str) -> Optional[Dict[str, Any]]:
    path = _manifest_path(save_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _write_manifest(save_dir: str, manifest: Dict[str, Any]) -> None:
    os.makedirs(save_dir, exist_ok=True)
    path = _manifest_path(save_dir)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2, sort_keys=True)


def _normalize_text(text: str) -> str:
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _chunk_markdown(text: str, chunk_size: int, overlap: int) -> List[str]:
    t = _normalize_text(text)
    if not t:
        return []

    parts = [p.strip() for p in re.split(r"\n{2,}", t) if p.strip()]
    chunks: List[str] = []
    buf = ""

    def flush():
        nonlocal buf
        if buf.strip():
            chunks.append(buf.strip())
        buf = ""

    for part in parts:
        candidate = part if not buf else f"{buf}\n\n{part}"
        if len(candidate) <= chunk_size:
            buf = candidate
            continue

        flush()

        if len(part) <= chunk_size:
            buf = part
            continue

        start = 0
        step = max(chunk_size - max(overlap, 0), 1)
        while start < len(part):
            end = min(start + chunk_size, len(part))
            seg = part[start:end].strip()
            if seg:
                chunks.append(seg)
            if end >= len(part):
                break
            start += step

    flush()
    return chunks


def _build_docs(files: List[str]) -> List[Dict[str, Any]]:
    docs: List[Dict[str, Any]] = []
    for path in files:
        file_name = os.path.basename(path)
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            continue

        chunks = _chunk_markdown(content, CHUNK_SIZE, CHUNK_OVERLAP)
        for idx, chunk in enumerate(chunks):
            docs.append({"file": file_name, "chunk": idx, "text": chunk})
    return docs


def _current_manifest(cfg: Dict[str, Any], files: List[str]) -> Dict[str, Any]:
    file_map: Dict[str, Any] = {}
    for path in files:
        file_map[os.path.basename(path)] = _file_state(path)

    return {
        "version": 2,
        "knowledge_dir": KNOWLEDGE_DIR,
        "chunk": {"size": CHUNK_SIZE, "overlap": CHUNK_OVERLAP},
        "hipporag": {
            "save_dir": cfg.get("save_dir"),
            "top_k": cfg.get("top_k"),
            "seed_k": cfg.get("seed_k"),
            "neighbor_cap": cfg.get("neighbor_cap"),
            "edge_cap_per_node": cfg.get("edge_cap_per_node"),
            "llm": cfg.get("llm"),
            "embedding": cfg.get("embedding"),
        },
        "files": file_map,
    }


def _manifest_equal(a: Optional[Dict[str, Any]], b: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    try:
        return json.dumps(a, ensure_ascii=False, sort_keys=True) == json.dumps(b, ensure_ascii=False, sort_keys=True)
    except Exception:
        return False


def ensure_index_ready(force: bool = False) -> bool:
    cfg = _load_hipporag_config()
    if not _is_cfg_ready(cfg):
        with _LOCK:
            _STATE["last_error"] = "config_not_ready"
        return False

    if not os.path.exists(KNOWLEDGE_DIR):
        os.makedirs(KNOWLEDGE_DIR, exist_ok=True)
        return False

    files = _list_knowledge_files()
    if not files:
        with _LOCK:
            _STATE["last_error"] = "no_knowledge_files"
        return False

    save_dir = cfg["save_dir"]
    new_manifest = _current_manifest(cfg, files)
    old_manifest = _read_manifest(save_dir)
    if not force and _manifest_equal(old_manifest, new_manifest):
        with _LOCK:
            _STATE["last_manifest"] = new_manifest
            if _STATE.get("index") is not None and _STATE.get("cfg_key") == _cfg_key(cfg):
                return True
        idx = _load_index(save_dir)
        if idx is None:
            return False
        with _LOCK:
            _STATE["cfg_key"] = _cfg_key(cfg)
            _STATE["index"] = idx
        return True

    with _LOCK:
        if _STATE["building"]:
            return False
        _STATE["building"] = True

    ok = False
    try:
        docs = _build_docs(files)
        if not docs:
            with _LOCK:
                _STATE["last_error"] = "docs_empty"
            return False
        client = _get_openai_client(cfg)
        if client is None:
            return False
        emb_model = ((cfg.get("embedding") or {}).get("model") or "").strip()
        batch_size = 16
        vectors: List[np.ndarray] = []
        texts = [str(d.get("text") or "") for d in docs]
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            arr = _embed_texts(client, emb_model, batch)
            if arr is None:
                return False
            vectors.append(arr)
        embeddings = np.vstack(vectors).astype(np.float32)
        graph = _build_graph(cfg, docs)
        _save_index(save_dir, docs, embeddings, graph)
        _write_manifest(save_dir, new_manifest)
        idx = {"docs": docs, "embeddings": embeddings, "graph": graph}
        ok = True
        with _LOCK:
            _STATE["last_manifest"] = new_manifest
            _STATE["last_error"] = None
            _STATE["cfg_key"] = _cfg_key(cfg)
            _STATE["index"] = idx
    except Exception as e:
        with _LOCK:
            _STATE["last_error"] = str(e)
        ok = False
    finally:
        with _LOCK:
            _STATE["building"] = False
    return ok


async def ensure_index_ready_async(force: bool = False) -> bool:
    return await asyncio.to_thread(ensure_index_ready, force=force)


def retrieve_knowledge(query: str, top_k: Optional[int] = None) -> Tuple[str, List[Dict[str, Any]]]:
    q = (query or "").strip()
    if not q:
        return "", []

    cfg = _load_hipporag_config()
    if not _is_cfg_ready(cfg):
        return "", []

    q_norm = re.sub(r"\s+", " ", q).strip()
    q_norm2 = re.sub(r"[^\w\u4e00-\u9fff]+", "", q_norm.lower()).strip()
    min_query_chars = int(cfg.get("min_query_chars") or 4)
    min_query_terms = int(cfg.get("min_query_terms") or 2)
    if min_query_chars < 1:
        min_query_chars = 1
    if min_query_terms < 1:
        min_query_terms = 1
    terms = _extract_terms(q_norm)
    if q_norm2 in _GREETINGS:
        return "", []
    if len(q_norm2) < min_query_chars and len(terms) < min_query_terms:
        return "", []

    if not ensure_index_ready():
        return "", []

    k = int(top_k or cfg.get("top_k") or 3)
    if k < 1:
        k = 1
    seed_k = int(cfg.get("seed_k") or 8)
    if seed_k < 1:
        seed_k = 1

    with _LOCK:
        idx = _STATE.get("index")
    if not isinstance(idx, dict):
        return "", []
    docs = idx.get("docs") or []
    embeddings: np.ndarray = idx.get("embeddings")
    graph: nx.Graph = idx.get("graph")
    if not isinstance(docs, list) or embeddings is None or not hasattr(embeddings, "shape"):
        return "", []
    if len(docs) == 0:
        return "", []

    client = _get_openai_client(cfg)
    if client is None:
        return "", []
    emb_model = ((cfg.get("embedding") or {}).get("model") or "").strip()
    qvec = _embed_texts(client, emb_model, [q])
    if qvec is None or qvec.size == 0:
        return "", []

    mat = embeddings.astype(np.float32)
    qv = qvec.astype(np.float32)[0]
    mat_norm = np.linalg.norm(mat, axis=1) + 1e-12
    qn = float(np.linalg.norm(qv) + 1e-12)
    sims = (mat @ qv) / (mat_norm * qn)
    sims = np.asarray(sims, dtype=np.float32)
    min_similarity = float(cfg.get("min_similarity") or 0.35)
    if min_similarity < 0:
        min_similarity = 0.0
    if float(np.max(sims)) < min_similarity:
        return "", []
    order = np.argsort(-sims)
    seed_ids = [int(i) for i in order[: min(seed_k, len(docs))]]

    sim_max = float(sims[seed_ids[0]]) if seed_ids else 1.0
    if sim_max <= 0:
        sim_max = 1.0
    personalization = {i: float(max(sims[i], 0.0) / sim_max) for i in seed_ids}

    pr_scores: Dict[int, float] = {}
    if isinstance(graph, nx.Graph) and graph.number_of_nodes() == len(docs) and graph.number_of_edges() > 0:
        try:
            pr_scores = nx.pagerank(graph, alpha=0.85, personalization=personalization, weight="weight")
        except Exception:
            pr_scores = {}

    final_scores: List[Tuple[int, float]] = []
    sim_top = float(max(np.max(sims), 1e-9))
    for i in range(len(docs)):
        s = float(max(sims[i], 0.0) / sim_top)
        pr = float(pr_scores.get(i, 0.0))
        final_scores.append((i, 0.35 * s + 0.65 * pr))
    final_scores.sort(key=lambda x: x[1], reverse=True)
    top_ids = [i for i, _ in final_scores[: min(k, len(final_scores))]]
    score_map = {i: float(s) for i, s in final_scores}

    sources: List[Dict[str, Any]] = []
    blocks: List[str] = []
    for i in top_ids:
        doc = docs[i] if i < len(docs) else {}
        file_name = doc.get("file") if isinstance(doc, dict) else None
        chunk_id = doc.get("chunk") if isinstance(doc, dict) else None
        content = doc.get("text") if isinstance(doc, dict) else None
        if not isinstance(content, str) or not content.strip():
            continue
        source: Dict[str, Any] = {"score": float(score_map.get(i, 0.0))}
        if isinstance(file_name, str) and file_name:
            source["file"] = file_name
        if isinstance(chunk_id, int):
            source["chunk"] = chunk_id
        sources.append(source)
        label = file_name or "unknown"
        if isinstance(chunk_id, int):
            label = f"{label}#{chunk_id}"
        blocks.append(f"【{label}】\n{content.strip()}")

    return "\n\n".join(blocks).strip(), sources


async def retrieve_knowledge_async(query: str, top_k: Optional[int] = None) -> Tuple[str, List[Dict[str, Any]]]:
    return await asyncio.to_thread(retrieve_knowledge, query, top_k)


def get_index_status() -> Dict[str, Any]:
    cfg = _load_hipporag_config()
    files = []
    try:
        files = _list_knowledge_files() if os.path.exists(KNOWLEDGE_DIR) else []
    except Exception:
        files = []
    with _LOCK:
        st = dict(_STATE)
    st.pop("index", None)
    return {
        "cfg_ready": _is_cfg_ready(cfg),
        "knowledge_files": len(files),
        "save_dir": cfg.get("save_dir"),
        "embedding": cfg.get("embedding"),
        "has_api_key": bool(_get_api_key(cfg)),
        "last_error": st.get("last_error"),
    }


async def on_message(event, actions, Manager, Segments):
    try:
        task = asyncio.create_task(ensure_index_ready_async())
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
    except Exception:
        pass
    return False


def _get_api_key(cfg: Dict[str, Any]) -> Optional[str]:
    embedding = cfg.get("embedding") if isinstance(cfg, dict) else None
    if isinstance(embedding, dict):
        v = embedding.get("api_key")
        if isinstance(v, str) and v.strip():
            return v.strip()
    llm = cfg.get("llm") if isinstance(cfg, dict) else None
    if isinstance(llm, dict):
        v = llm.get("api_key")
        if isinstance(v, str) and v.strip():
            return v.strip()
    for name in ("OPENAI_API_KEY", "SILICONFLOW_API_KEY", "DEEPSEEK_API_KEY"):
        v = os.environ.get(name)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _get_openai_client(cfg: Dict[str, Any]):
    try:
        from openai import OpenAI
    except Exception as e:
        with _LOCK:
            _STATE["last_error"] = str(e)
        return None
    api_key = _get_api_key(cfg)
    if not api_key:
        with _LOCK:
            _STATE["last_error"] = "missing_api_key"
        return None
    base_url = ((cfg.get("embedding") or {}).get("base_url") or "").strip()
    try:
        return OpenAI(api_key=api_key, base_url=base_url)
    except Exception as e:
        with _LOCK:
            _STATE["last_error"] = str(e)
        return None


def _embed_texts(client, model: str, texts: List[str]) -> Optional[np.ndarray]:
    if not texts:
        return np.zeros((0, 0), dtype=np.float32)
    try:
        resp = client.embeddings.create(model=model, input=texts)
    except Exception as e:
        with _LOCK:
            _STATE["last_error"] = str(e)
        return None
    data = getattr(resp, "data", None) or []
    vectors: List[List[float]] = []
    for item in data:
        emb = getattr(item, "embedding", None)
        if isinstance(emb, list) and emb:
            vectors.append(emb)
    if len(vectors) != len(texts):
        with _LOCK:
            _STATE["last_error"] = "embedding_length_mismatch"
        return None
    arr = np.asarray(vectors, dtype=np.float32)
    return arr


def _extract_terms(text: str) -> List[str]:
    tokens = jieba.lcut(text or "", cut_all=False)
    terms: List[str] = []
    for t in tokens:
        t = (t or "").strip().lower()
        if not t:
            continue
        if len(t) < 2:
            continue
        if re.fullmatch(r"[\W_]+", t):
            continue
        terms.append(t)
        if len(terms) >= 60:
            break
    return terms


def _build_graph(cfg: Dict[str, Any], docs: List[Dict[str, Any]]) -> nx.Graph:
    neighbor_cap = int(cfg.get("neighbor_cap") or 25)
    edge_cap_per_node = int(cfg.get("edge_cap_per_node") or 20)

    postings: Dict[str, List[int]] = {}
    for i, doc in enumerate(docs):
        terms = _extract_terms(str(doc.get("text") or ""))
        for term in set(terms):
            postings.setdefault(term, []).append(i)

    g = nx.Graph()
    g.add_nodes_from(range(len(docs)))

    for term, ids in postings.items():
        if len(ids) < 2:
            continue
        if len(ids) > neighbor_cap:
            ids = ids[:neighbor_cap]
        for a_i in range(len(ids)):
            a = ids[a_i]
            for b_i in range(a_i + 1, len(ids)):
                b = ids[b_i]
                if a == b:
                    continue
                w = g.get_edge_data(a, b, {}).get("weight", 0.0) + 1.0
                g.add_edge(a, b, weight=w)

    for n in list(g.nodes()):
        edges = sorted(
            [(nbr, g[n][nbr].get("weight", 1.0)) for nbr in g.neighbors(n)],
            key=lambda x: x[1],
            reverse=True,
        )
        if len(edges) <= edge_cap_per_node:
            continue
        keep = set([nbr for nbr, _ in edges[:edge_cap_per_node]])
        for nbr, _ in edges[edge_cap_per_node:]:
            if nbr not in keep and g.has_edge(n, nbr):
                g.remove_edge(n, nbr)

    return g


def _save_index(save_dir: str, docs: List[Dict[str, Any]], embeddings: np.ndarray, graph: nx.Graph) -> None:
    os.makedirs(save_dir, exist_ok=True)
    with open(_index_chunks_path(save_dir), "w", encoding="utf-8") as f:
        json.dump(docs, f, ensure_ascii=False, indent=2)
    np.save(_index_embeddings_path(save_dir), embeddings)
    edges = []
    for u, v, data in graph.edges(data=True):
        edges.append([int(u), int(v), float(data.get("weight", 1.0))])
    with open(_index_graph_path(save_dir), "w", encoding="utf-8") as f:
        json.dump({"n": len(docs), "edges": edges}, f, ensure_ascii=False, indent=2)


def _load_index(save_dir: str) -> Optional[Dict[str, Any]]:
    try:
        with open(_index_chunks_path(save_dir), "r", encoding="utf-8") as f:
            docs = json.load(f)
        embeddings = np.load(_index_embeddings_path(save_dir))
        with open(_index_graph_path(save_dir), "r", encoding="utf-8") as f:
            gdata = json.load(f)
    except Exception as e:
        with _LOCK:
            _STATE["last_error"] = str(e)
        return None
    if not isinstance(docs, list) or not isinstance(gdata, dict):
        with _LOCK:
            _STATE["last_error"] = "index_format_invalid"
        return None
    g = nx.Graph()
    g.add_nodes_from(range(len(docs)))
    for e in gdata.get("edges") or []:
        if not isinstance(e, list) or len(e) < 2:
            continue
        u = int(e[0])
        v = int(e[1])
        w = float(e[2]) if len(e) > 2 else 1.0
        if 0 <= u < len(docs) and 0 <= v < len(docs) and u != v:
            g.add_edge(u, v, weight=w)
    return {"docs": docs, "embeddings": embeddings.astype(np.float32), "graph": g}
