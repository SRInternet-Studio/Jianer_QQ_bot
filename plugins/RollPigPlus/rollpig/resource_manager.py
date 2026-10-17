from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote, urljoin, urlparse

import httpx
from ..host import localstore
from ..host import logger
from PIL import Image

from .config import Config, plugin_config
from .store.models import MAX_EXPERT_LEVEL

PACKAGE_DIR = Path(__file__).parent
RESOURCE_DIR = PACKAGE_DIR / "resource"


PLUGIN_DIR = PACKAGE_DIR
BUILTIN_RESOURCE_DIR = RESOURCE_DIR
BUILTIN_PIG_JSON = BUILTIN_RESOURCE_DIR / "pig.json"
BUILTIN_RULES_JSON = BUILTIN_RESOURCE_DIR / "pig_rules.json"
BUILTIN_EX_VARIANTS_JSON = BUILTIN_RESOURCE_DIR / "pig_ex_variants.json"
BUILTIN_IMAGE_DIR = BUILTIN_RESOURCE_DIR / "image"

CACHE_ROOT = localstore.get_plugin_data_dir() / "resources"
ACTIVE_RESOURCE_DIR = CACHE_ROOT / "active"
ACTIVE_IMAGE_DIR = ACTIVE_RESOURCE_DIR / "images"
STATE_FILE = CACHE_ROOT / "state.json"
PRIVATE_RESOURCE_DIR = CACHE_ROOT / "private_active"
PRIVATE_STATE_FILE = CACHE_ROOT / "private_state.json"
PRIVATE_RESOURCE_ROOT = CACHE_ROOT / "private_overlays"
OFFICIAL_GIF_RESOURCE_NAME = "official-gif"
OFFICIAL_GIF_RESOURCE_MANIFEST_URL = "https://pig.felislab.cc/resources/rollpig-gif/manifest.json"

PIG_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
PRIVATE_SOURCE_NAME_PATTERN = re.compile(r"[^a-z0-9_-]+")
# GIF 资源应优先于同名 PNG，让资源包把某只猪替换为动态版时无需改 pig.json。
IMAGE_SUFFIX_PRIORITY = (".gif", ".png")
ALLOWED_IMAGE_SUFFIXES = set(IMAGE_SUFFIX_PRIORITY)
VARIANT_IMAGE_MANIFEST_FIELDS = frozenset({"pig_id", "level", "filename", "path", "size", "sha256"})
RESOURCE_MANIFEST_MAX_SIZE = 1 * 1024 * 1024
RESOURCE_PIG_JSON_MAX_SIZE = 2 * 1024 * 1024
RESOURCE_RULES_JSON_MAX_SIZE = 256 * 1024
RESOURCE_EX_VARIANTS_JSON_MAX_SIZE = 512 * 1024
RESOURCE_PACKAGE_MAX_SIZE = 128 * 1024 * 1024
RESOURCE_MAX_IMAGES = 500
RESOURCE_MAX_EX_VARIANTS = 500
RESOURCE_MAX_VARIANT_IMAGES = 500
RESOURCE_MAX_FILES = 700
RESOURCE_SYNC_TIMEOUT_MIN_SECONDS = 1.0
RESOURCE_SYNC_TIMEOUT_MAX_SECONDS = 240.0


def _resource_sync_timeout() -> float:
    """返回资源请求的有效超时；配置保持兼容，实际网络等待限制在 1～240 秒。"""

    configured = float(plugin_config.rollpig_resource_sync_timeout or 10.0)
    return min(RESOURCE_SYNC_TIMEOUT_MAX_SECONDS, max(RESOURCE_SYNC_TIMEOUT_MIN_SECONDS, configured))


@dataclass
class ResourceSyncResult:
    updated: bool
    skipped: bool
    resource_version: str = ""
    message: str = ""


@dataclass
class _DownloadBudget:
    """限制单次资源同步的总文件数和总字节数，避免异常 manifest 拖垮内存或磁盘。"""

    max_total_size: int
    max_file_count: int
    total_size: int = 0
    file_count: int = 0

    def add_file(self, *, path: str, size: int) -> None:
        self.file_count += 1
        self.total_size += size
        if self.file_count > self.max_file_count:
            raise ValueError(f"资源包文件数量超过上限: {self.file_count}/{self.max_file_count}")
        if self.total_size > self.max_total_size:
            raise ValueError(f"资源包总大小超过上限: {path}")


@dataclass(frozen=True)
class _PrivateResourceSource:
    """一个私有 overlay 的运行时描述；缓存路径必须稳定，避免每次启动重复下载。"""

    name: str
    manifest_url: str
    token: str = ""
    active_dir: Path = PRIVATE_RESOURCE_DIR
    previous_dir: Path = CACHE_ROOT / "private_previous"
    state_file: Path = PRIVATE_STATE_FILE


@dataclass(frozen=True)
class _PigExVariantSpec:
    """差分 JSON 中尚未绑定实体路径的一条声明。"""

    pig_id: str
    level: int
    filename: str | None = None
    description: str | None = None
    analysis: str | None = None


@dataclass(frozen=True)
class PigExVariant:
    """已经通过结构校验并按需绑定本地图片的 EX 等级差分。"""

    pig_id: str
    level: int
    image_path: Path | None = None
    description: str | None = None
    analysis: str | None = None


@dataclass(frozen=True)
class ResolvedPigAppearance:
    """一次展示实际使用的文案、图片和差分等级。"""

    pig_data: dict[str, Any]
    image_path: Path | None
    base_image_path: Path | None
    requested_level: int
    applied_level: int
    resource_version: str


class RollPigResourceManager:
    def __init__(self) -> None:
        self._sync_lock = asyncio.Lock()
        self.pig_list: list[dict[str, Any]] = []
        self.pig_map: dict[str, dict[str, Any]] = {}
        self.food_pig_ids: set[str] = set()
        self.human_pig_ids: set[str] = set()
        self.eaten_pig_ids: set[str] = set()
        self.sold_pig_ids: set[str] = set()
        self.roast_excluded_pig_ids: set[str] = set()
        self.image_dirs: list[Path] = []
        self.ex_variants: dict[str, dict[int, PigExVariant]] = {}
        self.variant_blocked_pig_ids: set[str] = set()
        self.resource_version: str = "builtin"

    # ================================ 资源读取与内存快照 ================================ #
    # 资源读取统一走这里，命令层继续使用 PIG_LIST/find_image_file 这类旧接口，减少侵入面。
    # 缓存资源必须完整可读，否则直接回退到插件内置资源，避免坏资源包导致 bot 启动失败。
    def reload(self) -> None:
        active_pig_json = ACTIVE_RESOURCE_DIR / "pig.json"
        if active_pig_json.exists():
            try:
                self._load_from_dir(ACTIVE_RESOURCE_DIR, resource_version=self._read_state_version())
                self._load_private_overlay()
                return
            except Exception as error:
                logger.warning(f"rollpig 云端资源缓存读取失败，回退到内置资源: {error}")

        self._load_from_builtin()
        self._load_private_overlay()

    def _load_from_builtin(self) -> None:
        pig_list = self._read_pig_json(BUILTIN_PIG_JSON)
        self._apply_snapshot(
            pig_list=pig_list,
            rules=self._read_rules_json(BUILTIN_RULES_JSON),
            image_dirs=[BUILTIN_IMAGE_DIR],
            ex_variants=self._load_ex_variants(
                BUILTIN_EX_VARIANTS_JSON,
                pig_ids={str(item["id"]) for item in pig_list},
                image_dir=BUILTIN_IMAGE_DIR,
                strict=False,
            ),
            resource_version="builtin",
        )

    def _load_from_dir(self, resource_dir: Path, *, resource_version: str) -> None:
        pig_list = self._read_pig_json(resource_dir / "pig.json")
        rules = self._read_rules_json(resource_dir / "pig_rules.json")
        image_dir = resource_dir / "images"
        self._ensure_images_exist(pig_list, [image_dir, BUILTIN_IMAGE_DIR])
        self._apply_snapshot(
            pig_list=pig_list,
            rules=rules,
            image_dirs=[image_dir, BUILTIN_IMAGE_DIR],
            ex_variants=self._load_ex_variants(
                resource_dir / "pig_ex_variants.json",
                pig_ids={str(item["id"]) for item in pig_list},
                image_dir=image_dir,
                strict=False,
            ),
            resource_version=resource_version or "cloud",
        )

    def _load_private_overlay(self) -> None:
        """按配置顺序叠加私有资源包；单个私有包坏掉时不影响其它资源包可用性。"""
        try:
            private_sources = self._resolve_private_sources(plugin_config)
        except Exception as error:
            logger.warning(f"rollpig 私有资源运行时配置读取失败，已忽略全部私有 overlay: {error}")
            return
        if not private_sources:
            return

        for source in private_sources:
            active_private_pig_json = source.active_dir / "pig.json"
            if not active_private_pig_json.exists():
                continue
            try:
                self._apply_private_overlay(source.active_dir, resource_version=self._read_private_state_version(source))
            except Exception as error:
                logger.warning(f"rollpig 私有资源缓存读取失败，已忽略该 overlay: name={source.name} error={error}")

    def _apply_private_overlay(self, resource_dir: Path, *, resource_version: str) -> None:
        # 私有包只允许通过 pig.json 追加新猪；覆盖公有包或前序私有包必须显式写入 pig_overrides.json。
        private_pigs = self._read_pig_json(resource_dir / "pig.json")
        private_rules = self._read_rules_json(resource_dir / "pig_rules.json")
        pig_overrides = self._read_pig_overrides_json(resource_dir / "pig_overrides.json")

        self._ensure_images_exist(private_pigs, [resource_dir / "images"])

        base_ids = set(self.pig_map)
        duplicate_ids = [str(item["id"]) for item in private_pigs if str(item["id"]) in base_ids]
        if duplicate_ids:
            raise ValueError(f"私有资源 pig.json 不能重复已有 ID，请改用 pig_overrides.json: {', '.join(duplicate_ids[:10])}")

        merged_pig_map = {str(item["id"]): dict(item) for item in self.pig_list}
        overridden_ids: set[str] = set()
        for override in pig_overrides:
            pig_id = str(override["id"])
            if pig_id not in merged_pig_map:
                raise ValueError(f"pig_overrides 指向不存在的公有 ID: {pig_id}")
            updated_item = dict(merged_pig_map[pig_id])
            updated_item.update({key: value for key, value in override.items() if key != "id"})
            updated_item["id"] = pig_id
            merged_pig_map[pig_id] = updated_item
            overridden_ids.add(pig_id)

        merged_pig_list = [merged_pig_map[str(item["id"])] for item in self.pig_list]
        merged_pig_list.extend(private_pigs)
        self._validate_pig_list(merged_pig_list)

        self.pig_list = merged_pig_list
        self.pig_map = {str(item["id"]): item for item in merged_pig_list}
        self.food_pig_ids.update(self._read_id_set(private_rules, "food_pigs"))
        self.human_pig_ids.update(self._read_id_set(private_rules, "human_pigs"))
        self.eaten_pig_ids.update(self._read_id_set(private_rules, "eaten_pigs"))
        self.sold_pig_ids.update(self._read_id_set(private_rules, "sold_pigs"))
        self.roast_excluded_pig_ids.update(self._read_id_set(private_rules, "roast_excluded_pigs"))
        self.image_dirs = [resource_dir / "images", *self.image_dirs]
        # 首版不支持 Overlay 自带差分。用户显式覆盖基础猪时，禁用同 ID 的公有差分，
        # 避免达到 EX 等级后又突然被官方图片或文案覆盖回去。
        self.variant_blocked_pig_ids.update(overridden_ids)
        self.resource_version = f"{self.resource_version}+{resource_version or 'private'}"
        logger.info(
            f"rollpig 私有资源已叠加: version={resource_version}, private_pigs={len(private_pigs)}, total={len(self.pig_list)}"
        )

    def _apply_snapshot(
        self,
        *,
        pig_list: list[dict[str, Any]],
        rules: dict[str, Any],
        image_dirs: list[Path],
        ex_variants: dict[str, dict[int, PigExVariant]],
        resource_version: str,
    ) -> None:
        self._validate_pig_list(pig_list)
        self.pig_list = pig_list
        self.pig_map = {str(item["id"]): item for item in pig_list}
        self.food_pig_ids = self._read_id_set(rules, "food_pigs")
        self.human_pig_ids = self._read_id_set(rules, "human_pigs")
        self.eaten_pig_ids = self._read_id_set(rules, "eaten_pigs")
        self.sold_pig_ids = self._read_id_set(rules, "sold_pigs")
        self.roast_excluded_pig_ids = self._read_id_set(rules, "roast_excluded_pigs")
        self.image_dirs = image_dirs
        self.ex_variants = ex_variants
        self.variant_blocked_pig_ids = set()
        self.resource_version = resource_version
        variant_count = sum(len(levels) for levels in ex_variants.values())
        logger.info(
            f"rollpig 资源已加载: version={resource_version}, pigs={len(pig_list)}, "
            f"variant_pigs={len(ex_variants)}, variants={variant_count}"
        )

    def find_image_file(self, pig_id: str) -> Path | None:
        for image_dir in self.image_dirs:
            for suffix in IMAGE_SUFFIX_PRIORITY:
                image_file = image_dir / f"{pig_id}{suffix}"
                if image_file.exists():
                    return image_file
        return None

    def find_named_image_file(self, filename: str) -> Path | None:
        """按快照中的安全文件名查找图片；禁止把历史字段当作任意路径读取。"""

        if not filename or Path(filename).name != filename or "\\" in filename:
            return None
        for image_dir in self.image_dirs:
            image_file = image_dir / filename
            if image_file.is_file() and not image_file.is_symlink():
                return image_file
        return None

    @staticmethod
    def image_file_is_decodable(image_file: Path | None) -> bool:
        """只解码首帧确认快照图片可用；该检查仅发生在首次抽取快照补全。"""

        if image_file is None or not image_file.is_file() or image_file.is_symlink():
            return False
        try:
            with Image.open(image_file) as image:
                image.seek(0)
                image.load()
            return True
        except (OSError, ValueError, Image.DecompressionBombError):
            return False

    def resolve_pig_appearance(
        self,
        pig_data: Mapping[str, Any],
        ex_level: int,
    ) -> ResolvedPigAppearance:
        """按当前 EX Lv. 解析稀疏差分；任何结果都不会修改全局或调用方数据。"""

        resolved_data = dict(pig_data)
        pig_id = str(resolved_data.get("id") or "")
        requested_level = min(max(int(ex_level or 0), 0), MAX_EXPERT_LEVEL)
        base_image_path = self.find_image_file(pig_id) if pig_id else None

        image_path = base_image_path
        applied_level = 0
        if pig_id and pig_id not in self.variant_blocked_pig_ids:
            levels = self.ex_variants.get(pig_id, {})
            # 差分按等级从低到高逐字段覆盖。这样高等级只提供文案时，仍会继承
            # 较低等级的图片；声明了图片但文件运行时丢失时，则跳过该整级差分。
            for candidate_level in range(1, requested_level + 1):
                variant = levels.get(candidate_level)
                if variant is None:
                    continue
                if variant.image_path is not None and not variant.image_path.is_file():
                    logger.warning(
                        "rollpig EX 差分图片运行时缺失，已跳过该等级: "
                        f"pig_id={pig_id} level={candidate_level} file={variant.image_path}"
                    )
                    continue
                if variant.image_path is not None:
                    image_path = variant.image_path
                if variant.description is not None:
                    resolved_data["description"] = variant.description
                if variant.analysis is not None:
                    resolved_data["analysis"] = variant.analysis
                applied_level = candidate_level

        return ResolvedPigAppearance(
            pig_data=resolved_data,
            image_path=image_path,
            base_image_path=base_image_path,
            requested_level=requested_level,
            applied_level=applied_level,
            resource_version=self.resource_version,
        )

    def available_variant_levels(self, pig_id: str) -> tuple[int, ...]:
        """返回一只猪当前可用的差分等级；被 Overlay 覆盖时视为无差分。"""

        if not pig_id or pig_id in self.variant_blocked_pig_ids:
            return ()
        return tuple(sorted(self.ex_variants.get(pig_id, {})))

    def newly_unlocked_variant_levels(
        self,
        pig_id: str,
        previous_level: int,
        current_level: int,
    ) -> tuple[int, ...]:
        """返回本次升级真正跨过的差分档位，不为普通等级变化制造提示。"""

        previous = min(max(int(previous_level or 0), 0), MAX_EXPERT_LEVEL)
        current = min(max(int(current_level or 0), 0), MAX_EXPERT_LEVEL)
        if current <= previous:
            return ()
        return tuple(
            level
            for level in self.available_variant_levels(pig_id)
            if previous < level <= current
        )

    def variant_change_fields(self, pig_id: str, level: int) -> frozenset[str]:
        """返回指定差分档显式改变的展示类型，用于选择对应的成长文案池。"""

        precise_fields = self.variant_snapshot_fields(pig_id, level)
        fields: set[str] = set()
        if "image" in precise_fields:
            fields.add("image")
        if precise_fields & {"description", "analysis"}:
            fields.add("text")
        return frozenset(fields)

    def variant_snapshot_fields(self, pig_id: str, level: int) -> frozenset[str]:
        """返回差分真实提供的 image/description/analysis 字段，供历史快照精确记录。"""

        if not pig_id or pig_id in self.variant_blocked_pig_ids:
            return frozenset()
        variant = self.ex_variants.get(pig_id, {}).get(int(level or 0))
        if variant is None:
            return frozenset()

        fields: set[str] = set()
        if variant.image_path is not None:
            # 声明图片的差分以整档为单位应用；图片在加载后被删除或替换为链接时，
            # resolve_pig_appearance 也会跳过整档，因此这里不能再宣称图片或文案已变化。
            if not self.image_file_is_decodable(variant.image_path):
                return frozenset()
            fields.add("image")
        if variant.description is not None:
            fields.add("description")
        if variant.analysis is not None:
            fields.add("analysis")
        return frozenset(fields)

    def _read_state_version(self) -> str:
        try:
            state = json.loads(self._read_json_text(STATE_FILE))
            return str(state.get("resource_version") or "cloud")
        except Exception:
            return "cloud"

    def _active_variant_assets_complete(self, manifest: Mapping[str, Any]) -> bool:
        """核对当前 active 是否已完整保存 manifest 声明的差分资源。"""

        optional_files = manifest.get("optional_files") or {}
        raw_variant_items = manifest.get("variant_images", [])
        if not isinstance(optional_files, dict) or not isinstance(raw_variant_items, list):
            return False
        if len(raw_variant_items) > RESOURCE_MAX_VARIANT_IMAGES:
            return False

        variants_meta = optional_files.get("pig_ex_variants")
        if variants_meta is None and not raw_variant_items:
            return True
        if not isinstance(variants_meta, dict):
            return False

        try:
            self._validate_required_manifest_meta(
                variants_meta,
                label="optional_files.pig_ex_variants",
                expected_path="pig_ex_variants.json",
            )
            variants_path = ACTIVE_RESOURCE_DIR / "pig_ex_variants.json"
            if not self._local_file_matches_meta(variants_path, variants_meta):
                return False
            active_pigs = self._read_pig_json(ACTIVE_RESOURCE_DIR / "pig.json")
            variant_specs = self._read_ex_variant_specs(
                variants_path,
                pig_ids={str(item["id"]) for item in active_pigs},
                strict=True,
            )
            image_specs = {
                key: spec.filename
                for key, spec in variant_specs.items()
                if spec.filename is not None
            }

            seen_keys: set[tuple[str, int]] = set()
            for index, image_meta in enumerate(raw_variant_items):
                if not isinstance(image_meta, dict):
                    return False
                if set(image_meta) - VARIANT_IMAGE_MANIFEST_FIELDS:
                    return False
                pig_id = str(image_meta.get("pig_id") or "")
                raw_level = image_meta.get("level")
                if isinstance(raw_level, bool) or not isinstance(raw_level, int):
                    return False
                level = int(raw_level)
                filename = str(image_meta.get("filename") or "")
                self._validate_variant_image_filename(filename, pig_id=pig_id, level=level)
                self._validate_required_manifest_meta(
                    image_meta,
                    label=f"variant_images[{index}]",
                    expected_path=f"images/{filename}",
                )
                key = (pig_id, level)
                if key in seen_keys:
                    return False
                seen_keys.add(key)
                if image_specs.get(key) != filename:
                    return False
                if not self._local_file_matches_meta(ACTIVE_IMAGE_DIR / filename, image_meta):
                    return False
        except (OSError, TypeError, ValueError):
            return False
        return set(image_specs) == seen_keys

    def _local_file_matches_meta(self, path: Path, meta: Mapping[str, Any]) -> bool:
        """校验 active 文件大小与 SHA256；仅在同版本跳过下载前在线程中调用。"""

        if not path.is_file() or path.is_symlink():
            return False
        expected_size = meta.get("size")
        expected_hash = meta.get("sha256")
        if path.stat().st_size != expected_size or not isinstance(expected_hash, str):
            return False

        hasher = hashlib.sha256()
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                hasher.update(chunk)
        return hasher.hexdigest() == expected_hash

    def _read_private_state_version(self, source: _PrivateResourceSource | None = None) -> str:
        state_file = source.state_file if source is not None else PRIVATE_STATE_FILE
        try:
            state = json.loads(self._read_json_text(state_file))
            return str(state.get("resource_version") or "private")
        except Exception:
            return "private"

    def _resolve_private_sources(self, config: Config) -> list[_PrivateResourceSource]:
        """解析官方与用户 overlay；官方 GIF 包固定随云端资源启用，不暴露移除开关。"""

        raw_sources = list(getattr(config, "rollpig_private_resource_manifests", []) or [])
        sources: list[_PrivateResourceSource] = []
        seen_urls: set[str] = set()
        seen_names: set[str] = set()

        if config.rollpig_resource_sync_enabled:
            official_source = self._with_unique_private_source_name(
                self._official_gif_private_source(),
                index=0,
                seen_names=seen_names,
            )
            sources.append(official_source)
            seen_urls.add(official_source.manifest_url)

        for index, raw_source in enumerate(raw_sources, start=1):
            source = self._coerce_private_source(raw_source, index=index, default_token=config.rollpig_private_resource_token)
            if source is None:
                continue
            if source.manifest_url in seen_urls:
                logger.warning(f"rollpig 私有资源配置存在重复 manifest，已忽略: {source.manifest_url}")
                continue
            source = self._with_unique_private_source_name(source, index=index, seen_names=seen_names)
            sources.append(source)
            seen_urls.add(source.manifest_url)

        legacy_manifest_url = str(config.rollpig_private_resource_manifest_url or "").strip()
        if legacy_manifest_url and legacy_manifest_url not in seen_urls:
            sources.append(
                _PrivateResourceSource(
                    name="legacy",
                    manifest_url=legacy_manifest_url,
                    token=str(config.rollpig_private_resource_token or "").strip(),
                    active_dir=PRIVATE_RESOURCE_DIR,
                    previous_dir=CACHE_ROOT / "private_previous",
                    state_file=PRIVATE_STATE_FILE,
                )
            )

        return sources

    def _official_gif_private_source(self) -> _PrivateResourceSource:
        """官方 GIF 动态小猪包属于 Plus 云端资源组成部分，不走用户私有包配置。"""

        root = PRIVATE_RESOURCE_ROOT / OFFICIAL_GIF_RESOURCE_NAME
        return _PrivateResourceSource(
            name=OFFICIAL_GIF_RESOURCE_NAME,
            manifest_url=OFFICIAL_GIF_RESOURCE_MANIFEST_URL,
            active_dir=root / "active",
            previous_dir=root / "previous",
            state_file=root / "state.json",
        )

    def _coerce_private_source(
        self,
        raw_source: Any,
        *,
        index: int,
        default_token: str | None,
    ) -> _PrivateResourceSource | None:
        """把 JSON 中的字符串或 object 配置归一化为运行时 source。"""

        if isinstance(raw_source, str):
            manifest_url = raw_source.strip()
            raw_name = ""
            token = str(default_token or "").strip()
        elif isinstance(raw_source, dict) or hasattr(raw_source, "manifest_url"):
            if not isinstance(raw_source, dict):
                raw_source = raw_source.dict() if hasattr(raw_source, "dict") else vars(raw_source)
            manifest_url = str(raw_source.get("manifest_url") or raw_source.get("url") or "").strip()
            raw_name = str(raw_source.get("name") or "").strip()
            token = str(raw_source.get("token") or default_token or "").strip()
        else:
            raise ValueError(f"rollpig_private_resource_manifests[{index}] 必须是字符串或 object")

        if not manifest_url:
            return None

        name = self._normalize_private_source_name(raw_name or self._guess_private_source_name(manifest_url), index=index)
        root = PRIVATE_RESOURCE_ROOT / name
        return _PrivateResourceSource(
            name=name,
            manifest_url=manifest_url,
            token=token,
            active_dir=root / "active",
            previous_dir=root / "previous",
            state_file=root / "state.json",
        )

    def _guess_private_source_name(self, manifest_url: str) -> str:
        parsed = urlparse(manifest_url)
        if parsed.scheme in {"http", "https", "file"}:
            path = Path(unquote(parsed.path))
        else:
            path = Path(manifest_url)
        parent_name = path.parent.name if path.name == "manifest.json" else path.stem
        return parent_name or "private"

    def _normalize_private_source_name(self, name: str, *, index: int) -> str:
        normalized = PRIVATE_SOURCE_NAME_PATTERN.sub("-", name.strip().lower()).strip("-_")
        return normalized[:48] or f"private-{index}"

    def _with_unique_private_source_name(
        self,
        source: _PrivateResourceSource,
        *,
        index: int,
        seen_names: set[str],
    ) -> _PrivateResourceSource:
        """避免多个 overlay 使用同一缓存目录；同名时追加序号，保留配置顺序。"""

        if source.name not in seen_names:
            seen_names.add(source.name)
            return source

        base_name = source.name[:43].strip("-_") or "private"
        candidate = f"{base_name}-{index}"
        while candidate in seen_names:
            candidate = f"{base_name}-{uuid.uuid4().hex[:6]}"
        seen_names.add(candidate)
        logger.warning(f"rollpig 私有资源缓存名重复，已自动调整缓存目录名: {source.name} -> {candidate}")
        root = PRIVATE_RESOURCE_ROOT / candidate
        return replace(
            source,
            name=candidate,
            active_dir=root / "active",
            previous_dir=root / "previous",
            state_file=root / "state.json",
        )

    # ================================ 云端同步 ================================ #
    # 同步流程采用“临时目录下载 -> 完整校验 -> 原子替换 active”的方式，避免半包覆盖。
    async def sync_all(self, *, force: bool = False, wait_if_busy: bool = True) -> tuple[ResourceSyncResult, ResourceSyncResult]:
        """串行同步公有包与所有私有 overlay；手动同步等待，后台同步可选择忙时跳过。"""
        if not wait_if_busy and self._sync_lock.locked():
            return (
                ResourceSyncResult(updated=False, skipped=True, message="已有资源同步任务运行中"),
                ResourceSyncResult(updated=False, skipped=True, message=""),
        )
        async with self._sync_lock:
            public_result = await self._sync_from_remote_unlocked(force=force)
            private_result = await self._sync_private_overlays_from_remote_unlocked(force=force)
            if public_result.updated or private_result.updated:
                self.reload()
            return public_result, private_result

    async def sync_from_remote(self, *, force: bool = False) -> ResourceSyncResult:
        """兼容旧调用：单独同步公有包时也进入同一把锁。"""
        async with self._sync_lock:
            result = await self._sync_from_remote_unlocked(force=force)
            if result.updated:
                self.reload()
            return result

    async def sync_private_from_remote(self, *, force: bool = False) -> ResourceSyncResult:
        """兼容旧调用：单独同步私有包时也进入同一把锁，返回多 overlay 聚合结果。"""
        async with self._sync_lock:
            result = await self._sync_private_overlays_from_remote_unlocked(force=force)
            if result.updated:
                self.reload()
            return result

    async def _sync_from_remote_unlocked(self, *, force: bool = False) -> ResourceSyncResult:
        if not plugin_config.rollpig_resource_sync_enabled and not force:
            return ResourceSyncResult(updated=False, skipped=True, message="资源同步未启用")

        manifest_url = str(plugin_config.rollpig_resource_manifest_url or "").strip()
        if not manifest_url:
            return ResourceSyncResult(updated=False, skipped=True, message="未配置资源 manifest URL")

        timeout = _resource_sync_timeout()
        max_file_size = max(1024, int(plugin_config.rollpig_resource_max_file_size or 10 * 1024 * 1024))
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            manifest = await self._download_json(client, manifest_url, max_size=RESOURCE_MANIFEST_MAX_SIZE)

            resource_version = str(manifest.get("resource_version") or "").strip()
            if not resource_version:
                raise ValueError("manifest 缺少 resource_version")
            if not force and resource_version == self._read_state_version():
                # 旧客户端会忽略新 manifest 字段，却仍写入最新 resource_version。
                # 升级到支持差分的客户端后必须检查实体完整性，不能因版本号相同永久漏下差分。
                variants_complete = await asyncio.to_thread(self._active_variant_assets_complete, manifest)
                if variants_complete:
                    return ResourceSyncResult(
                        updated=False,
                        skipped=True,
                        resource_version=resource_version,
                        message=f"公有资源：已是最新（{resource_version}）",
                    )
                logger.info(f"rollpig 当前版本的 EX 差分资源不完整，将重新同步: {resource_version}")

            staging_dir = self._new_staging_dir("incoming")
            (staging_dir / "images").mkdir(parents=True, exist_ok=True)

            try:
                await self._download_manifest_files(
                    client,
                    manifest_url=manifest_url,
                    manifest=manifest,
                    staging_dir=staging_dir,
                    max_size=max_file_size,
                )
                pig_list = self._read_pig_json(staging_dir / "pig.json")
                self._ensure_images_exist(pig_list, [staging_dir / "images"])
                self._load_ex_variants(
                    staging_dir / "pig_ex_variants.json",
                    pig_ids={str(item["id"]) for item in pig_list},
                    image_dir=staging_dir / "images",
                    strict=True,
                )
                self._activate_staging_dir(staging_dir, resource_version)
            except Exception:
                if staging_dir.exists():
                    shutil.rmtree(staging_dir)
                raise

        return ResourceSyncResult(
            updated=True,
            skipped=False,
            resource_version=resource_version,
            message=f"公有资源：已更新（{resource_version}）",
        )

    async def _sync_private_overlays_from_remote_unlocked(self, *, force: bool = False) -> ResourceSyncResult:
        if not plugin_config.rollpig_resource_sync_enabled and not force:
            return ResourceSyncResult(updated=False, skipped=True, message="")
        sources = self._resolve_private_sources(plugin_config)
        if not sources:
            return ResourceSyncResult(updated=False, skipped=True, message="")

        results: list[ResourceSyncResult] = []
        for source in sources:
            try:
                results.append(await self._sync_private_source_from_remote_unlocked(source, force=force))
            except Exception as error:
                # 私有 overlay 是附加包：某个包失败必须报告，但不能让公有包或其它私有包失效。
                logger.warning(f"rollpig 私有资源同步失败，继续使用当前缓存: name={source.name} error={error}")
                results.append(
                    ResourceSyncResult(
                        updated=False,
                        skipped=False,
                        message=f"{self._private_source_label(source)}：同步失败（{error}）",
                    )
                )

        messages = [result.message for result in results if result.message]
        return ResourceSyncResult(
            updated=any(result.updated for result in results),
            skipped=all(result.skipped for result in results),
            resource_version="+".join(result.resource_version for result in results if result.resource_version),
            message="\n".join(messages),
        )

    async def _sync_private_source_from_remote_unlocked(
        self,
        source: _PrivateResourceSource,
        *,
        force: bool = False,
    ) -> ResourceSyncResult:
        timeout = _resource_sync_timeout()
        headers: dict[str, str] = {}
        if source.token:
            headers["Authorization"] = f"Bearer {source.token}"

        max_file_size = max(1024, int(plugin_config.rollpig_resource_max_file_size or 10 * 1024 * 1024))
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as client:
            manifest = await self._download_json(client, source.manifest_url, max_size=RESOURCE_MANIFEST_MAX_SIZE)

            if not bool(manifest.get("overlay")):
                raise ValueError(f"私有资源 manifest 必须标记 overlay=true: {source.name}")

            resource_version = str(manifest.get("resource_version") or "").strip()
            if not resource_version:
                raise ValueError(f"私有资源 manifest 缺少 resource_version: {source.name}")
            if not force and resource_version == self._read_private_state_version(source):
                return ResourceSyncResult(
                    updated=False,
                    skipped=True,
                    resource_version=resource_version,
                    message=f"{self._private_source_label(source)}：已是最新（{resource_version}）",
                )

            staging_dir = self._new_staging_dir(f"incoming_private_{source.name}")
            (staging_dir / "images").mkdir(parents=True, exist_ok=True)

            try:
                await self._download_private_manifest_files(
                    client,
                    manifest_url=source.manifest_url,
                    manifest=manifest,
                    staging_dir=staging_dir,
                    max_size=max_file_size,
                )
                private_pigs = self._read_pig_json(staging_dir / "pig.json")
                self._ensure_images_exist(private_pigs, [staging_dir / "images"])
                self._activate_private_staging_dir(source, staging_dir, resource_version)
            except Exception:
                if staging_dir.exists():
                    shutil.rmtree(staging_dir)
                raise

        return ResourceSyncResult(
            updated=True,
            skipped=False,
            resource_version=resource_version,
            message=f"{self._private_source_label(source)}：已更新（{resource_version}）",
        )

    def _private_source_label(self, source: _PrivateResourceSource) -> str:
        """把内部 overlay 名转成面向 QQ 消息的短标签，避免把缓存名直接暴露给用户。"""

        if source.name == OFFICIAL_GIF_RESOURCE_NAME:
            return "GIF 动态包"
        if source.name == "legacy":
            return "旧版私有资源"
        return f"私有资源 {source.name}"

    async def _download_manifest_files(
        self,
        client: httpx.AsyncClient,
        *,
        manifest_url: str,
        manifest: dict[str, Any],
        staging_dir: Path,
        max_size: int,
    ) -> None:
        pig_json_meta = manifest.get("pig_json")
        if not isinstance(pig_json_meta, dict):
            raise ValueError("manifest 缺少 pig_json")
        budget = _DownloadBudget(max_total_size=RESOURCE_PACKAGE_MAX_SIZE, max_file_count=RESOURCE_MAX_FILES)
        await self._download_file_by_meta(
            client,
            manifest_url=manifest_url,
            meta=pig_json_meta,
            target=staging_dir / "pig.json",
            max_size=min(max_size, RESOURCE_PIG_JSON_MAX_SIZE),
            budget=budget,
        )

        optional_files = manifest.get("optional_files") or {}
        if not isinstance(optional_files, dict):
            raise ValueError("manifest optional_files 必须是 object")
        rules_meta = optional_files.get("pig_rules")
        if isinstance(rules_meta, dict):
            await self._download_file_by_meta(
                client,
                manifest_url=manifest_url,
                meta=rules_meta,
                target=staging_dir / "pig_rules.json",
                max_size=min(max_size, RESOURCE_RULES_JSON_MAX_SIZE),
                budget=budget,
            )

        pig_list = self._read_pig_json(staging_dir / "pig.json")
        pig_ids = {str(item["id"]) for item in pig_list}
        variant_specs: dict[tuple[str, int], _PigExVariantSpec] = {}
        variants_meta = optional_files.get("pig_ex_variants")
        if "pig_ex_variants" in optional_files:
            if not isinstance(variants_meta, dict):
                raise ValueError("optional_files.pig_ex_variants 必须是 object")
            self._validate_required_manifest_meta(
                variants_meta,
                label="optional_files.pig_ex_variants",
                expected_path="pig_ex_variants.json",
            )
            await self._download_file_by_meta(
                client,
                manifest_url=manifest_url,
                meta=variants_meta,
                target=staging_dir / "pig_ex_variants.json",
                max_size=min(max_size, RESOURCE_EX_VARIANTS_JSON_MAX_SIZE),
                budget=budget,
            )
            variant_specs = self._read_ex_variant_specs(
                staging_dir / "pig_ex_variants.json",
                pig_ids=pig_ids,
                strict=True,
            )
        image_variant_specs = {
            key: spec.filename
            for key, spec in variant_specs.items()
            if spec.filename is not None
        }

        image_items = manifest.get("images")
        if not isinstance(image_items, list):
            raise ValueError("manifest 缺少 images 列表")
        if len(image_items) > RESOURCE_MAX_IMAGES:
            raise ValueError(f"manifest images 数量超过上限: {len(image_items)}/{RESOURCE_MAX_IMAGES}")
        base_image_filenames: set[str] = set()
        for image_meta in image_items:
            if not isinstance(image_meta, dict):
                raise ValueError("manifest images 存在非法条目")
            filename = str(image_meta.get("filename") or "")
            self._validate_image_filename(filename)
            if filename in base_image_filenames:
                raise ValueError(f"manifest images 重复声明文件: {filename}")
            base_image_filenames.add(filename)
            await self._download_file_by_meta(
                client,
                manifest_url=manifest_url,
                meta=image_meta,
                target=staging_dir / "images" / filename,
                max_size=max_size,
                budget=budget,
            )

        # ================================ EX差分Manifest对应校验 ================================ #
        # JSON、manifest 和实体图片必须一一对应；先完整检查元数据，再开始下载差分图。
        raw_variant_items = manifest.get("variant_images", [])
        if not isinstance(raw_variant_items, list):
            raise ValueError("manifest variant_images 必须是 list")
        if len(raw_variant_items) > RESOURCE_MAX_VARIANT_IMAGES:
            raise ValueError(
                f"manifest variant_images 数量超过上限: "
                f"{len(raw_variant_items)}/{RESOURCE_MAX_VARIANT_IMAGES}"
            )
        if not isinstance(variants_meta, dict) and raw_variant_items:
            raise ValueError("manifest 声明了 variant_images，但缺少 pig_ex_variants.json")

        seen_variant_keys: set[tuple[str, int]] = set()
        variant_downloads: list[tuple[dict[str, Any], str]] = []
        for index, image_meta in enumerate(raw_variant_items):
            if not isinstance(image_meta, dict):
                raise ValueError(f"manifest variant_images[{index}] 必须是 object")
            unknown_fields = sorted(set(image_meta) - VARIANT_IMAGE_MANIFEST_FIELDS)
            if unknown_fields:
                raise ValueError(
                    f"manifest variant_images[{index}] 含未支持字段: {', '.join(unknown_fields)}"
                )
            pig_id = str(image_meta.get("pig_id") or "")
            raw_level = image_meta.get("level")
            if isinstance(raw_level, bool) or not isinstance(raw_level, int):
                raise ValueError(f"manifest variant_images[{index}].level 必须是整数")
            level = int(raw_level)
            filename = str(image_meta.get("filename") or "")
            self._validate_variant_image_filename(filename, pig_id=pig_id, level=level)
            if filename in base_image_filenames:
                raise ValueError(f"差分图片与基础图片路径冲突: images/{filename}")
            expected_path = f"images/{filename}"
            self._validate_required_manifest_meta(
                image_meta,
                label=f"variant_images[{index}]",
                expected_path=expected_path,
            )

            key = (pig_id, level)
            if key in seen_variant_keys:
                raise ValueError(f"manifest 重复声明差分图片: {pig_id}/EX{level}")
            seen_variant_keys.add(key)
            if image_variant_specs.get(key) != filename:
                raise ValueError(f"manifest 差分图片未与 JSON 对应: {pig_id}/EX{level}")
            variant_downloads.append((image_meta, filename))

        missing_manifest_entries = sorted(set(image_variant_specs) - seen_variant_keys)
        if missing_manifest_entries:
            pig_id, level = missing_manifest_entries[0]
            raise ValueError(f"差分 JSON 引用的图片未写入 manifest: {pig_id}/EX{level}")

        for image_meta, filename in variant_downloads:
            await self._download_file_by_meta(
                client,
                manifest_url=manifest_url,
                meta=image_meta,
                target=staging_dir / "images" / filename,
                max_size=max_size,
                budget=budget,
            )

    async def _download_private_manifest_files(
        self,
        client: httpx.AsyncClient,
        *,
        manifest_url: str,
        manifest: dict[str, Any],
        staging_dir: Path,
        max_size: int,
    ) -> None:
        pig_json_meta = manifest.get("pig_json")
        if not isinstance(pig_json_meta, dict):
            raise ValueError("私有资源 manifest 缺少 pig_json")
        budget = _DownloadBudget(max_total_size=RESOURCE_PACKAGE_MAX_SIZE, max_file_count=RESOURCE_MAX_FILES)
        await self._download_file_by_meta(
            client,
            manifest_url=manifest_url,
            meta=pig_json_meta,
            target=staging_dir / "pig.json",
            max_size=min(max_size, RESOURCE_PIG_JSON_MAX_SIZE),
            budget=budget,
        )

        optional_files = manifest.get("optional_files") or {}
        if not isinstance(optional_files, dict):
            raise ValueError("私有资源 optional_files 必须是 object")
        if optional_files.get("pig_ex_variants") is not None:
            raise ValueError("当前版本暂不支持私有 Overlay 提供 EX 等级差分")
        private_variant_images = manifest.get("variant_images")
        if private_variant_images is not None:
            if not isinstance(private_variant_images, list):
                raise ValueError("私有资源 variant_images 必须是 list 或 null")
            # 旧版构建器可能统一写出空列表；它不包含任何差分，继续按普通 Overlay 处理。
            if private_variant_images:
                raise ValueError("当前版本暂不支持私有 Overlay 提供 EX 等级差分")
        for key, filename in (("pig_rules", "pig_rules.json"), ("pig_overrides", "pig_overrides.json")):
            file_meta = optional_files.get(key)
            if isinstance(file_meta, dict):
                await self._download_file_by_meta(
                    client,
                    manifest_url=manifest_url,
                    meta=file_meta,
                    target=staging_dir / filename,
                    max_size=min(max_size, RESOURCE_RULES_JSON_MAX_SIZE),
                    budget=budget,
                )

        image_items = manifest.get("images") or []
        if not isinstance(image_items, list):
            raise ValueError("私有资源 manifest images 必须是 list")
        if len(image_items) > RESOURCE_MAX_IMAGES:
            raise ValueError(f"私有资源 images 数量超过上限: {len(image_items)}/{RESOURCE_MAX_IMAGES}")
        for image_meta in image_items:
            if not isinstance(image_meta, dict):
                raise ValueError("私有资源 images 存在非法条目")
            filename = str(image_meta.get("filename") or "")
            self._validate_image_filename(filename)
            await self._download_file_by_meta(
                client,
                manifest_url=manifest_url,
                meta=image_meta,
                target=staging_dir / "images" / filename,
                max_size=max_size,
                budget=budget,
            )

    async def _download_json(self, client: httpx.AsyncClient, url: str, *, max_size: int) -> dict[str, Any]:
        content = await self._download_bytes(client, url, max_size=max_size)
        data = json.loads(content.decode("utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("manifest 必须是 JSON object")
        return data

    async def _download_file_by_meta(
        self,
        client: httpx.AsyncClient,
        *,
        manifest_url: str,
        meta: dict[str, Any],
        target: Path,
        max_size: int,
        budget: _DownloadBudget,
    ) -> None:
        path = str(meta.get("path") or meta.get("filename") or "").strip()
        if not path:
            raise ValueError("manifest 文件条目缺少 path")
        self._validate_manifest_path(path)

        expected_size = meta.get("size")
        if expected_size is not None and int(expected_size) > max_size:
            raise ValueError(f"文件超过大小上限: {path}")

        size, actual_hash, tmp = await self._copy_manifest_file_to_temp(
            client,
            manifest_url=manifest_url,
            path=path,
            target=target,
            max_size=max_size,
        )

        try:
            if expected_size is not None and int(expected_size) != size:
                raise ValueError(f"文件大小校验失败: {path}")

            expected_hash = str(meta.get("sha256") or "").lower()
            if expected_hash and actual_hash != expected_hash:
                raise ValueError(f"sha256 校验失败: {path}")

            budget.add_file(path=path, size=size)
            tmp.replace(target)
        finally:
            tmp.unlink(missing_ok=True)

    async def _download_bytes(self, client: httpx.AsyncClient, url: str, *, max_size: int) -> bytes:
        if self._is_local_manifest_url(url):
            path = self._local_manifest_path(url)
            return await asyncio.to_thread(self._read_local_bytes_sync, path, max_size)

        chunks: list[bytes] = []
        total = 0
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            self._validate_content_length(response.headers.get("Content-Length"), max_size=max_size, label=url)
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > max_size:
                    raise ValueError(f"文件超过大小上限: {url}")
                chunks.append(chunk)
        return b"".join(chunks)

    async def _copy_manifest_file_to_temp(
        self,
        client: httpx.AsyncClient,
        *,
        manifest_url: str,
        path: str,
        target: Path,
        max_size: int,
    ) -> tuple[int, str, Path]:
        """按 manifest 来源复制文件；本地包直接拷贝，远端包继续走流式下载。"""

        if self._is_local_manifest_url(manifest_url):
            source = self._local_manifest_path(manifest_url).parent / path
            return await asyncio.to_thread(self._copy_local_file_to_temp_sync, source, target, max_size)

        url = urljoin(manifest_url, path)
        return await self._download_file_to_temp(client, url, target, max_size=max_size)

    def _is_local_manifest_url(self, url: str) -> bool:
        parsed = urlparse(url)
        return parsed.scheme not in {"http", "https"}

    def _local_manifest_path(self, url: str) -> Path:
        """解析本地 manifest 路径；支持普通路径与 file:// URL，供自建私有包离线使用。"""

        parsed = urlparse(url)
        if parsed.scheme == "file":
            raw_path = unquote(parsed.path)
            if re.match(r"^/[A-Za-z]:/", raw_path):
                raw_path = raw_path[1:]
            if parsed.netloc:
                raw_path = f"//{parsed.netloc}{raw_path}"
        else:
            raw_path = url
        path = Path(raw_path).expanduser()
        return path if path.is_absolute() else Path.cwd() / path

    def _read_local_bytes_sync(self, path: Path, max_size: int) -> bytes:
        if not path.exists():
            raise FileNotFoundError(f"本地资源文件不存在: {path}")
        size = path.stat().st_size
        if size > max_size:
            raise ValueError(f"文件超过大小上限: {path}")
        return path.read_bytes()

    def _copy_local_file_to_temp_sync(self, source: Path, target: Path, max_size: int) -> tuple[int, str, Path]:
        """把本地资源文件复制到临时文件并计算 sha256，校验通过前不覆盖 active。"""

        if not source.exists():
            raise FileNotFoundError(f"本地资源文件不存在: {source}")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        total = 0
        hasher = hashlib.sha256()
        try:
            with source.open("rb") as source_file, tmp.open("wb") as target_file:
                for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
                    total += len(chunk)
                    if total > max_size:
                        raise ValueError(f"文件超过大小上限: {source}")
                    hasher.update(chunk)
                    target_file.write(chunk)
            return total, hasher.hexdigest(), tmp
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    async def _download_file_to_temp(
        self,
        client: httpx.AsyncClient,
        url: str,
        target: Path,
        *,
        max_size: int,
    ) -> tuple[int, str, Path]:
        """流式下载到临时文件；校验通过前绝不覆盖目标文件。"""
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        total = 0
        hasher = hashlib.sha256()
        try:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                self._validate_content_length(response.headers.get("Content-Length"), max_size=max_size, label=url)
                with tmp.open("wb") as file:
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > max_size:
                            raise ValueError(f"文件超过大小上限: {url}")
                        hasher.update(chunk)
                        file.write(chunk)
            return total, hasher.hexdigest(), tmp
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    def _activate_staging_dir(self, staging_dir: Path, resource_version: str) -> None:
        self._activate_resource_dir(
            staging_dir=staging_dir,
            active_dir=ACTIVE_RESOURCE_DIR,
            previous_dir=CACHE_ROOT / "previous",
            state_file=STATE_FILE,
            state_payload={"resource_version": resource_version, "synced_at": int(time.time())},
        )

    def _activate_private_staging_dir(
        self,
        source: _PrivateResourceSource,
        staging_dir: Path,
        resource_version: str,
    ) -> None:
        self._activate_resource_dir(
            staging_dir=staging_dir,
            active_dir=source.active_dir,
            previous_dir=source.previous_dir,
            state_file=source.state_file,
            state_payload={"name": source.name, "manifest_url": source.manifest_url, "resource_version": resource_version, "synced_at": int(time.time())},
        )

    def _activate_resource_dir(
        self,
        *,
        staging_dir: Path,
        active_dir: Path,
        previous_dir: Path,
        state_file: Path,
        state_payload: dict[str, Any],
    ) -> None:
        """事务式激活资源目录；任何一步失败都尽量恢复旧 active，避免资源目录被切空。"""
        CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        active_dir.parent.mkdir(parents=True, exist_ok=True)
        previous_dir.parent.mkdir(parents=True, exist_ok=True)
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_tmp = state_file.with_name(f".{state_file.name}.{uuid.uuid4().hex}.tmp")
        moved_old = False
        activated_new = False
        old_active_backup = active_dir.exists()
        old_previous_backup = previous_dir.exists()

        previous_backup_dir = CACHE_ROOT / f".{previous_dir.name}_rollback_{uuid.uuid4().hex}"
        if old_previous_backup:
            previous_dir.rename(previous_backup_dir)

        try:
            if active_dir.exists():
                active_dir.rename(previous_dir)
                moved_old = True
            staging_dir.rename(active_dir)
            activated_new = True
            state_tmp.write_text(json.dumps(state_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            state_tmp.replace(state_file)
            if previous_backup_dir.exists():
                shutil.rmtree(previous_backup_dir)
        except Exception:
            state_tmp.unlink(missing_ok=True)
            if activated_new and active_dir.exists():
                shutil.rmtree(active_dir, ignore_errors=True)
            if moved_old and previous_dir.exists() and not active_dir.exists():
                previous_dir.rename(active_dir)
            if old_previous_backup and previous_backup_dir.exists() and not previous_dir.exists():
                previous_backup_dir.rename(previous_dir)
            raise
        finally:
            if previous_backup_dir.exists():
                shutil.rmtree(previous_backup_dir, ignore_errors=True)

        if not old_active_backup and previous_dir.exists():
            # 没有旧 active 时，previous 不应凭空保留；这个分支只用于清理异常历史残留。
            shutil.rmtree(previous_dir, ignore_errors=True)

    def _new_staging_dir(self, prefix: str) -> Path:
        """每次同步使用 UUID staging，避免同一秒内多任务撞目录。"""
        return CACHE_ROOT / f".{prefix}_{uuid.uuid4().hex}"

    def _validate_content_length(self, content_length: str | None, *, max_size: int, label: str) -> None:
        if not content_length:
            return
        try:
            declared_size = int(content_length)
        except ValueError:
            return
        if declared_size > max_size:
            raise ValueError(f"文件超过大小上限: {label}")

    def _validate_manifest_path(self, path: str) -> None:
        parsed = urlparse(path)
        if parsed.scheme or parsed.netloc or path.startswith("/") or "\\" in path:
            raise ValueError(f"manifest 文件路径非法: {path}")
        parts = path.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError(f"manifest 文件路径非法: {path}")

    def _validate_required_manifest_meta(
        self,
        meta: Mapping[str, Any],
        *,
        label: str,
        expected_path: str,
    ) -> None:
        """差分协议要求路径、整数大小和 SHA256 齐全，避免可选资源绕过完整性校验。"""

        if str(meta.get("path") or "") != expected_path:
            raise ValueError(f"{label}.path 必须为 {expected_path}")
        size = meta.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError(f"{label}.size 必须是非负整数")
        sha256 = meta.get("sha256")
        if not isinstance(sha256, str) or SHA256_PATTERN.fullmatch(sha256) is None:
            raise ValueError(f"{label}.sha256 必须是 64 位小写十六进制")

    # ================================ 校验与解析 ================================ #
    def _read_json_text(self, path: Path) -> str:
        """读取资源 JSON 文本；兼容 1Panel/Windows 上传链路偶发写入的 UTF-8 BOM。"""
        return path.read_text(encoding="utf-8-sig")

    # ================================ EX等级差分解析 ================================ #
    # staging 同步必须严格拒绝坏包；读取既有 active 时则跳过坏差分，保证基础猪仍可用。

    def _report_variant_problem(self, path: Path, message: str, *, strict: bool) -> None:
        """按解析场景抛出或记录差分问题，避免维护两套结构校验。"""

        if strict:
            raise ValueError(message)
        logger.warning(f"rollpig EX 差分已忽略: file={path} error={message}")

    def _read_ex_variant_specs(
        self,
        path: Path,
        *,
        pig_ids: set[str],
        strict: bool,
    ) -> dict[tuple[str, int], _PigExVariantSpec]:
        """读取差分声明并完成纯 JSON 校验；此阶段不要求图片已经下载。"""

        if not path.exists():
            return {}
        try:
            if path.is_symlink():
                raise ValueError("pig_ex_variants.json 不能是符号链接")
            if path.stat().st_size > RESOURCE_EX_VARIANTS_JSON_MAX_SIZE:
                raise ValueError("pig_ex_variants.json 超过大小上限")
            data = json.loads(self._read_json_text(path))
        except Exception as error:
            self._report_variant_problem(path, str(error), strict=strict)
            return {}

        if not isinstance(data, dict):
            self._report_variant_problem(path, "pig_ex_variants.json 必须是 object", strict=strict)
            return {}
        if data.get("schema_version") != 1:
            self._report_variant_problem(path, "pig_ex_variants.json schema_version 当前只能为 1", strict=strict)
            return {}
        unknown_root_fields = sorted(set(data) - {"schema_version", "pigs"})
        if unknown_root_fields:
            self._report_variant_problem(
                path,
                f"pig_ex_variants.json 含未支持字段: {', '.join(unknown_root_fields)}",
                strict=strict,
            )

        raw_pigs = data.get("pigs")
        if not isinstance(raw_pigs, dict):
            self._report_variant_problem(path, "pig_ex_variants.pigs 必须是 object", strict=strict)
            return {}

        specs: dict[tuple[str, int], _PigExVariantSpec] = {}
        for raw_pig_id, raw_pig in raw_pigs.items():
            pig_id = str(raw_pig_id)
            if not PIG_ID_PATTERN.fullmatch(pig_id) or pig_id not in pig_ids:
                self._report_variant_problem(path, f"差分指向不存在或非法的小猪 ID: {pig_id}", strict=strict)
                continue
            if not isinstance(raw_pig, dict):
                self._report_variant_problem(path, f"差分猪条目必须是 object: {pig_id}", strict=strict)
                continue
            unknown_pig_fields = sorted(set(raw_pig) - {"levels"})
            if unknown_pig_fields:
                self._report_variant_problem(
                    path,
                    f"差分猪 {pig_id} 含未支持字段: {', '.join(unknown_pig_fields)}",
                    strict=strict,
                )
                if not strict:
                    continue

            raw_levels = raw_pig.get("levels")
            if not isinstance(raw_levels, dict) or not raw_levels:
                self._report_variant_problem(path, f"差分猪 {pig_id}.levels 必须是非空 object", strict=strict)
                continue
            for raw_level, raw_variant in raw_levels.items():
                if not isinstance(raw_level, str) or raw_level not in {"1", "2", "3", "4", "5"}:
                    self._report_variant_problem(path, f"差分等级非法: {pig_id}/{raw_level}", strict=strict)
                    continue
                level = int(raw_level)
                if not isinstance(raw_variant, dict):
                    self._report_variant_problem(path, f"差分等级条目必须是 object: {pig_id}/EX{level}", strict=strict)
                    continue
                unknown_variant_fields = sorted(set(raw_variant) - {"image", "description", "analysis"})
                if unknown_variant_fields:
                    self._report_variant_problem(
                        path,
                        f"差分 {pig_id}/EX{level} 含未支持字段: {', '.join(unknown_variant_fields)}",
                        strict=strict,
                    )
                    if not strict:
                        continue

                supported_fields = {"image", "description", "analysis"}
                if not set(raw_variant) & supported_fields:
                    self._report_variant_problem(
                        path,
                        f"差分 {pig_id}/EX{level} 至少需要 image、description、analysis 之一",
                        strict=strict,
                    )
                    continue

                filename: str | None = None
                if "image" in raw_variant:
                    raw_filename = raw_variant.get("image")
                    try:
                        self._validate_variant_image_filename(raw_filename, pig_id=pig_id, level=level)
                    except ValueError as error:
                        self._report_variant_problem(path, str(error), strict=strict)
                        continue
                    filename = str(raw_filename)

                text_values: dict[str, str | None] = {}
                invalid_text = False
                for field in ("description", "analysis"):
                    value = raw_variant.get(field)
                    if field in raw_variant and (not isinstance(value, str) or not value.strip()):
                        self._report_variant_problem(
                            path,
                            f"差分 {pig_id}/EX{level}.{field} 必须是非空字符串或省略",
                            strict=strict,
                        )
                        invalid_text = True
                        break
                    text_values[field] = value if isinstance(value, str) else None
                if invalid_text:
                    continue

                key = (pig_id, level)
                specs[key] = _PigExVariantSpec(
                    pig_id=pig_id,
                    level=level,
                    filename=filename,
                    description=text_values["description"],
                    analysis=text_values["analysis"],
                )
                if len(specs) > RESOURCE_MAX_EX_VARIANTS:
                    self._report_variant_problem(
                        path,
                        f"差分条目数量超过上限: {len(specs)}/{RESOURCE_MAX_EX_VARIANTS}",
                        strict=strict,
                    )
                    return {}
        return specs

    def _load_ex_variants(
        self,
        path: Path,
        *,
        pig_ids: set[str],
        image_dir: Path,
        strict: bool,
    ) -> dict[str, dict[int, PigExVariant]]:
        """加载差分并按需绑定本地图片；宽容模式只跳过损坏条目。"""

        specs = self._read_ex_variant_specs(path, pig_ids=pig_ids, strict=strict)
        variants: dict[str, dict[int, PigExVariant]] = {}
        for spec in specs.values():
            image_path: Path | None = None
            if spec.filename is not None:
                image_path = image_dir / spec.filename
                if not image_path.is_file() or image_path.is_symlink():
                    self._report_variant_problem(
                        path,
                        f"差分图片不存在或非法: {spec.filename}",
                        strict=strict,
                    )
                    continue
            variants.setdefault(spec.pig_id, {})[spec.level] = PigExVariant(
                pig_id=spec.pig_id,
                level=spec.level,
                image_path=image_path,
                description=spec.description,
                analysis=spec.analysis,
            )
        return variants

    def _validate_variant_image_filename(self, filename: Any, *, pig_id: str, level: int) -> None:
        """验证差分图文件名与猪 ID、等级严格对应，阻止路径穿越和误配。"""

        if not isinstance(filename, str) or not filename:
            raise ValueError(f"差分 {pig_id}/EX{level} 缺少 image")
        if not PIG_ID_PATTERN.fullmatch(pig_id) or not 1 <= level <= MAX_EXPERT_LEVEL:
            raise ValueError(f"差分图片的猪 ID 或等级非法: {pig_id}/EX{level}")
        path = Path(filename)
        if path.name != filename or "\\" in filename:
            raise ValueError(f"差分图片文件名不能包含路径: {filename}")
        if path.suffix.lower() not in ALLOWED_IMAGE_SUFFIXES or path.suffix != path.suffix.lower():
            raise ValueError(f"差分图片格式不受支持: {filename}")
        if path.stem != f"{pig_id}_ex{level}":
            raise ValueError(f"差分图片文件名必须为 {pig_id}_ex{level}.png 或 .gif: {filename}")

    def _read_pig_json(self, path: Path) -> list[dict[str, Any]]:
        data = json.loads(self._read_json_text(path))
        if not isinstance(data, list):
            raise ValueError(f"pig.json 必须是 list: {path}")
        return data

    def _read_rules_json(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        data = json.loads(self._read_json_text(path))
        if not isinstance(data, dict):
            raise ValueError(f"pig_rules.json 必须是 object: {path}")
        return data

    def _read_pig_overrides_json(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        data = json.loads(self._read_json_text(path))
        if not isinstance(data, list):
            raise ValueError(f"pig_overrides.json 必须是 list: {path}")
        seen_ids: set[str] = set()
        for item in data:
            if not isinstance(item, dict):
                raise ValueError("pig_overrides.json 存在非法条目")
            pig_id = str(item.get("id") or "")
            if not PIG_ID_PATTERN.match(pig_id):
                raise ValueError(f"pig_overrides.json 存在非法 ID: {pig_id}")
            if pig_id in seen_ids:
                raise ValueError(f"pig_overrides.json 存在重复 ID: {pig_id}")
            seen_ids.add(pig_id)
        return data

    def _validate_pig_list(self, pig_list: list[dict[str, Any]]) -> None:
        seen_ids: set[str] = set()
        for item in pig_list:
            if not isinstance(item, dict):
                raise ValueError("pig.json 存在非法条目")
            pig_id = str(item.get("id") or "")
            if not PIG_ID_PATTERN.match(pig_id):
                raise ValueError(f"非法 pig_id: {pig_id}")
            if pig_id in seen_ids:
                raise ValueError(f"重复 pig_id: {pig_id}")
            if not item.get("name"):
                raise ValueError(f"pig 缺少 name: {pig_id}")
            seen_ids.add(pig_id)

    def _ensure_images_exist(self, pig_list: list[dict[str, Any]], image_dirs: list[Path]) -> None:
        missing: list[str] = []
        for item in pig_list:
            pig_id = str(item.get("id") or "")
            if not any(
                (image_dir / f"{pig_id}{suffix}").exists()
                for image_dir in image_dirs
                for suffix in IMAGE_SUFFIX_PRIORITY
            ):
                missing.append(pig_id)
        if missing:
            raise ValueError(f"资源包缺少图片: {', '.join(missing[:10])}")

    def _read_id_set(self, rules: dict[str, Any], key: str) -> set[str]:
        raw_items = rules.get(key) or []
        if not isinstance(raw_items, list):
            raise ValueError(f"pig_rules.{key} 必须是 list")
        result: set[str] = set()
        for raw_id in raw_items:
            pig_id = str(raw_id)
            if not PIG_ID_PATTERN.match(pig_id):
                raise ValueError(f"pig_rules.{key} 存在非法 ID: {pig_id}")
            result.add(pig_id)
        return result

    def _validate_image_filename(self, filename: str) -> None:
        path = Path(filename)
        if path.name != filename:
            raise ValueError(f"图片文件名不能包含路径: {filename}")
        if path.suffix.lower() not in ALLOWED_IMAGE_SUFFIXES:
            raise ValueError(f"不支持的图片格式: {filename}")
        pig_id = path.stem
        if not PIG_ID_PATTERN.match(pig_id):
            raise ValueError(f"图片文件名非法: {filename}")


pig_resource_manager = RollPigResourceManager()


# ================================ 小猪资源快照 ================================ #
# 命令层高频读取 `PIG_LIST`，这里保留同一个 list 对象并用切片刷新。
# 这样 `from ... import PIG_LIST` 的旧调用方式不会因为资源重载而拿到过期引用。
PIG_LIST: list[dict] = []


def reload_rollpig_resources() -> None:
    """刷新内存中的小猪资源快照；资源管理器会在云端资源损坏时回退内置资源。"""

    pig_resource_manager.reload()
    PIG_LIST[:] = pig_resource_manager.pig_list


def find_image_file(pig_id: str) -> Path | None:
    """返回指定小猪的本地图片路径；不存在时返回 None 交给渲染层兜底。"""

    return pig_resource_manager.find_image_file(pig_id)


def get_pig_by_id(pig_id: str | None) -> dict | None:
    """从当前资源快照按 id 查找小猪数据。"""

    if not pig_id:
        return None
    for pig in PIG_LIST:
        if pig["id"] == pig_id:
            return pig
    return None


async def sync_rollpig_resources(force: bool = False) -> str:
    """同步小猪图片包和共享文案；任一附加包失败时保留其当前本地版本。"""

    messages: list[str] = []
    try:
        public_result, private_result = await pig_resource_manager.sync_all(force=force, wait_if_busy=force)
        if public_result.updated or private_result.updated:
            PIG_LIST[:] = pig_resource_manager.pig_list
        for result in (public_result, private_result):
            if result.message:
                messages.append(result.message)
    except Exception as error:
        # 图片包与共享文案是两个独立来源。图片端点故障时仍应继续同步一份
        # 可用的共享文案快照，不能让前者提前终止整条同步流程。
        logger.warning(f"rollpig 小猪图片资源同步失败，继续使用当前资源: {error}")
        messages.append("小猪资源：同步失败，继续使用当前资源")

    # 延迟导入避免资源模块和 AI 文案管理器在初始化阶段形成双向依赖。
    from .roast_manager import roast_manager

    try:
        roast_result = await roast_manager.sync_shared_library(
            force=force,
            wait_if_busy=force,
        )
        if roast_result.message:
            if messages:
                messages.append("")
            messages.append(roast_result.message)
    except Exception as error:
        # 共享文案是只读增强；远端故障不能让已经成功的图片资源同步被判定为失败。
        logger.warning(f"rollpig 共享烤猪文案同步失败，继续使用当前本地库: {error}")
        if messages:
            messages.append("")
        messages.append("共享文案：同步失败，继续使用本地库")
    return "\n".join(messages) or "小猪资源无需同步"


reload_rollpig_resources()
