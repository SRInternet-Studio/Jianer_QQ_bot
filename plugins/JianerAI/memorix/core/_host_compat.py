"""Jianer-side compatibility primitives for the long-memory host services.

The memory runtime imports a small set of optional ``src.*`` contracts for LLM
calls, configuration and chat history. This module provides those contracts
using Jianer's independent configuration while keeping every import optional.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional

from plugins.JianerAI.providers import ChatRequest, ProviderRegistry


_PROJECT_ROOT = Path.cwd().resolve()
_PROVIDERS: ProviderRegistry | None = None
_MEMORY_STORE: Any | None = None
_PLUGIN_CONFIG: Dict[str, Any] = {}


def configure_host_runtime(
    *,
    project_root: str | Path | None = None,
    memory_store: Any | None = None,
    plugin_config: Dict[str, Any] | None = None,
) -> None:
    """Bind the memory services to Jianer runtime state."""

    global _PROJECT_ROOT, _PROVIDERS, _MEMORY_STORE, _PLUGIN_CONFIG
    if project_root is not None:
        _PROJECT_ROOT = Path(project_root).resolve()
    _MEMORY_STORE = memory_store
    _PLUGIN_CONFIG = dict(plugin_config or {})
    try:
        _PROVIDERS = ProviderRegistry(_PROJECT_ROOT / "aiconfig")
    except Exception:
        _PROVIDERS = None
    _refresh_global_config(_PLUGIN_CONFIG)


def _provider_registry() -> ProviderRegistry:
    global _PROVIDERS
    if _PROVIDERS is None:
        configure_host_runtime(project_root=_PROJECT_ROOT)
    if _PROVIDERS is None:
        raise RuntimeError("Jianer provider registry is unavailable")
    return _PROVIDERS


def _read_ai_config() -> Dict[str, Any]:
    candidates = [
        os.environ.get("JIANER_AI_CONFIG", ""),
        str(_PROJECT_ROOT / "aiconfig" / "ai.json"),
        str(_PROJECT_ROOT / "aiconfig" / "config.json"),
        str(_PROJECT_ROOT / "aiconfig" / "example.ai.json"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            with open(candidate, encoding="utf-8") as stream:
                payload = json.load(stream)
            if isinstance(payload, dict):
                return payload
        except (OSError, ValueError):
            continue
    return {}


@dataclass(frozen=True)
class TaskConfig:
    model_list: list[str] = field(default_factory=list)
    max_tokens: Optional[int] = 2048
    temperature: Optional[float] = 0.2
    selection_strategy: str = "round_robin"
    hard_timeout: float = 120.0


@dataclass(frozen=True)
class LLMServiceRequest:
    task_name: str
    request_type: str
    prompt: str
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None


@dataclass(frozen=True)
class LLMGenerationOptions:
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None


@dataclass(frozen=True)
class _Completion:
    response: str = ""


@dataclass(frozen=True)
class LLMServiceResult:
    success: bool
    completion: _Completion = field(default_factory=_Completion)
    error: str = ""

    @classmethod
    def from_response_result(cls, result: Any) -> "LLMServiceResult":
        return cls(True, _Completion(str(getattr(result, "response", result) or "")))

    @classmethod
    def from_error(cls, message: str, detail: str = "") -> "LLMServiceResult":
        return cls(False, _Completion(""), f"{message}: {detail}".strip(": "))


def _provider_config() -> tuple[str, str, str, str]:
    raw = _read_ai_config()
    provider = str(raw.get("Provider", raw.get("provider", "openai")) or "openai").strip().lower()
    model = str(raw.get("Model", raw.get("model", "")) or "").strip()
    api_key = str(raw.get("ApiKey", raw.get("api_key", "")) or "").strip()
    base_url = str(raw.get("BaseUrl", raw.get("base_url", "https://api.openai.com/v1")) or "").strip().rstrip("/")
    return provider, model, api_key, base_url


def _available_models() -> Dict[str, TaskConfig]:
    try:
        registry = _provider_registry()
        model_names = list(registry.list_models().keys())
    except Exception:
        _provider, model, _key, _base = _provider_config()
        model_names = [model] if model else []
    if not model_names:
        return {}
    configs: Dict[str, TaskConfig] = {}
    for model_name in model_names:
        try:
            item = _provider_registry().get(model_name)
            configs[model_name] = TaskConfig(
                model_list=[model_name],
                max_tokens=int(item.max_tokens),
                temperature=float(item.temperature),
                hard_timeout=float(item.request_timeout_seconds),
            )
        except Exception:
            configs[model_name] = TaskConfig(model_list=[model_name])
    configured_tasks = _PLUGIN_CONFIG.get("llm_tasks")
    if isinstance(configured_tasks, dict):
        result: Dict[str, TaskConfig] = {}
        for task_name, raw_names in configured_tasks.items():
            values = raw_names if isinstance(raw_names, list) else [raw_names]
            names = [str(item).strip() for item in values if str(item).strip() in configs]
            if names:
                result[str(task_name)] = TaskConfig(model_list=names)
        if result:
            return result
    # Jianer model files have no task section. Share the configured
    # candidates across Jianer Memory text tasks while retaining task selection.
    cfg = TaskConfig(model_list=model_names)
    return {name: cfg for name in ("memory", "utils", "planner", "replyer", "tool_use")}


def _extract_response(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
        content = message.get("content", "") if isinstance(message, dict) else ""
        if isinstance(content, list):
            return "".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
        return str(content or "")
    # Gemini's REST response shape.
    candidates = payload.get("candidates")
    if isinstance(candidates, list) and candidates:
        content = candidates[0].get("content", {}) if isinstance(candidates[0], dict) else {}
        parts = content.get("parts", []) if isinstance(content, dict) else []
        return "".join(str(item.get("text", "")) for item in parts if isinstance(item, dict))
    return ""


class LLMServiceClient:
    """Small async client compatible with the runtime LLM contract."""

    def __init__(self, task_name: str = "utils", request_type: str = "") -> None:
        self.task_name = str(task_name or "utils")
        self.request_type = str(request_type or "")
        self._orchestrator = SimpleNamespace(
            model_for_task=_available_models().get(self.task_name),
            model_usage={},
            _refresh_task_config=lambda: _available_models().get(self.task_name),
        )

    async def generate_response(self, prompt: str, options: Optional[LLMGenerationOptions] = None) -> _Completion:
        # Use Jianer's provider abstraction so all configured endpoint styles,
        # retries, safety checks and Gemini/OpenAI response parsing stay in one
        # place. The urllib path below remains a bootstrap fallback for a
        # standalone copied config.
        try:
            registry = _provider_registry()
            task_cfg = self._orchestrator.model_for_task or TaskConfig()
            candidates = [str(item).strip() for item in (getattr(task_cfg, "model_list", []) or []) if str(item).strip()]
            if candidates:
                selected = candidates[0]
                model_cfg = registry.get(selected)
                opts = options or LLMGenerationOptions()
                request = ChatRequest(
                    message=str(prompt),
                    system_prompt="You are the Jianer Memory memory processing service. Return only the requested structured result.",
                )
                # ProviderRegistry uses model-file defaults when options are
                # omitted. For Jianer Memory requests, task settings are encoded
                # by the selected model file; request_type remains in logs.
                response = await registry.chat_request(selected, request)
                if not response:
                    raise RuntimeError("text model returned an empty response")
                return _Completion(response=response)
        except Exception:
            # Preserve a useful error from the configured provider below when
            # the registry has no model files or an old standalone deployment.
            if _provider_registry().list_models():
                raise

        _provider, model, api_key, base_url = _provider_config()
        task_cfg = self._orchestrator.model_for_task or TaskConfig(model_list=[model] if model else [])
        selected = str((getattr(task_cfg, "model_list", []) or [model])[0] or model).strip()
        if not selected or not api_key:
            raise RuntimeError("JianerAI text model is not configured; set aiconfig/ai.json")
        opts = options or LLMGenerationOptions()
        temperature = opts.temperature if opts.temperature is not None else task_cfg.temperature
        max_tokens = opts.max_tokens if opts.max_tokens is not None else task_cfg.max_tokens
        if _provider in {"gemini", "google", "googleai"}:
            url = f"{base_url}/v1beta/models/{selected}:generateContent?key={api_key}"
            body = {"contents": [{"parts": [{"text": str(prompt)}]}]}
            if temperature is not None or max_tokens is not None:
                body["generationConfig"] = {k: v for k, v in (("temperature", temperature), ("maxOutputTokens", max_tokens)) if v is not None}
            headers = {"Content-Type": "application/json"}
        else:
            url = f"{base_url}/chat/completions"
            body = {"model": selected, "messages": [{"role": "user", "content": str(prompt)}]}
            if temperature is not None:
                body["temperature"] = temperature
            if max_tokens is not None:
                body["max_tokens"] = max_tokens
            headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}

        def _request() -> str:
            request = urllib.request.Request(
                url,
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=float(getattr(task_cfg, "hard_timeout", 120.0) or 120.0)) as response:
                return _extract_response(json.loads(response.read().decode("utf-8")))

        response = await asyncio.to_thread(_request)
        if not response:
            raise RuntimeError("text model returned an empty response")
        return _Completion(response=response)


async def generate(request: LLMServiceRequest) -> LLMServiceResult:
    try:
        client = LLMServiceClient(task_name=request.task_name, request_type=request.request_type)
        response = await client.generate_response(
            request.prompt,
            LLMGenerationOptions(temperature=request.temperature, max_tokens=request.max_tokens),
        )
        return LLMServiceResult(True, response)
    except Exception as exc:
        return LLMServiceResult.from_error("文本模型调用失败", str(exc))


class _LLMApi:
    LLMServiceRequest = LLMServiceRequest
    LLMGenerationOptions = LLMGenerationOptions
    LLMServiceClient = LLMServiceClient
    LLMServiceResult = LLMServiceResult

    @staticmethod
    def get_available_models() -> Dict[str, TaskConfig]:
        return _available_models()

    @staticmethod
    async def generate(request: LLMServiceRequest) -> LLMServiceResult:
        return await generate(request)


llm_api = _LLMApi()


def load_prompt(name: str, **kwargs: Any) -> str:
    """Render the two structured prompt contracts used by Jianer Memory.

    Jianer does not ship a separate i18n catalog. These templates preserve the
    structured output and safety rules the services validate downstream.
    """

    if name == "a_memorix_chat_summary":
        template = (
            "请将聊天记录压缩为可长期检索的 JSON。只输出一个对象，字段必须为 "
            "summary、entities、relations、facts。summary 是中文摘要；没有新的长期信息时四个字段都必须为空数组或空字符串。"
            "不要保存密码、令牌、验证码或一次性闲聊。\n"
        )
    elif name == "memory_fuzzy_modify_plan":
        template = (
            "你是 Jianer Memory 记忆修正规划器。只输出 JSON 对象：confidence、reason、operations。"
            "operations 只能使用 mark_superseded、ingest_text、refresh_person_profile；目标必须来自 candidates。"
            "不要编造候选 ID，不要删除无法确认的事实。\n"
        )
    else:
        template = f"Jianer Memory task: {name}\n"
    for key, value in kwargs.items():
        template += f"{key}: {value}\n"
    return template


def _decode_stream_id(value: Any) -> tuple[str, str, str, str] | None:
    token = str(value or "").strip()
    parts = token.split(":", 3)
    if len(parts) == 4 and parts[2] in {"group", "private"}:
        return tuple(parts)  # type: ignore[return-value]
    return None


def _message_list(*_args: Any, **kwargs: Any) -> list[Any]:
    store = _MEMORY_STORE
    if store is None or not callable(getattr(store, "query_recent_chat", None)):
        return []
    decoded = _decode_stream_id(kwargs.get("chat_id"))
    if decoded is None:
        return []
    protocol, self_id, kind, conversation_id = decoded
    try:
        messages = store.query_recent_chat(
            protocol=protocol,
            self_id=self_id,
            conversation_kind=kind,
            conversation_id=conversation_id,
            limit=int(kwargs.get("limit", 50) or 50),
            max_characters=32000,
        )
    except Exception:
        return []
    start = float(kwargs.get("start_time", 0.0) or 0.0)
    end = float(kwargs.get("end_time", 0.0) or 0.0)
    selected = []
    for message in messages:
        stamp = getattr(message, "occurred_at", getattr(message, "timestamp", 0.0))
        try:
            stamp = float(stamp)
        except Exception:
            stamp = 0.0
        if start and stamp < start or end and stamp > end:
            continue
        selected.append(message)
    if kwargs.get("limit_mode") == "latest":
        selected.reverse()
    return selected


def _readable_messages(messages: Any) -> str:
    lines = []
    for message in messages or []:
        content = getattr(message, "content", None) or (message.get("content", "") if isinstance(message, dict) else "")
        if content:
            lines.append(str(content))
    return "\n".join(lines)


message_api = SimpleNamespace(get_messages_by_time_in_chat=_message_list, build_readable_messages=_readable_messages)


def _get_session(session_id: Any) -> Any:
    decoded = _decode_stream_id(session_id)
    if decoded is None:
        return None
    protocol, self_id, kind, conversation_id = decoded
    return SimpleNamespace(
        protocol=protocol,
        self_id=self_id,
        group_id=conversation_id if kind == "group" else "",
        user_id=conversation_id if kind == "private" else "",
    )


chat_manager = SimpleNamespace(get_existing_session_by_session_id=_get_session)


def _integration_defaults() -> SimpleNamespace:
    values = {
        "feedback_correction_enabled": False,
        "feedback_correction_window_hours": 12.0,
        "feedback_correction_check_interval_minutes": 30,
        "feedback_correction_batch_size": 20,
        "feedback_correction_auto_apply_threshold": 0.85,
        "feedback_correction_max_feedback_messages": 30,
        "feedback_correction_prefilter_enabled": True,
        "feedback_correction_paragraph_mark_enabled": True,
        "feedback_correction_paragraph_hard_filter_enabled": True,
        "feedback_correction_profile_refresh_enabled": True,
        "feedback_correction_profile_force_refresh_on_read": True,
        "feedback_correction_reconcile_batch_size": 20,
        "feedback_correction_reconcile_interval_minutes": 30,
        "feedback_correction_episode_query_block_enabled": True,
        "feedback_correction_episode_rebuild_enabled": True,
        "feedback_correction_profile_refresh_enabled": True,
        "fuzzy_modify_enabled": True,
        "fuzzy_modify_auto_execute_enabled": False,
        "fuzzy_modify_confirm_threshold": 0.85,
        "fuzzy_modify_candidate_limit": 20,
        "fuzzy_modify_max_targets": 5,
        "fuzzy_modify_allow_global_scope": False,
    }
    return SimpleNamespace(**values)


global_config = SimpleNamespace(
    bot=SimpleNamespace(nickname="Jianer"),
    personality=SimpleNamespace(personality=""),
    a_memorix=SimpleNamespace(integration=_integration_defaults()),
)


def _refresh_global_config(plugin_config: Dict[str, Any]) -> None:
    integration = dict(plugin_config.get("integration") or {})
    if isinstance(plugin_config.get("feedback_correction"), dict):
        integration.update(plugin_config["feedback_correction"])
    current = global_config.a_memorix.integration
    for key, value in integration.items():
        if hasattr(current, str(key)):
            setattr(current, str(key), value)
    bot = plugin_config.get("bot") if isinstance(plugin_config.get("bot"), dict) else {}
    personality = plugin_config.get("personality") if isinstance(plugin_config.get("personality"), dict) else {}
    if bot.get("nickname"):
        global_config.bot.nickname = str(bot["nickname"])
    if personality.get("personality"):
        global_config.personality.personality = str(personality["personality"])


class _ConfigManager:
    def get_model_config(self) -> Any:
        models = _available_models()
        try:
            models_dict = {
                name: _provider_registry().get(name)
                for cfg in models.values()
                for name in cfg.model_list
            }
        except Exception:
            models_dict = {}
        return SimpleNamespace(models_dict=models_dict, models=models)


config_manager = _ConfigManager()


class _MissingSession:
    def __enter__(self) -> "_MissingSession":
        raise RuntimeError("external person database is unavailable in Jianer; use Jianer Memory metadata")

    def __exit__(self, *_args: Any) -> None:
        return None


def get_db_session(*_args: Any, **_kwargs: Any) -> _MissingSession:
    return _MissingSession()


class PersonInfo:
    person_id = SimpleNamespace()
    person_name = SimpleNamespace()
    user_nickname = SimpleNamespace()
    group_cardname = SimpleNamespace()


__all__ = [
    "configure_host_runtime",
    "TaskConfig",
    "LLMServiceClient",
    "LLMServiceRequest",
    "LLMGenerationOptions",
    "LLMServiceResult",
    "llm_api",
    "load_prompt",
    "message_api",
    "chat_manager",
    "global_config",
    "config_manager",
    "get_db_session",
    "PersonInfo",
]
