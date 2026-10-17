from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from ..host import get_plugin_config
from ..host import logger
from pydantic import BaseModel, Field


def _is_json_config_strict() -> bool:
    """仅允许环境变量控制严格模式；配置文件本身坏掉时不能再依赖其中的开关。"""
    return os.getenv("ROLLPIG_CONFIG_STRICT", "").strip().lower() in {"1", "true", "yes", "on"}


def _handle_json_config_error(path: Path, error: Exception) -> dict[str, Any]:
    if _is_json_config_strict():
        raise error
    logger.error(f"rollpig JSON 配置读取失败，已忽略该文件: {path}: {error}")
    return {}


def _load_json_config_file() -> dict[str, Any]:
    """读取可选 JSON 配置；缺失或为空时等价于不配置，不影响插件启动。"""
    env_path = os.getenv("ROLLPIG_CONFIG_FILE", "").strip()
    candidate_paths = [
        Path(env_path) if env_path else None,
        Path.cwd() / "rollpig_config.json",
        Path.cwd() / "config" / "rollpig.json",
    ]
    for raw_path in candidate_paths:
        if raw_path is None:
            continue
        path = raw_path.expanduser()
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict):
                raise ValueError(f"rollpig JSON 配置必须是 object: {path}")
        except (json.JSONDecodeError, OSError, ValueError) as error:
            return _handle_json_config_error(path, error)
        nested = data.get("rollpig")
        if isinstance(nested, dict):
            data = nested
        return {str(key).lower(): value for key, value in data.items()}
    return {}


_EXPLICIT_NULL_OVERRIDE_FIELDS = {
    # 该字段默认指向官方地址，且明确约定 null 表示关闭；只要调用方传入了键，
    # None 就是有效配置值，不能按“未提供可选配置”处理。
    "rollpig_roast_library_manifest_url",
}


def _merge_json_config(env_data: dict[str, Any]) -> dict[str, Any]:
    """JSON 负责降低 .env 负担；NoneBot/.env 传入值始终覆盖 JSON。"""
    json_data = _load_json_config_file()
    if not json_data:
        return env_data
    normalized_env: dict[str, Any] = {}
    for key, value in env_data.items():
        normalized_key = str(key).lower()
        if value is None and normalized_key not in _EXPLICIT_NULL_OVERRIDE_FIELDS:
            continue
        normalized_env[normalized_key] = value
    return {**json_data, **normalized_env}

class PrivateResourceManifestConfig(BaseModel):
    """单个私有资源 overlay 配置；主要由 JSON 配置承载，避免 .env 过度膨胀。"""

    name: Optional[str] = None  # 缓存目录名；不填时会按 URL 自动生成稳定名称
    manifest_url: str  # 支持 http(s) manifest，也支持本地 manifest 文件路径
    token: Optional[str] = None  # 仅自建带鉴权资源服务时需要


class Config(BaseModel):
    def __init__(self, **data: Any):
        super().__init__(**_merge_json_config(data))

    # --- AI 烤猪配置 ---
    rollpig_ai_enabled: bool = False  # 是否开启 AI 生成
    # None 表示继承对应旧字段；不把默认地址或模型放进新字段，避免覆盖旧配置。
    rollpig_ai_api_key: Optional[str] = None
    rollpig_ai_base_url: Optional[str] = None
    rollpig_ai_model: Optional[str] = None
    rollpig_deepseek_key: Optional[str] = None  # DeepSeek API Key
    rollpig_deepseek_base: str = "https://api.deepseek.com" # Base URL
    rollpig_model: str = "deepseek-v4-flash" # 模型名称
    rollpig_ai_timeout: float = 20.0  # 单次 AI 生成超时时间（秒）
    rollpig_ai_concurrency: int = 4  # AI 文案生成并发上限
    rollpig_ai_max_tokens: int = 4096  # AI 单次响应 token 上限
    rollpig_ai_output_max_chars: int = 240  # AI 文案入库前的最大字符数
    rollpig_roast_cooldown_hours: float = 8.0  # 烤群友普通模式冷却时长（小时）
    rollpig_roast_charge_max: int = 2  # 普通烤群友最多可储存次数
    rollpig_storage_backend: str = "local"  # local / cloud
    rollpig_cloud_api_url: Optional[str] = None
    rollpig_cloud_token: Optional[str] = None
    rollpig_cloud_timeout: float = 5.0  # 云存储单次请求超时（秒），运行时限制为 0.5～60
    rollpig_cloud_strict_mode: bool = True  # true=云端异常直接失败；false=读接口可安全兜底，写接口仍提示稍后重试

    # --- 小猪资源云端同步 ---
    # 默认指向 FelisLab 静态资源包；同步失败时只回退到本地缓存/插件内置资源，不影响 Bot 启动。
    rollpig_resource_sync_enabled: bool = True
    rollpig_resource_manifest_url: str = "https://pig.felislab.cc/resources/rollpig/manifest.json"
    rollpig_resource_sync_interval_hours: int = 24
    rollpig_resource_sync_timeout: float = 10.0  # 资源请求超时（秒），运行时限制为 1～240
    rollpig_resource_max_file_size: int = 10 * 1024 * 1024
    # 未填写时使用官方共享烤猪文案；显式设为 "" 或 null 时关闭，并移除纯共享正文。
    # 自建服务可替换为自己的 manifest URL，不需要额外增加独立开关。
    rollpig_roast_library_manifest_url: Optional[str] = (
        "https://pig.felislab.cc/resources/rollpig-roasts/manifest.json"
    )
    # 私有资源包是公有全量包之上的用户 overlay；官方 GIF 包由资源管理器固定随云端资源启用。
    # 0.8.2 起使用 rollpig_private_resource_manifests 配置多个 overlay；
    # 两个旧字段保留兼容，会被当作一个 legacy overlay 追加到列表末尾。
    rollpig_private_resource_manifest_url: Optional[str] = ""
    rollpig_private_resource_token: Optional[str] = None
    rollpig_private_resource_manifests: list[PrivateResourceManifestConfig | str | dict[str, Any]] = Field(default_factory=list)

    # --- 定时日报 ---
    # 日报推送默认关闭，避免新部署实例在管理员未确认前主动向群里发卡。
    # 次日保护独立结算，关闭推送不妨碍主动查阅已封版日报。
    rollpig_daily_summary_enabled: bool = False

    # --- 普通小猪卡片渲染 ---
    # Pillow 不具备浏览器级字体回退；Docker/Linux 缺字或想换风格时可显式指定字体。
    # 相对路径按 Bot 运行目录解析，例如 fonts/msyh.ttc。
    rollpig_card_font_path: Optional[str] = None

    # --- 昨日回顾卡字体 ---
    # 标题和正文可分别替换；相对路径同样按 Bot 运行目录解析。
    # 留空时标题使用内置 ZCOOL 快乐体，正文复用内置思源黑体。
    rollpig_yesterday_card_title_font_path: Optional[str] = None
    rollpig_yesterday_card_body_font_path: Optional[str] = None

    # --- 图片版小猪图鉴 ---
    rollpig_catalog_enabled: bool = True
    # 单次 2× 绘制会同时持有多张大尺寸 RGBA 中间图；默认限制 2 个并发，
    # 防止突发请求把线程池和内存打满。512MB 部署建议显式设为 1。
    rollpig_catalog_render_concurrency: int = 2
    rollpig_catalog_cache_seconds: int = 300
    rollpig_catalog_output_format: str = "png"
    rollpig_catalog_scale_factor: float = 2.0

    # --- 代理设置 (可选，如果服务器在国内连不上API) ---
    rollpig_proxy: Optional[str] = None


# NoneBot 插件配置在启动后是静态快照；集中在这里合并 JSON 配置一次，
# 避免多个模块各自 get_plugin_config(Config) 时重复读取配置文件。
plugin_config = get_plugin_config(Config)
