"""Runtime configuration for the JianerMate plugin.

Matched to the host ``config.others`` convention used by the rest of the
repository: every value has a default so the plugin runs without extra
configuration beside the aiconfig model keys.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from plugins.JianerMate.persona import (
    DEFAULT_BEHAVIOR_STYLE,
    DEFAULT_PERSONALITY,
    DEFAULT_REPLY_STYLE,
    Persona,
)
from plugins.JianerMate.plugin_config import (
    DEFAULT_CONFIG_PATH,
    PluginConfigError,
    flatten_plugin_config,
    load_plugin_config,
    resolve_config_path,
)


PLUGIN_ID = "jianerbot-plugin-jianer-mate"
DEFAULT_DATA_DIR = "data/jianer_mate"
DEFAULT_AI_CONFIG_DIR = "aiconfig"
DEFAULT_QUIET_HOURS = "23:00-08:00"
DEFAULT_KAOMOJI = ("(￣▽￣)", "ovo", "qwq", "嘿嘿", "欸嘿")
DEFAULT_CATCHPHRASES = ("确实", "好耶", "emmm", "哈哈", "啊这")
DEFAULT_TOOL_ALLOWLIST = (
    "get_current_time",
    "calculate_expression",
    "get_current_user_profile",
    "get_current_chat_info",
    "read_recent_chat",
    "search_current_chat",
    "web_search",
    "github_repository",
    "render_information_card",
    "list_agent_skills",
    "load_agent_skill",
    "read_agent_skill_resource",
    "list_my_memories",
)


@dataclass(frozen=True, slots=True)
class ToolOptions:
    """Model-callable tools copied from the JianerAI tool package."""

    enabled: bool = True
    max_rounds: int = 3
    include_web_browser: bool = False
    allow_mutating: bool = False
    allowlist: tuple[str, ...] | None = DEFAULT_TOOL_ALLOWLIST
    max_output_chars: int = 4000
    skills_dirs: tuple[str, ...] = (".agents/skills",)
    skills_max_file_bytes: int = 65536
    mcp_enabled: bool = False
    mcp_servers: tuple[Mapping[str, Any], ...] = ()
    bash_enabled: bool = False
    bash_approval_timeout_seconds: float = 120.0
    bash_command_timeout_seconds: float = 60.0
    subagents_enabled: bool = False
    subagent_max_concurrency: int = 3
    subagent_max_tasks: int = 4
    subagent_timeout_seconds: float = 120.0

@dataclass(frozen=True, slots=True)
class StyleOptions:
    """Short-message and human-like reply shaping."""

    max_parts: int = 3
    max_chars: int = 24
    delay_min_ms: int = 600
    delay_max_ms: int = 1800
    typo_probability: float = 0.08
    typo_correction: bool = True
    kaomoji_probability: float = 0.15
    kaomoji: tuple[str, ...] = DEFAULT_KAOMOJI
    catchphrases: tuple[str, ...] = DEFAULT_CATCHPHRASES


@dataclass(frozen=True, slots=True)
class MemeOptions:
    """Meme library and vision-pick options."""

    enabled: bool = True
    steal_emoji: bool = True
    max_entries: int = 128
    max_size_mb: float = 5.0
    max_dimension: int = 4096
    min_dimension: int = 16
    candidates: int = 8
    thumbnail_px: int = 512
    reuse_cooldown_seconds: int = 3600

    @property
    def max_size_bytes(self) -> int:
        return max(1, int(self.max_size_mb * 1024 * 1024))


@dataclass(frozen=True, slots=True)
class RuntimeOptions:
    """Resolved plugin options."""

    enabled: bool = True
    project_root: Path = field(default_factory=lambda: Path.cwd().resolve())
    data_dir: Path = field(default_factory=lambda: Path(DEFAULT_DATA_DIR))
    database_path: Path = field(
        default_factory=lambda: Path(DEFAULT_DATA_DIR) / "mate.db"
    )
    meme_dir: Path = field(
        default_factory=lambda: Path(DEFAULT_DATA_DIR) / "memes"
    )
    ai_config_dir: Path = field(default_factory=lambda: Path(DEFAULT_AI_CONFIG_DIR))
    planner_model: str = ""
    replyer_model: str = ""
    moderation_enabled: bool = False
    moderation_model: str = ""
    persona: Persona = field(default_factory=Persona)
    style: StyleOptions = field(default_factory=StyleOptions)
    meme: MemeOptions = field(default_factory=MemeOptions)
    tools: ToolOptions = field(default_factory=ToolOptions)
    memory_enabled: bool = True
    memory_backend: str = "memorix"
    memory_db_path: Path = field(
        default_factory=lambda: Path(DEFAULT_DATA_DIR) / "memory.db"
    )
    memory_data_dir: Path = field(
        default_factory=lambda: Path(DEFAULT_DATA_DIR) / "memorix"
    )
    memory_embedding_config_path: Path = field(
        default_factory=lambda: Path(DEFAULT_AI_CONFIG_DIR) / "embedding.json"
    )
    tts_enabled: bool = False
    tts_options: Mapping[str, Any] = field(
        default_factory=lambda: {
            "voice": "zh-CN-XiaoyiNeural",
            "rate": "+0%",
            "volume": "+0%",
            "pitch": "+0Hz",
        }
    )
    suffix_enabled: bool = True
    suffix_config_path: Path = field(
        default_factory=lambda: Path("suffix_config.json")
    )
    context_messages: int = 40
    history_keep_messages: int = 300
    history_retention_days: int = 7
    talk_value_default: float = 0.5
    min_interval_seconds: int = 30
    debounce_seconds: float = 3.0
    max_debounce_seconds: float = 15.0
    quiet_hours_default: str = DEFAULT_QUIET_HOURS
    group_blacklist: tuple[str, ...] = ()
    max_concurrent_ai: int = 2
    request_timeout_seconds: float = 60.0
    reply_at_enabled: bool = False
    mentioned_reply_enabled: bool = True
    backoff_base_seconds: float = 15.0
    backoff_cap_seconds: float = 300.0
    backoff_start_count: int = 2
    config_path: Path = field(default_factory=lambda: Path(DEFAULT_CONFIG_PATH))
    config_loaded: bool = False
    config_keys: tuple[str, ...] = ()
    config_errors: tuple[str, ...] = ()

    @property
    def bot_name(self) -> str:
        return self.persona.name

    @classmethod
    def from_runtime(cls, runtime: Mapping[str, Any] | None) -> "RuntimeOptions":
        runtime = runtime or {}
        config = runtime.get("config")
        host_others = getattr(config, "others", None)
        if not isinstance(host_others, Mapping):
            host_others = {}
        project_root = Path.cwd().resolve()
        config_path = resolve_config_path(project_root, host_others)
        document, config_errors = load_plugin_config(config_path)
        plugin_keys: dict[str, Any] = {}
        if document:
            try:
                plugin_keys = flatten_plugin_config(document)
            except PluginConfigError as exc:
                config_errors = dict(config_errors)
                config_errors[str(config_path)] = str(exc)
        # Dedicated config file wins over the legacy host entries.
        others = {**host_others, **plugin_keys}
        bot_name = _clean_text(others.get("bot_name"), "简儿")
        data_dir = _resolve_path(project_root, others.get("jianer_mate_data_dir"), DEFAULT_DATA_DIR)
        database_value = others.get("jianer_mate_db_path") or (data_dir / "mate.db")
        database_path = _resolve_path(project_root, database_value, str(Path(DEFAULT_DATA_DIR) / "mate.db"))
        meme_dir = _resolve_path(
            project_root,
            others.get("jianer_mate_meme_dir"),
            str(data_dir / "memes"),
        )
        ai_config_dir = _resolve_path(
            project_root,
            others.get("jianer_mate_ai_config_dir"),
            DEFAULT_AI_CONFIG_DIR,
        )
        planner_model = _clean_text(
            others.get("jianer_mate_planner_model"),
            "",
        )
        replyer_model = _clean_text(others.get("jianer_mate_replyer_model"), "")
        moderation_model = _clean_text(
            others.get("jianer_mate_moderation_model"), ""
        )
        delay_min, delay_max = _parse_delay_pair(
            others.get("jianer_mate_reply_delay_ms"),
            (600, 1800),
        )
        context_messages = _clamp_int(
            others.get("jianer_mate_context_messages"), 40, minimum=6, maximum=200
        )
        style = StyleOptions(
            max_parts=_clamp_int(
                others.get("jianer_mate_reply_max_parts"), 3, minimum=1, maximum=6
            ),
            max_chars=_clamp_int(
                others.get("jianer_mate_reply_max_chars"), 24, minimum=8, maximum=200
            ),
            delay_min_ms=delay_min,
            delay_max_ms=delay_max,
            typo_probability=_clamp_float(
                others.get("jianer_mate_typo_probability"),
                0.08,
                minimum=0.0,
                maximum=1.0,
            ),
            typo_correction=_as_bool(
                others.get("jianer_mate_typo_correction"), True
            ),
            kaomoji_probability=_clamp_float(
                others.get("jianer_mate_kaomoji_probability"),
                0.15,
                minimum=0.0,
                maximum=1.0,
            ),
            kaomoji=_as_text_tuple(others.get("jianer_mate_kaomoji"), DEFAULT_KAOMOJI),
            catchphrases=_as_text_tuple(
                others.get("jianer_mate_catchphrases"), DEFAULT_CATCHPHRASES
            ),
        )
        meme = MemeOptions(
            enabled=_as_bool(others.get("jianer_mate_meme_enabled"), True),
            steal_emoji=_as_bool(others.get("jianer_mate_steal_emoji"), True),
            max_entries=_clamp_int(
                others.get("jianer_mate_meme_max_entries"),
                128,
                minimum=1,
                maximum=4096,
            ),
            max_size_mb=_clamp_float(
                others.get("jianer_mate_meme_max_size_mb"),
                5.0,
                minimum=0.1,
                maximum=50.0,
            ),
            max_dimension=_clamp_int(
                others.get("jianer_mate_meme_max_dimension"),
                4096,
                minimum=256,
                maximum=8192,
            ),
            candidates=_clamp_int(
                others.get("jianer_mate_meme_candidates"),
                8,
                minimum=1,
                maximum=24,
            ),
            thumbnail_px=_clamp_int(
                others.get("jianer_mate_meme_thumbnail_px"),
                512,
                minimum=128,
                maximum=1024,
            ),
            reuse_cooldown_seconds=_clamp_int(
                others.get("jianer_mate_meme_reuse_cooldown_seconds"),
                3600,
                minimum=0,
                maximum=86400,
            ),
        )
        allowlist_raw = others.get("jianer_mate_tool_allowlist")
        tool_options = ToolOptions(
            enabled=_as_bool(others.get("jianer_mate_tools_enabled"), True),
            max_rounds=_clamp_int(
                others.get("jianer_mate_tool_max_rounds"),
                3,
                minimum=1,
                maximum=10,
            ),
            include_web_browser=_as_bool(
                others.get("jianer_mate_tool_include_web_browser"), False
            ),
            allow_mutating=_as_bool(
                others.get("jianer_mate_tool_allow_mutating"), False
            ),
            allowlist=(
                DEFAULT_TOOL_ALLOWLIST
                if allowlist_raw is None
                else _as_tool_allowlist(allowlist_raw)
            ),
            max_output_chars=_clamp_int(
                others.get("jianer_mate_tool_max_output_chars"),
                4000,
                minimum=200,
                maximum=20000,
            ),
            skills_dirs=_as_text_tuple(
                others.get("jianer_mate_tool_skills_dirs"), (".agents/skills",)
            ),
            skills_max_file_bytes=_clamp_int(
                others.get("jianer_mate_tool_skills_max_file_bytes"),
                65536,
                minimum=1024,
                maximum=262144,
            ),
            mcp_enabled=_as_bool(
                others.get(
                    "jianer_mate_tool_mcp_enabled",
                    others.get("agent_mcp_enabled"),
                ),
                False,
            ),
            mcp_servers=_mcp_servers(
                others.get(
                    "jianer_mate_tool_mcp_servers",
                    others.get("agent_mcp_servers"),
                )
            ),
            bash_enabled=_as_bool(
                others.get(
                    "jianer_mate_tool_bash_enabled",
                    others.get("agent_bash_enabled"),
                ),
                False,
            ),
            bash_approval_timeout_seconds=_clamp_float(
                others.get(
                    "jianer_mate_tool_bash_approval_timeout_seconds",
                    others.get("agent_bash_approval_timeout_seconds"),
                ),
                120.0,
                minimum=5.0,
                maximum=600.0,
            ),
            bash_command_timeout_seconds=_clamp_float(
                others.get(
                    "jianer_mate_tool_bash_command_timeout_seconds",
                    others.get("agent_bash_command_timeout_seconds"),
                ),
                60.0,
                minimum=1.0,
                maximum=300.0,
            ),
            subagents_enabled=_as_bool(
                others.get(
                    "jianer_mate_tool_subagents_enabled",
                    others.get("agent_subagent_enabled"),
                ),
                False,
            ),
            subagent_max_concurrency=_clamp_int(
                others.get(
                    "jianer_mate_tool_subagent_max_concurrency",
                    others.get("agent_subagent_max_concurrency"),
                ),
                3,
                minimum=1,
                maximum=8,
            ),
            subagent_max_tasks=_clamp_int(
                others.get("jianer_mate_tool_subagent_max_tasks"),
                4,
                minimum=1,
                maximum=8,
            ),
            subagent_timeout_seconds=_clamp_float(
                others.get(
                    "jianer_mate_tool_subagent_timeout_seconds",
                    others.get("agent_subagent_timeout_seconds"),
                ),
                120.0,
                minimum=5.0,
                maximum=600.0,
            ),
        )
        memory_db_path = _resolve_path(
            project_root,
            others.get("jianer_mate_memory_db_path"),
            str(data_dir / "memory.db"),
        )
        return cls(
            enabled=_as_bool(others.get("jianer_mate_enabled"), True),
            project_root=project_root,
            data_dir=data_dir,
            database_path=database_path,
            meme_dir=meme_dir,
            ai_config_dir=ai_config_dir,
            planner_model=planner_model,
            replyer_model=replyer_model,
            moderation_enabled=_as_bool(
                others.get("jianer_mate_moderation_enabled"), False
            ),
            moderation_model=moderation_model,
            persona=Persona.from_value(
                others.get("jianer_mate_persona"),
                fallback_name=bot_name,
            ),
            style=style,
            meme=meme,
            tools=tool_options,
            memory_enabled=_as_bool(
                others.get("jianer_mate_memory_enabled"), True
            ),
            memory_backend=_clean_text(
                others.get("jianer_mate_memory_backend"), "memorix"
            ).casefold(),
            memory_db_path=memory_db_path,
            memory_data_dir=_resolve_path(
                project_root,
                others.get("jianer_mate_memory_data_dir"),
                str(data_dir / "memorix"),
            ),
            memory_embedding_config_path=_resolve_path(
                project_root,
                others.get("jianer_mate_memory_embedding_config_path"),
                str(Path(DEFAULT_AI_CONFIG_DIR) / "embedding.json"),
            ),
            tts_enabled=_as_bool(
                others.get("jianer_mate_tts_enabled"), False
            ),
            tts_options=(
                dict(others.get("jianer_mate_tts"))
                if isinstance(others.get("jianer_mate_tts"), Mapping)
                else {}
            )
            or {
                "voice": "zh-CN-XiaoyiNeural",
                "rate": "+0%",
                "volume": "+0%",
                "pitch": "+0Hz",
            },
            suffix_enabled=_as_bool(
                others.get("jianer_mate_suffix_enabled"), True
            ),
            suffix_config_path=_resolve_path(
                project_root,
                others.get("jianer_mate_suffix_config_path"),
                "suffix_config.json",
            ),
            context_messages=context_messages,
            history_keep_messages=_clamp_int(
                others.get("jianer_mate_history_keep_messages"),
                300,
                minimum=20,
                maximum=2000,
            ),
            history_retention_days=_clamp_int(
                others.get("jianer_mate_history_retention_days"),
                7,
                minimum=1,
                maximum=90,
            ),
            talk_value_default=_clamp_float(
                others.get("jianer_mate_talk_value_default"),
                0.5,
                minimum=0.0,
                maximum=1.0,
            ),
            min_interval_seconds=_clamp_int(
                others.get("jianer_mate_min_interval_seconds"),
                30,
                minimum=0,
                maximum=86400,
            ),
            debounce_seconds=_clamp_float(
                others.get("jianer_mate_debounce_seconds"),
                3.0,
                minimum=0.0,
                maximum=120.0,
            ),
            max_debounce_seconds=_clamp_float(
                others.get("jianer_mate_max_debounce_seconds"),
                15.0,
                minimum=1.0,
                maximum=600.0,
            ),
            quiet_hours_default=_clean_text(
                others.get("jianer_mate_quiet_hours_default"),
                DEFAULT_QUIET_HOURS,
            ),
            group_blacklist=_as_text_tuple(
                others.get("jianer_mate_group_blacklist"), ()
            ),
            max_concurrent_ai=_clamp_int(
                others.get("jianer_mate_max_concurrent_ai"),
                2,
                minimum=1,
                maximum=16,
            ),
            request_timeout_seconds=_clamp_float(
                others.get("jianer_mate_request_timeout_seconds"),
                60.0,
                minimum=5.0,
                maximum=600.0,
            ),
            reply_at_enabled=_as_bool(others.get("jianer_mate_reply_at"), False),
            mentioned_reply_enabled=_as_bool(
                others.get("jianer_mate_mentioned_reply"), True
            ),
            backoff_base_seconds=_clamp_float(
                others.get("jianer_mate_backoff_base_seconds"),
                15.0,
                minimum=0.0,
                maximum=3600.0,
            ),
            backoff_cap_seconds=_clamp_float(
                others.get("jianer_mate_backoff_cap_seconds"),
                300.0,
                minimum=0.0,
                maximum=86400.0,
            ),
            backoff_start_count=_clamp_int(
                others.get("jianer_mate_backoff_start_count"),
                2,
                minimum=1,
                maximum=20,
            ),
            config_path=config_path,
            config_loaded=config_path.is_file() and not config_errors,
            config_keys=tuple(sorted(plugin_keys)),
            config_errors=tuple(
                f"{source}: {message}"
                for source, message in sorted(config_errors.items())
            ),
        )


def _clean_text(value: Any, default: str) -> str:
    text = str(value if value is not None else "").strip()
    return text or default


def _resolve_path(project_root: Path, value: Any, default: str) -> Path:
    raw = str(value if value not in (None, "") else default)
    path = Path(raw)
    if not path.is_absolute():
        path = project_root / path
    return path


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().casefold()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def _clamp_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _clamp_float(value: Any, default: float, *, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _as_text_tuple(value: Any, default: Sequence[str]) -> tuple[str, ...]:
    if value is None:
        return tuple(default)
    if isinstance(value, str):
        items = [item.strip() for item in value.replace("，", ",").split(",")]
    elif isinstance(value, Sequence):
        items = [str(item).strip() for item in value]
    else:
        return tuple(default)
    cleaned = tuple(item for item in items if item)
    return cleaned or tuple(default)


def _parse_delay_pair(value: Any, default: tuple[int, int]) -> tuple[int, int]:
    minimum, maximum = default
    if isinstance(value, Mapping):
        minimum = _clamp_int(value.get("min"), minimum, minimum=0, maximum=60000)
        maximum = _clamp_int(value.get("max"), maximum, minimum=0, maximum=60000)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        items = list(value)
        if len(items) >= 2:
            minimum = _clamp_int(items[0], minimum, minimum=0, maximum=60000)
            maximum = _clamp_int(items[1], maximum, minimum=0, maximum=60000)
    if maximum < minimum:
        maximum = minimum
    return minimum, maximum


def _as_tool_allowlist(value: Any) -> tuple[str, ...]:
    """``[]`` disables every tool; strings accept comma separated names."""

    if isinstance(value, str):
        items: Sequence[Any] = [
            item for item in value.replace("，", ",").split(",")
        ]
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        items = list(value)
    else:
        return DEFAULT_TOOL_ALLOWLIST
    return tuple(str(item).strip() for item in items if str(item).strip())


def _mcp_servers(value: Any) -> tuple[Mapping[str, Any], ...]:
    """Accept JianerAI's mapping form or a plain list of server configs."""

    if isinstance(value, Mapping):
        items: list[Any] = []
        for name, config in value.items():
            if isinstance(config, Mapping):
                merged = dict(config)
                merged.setdefault("name", str(name))
                items.append(merged)
        return tuple(item for item in items if isinstance(item, Mapping))
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return tuple(item for item in value if isinstance(item, Mapping))
    return ()
