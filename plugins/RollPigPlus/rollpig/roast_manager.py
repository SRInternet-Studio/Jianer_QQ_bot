from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List
from urllib.parse import unquote, urljoin, urlparse

import httpx
from ..host import localstore as store
from ..host import logger
from openai import AsyncOpenAI

from .config import Config, plugin_config

# 数据文件
ROAST_LIB_FILE = store.get_plugin_data_file("roast_library.json")
ROAST_SOURCES_FILE = store.get_plugin_data_file("roast_library_sources.json")
ROAST_CACHE_DIR = store.get_plugin_cache_dir() / "shared_roast_library"
ROAST_SNAPSHOT_FILE = ROAST_CACHE_DIR / "shared_roast_snapshot.json"
ROAST_STATE_FILE = ROAST_CACHE_DIR / "shared_roast_state.json"

OFFICIAL_ROAST_LIBRARY_MANIFEST_URL = "https://pig.felislab.cc/resources/rollpig-roasts/manifest.json"
ROLLPIG_PLUS_SHARED_LIBRARY_VERSION = "0.10.0"
ROAST_MANIFEST_MAX_SIZE = 64 * 1024
ROAST_LIBRARY_MAX_SIZE = 5 * 1024 * 1024
ROAST_MAX_ORIGINS = 1_000
ROAST_MAX_PAIRS = 20_000
ROAST_MAX_TEXTS = 30_000
ROAST_MAX_TEXTS_PER_PAIR = 20
ROAST_MAX_TEXT_LENGTH = 600
ROAST_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
ROAST_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ROAST_VERSION_PATTERN = re.compile(r"^roasts-\d{4}-\d{2}-\d{2}\.\d+$")
ROAST_PLACEHOLDER_PATTERN = re.compile(r"\{([^{}]*)\}")
ROAST_SAFE_PLACEHOLDER_PATTERN = re.compile(r"\{\s*(k|v|origin|food)\s*\}", re.IGNORECASE)
ROAST_CONTROL_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

SOURCE_LOCAL = 1
SOURCE_SHARED = 2


@dataclass(frozen=True)
class RoastLibrarySyncResult:
    """描述一次共享文案同步结果，供后台日志和管理员命令统一排版。"""

    updated: bool
    skipped: bool
    resource_version: str = ""
    added: int = 0
    removed: int = 0
    local_count: int = 0
    message: str = ""


def _normalize_roast_text(text: str) -> str:
    """标准化文案占位符与空白，用于生成去重哈希。"""

    normalized = text.strip()
    quote_pairs = {('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’")}
    if len(normalized) >= 2 and (normalized[0], normalized[-1]) in quote_pairs:
        normalized = normalized[1:-1].strip()
    normalized = re.sub(r"\s+", " ", normalized)
    return ROAST_SAFE_PLACEHOLDER_PATTERN.sub(
        lambda match: "{" + match.group(1).lower() + "}",
        normalized,
    )


def _roast_text_identity(text: str) -> str:
    return hashlib.sha256(_normalize_roast_text(text).encode("utf-8")).hexdigest()


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """解析 JSON 时检查重复键。"""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON 对象存在重复字段: {key}")
        result[key] = value
    return result


def _copy_library(library: dict[str, dict[str, list[str]]]) -> dict[str, dict[str, list[str]]]:
    return {
        origin_id: {target_id: list(texts) for target_id, texts in targets.items()}
        for origin_id, targets in library.items()
    }


def _safe_roast_library(data: Any) -> dict[str, dict[str, list[str]]]:
    """读取并校验本地文案库基本结构。"""

    if not isinstance(data, dict):
        raise ValueError("顶层必须是 object")
    result: dict[str, dict[str, list[str]]] = {}
    for raw_origin_id, raw_targets in data.items():
        if not isinstance(raw_origin_id, str) or not isinstance(raw_targets, dict):
            raise ValueError("文案库层级或 ID 类型非法")
        target_result: dict[str, list[str]] = {}
        for raw_target_id, raw_texts in raw_targets.items():
            if not isinstance(raw_target_id, str) or not isinstance(raw_texts, list):
                raise ValueError(f"文案库组合结构非法: {raw_origin_id}/{raw_target_id}")
            if not all(isinstance(text, str) for text in raw_texts):
                raise ValueError(f"文案库包含非字符串内容: {raw_origin_id}/{raw_target_id}")
            target_result[raw_target_id] = list(raw_texts)
        result[raw_origin_id] = target_result
    return result


def _validate_shared_library(data: Any) -> tuple[dict[str, dict[str, list[str]]], dict[str, int]]:
    """校验远端共享文案库快照格式与安全限制。"""

    if not isinstance(data, dict):
        raise ValueError("共享文案库顶层必须是 object")
    if len(data) > ROAST_MAX_ORIGINS:
        raise ValueError(f"共享文案原始小猪数量超过上限: {len(data)}/{ROAST_MAX_ORIGINS}")

    result: dict[str, dict[str, list[str]]] = {}
    pair_count = 0
    text_count = 0
    for origin_id, targets in data.items():
        if not isinstance(origin_id, str) or ROAST_ID_PATTERN.fullmatch(origin_id) is None:
            raise ValueError(f"共享文案原始小猪 ID 非法: {origin_id!r}")
        if not isinstance(targets, dict):
            raise ValueError(f"共享文案第二层必须是 object: {origin_id}")
        pair_count += len(targets)
        if pair_count > ROAST_MAX_PAIRS:
            raise ValueError(f"共享文案组合数量超过上限: {pair_count}/{ROAST_MAX_PAIRS}")

        target_result: dict[str, list[str]] = {}
        for target_id, texts in targets.items():
            if not isinstance(target_id, str):
                raise ValueError(f"共享文案目标 ID 必须是字符串: {origin_id}")
            is_pvp = target_id.endswith("_pvp")
            food_id = target_id[:-4] if is_pvp else target_id
            if ROAST_ID_PATTERN.fullmatch(food_id) is None:
                raise ValueError(f"共享文案目标 ID 非法: {origin_id}/{target_id}")
            if not isinstance(texts, list):
                raise ValueError(f"共享文案组合必须是 list: {origin_id}/{target_id}")
            if len(texts) > ROAST_MAX_TEXTS_PER_PAIR:
                raise ValueError(
                    f"共享文案单组合数量超过上限: {origin_id}/{target_id} "
                    f"{len(texts)}/{ROAST_MAX_TEXTS_PER_PAIR}"
                )

            accepted: list[str] = []
            seen: set[str] = set()
            for index, text in enumerate(texts):
                if not isinstance(text, str) or not text.strip():
                    raise ValueError(f"共享文案必须是非空字符串: {origin_id}/{target_id}[{index}]")
                if len(text) > ROAST_MAX_TEXT_LENGTH:
                    raise ValueError(f"共享文案超过字符上限: {origin_id}/{target_id}[{index}]")
                if "\ufffd" in text or ROAST_CONTROL_PATTERN.search(text):
                    raise ValueError(f"共享文案包含乱码或控制字符: {origin_id}/{target_id}[{index}]")
                if text.count("{") != text.count("}") or "{" in re.sub(ROAST_PLACEHOLDER_PATTERN, "", text):
                    raise ValueError(f"共享文案包含损坏占位符: {origin_id}/{target_id}[{index}]")
                if "}" in re.sub(ROAST_PLACEHOLDER_PATTERN, "", text):
                    raise ValueError(f"共享文案包含损坏占位符: {origin_id}/{target_id}[{index}]")
                placeholders = {match.group(1) for match in ROAST_PLACEHOLDER_PATTERN.finditer(text)}
                if placeholders - {"k", "v", "origin", "food"}:
                    raise ValueError(f"共享文案包含未知占位符: {origin_id}/{target_id}[{index}]")
                if is_pvp and not {"k", "v"}.issubset(placeholders):
                    raise ValueError(f"共享 PvP 文案缺少 {{k}} 或 {{v}}: {origin_id}/{target_id}[{index}]")
                if not is_pvp and ({"k", "v"} & placeholders):
                    raise ValueError(f"共享普通文案错误包含 PvP 占位符: {origin_id}/{target_id}[{index}]")

                identity = _roast_text_identity(text)
                if identity in seen:
                    raise ValueError(f"共享文案组合内存在重复正文: {origin_id}/{target_id}[{index}]")
                seen.add(identity)
                accepted.append(text)
                text_count += 1
                if text_count > ROAST_MAX_TEXTS:
                    raise ValueError(f"共享文案总数超过上限: {text_count}/{ROAST_MAX_TEXTS}")
            if accepted:
                target_result[target_id] = accepted
        if target_result:
            result[origin_id] = target_result

    return result, {
        "origin_count": len(result),
        "pair_count": sum(len(targets) for targets in result.values()),
        "text_count": text_count,
    }


def _clamp_number(value: object, default: float, minimum: float, maximum: float) -> float:
    """把外部配置收敛到安全区间，避免极端值拖垮事件循环或 API 账单。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _clamp_int(value: object, default: int, minimum: int, maximum: int) -> int:
    return int(_clamp_number(value, default, minimum, maximum))


# ================================ DeepSeek V4 兼容 ================================ #
# DeepSeek 旧模型名会在 2026-07-24 弃用；这里在运行时兼容旧配置，
# 同时只对官方 API 注入 thinking 扩展字段，避免第三方 OpenAI 网关不兼容。
def _is_deepseek_official_base(base_url: str) -> bool:
    """仅对 DeepSeek 官方地址注入 V4 私有参数，避免破坏第三方 OpenAI 兼容网关。"""

    try:
        parsed = urlparse((base_url or "").strip())
    except Exception:
        return False
    return parsed.scheme in {"http", "https"} and parsed.netloc.lower() == "api.deepseek.com"


def _resolve_deepseek_model(model: str, base_url: str, *, warn: bool = True) -> tuple[str, dict | None]:
    """兼容 DeepSeek 旧模型名，并为 V4 Flash 短文案默认关闭思考模式。"""

    configured_model = (model or "").strip() or "deepseek-v4-flash"
    normalized_model = configured_model.lower()
    is_official = _is_deepseek_official_base(base_url)

    if normalized_model == "deepseek-chat":
        if not is_official:
            return configured_model, None
        if warn:
            logger.warning(
                "检测到旧 DeepSeek 模型名 deepseek-chat，已自动兼容为 deepseek-v4-flash 非思考模式；"
                "建议更新 rollpig_ai_model 配置。"
            )
        return "deepseek-v4-flash", {"thinking": {"type": "disabled"}}

    if normalized_model == "deepseek-reasoner":
        if not is_official:
            return configured_model, None
        if warn:
            logger.warning(
                "检测到旧 DeepSeek 模型名 deepseek-reasoner，已自动兼容为 deepseek-v4-flash 思考模式；"
                "AI 烤猪短文案建议改用 deepseek-v4-flash 非思考模式。"
            )
        return "deepseek-v4-flash", {"thinking": {"type": "enabled"}}

    if normalized_model == "deepseek-v4-flash":
        return configured_model, {"thinking": {"type": "disabled"}} if is_official else None

    return configured_model, None

# ================= 默认兜底文案模板 =================
DEFAULT_TEMPLATES = [
    "你本是一只无忧无虑的【{origin}】，却没能逃过命运的安排，含泪变成了【{food}】。",
    "看看你现在的样子！虽然不再是【{origin}】，但作为【{food}】的你，依然散发着诱人的光泽。",
]

BURNT_TEMPLATES = [
    "住手！它已经是一块【{origin}】了！在你无情的二次烧烤下，它彻底变成了黑漆漆的焦炭。",
    "你还不满足吗？这块可怜的【{origin}】已经被你烤得面目全非，化作了尘埃。",
]

PVP_TEMPLATES = [
    "【{k}】手法娴熟，手起刀落，将【{v}】（{origin}）做成了美味的【{food}】！",
    "【{v}】还没反应过来，就被【{k}】扔上了烤架。再见了，{origin}；你好，{food}。",
]

# 每个“原始猪 × 熟食 × 场景”最多主动积累 5 条本地 AI 文案。
# 这是停止继续生成的软目标，不会裁剪历史文件中已经超过目标的文案。
AI_TEXTS_PER_PAIR_TARGET = 5


def _extract_origin_feature(origin_pig: dict) -> str:
    """提取适合 AI 提示词的短特征，兼容 Overlay 省略可选文案字段。"""

    description = str(origin_pig.get("description") or "").strip()
    if description and len(description) <= 15:
        return description

    analysis = str(origin_pig.get("analysis") or "").strip()
    if analysis:
        return analysis[:20]

    pig_name = str(origin_pig.get("name") or "").strip()
    return pig_name[:20] or "一只普通小猪"


class RoastManager:
    def __init__(
        self,
        config: Config | None = None,
        *,
        library_file: Path | None = None,
        sources_file: Path | None = None,
        cache_dir: Path | None = None,
    ):
        """初始化 AI 与共享文案管理；可注入隔离路径供迁移测试使用。"""

        # 运行时不传则沿用 NoneBot 当前插件配置；路径参数只用于测试或显式迁移工具。
        active_config = config or plugin_config
        self.config = active_config
        self.file = library_file or ROAST_LIB_FILE
        self.sources_file = sources_file or ROAST_SOURCES_FILE
        self.cache_dir = cache_dir or ROAST_CACHE_DIR
        self.snapshot_file = self.cache_dir / ROAST_SNAPSHOT_FILE.name
        self.state_file = self.cache_dir / ROAST_STATE_FILE.name
        self.library: Dict[str, Dict[str, List[str]]] = self._load_local_library()
        self._source_index_valid = False
        self._source_flags = self._load_source_flags()
        self._source_flags = self._reconcile_source_flags(self.library, self._source_flags)
        self._local_text_pools: dict[tuple[str, str], tuple[str, ...]] = {}
        self._shared_text_pools: dict[tuple[str, str], tuple[str, ...]] = {}
        self._rebuild_text_pools()
        # AI 文案可能由多个群同时触发生成；保存文案库时必须串行化，
        # 否则未来改成线程写或多路保存时容易出现覆盖/半写风险。
        self._lock = asyncio.Lock()
        # 下载和校验阶段不占用正文写锁；只有最终合并时才与 AI 入库串行。
        self._shared_sync_lock = asyncio.Lock()
        
        self.client = None
        self.ai_timeout = _clamp_number(active_config.rollpig_ai_timeout, 20.0, 1.0, 60.0)
        self.ai_max_tokens = _clamp_int(active_config.rollpig_ai_max_tokens, 4096, 64, 4096)
        self.ai_output_max_chars = _clamp_int(active_config.rollpig_ai_output_max_chars, 240, 40, 600)
        self._ai_semaphore = asyncio.Semaphore(
            _clamp_int(active_config.rollpig_ai_concurrency, 4, 1, 6)
        )
        # ================================ 兼容旧版 AI 配置 ================================ #
        # AI 只有在“开关开启 + key 存在”时才会启用。
        # 新字段逐项优先；空字符串也是显式配置，不能重新启用旧 key。
        ai_key = getattr(active_config, "rollpig_ai_api_key", None)
        ai_base = getattr(active_config, "rollpig_ai_base_url", None)
        ai_model = getattr(active_config, "rollpig_ai_model", None)
        ai_key = active_config.rollpig_deepseek_key if ai_key is None else ai_key
        ai_base = active_config.rollpig_deepseek_base if ai_base is None else ai_base
        ai_model = active_config.rollpig_model if ai_model is None else ai_model
        self.ai_ready = bool(active_config.rollpig_ai_enabled and ai_key)
        self.ai_model, self.ai_extra_body = _resolve_deepseek_model(
            ai_model,
            ai_base,
            warn=self.ai_ready,
        )
        if self.ai_ready:
            self.client = AsyncOpenAI(
                api_key=ai_key,
                base_url=ai_base,
            )

    # ================================ 文案库持久化 ================================ #
    # 未知来源正文默认按本地处理，防止误删用户自定义文案。
    def _backup_corrupt_file(self, path: Path, label: str) -> None:
        """备份并隔离损坏的文件。"""

        if not path.exists():
            return
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.stem}.corrupt-{stamp}-{uuid.uuid4().hex[:8]}{path.suffix}")
        try:
            path.replace(backup)
            logger.warning(f"{label} 已隔离到: {backup}")
        except OSError as error:
            logger.error(f"{label} 损坏且无法隔离，已停止覆盖该文件: {error}")

    def _load_local_library(self) -> dict[str, dict[str, list[str]]]:
        if not self.file.is_file():
            return {}
        try:
            return _safe_roast_library(json.loads(self.file.read_text(encoding="utf-8-sig")))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            logger.warning(f"roast_library.json 读取失败，已使用空文案库兜底: {error}")
            self._backup_corrupt_file(self.file, "roast_library.json")
            return {}

    def _load_source_flags(self) -> dict[tuple[str, str], dict[str, int]]:
        if not self.sources_file.is_file():
            return {}
        try:
            payload = json.loads(self.sources_file.read_text(encoding="utf-8-sig"))
            if not isinstance(payload, dict) or payload.get("schema_version") != 1:
                raise ValueError("schema_version 当前只能为 1")
            raw_entries = payload.get("entries")
            if not isinstance(raw_entries, dict):
                raise ValueError("entries 必须是 object")

            result: dict[tuple[str, str], dict[str, int]] = {}
            for origin_id, raw_targets in raw_entries.items():
                if not isinstance(origin_id, str) or not isinstance(raw_targets, dict):
                    raise ValueError("来源索引第一、二层结构非法")
                for target_id, raw_hashes in raw_targets.items():
                    if not isinstance(target_id, str) or not isinstance(raw_hashes, dict):
                        raise ValueError(f"来源索引组合结构非法: {origin_id}/{target_id}")
                    flags_by_hash: dict[str, int] = {}
                    for identity, raw_flags in raw_hashes.items():
                        if ROAST_SHA256_PATTERN.fullmatch(str(identity)) is None or not isinstance(raw_flags, dict):
                            raise ValueError(f"来源索引条目非法: {origin_id}/{target_id}")
                        flags = 0
                        if raw_flags.get("local") is True:
                            flags |= SOURCE_LOCAL
                        if raw_flags.get("shared") is True:
                            flags |= SOURCE_SHARED
                        if flags:
                            flags_by_hash[str(identity)] = flags
                    if flags_by_hash:
                        result[(origin_id, target_id)] = flags_by_hash
            self._source_index_valid = True
            return result
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            logger.warning(f"roast_library_sources.json 读取失败，将当前正文保守标记为本地来源: {error}")
            self._backup_corrupt_file(self.sources_file, "roast_library_sources.json")
            return {}

    def _reconcile_source_flags(
        self,
        library: dict[str, dict[str, list[str]]],
        source_flags: dict[tuple[str, str], dict[str, int]],
    ) -> dict[tuple[str, str], dict[str, int]]:
        """丢弃无正文的旧标记，并把所有未标记正文视为用户本地内容。"""

        result: dict[tuple[str, str], dict[str, int]] = {}
        for origin_id, targets in library.items():
            for target_id, texts in targets.items():
                key = (origin_id, target_id)
                old_flags = source_flags.get(key, {})
                flags_by_hash: dict[str, int] = {}
                for text in texts:
                    identity = _roast_text_identity(text)
                    flags_by_hash[identity] = old_flags.get(identity, SOURCE_LOCAL) or SOURCE_LOCAL
                if flags_by_hash:
                    result[key] = flags_by_hash
        return result

    def _serialize_source_flags(
        self,
        source_flags: dict[tuple[str, str], dict[str, int]],
    ) -> dict[str, Any]:
        """只持久化带 shared 来源的条目；未登记正文天然按 local 处理。"""

        entries: dict[str, dict[str, dict[str, dict[str, bool]]]] = {}
        for (origin_id, target_id), flags_by_hash in sorted(source_flags.items()):
            serialized_hashes = {
                identity: {
                    "local": bool(flags & SOURCE_LOCAL),
                    "shared": True,
                }
                for identity, flags in sorted(flags_by_hash.items())
                if flags & SOURCE_SHARED
            }
            if serialized_hashes:
                entries.setdefault(origin_id, {})[target_id] = serialized_hashes
        return {"schema_version": 1, "entries": entries}

    def _write_json_temp(self, target: Path, payload: Any, token: str) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f".{target.name}.{token}.tmp")
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return temp

    def _commit_library_and_sources_sync(
        self,
        library: dict[str, dict[str, list[str]]],
        source_flags: dict[tuple[str, str], dict[str, int]],
    ) -> None:
        """原子保存文案库与来源索引。"""

        token = uuid.uuid4().hex
        library_temp: Path | None = None
        sources_temp: Path | None = None
        try:
            library_temp = self._write_json_temp(self.file, library, token)
            sources_temp = self._write_json_temp(
                self.sources_file,
                self._serialize_source_flags(source_flags),
                token,
            )
            sources_temp.replace(self.sources_file)
            sources_temp = None
            library_temp.replace(self.file)
            library_temp = None
        finally:
            for temp in (library_temp, sources_temp):
                if temp is not None:
                    temp.unlink(missing_ok=True)

    def _write_cache_json_sync(self, path: Path, payload: Any) -> None:
        token = uuid.uuid4().hex
        temp = self._write_json_temp(path, payload, token)
        try:
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)

    async def _save_new_text(self, origin_id: str, target_id: str, text: str):
        async with self._lock:
            library = _copy_library(self.library)
            source_flags = {
                key: dict(flags_by_hash)
                for key, flags_by_hash in self._source_flags.items()
            }
            identity = _roast_text_identity(text)
            current_texts = self.library.get(origin_id, {}).get(target_id, [])
            identity_already_exists = any(
                _roast_text_identity(current_text) == identity
                for current_text in current_texts
            )
            texts = library.setdefault(origin_id, {}).setdefault(target_id, [])
            # 正文仅空白或引号差异时不重复添加，仅标记本地来源
            if not identity_already_exists:
                texts.append(text)

            flags_by_hash = source_flags.setdefault((origin_id, target_id), {})
            previous_flags = flags_by_hash.get(identity, 0)
            flags_by_hash[identity] = previous_flags | SOURCE_LOCAL
            if identity_already_exists and previous_flags & SOURCE_LOCAL:
                return

            await asyncio.to_thread(self._commit_library_and_sources_sync, library, source_flags)
            self.library = library
            self._source_flags = source_flags
            self._source_index_valid = True
            self._rebuild_text_pools()

    def _rebuild_text_pools(self) -> None:
        """同步后一次性拆分本地/共享池，请求期不再扫描正文或计算哈希。"""

        local_pools: dict[tuple[str, str], tuple[str, ...]] = {}
        shared_pools: dict[tuple[str, str], tuple[str, ...]] = {}
        for origin_id, targets in self.library.items():
            for target_id, texts in targets.items():
                key = (origin_id, target_id)
                flags_by_hash = self._source_flags.get(key, {})
                local_texts: list[str] = []
                shared_texts: list[str] = []
                for text in texts:
                    flags = flags_by_hash.get(_roast_text_identity(text), SOURCE_LOCAL)
                    if flags & SOURCE_LOCAL:
                        local_texts.append(text)
                    elif flags & SOURCE_SHARED:
                        shared_texts.append(text)
                    else:
                        local_texts.append(text)
                if local_texts:
                    local_pools[key] = tuple(local_texts)
                if shared_texts:
                    shared_pools[key] = tuple(shared_texts)
        self._local_text_pools = local_pools
        self._shared_text_pools = shared_pools

    def _partition_texts(
        self,
        origin_id: str,
        target_id: str,
        texts: list[str],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """请求期只做两次字典读取；共享池排除双来源正文，避免同一句重复加权。"""

        key = (origin_id, target_id)
        local_texts = self._local_text_pools.get(key)
        shared_texts = self._shared_text_pools.get(key)
        if local_texts is None and shared_texts is None and texts:
            # 兼容外部调试代码直接修改公开 library 属性；正常运行不走该分支。
            return tuple(texts), ()
        return local_texts or (), shared_texts or ()

    # ================================ 共享文案下载与校验 ================================ #
    # 网络、JSON 解析和 SHA256 均在正文锁外完成。远端包只有完整通过校验后，
    # 才会进入最终合并阶段；失败时现有 roast_library.json 不发生任何变化。
    def _configured_shared_manifest_url(self) -> str | None:
        value = getattr(
            self.config,
            "rollpig_roast_library_manifest_url",
            OFFICIAL_ROAST_LIBRARY_MANIFEST_URL,
        )
        if value is None:
            return None
        return str(value).strip() or None

    def _resource_sync_timeout(self) -> float:
        return _clamp_number(
            getattr(self.config, "rollpig_resource_sync_timeout", 10.0),
            10.0,
            1.0,
            240.0,
        )

    def _resource_file_limit(self) -> int:
        configured = _clamp_int(
            getattr(self.config, "rollpig_resource_max_file_size", 10 * 1024 * 1024),
            10 * 1024 * 1024,
            1024,
            128 * 1024 * 1024,
        )
        return min(configured, ROAST_LIBRARY_MAX_SIZE)

    def _is_local_resource_url(self, url: str) -> bool:
        parsed = urlparse(url)
        # Windows 盘符会被 urlparse 识别成单字母 scheme；除明确 HTTP(S) 外均按本地路径处理。
        return parsed.scheme.lower() not in {"http", "https"}

    def _local_resource_path(self, url: str) -> Path:
        parsed = urlparse(url)
        if parsed.scheme.lower() == "file":
            raw_path = unquote(parsed.path)
            if parsed.netloc:
                raw_path = f"//{parsed.netloc}{raw_path}"
            if re.match(r"^/[A-Za-z]:/", raw_path):
                raw_path = raw_path[1:]
            return Path(raw_path).expanduser().resolve()
        return Path(url).expanduser().resolve()

    async def _read_resource_bytes(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        max_size: int,
        label: str,
    ) -> bytes:
        if self._is_local_resource_url(url):
            path = self._local_resource_path(url)

            def read_local() -> bytes:
                if not path.is_file() or path.is_symlink():
                    raise ValueError(f"{label} 本地文件不存在或为符号链接: {path}")
                size = path.stat().st_size
                if size > max_size:
                    raise ValueError(f"{label} 超过大小上限: {size}/{max_size}")
                return path.read_bytes()

            return await asyncio.to_thread(read_local)

        parsed = urlparse(url)
        if parsed.scheme.lower() not in {"http", "https"}:
            raise ValueError(f"{label} URL 仅支持 http、https、file 或本地路径")

        chunks: list[bytes] = []
        total_size = 0
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            content_length = response.headers.get("content-length")
            if content_length:
                try:
                    declared_size = int(content_length)
                except ValueError as error:
                    raise ValueError(f"{label} Content-Length 非法") from error
                if declared_size > max_size:
                    raise ValueError(f"{label} 声明大小超过上限: {declared_size}/{max_size}")
            async for chunk in response.aiter_bytes():
                total_size += len(chunk)
                if total_size > max_size:
                    raise ValueError(f"{label} 下载大小超过上限: {total_size}/{max_size}")
                chunks.append(chunk)
        return b"".join(chunks)

    def _resolve_shared_library_url(self, manifest_url: str, relative_path: str) -> str:
        if self._is_local_resource_url(manifest_url):
            return str(self._local_resource_path(manifest_url).parent / relative_path)
        return urljoin(manifest_url, relative_path)

    def _parse_shared_manifest(self, raw: bytes) -> tuple[dict[str, Any], str, str, int, str]:
        try:
            manifest = json.loads(
                raw.decode("utf-8-sig"),
                object_pairs_hook=_reject_duplicate_json_keys,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(f"共享文案 manifest 解析失败: {error}") from error
        if not isinstance(manifest, dict):
            raise ValueError("共享文案 manifest 顶层必须是 object")
        if manifest.get("schema_version") != 1 or manifest.get("package_type") != "roast_library":
            raise ValueError("共享文案 manifest 类型或 schema_version 不受支持")

        resource_version = str(manifest.get("resource_version") or "").strip()
        if ROAST_VERSION_PATTERN.fullmatch(resource_version) is None:
            raise ValueError("共享文案 resource_version 必须使用 roasts-YYYY-MM-DD.N")
        min_plugin_version = str(manifest.get("min_plugin_version") or "").strip()
        if not min_plugin_version:
            raise ValueError("共享文案 manifest 缺少 min_plugin_version")
        if self._version_tuple(self._current_plugin_version()) < self._version_tuple(min_plugin_version):
            raise ValueError(
                f"共享文案要求 RollPig Plus >= {min_plugin_version}，当前为 {self._current_plugin_version()}"
            )

        file_meta = manifest.get("roast_library")
        if not isinstance(file_meta, dict):
            raise ValueError("共享文案 manifest 缺少 roast_library")
        relative_path = file_meta.get("path")
        if not isinstance(relative_path, str):
            raise ValueError("共享文案 roast_library.path 必须是字符串")
        pure_path = PurePosixPath(relative_path)
        if (
            relative_path != "roast_library.json"
            or pure_path.is_absolute()
            or any(part in {"", ".", ".."} for part in pure_path.parts)
        ):
            raise ValueError("共享文案 roast_library.path 只能为 roast_library.json")

        expected_size = file_meta.get("size")
        if (
            isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or expected_size < 0
            or expected_size > self._resource_file_limit()
        ):
            raise ValueError("共享文案 roast_library.size 非法或超过客户端上限")
        expected_sha256 = file_meta.get("sha256")
        if not isinstance(expected_sha256, str) or ROAST_SHA256_PATTERN.fullmatch(expected_sha256) is None:
            raise ValueError("共享文案 roast_library.sha256 非法")
        return manifest, resource_version, relative_path, expected_size, expected_sha256

    def _version_tuple(self, value: str) -> tuple[int, int, int]:
        match = re.search(r"(\d+)\.(\d+)\.(\d+)", value)
        if match is None:
            raise ValueError(f"插件版本格式非法: {value!r}")
        return tuple(int(part) for part in match.groups())

    def _current_plugin_version(self) -> str:
        try:
            installed = distribution_version("nonebot-plugin-rollpig-plus")
        except PackageNotFoundError:
            return ROLLPIG_PLUS_SHARED_LIBRARY_VERSION
        try:
            if self._version_tuple(installed) >= self._version_tuple(ROLLPIG_PLUS_SHARED_LIBRARY_VERSION):
                return installed
        except ValueError:
            pass
        # 源码分支测试时，环境里可能仍安装上一版 wheel；功能常量代表当前代码能力。
        return ROLLPIG_PLUS_SHARED_LIBRARY_VERSION

    def _load_state_sync(self) -> dict[str, Any]:
        if not self.state_file.is_file():
            return {}
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8-sig"))
            return data if isinstance(data, dict) else {}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}

    def _write_staging_sync(self, manifest_bytes: bytes, library_bytes: bytes) -> Path:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        staging_dir = self.cache_dir / f".shared_roast_staging_{uuid.uuid4().hex}"
        staging_dir.mkdir()
        (staging_dir / "manifest.json").write_bytes(manifest_bytes)
        (staging_dir / "roast_library.json").write_bytes(library_bytes)
        return staging_dir

    def _write_cache_bytes_sync(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex
        temp = path.with_name(f".{path.name}.{token}.tmp")
        try:
            temp.write_bytes(data)
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)

    # ================================ 来源合并与共享撤回 ================================ #
    def _library_identities(
        self,
        library: dict[str, dict[str, list[str]]],
    ) -> set[tuple[str, str, str]]:
        return {
            (origin_id, target_id, _roast_text_identity(text))
            for origin_id, targets in library.items()
            for target_id, texts in targets.items()
            for text in texts
        }

    def _merge_shared_snapshot(
        self,
        current_library: dict[str, dict[str, list[str]]],
        current_flags: dict[tuple[str, str], dict[str, int]],
        shared_library: dict[str, dict[str, list[str]]],
    ) -> tuple[
        dict[str, dict[str, list[str]]],
        dict[tuple[str, str], dict[str, int]],
        int,
        int,
        int,
    ]:
        """共享文案排在前部，并无损保留每一条旧本地原文及其顺序。"""

        reconciled_flags = self._reconcile_source_flags(current_library, current_flags)
        before_identities = self._library_identities(current_library)
        result: dict[str, dict[str, list[str]]] = {}
        result_flags: dict[tuple[str, str], dict[str, int]] = {}

        ordered_origins = list(shared_library)
        ordered_origins.extend(origin_id for origin_id in current_library if origin_id not in shared_library)
        for origin_id in ordered_origins:
            shared_targets = shared_library.get(origin_id, {})
            current_targets = current_library.get(origin_id, {})
            ordered_targets = list(shared_targets)
            ordered_targets.extend(target_id for target_id in current_targets if target_id not in shared_targets)

            for target_id in ordered_targets:
                key = (origin_id, target_id)
                old_flags = reconciled_flags.get(key, {})
                local_entries: list[tuple[str, str]] = []
                first_local_index: dict[str, int] = {}
                for text in current_targets.get(target_id, []):
                    identity = _roast_text_identity(text)
                    if old_flags.get(identity, SOURCE_LOCAL) & SOURCE_LOCAL:
                        index = len(local_entries)
                        local_entries.append((identity, text))
                        first_local_index.setdefault(identity, index)

                merged_texts: list[str] = []
                flags_by_hash: dict[str, int] = {}
                used_shared_identities: set[str] = set()
                consumed_local_indexes: set[int] = set()
                for shared_text in shared_targets.get(target_id, []):
                    identity = _roast_text_identity(shared_text)
                    if identity in used_shared_identities:
                        continue
                    # 本地正文与共享库重复时，保留本地原文与顺序
                    local_index = first_local_index.get(identity)
                    if local_index is None:
                        merged_texts.append(shared_text)
                    else:
                        merged_texts.append(local_entries[local_index][1])
                        consumed_local_indexes.add(local_index)
                    flags = SOURCE_SHARED
                    if local_index is not None:
                        flags |= SOURCE_LOCAL
                    flags_by_hash[identity] = flags
                    used_shared_identities.add(identity)

                for index, (identity, text) in enumerate(local_entries):
                    if index in consumed_local_indexes:
                        continue
                    merged_texts.append(text)
                    flags_by_hash[identity] = flags_by_hash.get(identity, 0) | SOURCE_LOCAL

                if merged_texts:
                    result.setdefault(origin_id, {})[target_id] = merged_texts
                    result_flags[key] = flags_by_hash

        after_identities = self._library_identities(result)
        local_count = sum(
            1
            for origin_id, targets in result.items()
            for target_id, texts in targets.items()
            for text in texts
            if result_flags.get((origin_id, target_id), {}).get(_roast_text_identity(text), 0)
            & SOURCE_LOCAL
        )
        return (
            result,
            result_flags,
            len(after_identities - before_identities),
            len(before_identities - after_identities),
            local_count,
        )

    def _remove_shared_only_entries(
        self,
        current_library: dict[str, dict[str, list[str]]],
        current_flags: dict[tuple[str, str], dict[str, int]],
    ) -> tuple[
        dict[str, dict[str, list[str]]],
        dict[tuple[str, str], dict[str, int]],
        int,
        int,
    ]:
        reconciled_flags = self._reconcile_source_flags(current_library, current_flags)
        result: dict[str, dict[str, list[str]]] = {}
        result_flags: dict[tuple[str, str], dict[str, int]] = {}
        removed = 0
        local_count = 0
        for origin_id, targets in current_library.items():
            for target_id, texts in targets.items():
                key = (origin_id, target_id)
                old_flags = reconciled_flags.get(key, {})
                kept: list[str] = []
                flags_by_hash: dict[str, int] = {}
                for text in texts:
                    identity = _roast_text_identity(text)
                    # 保留具有本地来源标记的正文
                    if old_flags.get(identity, SOURCE_LOCAL) & SOURCE_LOCAL:
                        kept.append(text)
                        flags_by_hash[identity] = SOURCE_LOCAL
                        local_count += 1
                    else:
                        removed += 1
                if kept:
                    result.setdefault(origin_id, {})[target_id] = kept
                    result_flags[key] = flags_by_hash
        return result, result_flags, removed, local_count

    async def _disable_shared_library(self) -> RoastLibrarySyncResult:
        async with self._lock:
            library, source_flags, removed, local_count = self._remove_shared_only_entries(
                self.library,
                self._source_flags,
            )
            state = await asyncio.to_thread(self._load_state_sync)
            has_shared_flags = any(
                flags & SOURCE_SHARED
                for flags_by_hash in self._source_flags.values()
                for flags in flags_by_hash.values()
            )
            if not has_shared_flags and state.get("applied") is False:
                return RoastLibrarySyncResult(
                    updated=False,
                    skipped=True,
                    local_count=local_count,
                    message="共享文案：已关闭",
                )

            await asyncio.to_thread(self._commit_library_and_sources_sync, library, source_flags)
            self.library = library
            self._source_flags = source_flags
            self._source_index_valid = True
            self._rebuild_text_pools()

        disabled_state = {
            **state,
            "manifest_url": None,
            "applied": False,
            "synced_at": int(time.time()),
        }
        await asyncio.to_thread(self._write_cache_json_sync, self.state_file, disabled_state)
        return RoastLibrarySyncResult(
            updated=removed > 0 or has_shared_flags,
            skipped=False,
            removed=removed,
            local_count=local_count,
            message=f"共享文案：已关闭（移除 {removed} 条纯共享文案）",
        )

    async def sync_shared_library(
        self,
        *,
        force: bool = False,
        wait_if_busy: bool = True,
    ) -> RoastLibrarySyncResult:
        """下载并无损合并共享快照；显式空 URL 时只清理纯共享内容。"""

        manifest_url = self._configured_shared_manifest_url()
        if manifest_url is None:
            return await self._disable_shared_library()
        if not bool(getattr(self.config, "rollpig_resource_sync_enabled", True)) and not force:
            return RoastLibrarySyncResult(
                updated=False,
                skipped=True,
                message="共享文案：跟随资源同步关闭",
            )
        if not wait_if_busy and self._shared_sync_lock.locked():
            return RoastLibrarySyncResult(
                updated=False,
                skipped=True,
                message="共享文案：已有同步任务运行中",
            )

        async with self._shared_sync_lock:
            timeout = self._resource_sync_timeout()
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                manifest_bytes = await self._read_resource_bytes(
                    client,
                    manifest_url,
                    max_size=ROAST_MANIFEST_MAX_SIZE,
                    label="共享文案 manifest",
                )
                manifest, resource_version, relative_path, expected_size, expected_sha256 = (
                    self._parse_shared_manifest(manifest_bytes)
                )
                state = await asyncio.to_thread(self._load_state_sync)
                # state 属于可删除缓存；字段损坏时强制重验快照，不能让类型异常永久卡住同步。
                raw_state_text_count = state.get("text_count")
                state_text_count = (
                    raw_state_text_count
                    if isinstance(raw_state_text_count, int)
                    and not isinstance(raw_state_text_count, bool)
                    and raw_state_text_count >= 0
                    else -1
                )
                raw_state_local_count = state.get("local_count")
                state_local_count = (
                    raw_state_local_count
                    if isinstance(raw_state_local_count, int)
                    and not isinstance(raw_state_local_count, bool)
                    and raw_state_local_count >= 0
                    else 0
                )
                current_shared_count = sum(
                    bool(flags & SOURCE_SHARED)
                    for flags_by_hash in self._source_flags.values()
                    for flags in flags_by_hash.values()
                )
                if (
                    not force
                    and state.get("manifest_url") == manifest_url
                    and state.get("resource_version") == resource_version
                    and state.get("sha256") == expected_sha256
                    and state.get("applied") is True
                    and self._source_index_valid
                    and current_shared_count == state_text_count
                ):
                    return RoastLibrarySyncResult(
                        updated=False,
                        skipped=True,
                        resource_version=resource_version,
                        local_count=state_local_count,
                        message=f"共享文案：已是最新（{resource_version}）",
                    )

                library_url = self._resolve_shared_library_url(manifest_url, relative_path)
                library_bytes = await self._read_resource_bytes(
                    client,
                    library_url,
                    max_size=min(expected_size, self._resource_file_limit()),
                    label="共享文案正文",
                )
            if len(library_bytes) != expected_size:
                raise ValueError(
                    f"共享文案正文大小不符: manifest={expected_size}, actual={len(library_bytes)}"
                )
            actual_sha256 = hashlib.sha256(library_bytes).hexdigest()
            if actual_sha256 != expected_sha256:
                raise ValueError(
                    f"共享文案正文 SHA256 不符: manifest={expected_sha256}, actual={actual_sha256}"
                )

            def parse_library() -> tuple[dict[str, dict[str, list[str]]], dict[str, int]]:
                try:
                    raw_library = json.loads(
                        library_bytes.decode("utf-8-sig"),
                        object_pairs_hook=_reject_duplicate_json_keys,
                    )
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise ValueError(f"共享文案正文解析失败: {error}") from error
                return _validate_shared_library(raw_library)

            shared_library, statistics = await asyncio.to_thread(parse_library)
            if manifest.get("statistics") != statistics:
                raise ValueError(
                    f"共享文案 statistics 不一致: manifest={manifest.get('statistics')!r}, "
                    f"actual={statistics!r}"
                )

            staging_dir = await asyncio.to_thread(
                self._write_staging_sync,
                manifest_bytes,
                library_bytes,
            )
            try:
                async with self._lock:
                    library, source_flags, added, removed, local_count = self._merge_shared_snapshot(
                        self.library,
                        self._source_flags,
                        shared_library,
                    )
                    await asyncio.to_thread(
                        self._commit_library_and_sources_sync,
                        library,
                        source_flags,
                    )
                    self.library = library
                    self._source_flags = source_flags
                    self._source_index_valid = True
                    self._rebuild_text_pools()

                # state 最后落盘。若 snapshot/state 写入失败，下次会重复一次幂等合并，
                # 但当前正文已经可用且不会丢失本地来源。
                await asyncio.to_thread(
                    self._write_cache_bytes_sync,
                    self.snapshot_file,
                    library_bytes,
                )
                next_state = {
                    "manifest_url": manifest_url,
                    "resource_version": resource_version,
                    "sha256": expected_sha256,
                    "applied": True,
                    "synced_at": int(time.time()),
                    "origin_count": statistics["origin_count"],
                    "pair_count": statistics["pair_count"],
                    "text_count": statistics["text_count"],
                    "local_count": local_count,
                }
                await asyncio.to_thread(self._write_cache_json_sync, self.state_file, next_state)
            finally:
                await asyncio.to_thread(shutil.rmtree, staging_dir, True)

            return RoastLibrarySyncResult(
                updated=True,
                skipped=False,
                resource_version=resource_version,
                added=added,
                removed=removed,
                local_count=local_count,
                message=(
                    f"共享文案：已更新（{resource_version}）\n"
                    f"新增 {added} 条｜移除 {removed} 条｜本地保留 {local_count} 条"
                ),
            )

    def _format_text(self, text: str, origin: str, food: str, killer: str = None, victim: str = None) -> str:
        res = text.replace("{origin}", origin).replace("{food}", food)
        k_name = killer if killer else "神秘人"
        v_name = victim if victim else "倒霉蛋"
        res = res.replace("{k}", k_name).replace("{v}", v_name)
        return res

    async def get_roast_text(self, origin_pig: dict, target_food: dict, 
                             operator_name: str = None, target_name: str = None) -> str:
        o_id = origin_pig["id"]
        t_id = target_food["id"]
        o_name = origin_pig["name"]
        t_name = target_food["name"]

        # --- 场景 1: 焦炭 ---
        # 该分支目前主要用于兼容/扩展；当前命令层默认不再触发“熟食再烤”。
        if t_id == "burnt":
            if self.ai_ready and self.client:
                try:
                    text = await self._call_ai(origin_pig, target_food, is_burnt=True)
                    return self._format_text(text, o_name, t_name)
                except Exception as e:
                    logger.warning(f"焦炭文案 AI 生成失败，回落本地模板: {e}")
            return random.choice(BURNT_TEMPLATES).format(origin=o_name)
        
        # --- 场景 2 & 3: PvP / PvE ---
        lookup_t_id = t_id + ("_pvp" if operator_name else "")
        all_texts = self.library.get(o_id, {}).get(lookup_t_id, [])
        local_texts, shared_texts = self._partition_texts(o_id, lookup_t_id, all_texts)
        
        # 只有 AI 已开启且可用时，才允许新增文案生成
        should_generate = self.ai_ready and (
            (not local_texts)
            or (len(local_texts) < AI_TEXTS_PER_PAIR_TARGET and random.random() < 0.4)
        )

        template_text = None
        if should_generate:
            try:
                template_text = await self._call_ai(origin_pig, target_food, is_pvp=bool(operator_name))
                if template_text:
                    await self._save_new_text(o_id, lookup_t_id, template_text)
            except Exception as e:
                logger.error(f"AI 生成失败: {e}")

        if not template_text:
            # 本地和纯共享文案先等概率选池，再在池内随机；共享规模增长不会淹没实例自有内容。
            available_pools = [pool for pool in (local_texts, shared_texts) if pool]
            if available_pools:
                template_text = random.choice(random.choice(available_pools))
            
        if not template_text:
            template_text = random.choice(PVP_TEMPLATES) if operator_name else random.choice(DEFAULT_TEMPLATES)

        return self._format_text(template_text, o_name, t_name, operator_name, target_name)

    async def _call_ai(self, origin_pig: dict, target_food: dict, is_pvp: bool = False, is_burnt: bool = False) -> str:
        if not self.client:
            raise RuntimeError("AI client is not initialized")
        
        # 1. 提取特征
        origin_feature = _extract_origin_feature(origin_pig)

        # 基础 System Prompt
        system_prompt = "你是一个擅长黑色幽默、说话刻薄但好笑的脱口秀演员。你的任务是进行‘猪生终结’吐槽。"

        # === 场景 A: 变成焦炭 (二次烧烤兼容分支) ===
        if is_burnt:
            prompt = (
                f"【吐槽对象】：一块已经是美食的【{origin_pig['name']}】，被贪婪的人类再次放上烤架，彻底烤成了【焦炭/致癌物】。\n"
                f"请写一段40字以内的毒舌吐槽。\n\n"
                f"严格遵守【对比公式】：\n"
                f"“曾经你(美食状态)...如今你(焦炭状态)...”\n\n"
                f"参考范例：\n"
                f"- “曾经你是鲜嫩多汁的培根，如今却变成了一块用来画眉毛的木炭。人类的贪婪真是你的火葬场。”\n"
                f"要求：风格地狱笑话，尖酸刻薄，严禁客套。"
            )

        # === 场景 B: 烤群友 PvP ===
        elif is_pvp:
            prompt = (
                f"【吐槽对象】：凶手把受害者（本体【{origin_pig['name']}】，特征：{origin_feature}）残忍地做成了【{target_food['name']}】。\n"
                f"请写一段40字以内的解说，**必须使用占位符**：{{k}}代表凶手，{{v}}代表受害者。\n\n"
                f"严格遵守【对比公式】：\n"
                f"“{{k}} (动作)... 把 {{v}} (惨状/前世特征)... 变成了 (今生美食)...”\n\n"
                f"参考范例：\n"
                f"- “{{k}} 没给 {{v}} 任何辩解的机会。前一秒它还是只特立独行的野猪，下一秒就成了 {{k}} 盘子里滋滋作响的五花肉。”\n"
                f"- “{{k}} 的手艺真是‘惊天地泣鬼神’，硬生生把 {{v}} 这只大懒猪，炼成了一锅香喷喷的猪油。”\n"
                f"要求：既要体现受害者惨状，又要调侃凶手，必须包含 {{k}} 和 {{v}}。"
            )

        # === 场景 C: 标准烤猪 PvE ===
        else:
            prompt = (
                f"现在进行一场【猪生终结吐槽大会】。\n"
                f"对象前世：【{origin_pig['name']}】（特征：{origin_feature}）\n"
                f"对象今生：【{target_food['name']}】\n\n"
                
                f"请写一段40字以内的神吐槽。必须严格遵守以下【对比公式】：\n"
                f"“曾经你(前世特征/地位)...如今你(死后状态/口感)...”\n\n"
                
                f"参考范例（学习这种语气）：\n"
                f"- “曾经你是丛林里的一方霸主野猪，如今却成为培根在我的平底锅里滋滋作响。别说，比起你的獠牙，还是你的油脂更迷人。”\n"
                f"- “生前你是个除了吃就是睡的大懒猪，没想到变成红烧肉后，这层肥膘反而成了精华，真是懒猪有懒福。”\n\n"
                
                f"要求：\n"
                f"1. 必须同时提到“生前”和“死后”的反差。\n"
                f"2. 风格要毒舌、幽默、带点地狱笑话，不要纯夸好吃。\n"
                f"3. 严禁出现“这道菜”、“这道美食”这种客套话，直接对话（用“你”）。"
            )

        try:
            # OpenAI 兼容接口可能在网络抖动时长时间挂起；这里用本地超时和并发闸门
            # 保护 NoneBot 事件循环，失败后由调用方回落本地模板。
            async with self._ai_semaphore:
                request_kwargs = {
                    "model": self.ai_model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt},
                    ],
                    "stream": False,
                    "max_tokens": self.ai_max_tokens,
                }
                if self.ai_extra_body is not None:
                    # DeepSeek V4 的 thinking 是官方 OpenAI 兼容接口扩展字段；
                    # 第三方网关不一定支持，所以只在解析阶段确认安全时才注入。
                    request_kwargs["extra_body"] = self.ai_extra_body

                response = await asyncio.wait_for(
                    self.client.chat.completions.create(**request_kwargs),
                    timeout=self.ai_timeout,
                )
            usage = getattr(response, "usage", None)
            if usage:
                logger.info(
                    "AI 烤猪 token 用量: "
                    f"prompt={getattr(usage, 'prompt_tokens', None)} "
                    f"completion={getattr(usage, 'completion_tokens', None)} "
                    f"total={getattr(usage, 'total_tokens', None)} "
                    f"max_tokens={self.ai_max_tokens}"
                )
            content = response.choices[0].message.content
            if not isinstance(content, str) or not content.strip():
                raise ValueError("AI empty response")
            text = content.strip().strip('"').strip("'").replace("\n", "")
            return text[: self.ai_output_max_chars]
        except Exception as e:
            logger.error(f"AI API 请求错误: {e}")
            raise e

roast_manager = RoastManager()
