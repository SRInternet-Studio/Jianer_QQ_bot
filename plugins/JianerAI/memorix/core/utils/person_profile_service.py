"""
人物画像服务

主链路：
person_id -> 用户名/别名 -> 图谱关系 + 向量证据 -> 证据总结画像 -> 快照版本化存储
"""

import hashlib
import json
import re
import time
from typing import Any, Dict, List, Optional, Tuple

try:
    from json_repair import repair_json
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    def repair_json(value: Any) -> Any:
        """Use strict JSON parsing when json_repair is unavailable."""
        return value
from sqlalchemy import or_
from sqlmodel import select


from ..._compat import get_logger
try:  # Identity DB/config/LLM are optional host integrations.
    from src.common.database.database import get_db_session
    from src.common.database.database_model import PersonInfo
    from src.config.config import global_config
    from src.services import llm_service as llm_api
except ModuleNotFoundError:  # pragma: no cover - Jianer host path
    from .._host_compat import PersonInfo, get_db_session, global_config, llm_api

from ..embedding import EmbeddingAPIAdapter
from ..retrieval import (
    DualPathRetriever,
    DualPathRetrieverConfig,
    FusionConfig,
    GraphRelationRecallConfig,
    PosteriorGraphConfig,
    RetrievalStrategy,
    SparseBM25Config,
    VectorPoolsConfig,
)
from ..storage import MetadataStore, GraphStore, VectorStore
from .metadata import coerce_metadata_dict
from .model_routing import (
    ResolvedLLMModel,
    generate_with_resolved_model,
    get_text_generation_model_tasks,
    pick_text_generation_task,
)
from .profile_text import (
    build_profile_injection_text,
    build_structured_profile_text,
    parse_profile_sections,
    section_is_empty,
)

logger = get_logger("Jianer Memory.PersonProfileService")

PROFILE_CLASSIFICATION_REQUEST_TYPE = "A_Memorix.PersonProfileEvidenceClassify"
PROFILE_GENERATION_VERSION = 3


class PersonProfileService:
    """人物画像聚合/刷新服务。"""

    def __init__(
        self,
        metadata_store: MetadataStore,
        graph_store: Optional[GraphStore] = None,
        vector_store: Optional[VectorStore] = None,
        paragraph_vector_store: Optional[VectorStore] = None,
        graph_vector_store: Optional[VectorStore] = None,
        embedding_manager: Optional[EmbeddingAPIAdapter] = None,
        sparse_index: Any = None,
        plugin_config: Optional[dict] = None,
        retriever: Optional[DualPathRetriever] = None,
        relation_write_service: Any = None,
    ):
        self.metadata_store = metadata_store
        self.graph_store = graph_store
        self.vector_store = vector_store
        self.paragraph_vector_store = paragraph_vector_store
        self.graph_vector_store = graph_vector_store
        self.embedding_manager = embedding_manager
        self.sparse_index = sparse_index
        self.plugin_config = plugin_config or {}
        self.retriever = retriever or self._build_retriever()
        self.relation_write_service = relation_write_service

    def _cfg(self, key: str, default: Any = None) -> Any:
        """读取嵌套配置。"""
        if not isinstance(self.plugin_config, dict):
            return default
        current: Any = self.plugin_config
        for part in key.split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return default
        return current

    @classmethod
    def _canonical_profile_value(cls, value: Any) -> Any:
        """规范化画像证据，消除字典顺序、集合顺序和召回排序抖动。"""
        if isinstance(value, dict):
            return {
                str(key): cls._canonical_profile_value(item)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            }
        if isinstance(value, (list, tuple, set)):
            normalized = [cls._canonical_profile_value(item) for item in value]
            return sorted(
                normalized,
                key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, default=str),
            )
        if isinstance(value, float):
            return round(value, 12)
        return value

    def _profile_generation_signature(self) -> Dict[str, Any]:
        """返回会影响画像文本生成结果的实现与模型配置签名。"""
        model = self._resolve_profile_classification_model()
        if model is None:
            model_signature: Dict[str, Any] = {"mode": "rule_based"}
        else:
            model_signature = {
                "mode": "llm",
                "task_name": model.task_name,
                "selected_model_name": model.selected_model_name,
                "model_list": sorted(
                    str(item).strip() for item in getattr(model.task_config, "model_list", []) if str(item).strip()
                ),
            }
        return {
            "generation_version": PROFILE_GENERATION_VERSION,
            "classification_max_tokens": self._profile_classification_max_tokens(),
            "model": model_signature,
        }

    def _profile_evidence_fingerprint(
        self,
        *,
        person_id: str,
        primary_name: str,
        aliases: List[str],
        relation_edges: List[Dict[str, Any]],
        vector_evidence: List[Dict[str, Any]],
        memory_traits: List[str],
        fact_claims: List[Dict[str, Any]],
    ) -> str:
        """计算画像输入版本；检索分数不属于证据内容，故不进入指纹。"""
        stable_vector_evidence = [
            {key: value for key, value in item.items() if key != "score"} for item in vector_evidence
        ]
        payload = self._canonical_profile_value(
            {
                "person_id": person_id,
                "primary_name": primary_name,
                "aliases": aliases,
                "memory_traits": memory_traits,
                "relation_edges": relation_edges,
                "vector_evidence": stable_vector_evidence,
                "fact_claims": fact_claims,
                "generation": self._profile_generation_signature(),
            }
        )
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _profile_classification_max_tokens(self) -> int:
        """读取人物画像证据分类的最大输出 token 数。"""
        raw_value = self._cfg("person_profile.evidence_classification_max_tokens", 1200)
        try:
            return min(32768, max(128, int(raw_value or 1200)))
        except (TypeError, ValueError):
            return 1200

    def _build_retriever(self) -> Optional[DualPathRetriever]:
        """按需构建检索器（无依赖时返回 None）。"""
        if not all(
            [
                self.vector_store is not None,
                self.graph_store is not None,
                self.metadata_store is not None,
                self.embedding_manager is not None,
            ]
        ):
            return None
        try:
            sparse_cfg_raw = self._cfg("retrieval.sparse", {}) or {}
            fusion_cfg_raw = self._cfg("retrieval.fusion", {}) or {}
            graph_recall_cfg_raw = self._cfg("retrieval.search.graph_recall", {}) or {}
            posterior_graph_cfg_raw = self._cfg("retrieval.search.posterior_graph", {}) or {}
            vector_pools_cfg_raw = self._cfg("retrieval.vector_pools", {}) or {}
            if not isinstance(sparse_cfg_raw, dict):
                sparse_cfg_raw = {}
            if not isinstance(fusion_cfg_raw, dict):
                fusion_cfg_raw = {}
            if not isinstance(graph_recall_cfg_raw, dict):
                graph_recall_cfg_raw = {}
            if not isinstance(posterior_graph_cfg_raw, dict):
                posterior_graph_cfg_raw = {}
            if not isinstance(vector_pools_cfg_raw, dict):
                vector_pools_cfg_raw = {}

            runtime_cfg = self._cfg("runtime", {}) or {}
            if isinstance(runtime_cfg, dict) and "vector_pools_ready" in runtime_cfg:
                vector_pools_ready = bool(runtime_cfg.get("vector_pools_ready", False))
            else:
                vector_pools_ready = self.paragraph_vector_store is not None and self.graph_vector_store is not None
            configured_mode = str(vector_pools_cfg_raw.get("mode", "dual") or "dual").strip().lower()
            if configured_mode == "dual" and not vector_pools_ready:
                vector_pools_cfg_raw = dict(vector_pools_cfg_raw)
                vector_pools_cfg_raw["mode"] = "single"

            sparse_cfg = SparseBM25Config(**sparse_cfg_raw)
            fusion_cfg = FusionConfig(**fusion_cfg_raw)
            graph_recall_cfg = GraphRelationRecallConfig(**graph_recall_cfg_raw)
            posterior_graph_cfg = PosteriorGraphConfig(**posterior_graph_cfg_raw)
            vector_pools_cfg = VectorPoolsConfig(**vector_pools_cfg_raw)
            config = DualPathRetrieverConfig(
                top_k_paragraphs=int(self._cfg("retrieval.top_k_paragraphs", 20)),
                top_k_relations=int(self._cfg("retrieval.top_k_relations", 10)),
                top_k_final=int(self._cfg("retrieval.top_k_final", 10)),
                alpha=float(self._cfg("retrieval.alpha", 0.5)),
                enable_ppr=bool(self._cfg("retrieval.enable_ppr", True)),
                ppr_alpha=float(self._cfg("retrieval.ppr_alpha", 0.85)),
                ppr_concurrency_limit=int(self._cfg("retrieval.ppr_concurrency_limit", 4)),
                enable_parallel=bool(self._cfg("retrieval.enable_parallel", True)),
                retrieval_strategy=RetrievalStrategy.DUAL_PATH,
                debug=bool(self._cfg("advanced.debug", False)),
                sparse=sparse_cfg,
                fusion=fusion_cfg,
                graph_recall=graph_recall_cfg,
                posterior_graph=posterior_graph_cfg,
                vector_pools=vector_pools_cfg,
            )
            return DualPathRetriever(
                vector_store=self.vector_store,
                paragraph_vector_store=self.paragraph_vector_store or self.vector_store,
                graph_vector_store=self.graph_vector_store or self.vector_store,
                graph_store=self.graph_store,
                metadata_store=self.metadata_store,
                embedding_manager=self.embedding_manager,
                sparse_index=self.sparse_index,
                config=config,
            )
        except Exception as e:
            logger.warning(f"初始化人物画像检索器失败，将只使用关系证据: {e}")
            return None

    @staticmethod
    def resolve_person_id(identifier: str) -> str:
        """按 person_id 或姓名/别名解析 person_id。"""
        if not identifier:
            return ""
        key = str(identifier).strip()
        if not key:
            return ""

        try:
            with get_db_session(auto_commit=False) as session:
                record = session.exec(select(PersonInfo.person_id).where(PersonInfo.person_id == key).limit(1)).first()
                if record:
                    return str(record)

                record = session.exec(
                    select(PersonInfo.person_id)
                    .where(
                        or_(
                            PersonInfo.person_name == key,
                            PersonInfo.user_nickname == key,
                        )
                    )
                    .limit(1)
                ).first()
                if record:
                    return str(record)

                record = session.exec(
                    select(PersonInfo.person_id).where(PersonInfo.group_cardname.contains(key)).limit(1)
                ).first()
                if record:
                    return str(record)
        except Exception as e:
            logger.warning(f"按别名解析 person_id 失败: identifier={key}, err={e}")

        if len(key) == 32 and all(ch in "0123456789abcdefABCDEF" for ch in key):
            return key.lower()

        return ""

    def _parse_group_nicks(self, raw_value: Any) -> List[str]:
        if not raw_value:
            return []
        if isinstance(raw_value, list):
            items = raw_value
        else:
            try:
                items = json.loads(raw_value)
            except Exception:
                return []
        names: List[str] = []
        for item in items:
            if isinstance(item, dict):
                value = str(item.get("group_cardname") or item.get("group_nick_name") or "").strip()
                if value:
                    names.append(value)
            elif isinstance(item, str):
                value = item.strip()
                if value:
                    names.append(value)
        return names

    def _parse_memory_traits(self, raw_value: Any) -> List[str]:
        if not raw_value:
            return []
        try:
            values = json.loads(raw_value) if isinstance(raw_value, str) else raw_value
        except Exception:
            return []
        if not isinstance(values, list):
            return []
        traits: List[str] = []
        for item in values:
            text = str(item).strip()
            if not text:
                continue
            if ":" in text:
                parts = text.split(":")
                if len(parts) >= 3:
                    content = ":".join(parts[1:-1]).strip()
                    if content:
                        traits.append(content)
                        continue
            traits.append(text)
        return traits[:10]

    def _collect_alias_suggestions_from_memory(self, person_id: str, trusted_aliases: List[str]) -> List[str]:
        """收集与人物同段出现的实体，仅作为待人工确认的别名候选。"""
        if not person_id:
            return []

        suggestions: List[str] = []
        excluded = {
            str(item or "").strip().lower() for item in [person_id, *trusted_aliases] if str(item or "").strip()
        }
        seen = set(excluded)

        paragraphs_by_hash: Dict[str, Dict[str, Any]] = {}
        query_aliases: List[str] = []
        query_alias_keys = set()
        for item in trusted_aliases:
            alias = str(item or "").strip()
            alias_key = alias.lower()
            if not alias or alias_key in query_alias_keys:
                continue
            query_alias_keys.add(alias_key)
            query_aliases.append(alias)
        for alias in query_aliases:
            try:
                paragraphs = self.metadata_store.get_paragraphs_by_entity(alias)
            except Exception as e:
                logger.warning(f"从记忆证据收集人物别名候选失败: person_id={person_id}, alias={alias}, err={e}")
                continue
            for paragraph in paragraphs:
                paragraph_hash = str(paragraph.get("hash", "") or "").strip()
                if paragraph_hash and paragraph_hash not in paragraphs_by_hash:
                    paragraphs_by_hash[paragraph_hash] = paragraph

        for paragraph in list(paragraphs_by_hash.values())[:20]:
            paragraph_hash = str(paragraph.get("hash", "") or "").strip()
            if not paragraph_hash:
                continue
            try:
                paragraph_entities = self.metadata_store.get_paragraph_entities(paragraph_hash)
            except Exception:
                paragraph_entities = []
            for entity in paragraph_entities:
                name = str(entity.get("name", "") or "").strip()
                if not name or name == person_id:
                    continue
                key = name.lower()
                if key in seen:
                    continue
                seen.add(key)
                suggestions.append(name)
        return suggestions

    @staticmethod
    def _extract_explicit_person_aliases(text: str) -> List[str]:
        """Extract names explicitly assigned to the current profile subject."""

        content = re.sub(r"[\r\n]+", " ", str(text or "")).strip()
        if not content:
            return []
        stop = r"\s，。！？；!?;:：、,\"'“”‘’（）()\[\]【】"
        patterns = (
            rf"(?:我|他|她|这个人|对方)(?:就)?(?:叫|是|名叫|名字是|自称)\s*([^{stop}]{{1,32}})",
            rf"(?:称呼|叫作|叫做)(?:我|他|她|这个人|对方)?\s*([^{stop}]{{1,32}})",
            rf"只称呼(?:我|他|她|这个人|对方)?\s*([^{stop}]{{1,32}})",
        )
        ignored = {
            "我",
            "本人",
            "用户",
            "当前用户",
            "他",
            "她",
            "它",
            "对方",
            "某人",
            "同型号",
            "同型号的机娘",
            "同型号机娘",
            "计算机特化型",
            "计算机特化型的",
            "这样",
            "这么",
            "如此",
        }
        aliases: List[str] = []
        seen = set()
        for pattern in patterns:
            for match in re.finditer(pattern, content, flags=re.IGNORECASE):
                candidate = str(match.group(1) or "").strip(" ，,、:：\"'“”‘’（）()[]【】")
                if (
                    not candidate
                    or candidate in ignored
                    or "同型号" in candidate
                    or "机娘" in candidate
                    or candidate.startswith("计算机特化")
                    or candidate.endswith(("的", "了", "呢", "啦"))
                    or any(token in candidate for token in ("交代", "确认", "告诉", "记住", "让我"))
                ):
                    continue
                key = candidate.casefold()
                if key in seen:
                    continue
                seen.add(key)
                aliases.append(candidate[:64])
        return aliases

    def _fact_derived_person_aliases(self, person_id: str) -> Tuple[List[str], str]:
        """Read explicit identity corrections from the memory fact ledger."""

        try:
            claims = self.metadata_store.list_person_profile_fact_claims(
                person_id,
                effective_at=time.time(),
                limit=200,
            )
        except Exception:
            return [], ""
        candidates: List[str] = []
        seen = set()
        for claim in claims:
            text = str(claim.get("value_text", "") or "").strip()
            for alias in self._extract_explicit_person_aliases(text):
                key = alias.casefold()
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(alias)
        return candidates, (candidates[0] if candidates else "")

    def _configured_bot_name(self) -> str:
        configured = self._cfg("bot", {})
        if isinstance(configured, dict):
            for key in ("nickname", "name", "display_name"):
                value = str(configured.get(key, "") or "").strip()
                if value:
                    return value
        return ""

    def _get_derived_person_aliases(self, person_id: str) -> Tuple[List[str], str, List[str]]:
        """只从人物主档案中的可信身份字段推导自动别名。"""
        aliases: List[str] = []
        primary_name = ""
        memory_traits: List[str] = []
        if not person_id:
            return aliases, primary_name, memory_traits
        record = None
        try:
            with get_db_session(auto_commit=False) as session:
                record = session.exec(select(PersonInfo).where(PersonInfo.person_id == person_id).limit(1)).first()
        except Exception as e:
            logger.debug(f"宿主人物数据库不可用，改用事实别名: person_id={person_id}, err={e}")
        fact_aliases, fact_primary = self._fact_derived_person_aliases(person_id)
        if record is not None:
            person_name = str(record.person_name or "").strip()
            nickname = str(record.user_nickname or "").strip()
            group_nicks = self._parse_group_nicks(record.group_cardname)
            memory_traits = self._parse_memory_traits(record.memory_points)

            primary_name = (
                person_name
                or nickname
                or next((item for item in group_nicks if item), "")
                or str(record.user_id or "").strip()
                or person_id
            )

            candidates = [person_name, nickname] + group_nicks
            seen = set()
            for item in candidates:
                norm = str(item or "").strip()
                key = norm.lower()
                if not norm or key in seen:
                    continue
                seen.add(key)
                aliases.append(norm)
        if fact_aliases:
            known = {item.casefold() for item in aliases}
            aliases.extend(item for item in fact_aliases if item.casefold() not in known)
            # An explicit correction such as "他叫桃子" is more precise than
            # an old host nickname, while the host record remains available as
            # an additional alias for lookup.
            primary_name = fact_primary or primary_name
        if not aliases:
            aliases = [primary_name or person_id]
            primary_name = primary_name or person_id
        return aliases, primary_name, memory_traits

    def get_person_alias_details(self, person_id: str) -> Dict[str, Any]:
        """返回自动别名、人工覆盖和当前实际使用的别名。"""
        token = str(person_id or "").strip()
        if not token:
            return {
                "person_id": "",
                "primary_name": "",
                "derived_aliases": [],
                "suggested_aliases": [],
                "manual_aliases": [],
                "effective_aliases": [],
                "has_override": False,
                "memory_traits": [],
            }

        derived_aliases, primary_name, memory_traits = self._get_derived_person_aliases(token)
        override = self.metadata_store.get_person_profile_alias_override(token)
        manual_aliases = list(override.get("aliases", [])) if override else []
        suggested_aliases = self._collect_alias_suggestions_from_memory(token, derived_aliases + manual_aliases)
        return {
            "person_id": token,
            "primary_name": primary_name,
            "derived_aliases": derived_aliases,
            "suggested_aliases": suggested_aliases,
            "manual_aliases": manual_aliases,
            "effective_aliases": manual_aliases if override else derived_aliases,
            "has_override": override is not None,
            "memory_traits": memory_traits,
            "override": override,
        }

    def get_person_aliases(self, person_id: str) -> Tuple[List[str], str, List[str]]:
        """获取画像实际使用的别名集合、主展示名和记忆特征。"""
        details = self.get_person_alias_details(person_id)
        return (
            list(details["effective_aliases"]),
            str(details["primary_name"]),
            list(details["memory_traits"]),
        )

    def _collect_relation_evidence(
        self,
        aliases: List[str],
        limit: int = 30,
        *,
        person_id: str = "",
    ) -> List[Dict[str, Any]]:
        relation_by_hash: Dict[str, Dict[str, Any]] = {}
        # Automatic review relations may use the canonical person ID when the
        # message only says "我". Include it as a lookup token while keeping it
        # out of the user-facing alias list.
        lookup_aliases = list(aliases)
        if person_id and str(person_id).strip() not in lookup_aliases:
            lookup_aliases.append(str(person_id).strip())
        for alias in lookup_aliases:
            for rel in self.metadata_store.get_relations(subject=alias, include_inactive=False):
                h = str(rel.get("hash", ""))
                if h:
                    relation_by_hash[h] = rel
            for rel in self.metadata_store.get_relations(object=alias, include_inactive=False):
                h = str(rel.get("hash", ""))
                if h:
                    relation_by_hash[h] = rel

        relations = list(relation_by_hash.values())
        if person_id:
            relations = [rel for rel in relations if self._is_relation_bound_to_person(rel, person_id=person_id)]
        relations.sort(key=lambda item: float(item.get("confidence", 0.0)), reverse=True)
        relations = relations[: max(1, int(limit))]

        edges: List[Dict[str, Any]] = []
        for rel in relations:
            edges.append(
                {
                    "hash": str(rel.get("hash", "")),
                    "subject": str(rel.get("subject", "")),
                    "predicate": str(rel.get("predicate", "")),
                    "object": str(rel.get("object", "")),
                    "confidence": float(rel.get("confidence", 1.0) or 1.0),
                }
            )
        return edges

    def _is_relation_bound_to_person(
        self,
        relation: Dict[str, Any],
        *,
        person_id: str,
    ) -> bool:
        pid = str(person_id or "").strip()
        if not pid:
            return False

        metadata = coerce_metadata_dict(relation.get("metadata"))
        if str(metadata.get("person_id", "") or "").strip() == pid:
            return True
        if pid in self._list_tokens(metadata.get("person_ids")):
            return True

        source_paragraph = str(relation.get("source_paragraph", "") or "").strip()
        if source_paragraph:
            try:
                paragraph = self.metadata_store.get_paragraph(source_paragraph)
            except Exception:
                paragraph = None
            if isinstance(paragraph, dict):
                payload = {
                    "hash": source_paragraph,
                    "source": str(paragraph.get("source", "") or ""),
                    "metadata": coerce_metadata_dict(paragraph.get("metadata")),
                }
                return self._is_evidence_bound_to_person(payload, person_id=pid)

        return False

    def _collect_person_fact_evidence(self, person_id: str, limit: int = 4) -> List[Dict[str, Any]]:
        token = str(person_id or "").strip()
        if not token:
            return []

        source = f"person_fact:{token}"
        paragraphs = [
            row for row in self.metadata_store.get_paragraphs_by_source(source) if not bool(row.get("is_deleted", 0))
        ]
        paragraphs.sort(
            key=lambda item: float(item.get("updated_at") or item.get("created_at") or 0.0),
            reverse=True,
        )

        evidence: List[Dict[str, Any]] = []
        for row in paragraphs[: max(1, int(limit))]:
            paragraph_hash = str(row.get("hash", "") or "")
            content = str(row.get("content", "") or "").strip()
            if not paragraph_hash or not content:
                continue
            evidence.append(
                {
                    "hash": paragraph_hash,
                    "type": "paragraph",
                    "score": 1.1,
                    "content": content[:220],
                    "source": str(row.get("source", "") or source),
                    "metadata": coerce_metadata_dict(row.get("metadata")),
                }
            )
        return self._filter_stale_paragraph_evidence(evidence)

    def _collect_person_fact_claims(self, person_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        """读取人物事实账本；该顺序直接决定有界画像投影，不能依赖召回分数。"""

        return self.metadata_store.list_person_profile_fact_claims(
            person_id,
            effective_at=time.time(),
            limit=max(1, int(limit)),
        )

    @staticmethod
    def _extract_explicit_relation_candidates(
        text: str,
        *,
        primary_name: str,
        person_id: str,
        person_aliases: Optional[List[str]] = None,
        bot_name: str = "",
    ) -> List[Tuple[str, str, str]]:
        """Extract explicit two-party statements and resolve host pronouns.

        Person facts are written from the bot's first-person perspective. In
        that context ``我`` means the configured bot, while ``他``/``她``
        generally refers to the person whose profile is being refreshed. The
        caller can omit ``bot_name`` for compatibility with older standalone
        tests, in which case the historical current-person mapping remains.
        """

        normalized = re.sub(r"[\r\n]+", " ", str(text or "")).strip().strip("- ")
        if not normalized:
            return []
        current = str(primary_name or "").strip() or str(person_id or "").strip()
        if not current:
            return []
        aliases = {
            str(item).strip().casefold()
            for item in (current, person_id, *(person_aliases or []))
            if str(item).strip()
        }
        bot = str(bot_name or "").strip()
        bot_key = bot.casefold()
        person_pronouns = {"他", "她", "这个人", "对方", "该用户", "用户"}
        bot_pronouns = {"我", "本人", "你", "机器人", "bot"}

        def resolve_endpoint(value: str) -> str:
            token = str(value or "").strip(" ，,、:：\"'“”‘’")
            key = token.casefold()
            if key in {"是", "为", "属于", "和", "与", "跟"}:
                return ""
            if key in aliases or key == str(person_id or "").strip().casefold():
                return current
            if key in person_pronouns:
                return current
            if key in bot_pronouns or (bot_key and key == bot_key):
                return bot or current
            return token

        def clean_predicate(value: str) -> str:
            predicate = re.sub(r"^(?:是|为|属于)\s*", "", str(value or "")).strip(" ，,、")
            predicate = re.sub(r"^(?:的|一名|一个)\s*", "", predicate).strip()
            return predicate

        candidates: List[Tuple[str, str, str]] = []
        declared_pair_pattern = re.compile(
            r"(?:他|她|这个人|对方)(?:就)?(?:叫|是|名叫|名字是)\s*"
            r"(?P<declared>[^，。！？；!?;]{1,32}?)\s*[,，]\s*"
            r"(?:是\s*)?(?:和|与|跟)\s*"
            r"(?P<right>[^，。！？；!?;]{1,16}?)\s*"
            r"(?:是\s*)?(?P<predicate>同型号(?:的)?[^，。！？；!?;]{0,32})"
        )
        for match in declared_pair_pattern.finditer(normalized):
            left = resolve_endpoint(match.group("declared"))
            right = resolve_endpoint(match.group("right"))
            predicate = clean_predicate(match.group("predicate"))
            if left and right and predicate and left != right:
                candidates.append((left, predicate, right))

        pair_pattern = re.compile(
            r"(?P<left>[^，。！？；!?;]{1,32}?)[和与跟](?P<right>[^，。！？；!?;]{1,32}?)(?:是|为|属于)(?P<predicate>[^，。！？；!?;]{1,48})"
        )
        for match in pair_pattern.finditer(normalized):
            left = match.group("left").strip(" ，,、")
            right = match.group("right").strip(" ，,、")
            predicate = clean_predicate(match.group("predicate"))
            left = resolve_endpoint(left)
            right = resolve_endpoint(right)
            if left and right and predicate and left != right:
                candidates.append((left, predicate, right))

        # Common shorthand omits the copula: ``桃子和我同型号的机娘``.
        shorthand_pattern = re.compile(
            r"(?P<left>[^，。！？；!?;]{1,32}?)[和与跟](?P<right>[^，。！？；!?;]{1,16}?)(?:是)?(?P<predicate>同型号(?:的)?[^，。！？；!?;]{0,32})"
        )
        for match in shorthand_pattern.finditer(normalized):
            left = resolve_endpoint(match.group("left"))
            right = resolve_endpoint(match.group("right"))
            predicate = clean_predicate(match.group("predicate"))
            if left and right and predicate and left != right:
                candidates.append((left, predicate, right))

        personal_pattern = re.compile(
            r"(?P<other>[^，。！？；!?;]{1,32}?)是我的(?P<predicate>[^，。！？；!?;]{1,32})"
        )
        for match in personal_pattern.finditer(normalized):
            other = resolve_endpoint(match.group("other"))
            predicate = clean_predicate(match.group("predicate"))
            if other and predicate and other != current:
                candidates.append((current, predicate, other))

        deduped: List[Tuple[str, str, str]] = []
        seen = set()
        for candidate in candidates:
            key = tuple(item.casefold() for item in candidate)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(candidate)
        return deduped[:6]

    async def _backfill_explicit_fact_relations(
        self,
        *,
        person_id: str,
        primary_name: str,
        fact_claims: List[Dict[str, Any]],
        person_aliases: Optional[List[str]] = None,
    ) -> None:
        """Project old explicit person facts into graph relations once observed."""

        writer = self.relation_write_service
        if writer is None or not bool(self._cfg("person_profile.auto_relation_backfill", True)):
            return
        write_vectors = bool(self._cfg("retrieval.relation_vectorization.enabled", False))
        bot_name = self._configured_bot_name()
        planned: list[dict[str, Any]] = []
        for claim in fact_claims[:64]:
            text = str(claim.get("value_text", "") or "").strip()
            if not text:
                continue
            candidates = self._extract_explicit_relation_candidates(
                text,
                primary_name=primary_name,
                person_id=person_id,
                person_aliases=person_aliases,
                bot_name=bot_name,
            )
            if not candidates:
                continue
            try:
                confidence = max(0.0, min(1.0, float(claim.get("confidence", 0.65) or 0.65)))
            except (TypeError, ValueError):
                confidence = 0.65
            # Model-derived claims stay visibly lower-confidence than direct
            # user facts, while still becoming searchable graph evidence.
            authority = str(claim.get("authority", "") or "").strip().casefold()
            if authority not in {"manual", "direct_user", "imported"}:
                confidence = min(confidence, 0.75)
            source_paragraph = str(claim.get("evidence_id", "") or "").strip() or None
            if not source_paragraph:
                try:
                    evidence_rows = self.metadata_store.get_fact_evidence(
                        str(claim.get("claim_id", "") or "")
                    )
                except Exception:
                    evidence_rows = []
                source_paragraph = next(
                    (
                        str(item.get("evidence_id", "") or "").strip()
                        for item in evidence_rows
                        if str(item.get("evidence_type", "") or "").strip() == "paragraph"
                        and str(item.get("evidence_id", "") or "").strip()
                    ),
                    None,
                )
            for subject, predicate, obj in candidates:
                relation_metadata = {
                    "source_type": "person_fact_relation",
                    "relation_origin": "automatic_profile_backfill",
                    "person_id": person_id,
                    "person_ids": [person_id],
                    "fact_claim_id": str(claim.get("claim_id", "") or ""),
                    "authority": authority,
                }
                if bot_name:
                    relation_metadata.update(
                        {
                            "person_display_name": primary_name,
                            "bot_display_name": bot_name,
                        }
                    )
                planned.append(
                    {
                        "subject": subject,
                        "predicate": predicate,
                        "object": obj,
                        "confidence": confidence,
                        "source_paragraph": source_paragraph,
                        "metadata": relation_metadata,
                    }
                )

        if not planned:
            return

        desired_signatures = {
            (
                str(item["subject"]).casefold(),
                str(item["predicate"]).casefold(),
                str(item["object"]).casefold(),
            )
            for item in planned
        }
        stale_hashes: list[str] = []
        try:
            existing_relations = self.metadata_store.get_relations(include_inactive=False)
        except Exception:
            existing_relations = []
        for relation in existing_relations:
            metadata = coerce_metadata_dict(relation.get("metadata"))
            if str(metadata.get("source_type", "") or "").strip() != "person_fact_relation":
                continue
            if str(metadata.get("person_id", "") or "").strip() != str(person_id).strip():
                continue
            signature = (
                str(relation.get("subject", "") or "").strip().casefold(),
                str(relation.get("predicate", "") or "").strip().casefold(),
                str(relation.get("object", "") or "").strip().casefold(),
            )
            relation_hash = str(relation.get("hash", "") or "").strip()
            if relation_hash and signature not in desired_signatures:
                stale_hashes.append(relation_hash)
        if stale_hashes:
            try:
                self.metadata_store.mark_relations_inactive(
                    stale_hashes,
                    reason="profile_identity_normalized",
                )
                self._publish_relation_projection()
            except Exception as exc:
                logger.debug(
                    "人物事实关系旧身份边清理失败: person_id=%s, err=%s",
                    person_id,
                    exc,
                )

        for item in planned:
            subject = str(item["subject"])
            predicate = str(item["predicate"])
            obj = str(item["object"])
            try:
                relation_hash = self.metadata_store.compute_relation_hash(
                    subject,
                    predicate,
                    obj,
                )
                existing = self.metadata_store.get_relation(
                    relation_hash,
                    include_inactive=False,
                )
                if existing is not None:
                    continue
            except Exception:
                # The write service remains the source of truth if an
                # older metadata store lacks the optional lookup helper.
                pass
            try:
                await writer.upsert_relation_with_vector(
                    subject=subject,
                    predicate=predicate,
                    obj=obj,
                    confidence=float(item["confidence"]),
                    source_paragraph=item["source_paragraph"],
                    metadata=dict(item["metadata"]),
                    write_vector=write_vectors,
                )
            except Exception as exc:
                logger.debug(
                    "人物事实关系回填失败: person_id=%s, relation=%s-%s-%s, err=%s",
                    person_id,
                    subject,
                    predicate,
                    obj,
                    exc,
                )

    def _publish_relation_projection(self) -> None:
        """Publish relation lifecycle changes through the active SDK kernel."""

        runtime = self._cfg("plugin_instance")
        publish = getattr(runtime, "publish_authoritative_graph_projection", None)
        if callable(publish):
            publish()

    async def reconcile_profile_relations(
        self,
        person_ids: Optional[List[str]] = None,
    ) -> Dict[str, int]:
        """Repair automatic profile relations before graph/profile reads.

        This keeps old snapshots useful after an identity resolver changes. Only
        relations produced by the profile backfill are considered, so manually
        authored graph edges remain untouched.
        """

        if self.relation_write_service is None:
            return {"scanned": 0, "refreshed": 0}
        targets = {
            str(item).strip()
            for item in (person_ids or [])
            if str(item).strip()
        }
        if not targets:
            try:
                rows = self.metadata_store.get_relations(include_inactive=False)
            except Exception:
                rows = []
            for relation in rows:
                metadata = coerce_metadata_dict(relation.get("metadata"))
                if str(metadata.get("source_type", "") or "").strip() != "person_fact_relation":
                    continue
                person_id = str(metadata.get("person_id", "") or "").strip()
                if person_id:
                    targets.add(person_id)
        refreshed = 0
        for person_id in sorted(targets):
            aliases, primary_name, _ = self.get_person_aliases(person_id)
            fact_claims = self._collect_person_fact_claims(person_id, limit=64)
            await self._backfill_explicit_fact_relations(
                person_id=person_id,
                primary_name=primary_name,
                person_aliases=aliases,
                fact_claims=fact_claims,
            )
            refreshed += 1
        return {"scanned": len(targets), "refreshed": refreshed}

    def _merge_fact_claim_buckets(
        self,
        classified_buckets: Dict[str, List[str]],
        fact_claims: List[Dict[str, Any]],
    ) -> Dict[str, List[str]]:
        """将账本事实置于分类证据之前，保证 top-k 抖动不能挤出稳定事实。"""

        merged: Dict[str, List[str]] = {key: [] for key in classified_buckets}
        for claim in fact_claims:
            section = str(claim.get("profile_section", "stable_facts") or "stable_facts").strip()
            if section not in merged:
                raise ValueError(f"事实 claim 使用了未知画像段落: {section}")
            raw_text = str(claim.get("value_text", "") or "").strip()
            if not raw_text:
                continue

            authority = str(claim.get("authority", "") or "").strip().casefold()
            stability = str(claim.get("stability", "") or "").strip().casefold()
            trusted = authority in {"manual", "direct_user", "imported"} and stability == "stable"
            for text in self._split_profile_evidence(raw_text):
                inferred = self._guess_profile_bucket(text)
                target = section
                # Older and model-derived person facts are stored in
                # ``uncertain_notes``. Keep that uncertainty visible while
                # still placing explicit identity/relationship statements in
                # the section where the console and prompt can use them.
                if section in {"uncertain_notes", "stable_facts"} and inferred != "stable_facts":
                    target = inferred
                if target in {"identity_settings", "relationship_settings", "interaction_preferences"} and not trusted:
                    text = f"待确认：{text}"
                self._append_profile_bucket(merged, target, text)
        for section, values in classified_buckets.items():
            for value in values:
                self._append_profile_bucket(merged, section, value)
        return merged

    @staticmethod
    def _split_profile_evidence(text: str) -> List[str]:
        """把一条较长的记忆拆成可读的画像要点。"""

        normalized = re.sub(r"[\r\n]+", " ", str(text or "")).strip()
        if not normalized:
            return []
        parts = [item.strip(" -") for item in re.split(r"(?<=[。！？；!?;])\s*", normalized) if item.strip(" -")]
        return parts[:8] or [normalized[:320]]

    def _confine_untrusted_profile_buckets(
        self,
        classified_buckets: Dict[str, List[str]],
    ) -> Dict[str, List[str]]:
        """禁止模型分类结果直接成为稳定画像真相，同时保留可审阅的语义位置。"""

        confined: Dict[str, List[str]] = {key: [] for key in classified_buckets}
        for section, values in classified_buckets.items():
            for value in values:
                target = section if section in {"recent_interactions", "uncertain_notes"} else "uncertain_notes"
                text = str(value or "").strip()
                inferred = self._guess_profile_bucket(text)
                if section in {"identity_settings", "relationship_settings", "interaction_preferences"}:
                    target = section
                    text = f"待确认：{text}"
                elif section == "stable_facts" and inferred in {
                    "identity_settings",
                    "relationship_settings",
                    "interaction_preferences",
                }:
                    target = inferred
                    text = f"待确认：{text}"
                self._append_profile_bucket(confined, target, text)
        return confined

    @staticmethod
    def _list_tokens(value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, (list, tuple, set)):
            return [str(item or "").strip() for item in value if str(item or "").strip()]
        token = str(value or "").strip()
        return [token] if token else []

    def _is_evidence_bound_to_person(
        self,
        item: Dict[str, Any],
        *,
        person_id: str,
    ) -> bool:
        """画像证据必须显式绑定到 person_id，避免别名全局召回串人。"""
        pid = str(person_id or "").strip()
        if not pid:
            return False

        metadata = coerce_metadata_dict(item.get("metadata"))
        source = str(item.get("source", "") or metadata.get("source", "") or "").strip()
        if source == f"person_fact:{pid}":
            return True

        if str(metadata.get("person_id", "") or "").strip() == pid:
            return True
        if pid in self._list_tokens(metadata.get("person_ids")):
            return True

        return False

    @staticmethod
    def _source_type_from_source(source: str) -> str:
        token = str(source or "").strip()
        if token.startswith("chat_summary:"):
            return "chat_summary"
        if token.startswith("person_fact:"):
            return "person_fact"
        return ""

    def _enrich_paragraph_evidence_metadata(
        self,
        paragraph_hash: str,
        metadata: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], str]:
        merged = coerce_metadata_dict(metadata)
        source = str(merged.get("source", "") or "").strip()
        try:
            paragraph = self.metadata_store.get_paragraph(paragraph_hash)
        except Exception:
            paragraph = None
        if isinstance(paragraph, dict):
            paragraph_metadata = coerce_metadata_dict(paragraph.get("metadata"))
            if paragraph_metadata:
                merged = {**paragraph_metadata, **merged}
            source = source or str(paragraph.get("source", "") or "").strip()
        source_type = str(merged.get("source_type", "") or "").strip() or self._source_type_from_source(source)
        if source_type:
            merged["source_type"] = source_type
        if source:
            merged["source"] = source
        return merged, source

    @staticmethod
    def _is_chat_summary_evidence(item: Dict[str, Any]) -> bool:
        metadata = item.get("metadata", {}) if isinstance(item.get("metadata"), dict) else {}
        source_type = str(metadata.get("source_type", "") or "").strip()
        source = str(item.get("source", "") or metadata.get("source", "") or "").strip()
        return source_type == "chat_summary" or source.startswith("chat_summary:")

    def _filter_stale_paragraph_evidence(
        self,
        evidence: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        memory_cfg = global_config.a_memorix.integration
        if not bool(getattr(memory_cfg, "feedback_correction_paragraph_hard_filter_enabled", True)):
            return evidence
        paragraph_hashes = [
            str(item.get("hash", "") or "").strip()
            for item in evidence
            if str(item.get("type", "") or "").strip() == "paragraph" and str(item.get("hash", "") or "").strip()
        ]
        if not paragraph_hashes:
            return evidence

        marks_by_paragraph = self.metadata_store.get_paragraph_stale_relation_marks_batch(paragraph_hashes)
        relation_hashes: List[str] = []
        seen = set()
        for marks in marks_by_paragraph.values():
            for mark in marks:
                relation_hash = str(mark.get("relation_hash", "") or "").strip()
                if not relation_hash or relation_hash in seen:
                    continue
                seen.add(relation_hash)
                relation_hashes.append(relation_hash)
        status_map = self.metadata_store.get_relation_status_batch(relation_hashes) if relation_hashes else {}

        filtered: List[Dict[str, Any]] = []
        for item in evidence:
            item_type = str(item.get("type", "") or "").strip()
            item_hash = str(item.get("hash", "") or "").strip()
            if item_type != "paragraph" or not item_hash:
                filtered.append(item)
                continue
            marks = marks_by_paragraph.get(item_hash, [])
            should_hide = any(
                status_map.get(str(mark.get("relation_hash", "") or "").strip()) is None
                or bool((status_map.get(str(mark.get("relation_hash", "") or "").strip()) or {}).get("is_inactive"))
                for mark in marks
                if str(mark.get("relation_hash", "") or "").strip()
            )
            if should_hide:
                continue
            filtered.append(item)
        return filtered

    async def _collect_vector_evidence(
        self,
        aliases: List[str],
        top_k: int = 12,
        person_id: str = "",
    ) -> List[Dict[str, Any]]:
        alias_queries = [a for a in aliases if a]
        if not alias_queries and not person_id:
            return []

        if self.retriever is None:
            # 回退：无检索器时只做简单内容匹配
            fallback: List[Dict[str, Any]] = []
            seen_hash = set()
            for alias in alias_queries:
                for para in self.metadata_store.search_paragraphs_by_content(alias)[: max(2, top_k // 2)]:
                    h = str(para.get("hash", ""))
                    if not h or h in seen_hash:
                        continue
                    seen_hash.add(h)
                    fallback.append(
                        {
                            "hash": h,
                            "type": "paragraph",
                            "score": 0.0,
                            "content": str(para.get("content", ""))[:180],
                            "source": str(para.get("source", "") or ""),
                            "metadata": coerce_metadata_dict(para.get("metadata")),
                        }
                    )
                    if not self._is_evidence_bound_to_person(fallback[-1], person_id=person_id):
                        fallback.pop()
            return self._filter_stale_paragraph_evidence(fallback[:top_k])

        per_alias_top_k = max(2, int(top_k / max(1, len(alias_queries))))
        seen_hash = set()
        evidence: List[Dict[str, Any]] = []
        for item in self._collect_person_fact_evidence(person_id, limit=max(2, min(4, top_k))):
            h = str(item.get("hash", "") or "")
            if not h or h in seen_hash:
                continue
            seen_hash.add(h)
            evidence.append(item)

        for alias in alias_queries:
            try:
                results = await self.retriever.retrieve(alias, top_k=per_alias_top_k)
            except Exception as e:
                logger.warning(f"向量证据召回失败: alias={alias}, err={e}")
                continue
            for item in results:
                h = str(item.hash_value or "")
                if not h or h in seen_hash:
                    continue
                metadata, source = self._enrich_paragraph_evidence_metadata(
                    h,
                    coerce_metadata_dict(item.metadata),
                )
                payload = {
                    "hash": h,
                    "type": str(item.result_type),
                    "score": float(item.score or 0.0),
                    "content": str(item.content or "")[:220],
                    "source": source,
                    "metadata": metadata,
                }
                if not self._is_evidence_bound_to_person(payload, person_id=person_id):
                    continue
                seen_hash.add(h)
                evidence.append(payload)
        evidence.sort(key=lambda x: x.get("score", 0.0), reverse=True)
        return self._filter_stale_paragraph_evidence(evidence[:top_k])

    def _build_profile_text(
        self,
        person_id: str,
        primary_name: str,
        aliases: List[str],
        relation_edges: List[Dict[str, Any]],
        vector_evidence: List[Dict[str, Any]],
        memory_traits: List[str],
        classified_buckets: Optional[Dict[str, List[str]]] = None,
    ) -> str:
        """基于证据构建画像文本（供 LLM 上下文注入）。"""
        buckets = classified_buckets or self._classify_profile_evidence_rule_based(
            relation_edges=relation_edges,
            vector_evidence=vector_evidence,
            memory_traits=memory_traits,
        )
        return build_structured_profile_text(
            person_id=person_id,
            primary_name=primary_name,
            aliases=aliases[:8],
            identity_settings=buckets.get("identity_settings", []),
            relationship_settings=buckets.get("relationship_settings", []),
            stable_facts=buckets.get("stable_facts", []),
            interaction_preferences=buckets.get("interaction_preferences", []),
            recent_interactions=buckets.get("recent_interactions", []),
            uncertain_notes=buckets.get("uncertain_notes", []),
        )

    def _classify_profile_evidence_rule_based(
        self,
        *,
        relation_edges: List[Dict[str, Any]],
        vector_evidence: List[Dict[str, Any]],
        memory_traits: List[str],
    ) -> Dict[str, List[str]]:
        """规则分桶画像证据，作为 LLM 不可用时的稳定回退。"""
        buckets: Dict[str, List[str]] = {
            "identity_settings": [],
            "relationship_settings": [],
            "stable_facts": [],
            "interaction_preferences": [],
            "recent_interactions": [],
            "uncertain_notes": [],
        }
        for trait in memory_traits[:6]:
            text = str(trait or "").strip()
            if text:
                self._append_profile_bucket(buckets, self._guess_profile_bucket(text), text)

        for rel in relation_edges[:8]:
            text = self._format_relation_evidence_text(rel)
            if not text:
                continue
            bucket = self._guess_profile_bucket(text)
            if bucket == "stable_facts":
                bucket = "relationship_settings"
            self._append_profile_bucket(buckets, bucket, text)

        for item in vector_evidence:
            content = str(item.get("content", "") or "").strip()
            if not content:
                continue
            if self._is_chat_summary_evidence(item):
                self._append_profile_bucket(buckets, "recent_interactions", content)
                continue
            self._append_profile_bucket(buckets, self._guess_profile_bucket(content), content)
        return buckets

    async def _classify_profile_evidence(
        self,
        *,
        person_id: str,
        primary_name: str,
        aliases: List[str],
        relation_edges: List[Dict[str, Any]],
        vector_evidence: List[Dict[str, Any]],
        memory_traits: List[str],
    ) -> Dict[str, List[str]]:
        """用 LLM 辅助证据分桶，失败时返回规则结果。"""
        fallback = self._classify_profile_evidence_rule_based(
            relation_edges=relation_edges,
            vector_evidence=vector_evidence,
            memory_traits=memory_traits,
        )
        candidates = self._build_profile_classification_candidates(
            relation_edges=relation_edges,
            vector_evidence=vector_evidence,
            memory_traits=memory_traits,
        )
        if not candidates:
            return fallback

        model = self._resolve_profile_classification_model()
        if model is None:
            return fallback

        prompt = self._build_profile_classification_prompt(
            person_id=person_id,
            primary_name=primary_name,
            aliases=aliases,
            candidates=candidates,
        )
        try:
            result = await generate_with_resolved_model(
                model,
                PROFILE_CLASSIFICATION_REQUEST_TYPE,
                prompt,
                temperature=0.1,
                max_tokens=self._profile_classification_max_tokens(),
            )
        except Exception as exc:
            logger.debug(f"人物画像证据分类模型调用失败: person_id={person_id}, err={exc}")
            return fallback
        if not bool(getattr(result, "success", False)):
            return fallback
        response = str(getattr(getattr(result, "completion", None), "response", "") or "").strip()
        parsed = self._parse_profile_classification_response(response)
        if not parsed:
            return fallback
        return self._merge_profile_classification(fallback, parsed)

    def _resolve_profile_classification_model(self) -> Optional[ResolvedLLMModel]:
        try:
            available_tasks = get_text_generation_model_tasks(llm_api)
            task_name, task_config = pick_text_generation_task(
                available_tasks,
                preferred=("memory", "utils", "planner", "tool_use", "replyer"),
            )
            if not task_name or task_config is None:
                return None
            return ResolvedLLMModel(task_name=task_name, task_config=task_config)
        except Exception as exc:
            logger.debug(f"解析人物画像分类模型失败: {exc}")
            return None

    @staticmethod
    def _build_profile_classification_prompt(
        *,
        person_id: str,
        primary_name: str,
        aliases: List[str],
        candidates: List[Dict[str, str]],
    ) -> str:
        return (
            "你要把人物画像证据归类到固定段落。只根据证据归类，不要编造。\n"
            f"人物ID: {person_id}\n"
            f"主称呼: {primary_name}\n"
            f"别名: {json.dumps(aliases, ensure_ascii=False)}\n\n"
            "分类定义：\n"
            "- identity_settings: 稳定身份、角色、长期自我描述、重要背景。\n"
            "- relationship_settings: 与麦麦、群友、组织、作品角色等长期关系或称呼关系。\n"
            "- stable_facts: 长期稳定、证据明确的人物事实。\n"
            "- interaction_preferences: 互动偏好、雷点、沟通习惯、喜欢/讨厌的相处方式。\n"
            "- recent_interactions: 最近发生、对当前聊天有帮助但不应上升为长期事实的内容。\n"
            "- uncertain_notes: 证据不足、推测、玩笑、自嘲、临时状态或疑似偏好。\n\n"
            "要求：\n"
            "1. 每条内容必须是简短中文陈述句。\n"
            "2. 不要输出证据编号、hash 或置信度。\n"
            "3. chat_summary 来源通常只能归入 recent_interactions 或 uncertain_notes。\n"
            "4. 临时状态、计划、可能、似乎、玩笑类内容不能归入 stable_facts。\n"
            "5. 只输出 JSON 对象，键为上述六类，值为字符串数组。\n\n"
            f"证据列表：\n{json.dumps(candidates, ensure_ascii=False, indent=2)}"
        )

    @staticmethod
    def _parse_profile_classification_response(raw: str) -> Dict[str, List[str]]:
        text = str(raw or "").strip()
        if not text:
            return {}
        try:
            repaired = repair_json(text)
            payload = json.loads(repaired) if isinstance(repaired, str) else repaired
        except Exception:
            return {}
        if not isinstance(payload, dict):
            return {}
        allowed_keys = (
            "identity_settings",
            "relationship_settings",
            "stable_facts",
            "interaction_preferences",
            "recent_interactions",
            "uncertain_notes",
        )
        parsed: Dict[str, List[str]] = {key: [] for key in allowed_keys}
        for key in allowed_keys:
            values = payload.get(key)
            if not isinstance(values, list):
                continue
            parsed[key] = [str(item or "").strip() for item in values if str(item or "").strip()]
        return parsed

    def _merge_profile_classification(
        self,
        fallback: Dict[str, List[str]],
        llm_result: Dict[str, List[str]],
    ) -> Dict[str, List[str]]:
        buckets: Dict[str, List[str]] = {key: [] for key in fallback}
        for key in buckets:
            source_values = llm_result.get(key) or fallback.get(key) or []
            for value in source_values:
                target_key = key
                if key == "stable_facts" and self._looks_uncertain_or_temporary(value):
                    target_key = "uncertain_notes"
                self._append_profile_bucket(buckets, target_key, value)
        return buckets

    def _build_profile_classification_candidates(
        self,
        *,
        relation_edges: List[Dict[str, Any]],
        vector_evidence: List[Dict[str, Any]],
        memory_traits: List[str],
    ) -> List[Dict[str, str]]:
        candidates: List[Dict[str, str]] = []
        for index, trait in enumerate(memory_traits[:8], start=1):
            text = str(trait or "").strip()
            if text:
                candidates.append({"id": f"trait-{index}", "source_type": "memory_trait", "text": text})
        for index, rel in enumerate(relation_edges[:12], start=1):
            text = self._format_relation_evidence_text(rel)
            if text:
                candidates.append({"id": f"relation-{index}", "source_type": "relation", "text": text})
        for index, item in enumerate(vector_evidence[:16], start=1):
            content = str(item.get("content", "") or "").strip()
            if not content:
                continue
            metadata = coerce_metadata_dict(item.get("metadata"))
            source_type = str(metadata.get("source_type", "") or "").strip()
            if not source_type:
                source_type = "chat_summary" if self._is_chat_summary_evidence(item) else "person_fact"
            candidates.append({"id": f"evidence-{index}", "source_type": source_type, "text": content[:260]})
        return candidates

    @staticmethod
    def _append_profile_bucket(buckets: Dict[str, List[str]], bucket: str, text: str) -> None:
        clean = str(text or "").strip().strip("- ")
        if not clean:
            return
        values = buckets.setdefault(bucket, [])
        if clean not in values:
            values.append(clean)

    @staticmethod
    def _format_relation_evidence_text(rel: Dict[str, Any]) -> str:
        subject = str(rel.get("subject", "") or "").strip()
        predicate = str(rel.get("predicate", "") or "").strip()
        obj = str(rel.get("object", "") or "").strip()
        if not (subject and predicate and obj):
            return ""
        return f"{subject}{predicate}{obj}。"

    @classmethod
    def _guess_profile_bucket(cls, text: str) -> str:
        content = str(text or "").strip()
        if not content:
            return "stable_facts"
        if any(
            token in content
            for token in (
                "关系",
                "朋友",
                "同事",
                "群友",
                "主人",
                "搭档",
                "称呼",
                "叫我",
                "叫做",
                "叫作",
                "认识",
                "同型号",
                "同款",
                "亲属",
                "兄弟",
                "姐妹",
                "家人",
            )
        ) or re.search(
            r"(?:与|和|跟)[^，。！？；!?;]{0,24}(?:是|为|属于|同型号|同款|朋友|搭档|主人|同事|群友|亲属|兄弟|姐妹|家人)",
            content,
        ):
            return "relationship_settings"
        if any(
            token in content
            for token in (
                "身份",
                "职业",
                "工作",
                "学生",
                "老师",
                "作者",
                "画师",
                "设定",
                "角色",
                "来自",
                "系统",
                "操作系统",
                "发行版",
                "内核",
                "主机",
                "电脑",
                "设备",
                "用户名",
                "用户ID",
                "昵称",
                "QQ",
                "型号",
                "机娘",
                "AI",
                "计算机",
                "窗口管理器",
                "shell",
                "终端",
            )
        ):
            return "identity_settings"
        if any(
            token in content for token in ("喜欢", "讨厌", "偏好", "习惯", "不喜欢", "希望", "雷点", "介意", "更愿意")
        ):
            return "interaction_preferences"
        if cls._looks_uncertain_or_temporary(content):
            return "uncertain_notes"
        return "stable_facts"

    @staticmethod
    def _looks_uncertain_or_temporary(text: str) -> bool:
        content = str(text or "").strip()
        if not content:
            return False
        markers = (
            "可能",
            "似乎",
            "好像",
            "大概",
            "也许",
            "疑似",
            "暂时",
            "今天",
            "现在",
            "刚刚",
            "最近",
            "计划",
            "打算",
            "玩笑",
            "自嘲",
            "临时",
        )
        return any(marker in content for marker in markers)

    def _snapshot_needs_profile_regeneration(
        self,
        snapshot: Optional[Dict[str, Any]],
        person_id: str,
    ) -> bool:
        """检测旧快照是否还没有投影身份/关系事实。"""

        if not snapshot:
            return True
        # Identity and relation normalization is part of the profile input.
        # Older snapshots may still expose the canonical ID or unresolved
        # pronouns as graph endpoints; force one refresh so the backfill can
        # repair those edges instead of serving the stale cache indefinitely.
        try:
            aliases, primary_name, _ = self.get_person_aliases(person_id)
        except Exception:
            aliases, primary_name = [], str(person_id or "").strip()
        snapshot_aliases = {
            str(item).strip().casefold()
            for item in (snapshot.get("aliases") or [])
            if str(item).strip()
        }
        expected_aliases = {
            str(item).strip().casefold()
            for item in (aliases or [primary_name])
            if str(item).strip()
        }
        if expected_aliases and not expected_aliases.issubset(snapshot_aliases):
            return True
        for relation in snapshot.get("relation_edges") or []:
            if not isinstance(relation, dict):
                continue
            endpoints = {
                str(relation.get("subject", "") or "").strip().casefold(),
                str(relation.get("object", "") or "").strip().casefold(),
            }
            if str(person_id or "").strip().casefold() in endpoints or endpoints.intersection(
                {"他", "她", "它", "对方", "某人"}
            ):
                return True
        sections = parse_profile_sections(str(snapshot.get("profile_text", "") or ""))
        if not sections:
            return True
        if not section_is_empty(sections.get("身份设定", [])) and not section_is_empty(
            sections.get("关系设定", [])
        ):
            return False
        for claim in self._collect_person_fact_claims(person_id, limit=64):
            for statement in self._split_profile_evidence(str(claim.get("value_text", "") or "")):
                if self._guess_profile_bucket(statement) in {"identity_settings", "relationship_settings"}:
                    return True
        return False

    @staticmethod
    def _is_snapshot_stale(snapshot: Optional[Dict[str, Any]], ttl_seconds: float) -> bool:
        if not snapshot:
            return True
        now = time.time()
        expires_at = snapshot.get("expires_at")
        if expires_at is not None:
            try:
                return now >= float(expires_at)
            except Exception:
                return True
        updated_at = float(snapshot.get("updated_at") or 0.0)
        return (now - updated_at) >= ttl_seconds

    def _apply_manual_override(self, person_id: str, profile_payload: Dict[str, Any]) -> Dict[str, Any]:
        """将手工覆盖并入画像结果（覆盖 profile_text，同时保留 auto_profile_text）。"""
        payload = dict(profile_payload or {})
        auto_text = str(payload.get("profile_text", "") or "")
        payload["auto_profile_text"] = auto_text
        payload["has_manual_override"] = False
        payload["manual_override_text"] = ""
        payload["override_updated_at"] = None
        payload["override_updated_by"] = ""
        payload["profile_source"] = "auto_snapshot"

        if not person_id or self.metadata_store is None:
            return payload

        try:
            override = self.metadata_store.get_person_profile_override(person_id)
        except Exception as e:
            logger.warning(f"读取人物画像手工覆盖失败: person_id={person_id}, err={e}")
            return payload

        if not override:
            return payload

        manual_text = str(override.get("override_text", "") or "").strip()
        if not manual_text:
            return payload

        payload["has_manual_override"] = True
        payload["manual_override_text"] = manual_text
        payload["override_updated_at"] = override.get("updated_at")
        payload["override_updated_by"] = str(override.get("updated_by", "") or "")
        payload["profile_text"] = manual_text
        payload["profile_source"] = "manual_override"
        return payload

    async def query_person_profile(
        self,
        person_id: str = "",
        person_keyword: str = "",
        top_k: int = 12,
        ttl_seconds: float = 6 * 3600,
        force_refresh: bool = False,
        source_note: str = "",
    ) -> Dict[str, Any]:
        """查询或刷新人物画像。"""
        pid = str(person_id or "").strip()
        if not pid and person_keyword:
            pid = self.resolve_person_id(person_keyword)

        if not pid:
            return {
                "success": False,
                "error": "person_id 无效，且未能通过别名解析",
            }

        latest = self.metadata_store.get_latest_person_profile_snapshot(pid)
        if (
            not force_refresh
            and not self._is_snapshot_stale(latest, ttl_seconds)
            and not self._snapshot_needs_profile_regeneration(latest, pid)
        ):
            aliases, primary_name, _ = self.get_person_aliases(pid)
            payload = {
                "success": True,
                "person_id": pid,
                "person_name": primary_name,
                "from_cache": True,
                **(latest or {}),
            }
            if aliases and not payload.get("aliases"):
                payload["aliases"] = aliases
            return {
                **self._apply_manual_override(pid, payload),
            }

        aliases, primary_name, memory_traits = self.get_person_aliases(pid)
        if not aliases and person_keyword:
            aliases = [person_keyword.strip()]
            primary_name = person_keyword.strip()
        fact_claims = self._collect_person_fact_claims(pid, limit=max(32, top_k * 8))
        await self._backfill_explicit_fact_relations(
            person_id=pid,
            primary_name=primary_name,
            fact_claims=fact_claims,
            person_aliases=aliases,
        )
        relation_edges = self._collect_relation_evidence(aliases, limit=max(10, top_k * 2), person_id=pid)
        vector_evidence = await self._collect_vector_evidence(aliases, top_k=max(4, top_k), person_id=pid)
        evidence_fingerprint = self._profile_evidence_fingerprint(
            person_id=pid,
            primary_name=primary_name,
            aliases=aliases,
            relation_edges=relation_edges,
            vector_evidence=vector_evidence,
            memory_traits=memory_traits,
            fact_claims=fact_claims,
        )
        expires_at = time.time() + float(ttl_seconds) if ttl_seconds > 0 else None
        if latest and str(latest.get("evidence_fingerprint", "")) == evidence_fingerprint:
            snapshot_id = latest.get("snapshot_id")
            if snapshot_id is None:
                raise RuntimeError("人物画像快照缺少 snapshot_id，无法执行证据版本短路")
            refresh_note = source_note if source_note else str(latest.get("source_note", ""))
            refreshed = self.metadata_store.refresh_person_profile_snapshot_cache(
                int(snapshot_id),
                expires_at=expires_at,
                source_note=refresh_note,
            )
            payload = {
                "success": True,
                "person_id": pid,
                "person_name": primary_name,
                "from_cache": True,
                "evidence_unchanged": True,
                **refreshed,
            }
            if aliases and not payload.get("aliases"):
                payload["aliases"] = aliases
            return self._apply_manual_override(pid, payload)

        unstructured_vector_evidence = [
            item
            for item in vector_evidence
            if not self._source_type_from_source(str(item.get("source", ""))) == "person_fact"
        ]
        classified_buckets = await self._classify_profile_evidence(
            person_id=pid,
            primary_name=primary_name,
            aliases=aliases,
            relation_edges=relation_edges,
            vector_evidence=unstructured_vector_evidence,
            memory_traits=memory_traits,
        )
        classified_buckets = self._confine_untrusted_profile_buckets(classified_buckets)
        classified_buckets = self._merge_fact_claim_buckets(classified_buckets, fact_claims)

        evidence_ids = [
            str(item.get("hash", ""))
            for item in (relation_edges + vector_evidence)
            if str(item.get("hash", "")).strip()
        ]
        dedup_ids: List[str] = []
        seen = set()
        for item in evidence_ids:
            if item in seen:
                continue
            seen.add(item)
            dedup_ids.append(item)

        profile_text = self._build_profile_text(
            person_id=pid,
            primary_name=primary_name,
            aliases=aliases,
            relation_edges=relation_edges,
            vector_evidence=vector_evidence,
            memory_traits=memory_traits,
            classified_buckets=classified_buckets,
        )

        snapshot = self.metadata_store.upsert_person_profile_snapshot(
            person_id=pid,
            profile_text=profile_text,
            aliases=aliases,
            relation_edges=relation_edges,
            vector_evidence=vector_evidence,
            evidence_ids=dedup_ids,
            fact_claim_ids=[str(item.get("claim_id", "")) for item in fact_claims],
            evidence_fingerprint=evidence_fingerprint,
            expires_at=expires_at,
            source_note=source_note,
        )
        payload = {
            "success": True,
            "person_id": pid,
            "person_name": primary_name,
            "from_cache": False,
            **snapshot,
        }
        return {
            **self._apply_manual_override(pid, payload),
        }

    @staticmethod
    def format_persona_profile_block(profile: Dict[str, Any]) -> str:
        """格式化给 replyer 的注入块。"""
        if not profile or not profile.get("success"):
            return ""
        text = str(profile.get("profile_text", "") or "").strip()
        if not text:
            return ""
        text = build_profile_injection_text(text)
        if not text:
            return ""
        return f"【人物画像-内部参考】\n{text}\n仅供内部推理，不要向用户逐字复述。"
