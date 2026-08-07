from __future__ import annotations

import base64
import hashlib
import inspect
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from plugins.JianerAI.media_input import (
    MediaProcessingError,
    normalize_audio,
    process_video,
)


DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com"
DEFAULT_REQUEST_TIMEOUT = 120.0
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
_SUPPORTED_ATTACHMENT_MIME_PREFIXES = ("image/", "audio/", "video/")
_PROTECTED_PAYLOAD_KEYS = frozenset(
    {
        "model",
        "messages",
        "input",
        "instructions",
        "previous_response_id",
        "store",
        "contents",
        "systemInstruction",
        "generationConfig",
        "system",
        "tools",
        "tool_choice",
    }
)


class ProviderError(RuntimeError):
    """Base error surfaced by the AI provider layer."""


class ModelConfigError(ProviderError):
    """A model configuration cannot be loaded or used."""


class UnknownModelError(ProviderError):
    """The requested model configuration does not exist."""


class ProviderRequestError(ProviderError):
    """A provider request failed without exposing credentials or response bodies."""


class EmptyProviderResponseError(ProviderError):
    """The provider returned no assistant text."""


class ToolsUnsupportedError(ProviderError):
    """The configured endpoint explicitly rejected function tools."""


class MediaCapabilityError(ProviderError):
    """The selected protocol cannot safely consume an attachment."""

    def __init__(self, safe_message: str) -> None:
        self.safe_message = str(safe_message or "当前模型无法读取该附件。").strip()
        super().__init__(self.safe_message)


@dataclass(frozen=True, slots=True)
class MediaAttachment:
    """Trusted media bytes produced by JianerCore ``resolve_media``.

    This object deliberately has no URL/path constructor. Callers must resolve
    an untrusted locator through the adapter first, then pass the fixed
    ``MediaResolution`` result to :meth:`from_resolution`.
    """

    data: bytes = field(repr=False)
    mime: str
    source: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.data, bytes) or not self.data:
            raise ValueError("attachment data must be non-empty bytes")
        if len(self.data) > MAX_ATTACHMENT_BYTES:
            raise ValueError("attachment exceeds the provider safety limit")
        normalized_mime = str(self.mime or "").split(";", 1)[0].strip().casefold()
        if not normalized_mime.startswith(_SUPPORTED_ATTACHMENT_MIME_PREFIXES):
            raise ValueError(f"unsupported attachment MIME type: {normalized_mime or 'missing'}")
        object.__setattr__(self, "mime", normalized_mime)
        object.__setattr__(self, "source", str(self.source or ""))

    @classmethod
    def from_resolution(cls, resolution: Any) -> "MediaAttachment":
        status = _get_value(resolution, "status")
        status_value = getattr(status, "value", status)
        if str(status_value).casefold() != "ok":
            error_code = _get_value(resolution, "error_code")
            safe_code = getattr(error_code, "value", error_code) or "resolution_failed"
            raise ValueError(f"media resolution did not succeed: {safe_code}")
        data = _get_value(resolution, "data")
        mime = _get_value(resolution, "mime")
        size = _get_value(resolution, "size")
        if not isinstance(data, bytes) or size != len(data):
            raise ValueError("media resolution contains inconsistent bytes")
        return cls(
            data=data,
            mime=str(mime or ""),
            source=str(_get_value(resolution, "source") or ""),
        )


@dataclass(frozen=True, slots=True)
class ModelConfig:
    key: str
    friendly_name: str
    provider: str
    model: str
    api_key: str = field(repr=False)
    base_url: str = ""
    temperature: float = 0.5
    max_tokens: int = 1000
    top_p: float = 1.0
    personality: str = ""
    empty_response_text: str = "模型没有返回任何内容"
    max_history_messages: int = 20
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT
    endpoint_style: str = ""
    tools_enabled: str = "auto"
    transcription_model: str = "gpt-4o-mini-transcribe"
    thinking_level: str = ""
    extra_parameters: Mapping[str, Any] = field(default_factory=dict, repr=False)
    extra_body: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        key = str(self.key or "").strip()
        friendly_name = str(self.friendly_name or key).strip()
        provider = _normalize_provider(self.provider)
        model = str(self.model or "").strip()
        api_key = str(self.api_key or "").strip()
        if not key:
            raise ModelConfigError("model config key cannot be empty")
        if not model:
            raise ModelConfigError(f"model config {key!r} is missing Model")
        if not api_key:
            raise ModelConfigError(f"model config {key!r} is missing an API key")
        if self.max_tokens <= 0:
            raise ModelConfigError(f"model config {key!r} has an invalid MaxTokens")
        if self.max_history_messages < 0:
            raise ModelConfigError(
                f"model config {key!r} has an invalid max_history_messages"
            )
        if self.request_timeout_seconds <= 0:
            raise ModelConfigError(f"model config {key!r} has an invalid timeout")
        object.__setattr__(self, "key", key)
        object.__setattr__(self, "friendly_name", friendly_name or key)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "api_key", api_key)
        object.__setattr__(self, "base_url", str(self.base_url or "").strip())
        object.__setattr__(self, "personality", str(self.personality or ""))
        object.__setattr__(
            self,
            "transcription_model",
            str(self.transcription_model or "gpt-4o-mini-transcribe").strip(),
        )
        thinking_level = str(self.thinking_level or "").strip().casefold()
        if thinking_level not in {"", "minimal", "low", "medium", "high"}:
            raise ModelConfigError(
                "ThinkingLevel must be minimal, low, medium, or high"
            )
        object.__setattr__(self, "thinking_level", thinking_level)
        object.__setattr__(
            self,
            "empty_response_text",
            str(self.empty_response_text or "模型没有返回任何内容"),
        )
        object.__setattr__(
            self,
            "endpoint_style",
            str(self.endpoint_style or "").strip().casefold(),
        )
        tools_enabled = self.tools_enabled
        if isinstance(tools_enabled, bool):
            tools_mode = "enabled" if tools_enabled else "disabled"
        else:
            tools_mode = str(tools_enabled or "auto").strip().casefold()
        aliases = {
            "true": "enabled",
            "on": "enabled",
            "enabled": "enabled",
            "false": "disabled",
            "off": "disabled",
            "disabled": "disabled",
            "auto": "auto",
        }
        if tools_mode not in aliases:
            raise ModelConfigError(
                f"model config {key!r} has an invalid ToolsEnabled value"
            )
        object.__setattr__(self, "tools_enabled", aliases[tools_mode])
        object.__setattr__(self, "extra_parameters", dict(self.extra_parameters))
        object.__setattr__(self, "extra_body", dict(self.extra_body))


class ProviderTransport(Protocol):
    def __call__(
        self,
        provider: str,
        config: ModelConfig,
        payload: Mapping[str, Any],
    ) -> Awaitable[Any] | Any:
        ...


@dataclass(frozen=True, slots=True)
class ChatRequest:
    message: str
    history: tuple[Mapping[str, Any], ...] = ()
    system_prompt: str = ""
    attachments: tuple[MediaAttachment, ...] = ()
    tools: tuple["FunctionTool", ...] = ()
    turns: tuple["AssistantTurn | ToolResultTurn", ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "message", str(self.message or ""))
        object.__setattr__(self, "system_prompt", str(self.system_prompt or ""))
        object.__setattr__(self, "history", tuple(self.history or ()))
        attachments = tuple(self.attachments or ())
        if any(not isinstance(item, MediaAttachment) for item in attachments):
            raise TypeError(
                "attachments must be MediaAttachment objects created from resolved bytes"
            )
        object.__setattr__(self, "attachments", attachments)
        tools = tuple(self.tools or ())
        if any(not isinstance(item, FunctionTool) for item in tools):
            raise TypeError("tools must be FunctionTool objects")
        object.__setattr__(self, "tools", tools)
        turns = tuple(self.turns or ())
        if any(not isinstance(item, (AssistantTurn, ToolResultTurn)) for item in turns):
            raise TypeError("turns must contain AssistantTurn or ToolResultTurn objects")
        object.__setattr__(self, "turns", turns)


@dataclass(frozen=True, slots=True)
class FunctionTool:
    name: str
    description: str
    parameters: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", str(self.name or "").strip())
        object.__setattr__(self, "description", str(self.description or "").strip())
        object.__setattr__(self, "parameters", dict(self.parameters))


@dataclass(frozen=True, slots=True)
class ProviderToolCall:
    id: str
    name: str
    arguments: Any


@dataclass(frozen=True, slots=True)
class AssistantTurn:
    text: str = ""
    tool_calls: tuple[ProviderToolCall, ...] = ()
    provider_content: Mapping[str, Any] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", str(self.text or ""))
        object.__setattr__(self, "tool_calls", tuple(self.tool_calls or ()))
        if self.provider_content is not None:
            object.__setattr__(self, "provider_content", _plain_mapping(self.provider_content))


@dataclass(frozen=True, slots=True)
class ToolResultTurn:
    call_id: str
    name: str
    content: str


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    text: str
    tool_calls: tuple[ProviderToolCall, ...]
    turn: AssistantTurn


class ProviderRegistry:
    """Loads model configs and dispatches async chat requests.

    A transport can be injected for tests or custom networking. The default
    transports are the only code paths that perform network I/O.
    """

    def __init__(
        self,
        config_dir: str | Path,
        *,
        transport: ProviderTransport | None = None,
    ) -> None:
        self.config_dir = Path(config_dir)
        self._transport = transport
        self._configs: dict[str, ModelConfig] = {}
        self._load_errors: dict[str, str] = {}
        self._tool_unsupported: set[str] = set()
        self.reload()

    @property
    def load_errors(self) -> Mapping[str, str]:
        return dict(self._load_errors)

    def reload(self) -> Mapping[str, ModelConfig]:
        configs: dict[str, ModelConfig] = {}
        errors: dict[str, str] = {}
        if not self.config_dir.exists():
            self._configs = {}
            self._load_errors = {}
            return {}
        if not self.config_dir.is_dir():
            raise ModelConfigError(f"AI config path is not a directory: {self.config_dir}")
        for path in sorted(self.config_dir.glob("*.ai.json")):
            try:
                config = load_model_config(path)
            except (OSError, json.JSONDecodeError, ModelConfigError, TypeError, ValueError) as exc:
                errors[path.name] = _safe_config_error(exc)
                continue
            if config.key in configs:
                errors[path.name] = f"duplicate model config key: {config.key}"
                continue
            configs[config.key] = config
        self._configs = configs
        self._load_errors = errors
        self._tool_unsupported.intersection_update(configs)
        return dict(configs)

    def list_models(self) -> Mapping[str, str]:
        return {
            key: config.friendly_name
            for key, config in sorted(self._configs.items())
        }

    def get(self, key: str) -> ModelConfig:
        normalized = str(key or "").strip()
        try:
            return self._configs[normalized]
        except KeyError as exc:
            raise UnknownModelError(f"unknown AI model config: {normalized}") from exc

    def supports_tools(self, key: str) -> bool:
        config = self.get(key)
        return (
            config.tools_enabled != "disabled"
            and config.key not in self._tool_unsupported
        )

    def mark_tools_unsupported(self, key: str) -> None:
        self._tool_unsupported.add(self.get(key).key)

    async def chat(
        self,
        key: str,
        message: str,
        *,
        history: Sequence[Mapping[str, Any]] = (),
        system_prompt: str = "",
        attachments: Sequence[MediaAttachment] = (),
    ) -> str:
        return await self.chat_request(
            key,
            ChatRequest(
                message=message,
                history=tuple(history),
                system_prompt=system_prompt,
                attachments=tuple(attachments),
            ),
        )

    async def chat_request(self, key: str, request: ChatRequest) -> str:
        response = await self.complete_request(key, request)
        normalized_text = str(response.text or "").rstrip()
        if normalized_text:
            return normalized_text
        config = self.get(key)
        if config.empty_response_text:
            return config.empty_response_text
        raise EmptyProviderResponseError("provider returned no assistant text")

    async def complete_request(
        self,
        key: str,
        request: ChatRequest,
    ) -> ProviderResponse:
        config = self.get(key)
        if request.tools and not self.supports_tools(key):
            raise ToolsUnsupportedError(
                f"function tools are disabled for model {config.key!r}"
            )
        if config.provider == "openai_chat_completions":
            request = await self._prepare_openai_chat_request(request)
            payload = _build_openai_payload(config, request)
            response = await self._request("openai", config, payload)
            result = await _extract_openai_response(response)
            if not result.tool_calls:
                textual_calls = _parse_dsml_tool_calls(result.text)
                if textual_calls:
                    turn = AssistantTurn(tool_calls=textual_calls)
                    result = ProviderResponse("", textual_calls, turn)
                elif _looks_like_dsml_tool_call(result.text):
                    raise ProviderRequestError(
                        "provider returned malformed textual tool calls"
                    )
        elif config.provider == "openai_responses":
            result = await self._complete_responses(config, request)
        elif config.provider == "google_generate_content":
            payload = _build_gemini_payload(config, request)
            response = await self._request("gemini", config, payload)
            result = _extract_gemini_response(response)
            if not result.text and not result.tool_calls:
                raise _gemini_empty_response_error(response)
        elif config.provider == "anthropic_messages":
            payload = _build_anthropic_payload(config, request)
            response = await self._request("anthropic", config, payload)
            result = _extract_anthropic_response(response)
        else:  # ModelConfig validation keeps this branch unreachable.
            raise ModelConfigError(f"unsupported provider: {config.provider}")
        normalized_text = str(result.text or "").rstrip()
        if not normalized_text and not result.tool_calls:
            normalized_text = config.empty_response_text
            if not normalized_text:
                raise EmptyProviderResponseError("provider returned no assistant text")
        if normalized_text != result.text:
            turn = AssistantTurn(
                text=normalized_text,
                tool_calls=result.tool_calls,
                provider_content=result.turn.provider_content,
            )
            return ProviderResponse(normalized_text, result.tool_calls, turn)
        return result

    async def _complete_responses(
        self,
        config: ModelConfig,
        request: ChatRequest,
    ) -> ProviderResponse:
        prepared_request = await self._prepare_responses_request(
            config,
            request,
        )
        payload = _build_responses_payload(config, prepared_request)
        response = await self._request("responses", config, payload)
        return _extract_responses_response(response)

    async def _prepare_openai_chat_request(
        self,
        request: ChatRequest,
    ) -> ChatRequest:
        prepared: list[MediaAttachment] = []
        for attachment in request.attachments:
            if attachment.mime.startswith("video/"):
                raise MediaCapabilityError(
                    "OpenAI Chat Completions 不支持视频输入，请切换到 "
                    "OpenAI Responses 或 Google GenerateContent。"
                )
            if not attachment.mime.startswith("audio/") or attachment.mime in {
                "audio/mpeg",
                "audio/mp3",
                "audio/wav",
                "audio/x-wav",
            }:
                prepared.append(attachment)
                continue
            try:
                normalized = await normalize_audio(
                    attachment.data,
                    attachment.mime,
                )
            except MediaProcessingError as exc:
                raise MediaCapabilityError(exc.safe_message) from exc
            prepared.append(
                MediaAttachment(
                    data=normalized.data,
                    mime=normalized.mime,
                    source=attachment.source,
                )
            )
        return replace(request, attachments=tuple(prepared))

    async def _prepare_responses_request(
        self,
        config: ModelConfig,
        request: ChatRequest,
    ) -> ChatRequest:
        prepared: list[MediaAttachment] = []
        notes: list[str] = []
        video_count = 0
        for attachment in request.attachments:
            if attachment.mime.startswith("image/"):
                prepared.append(attachment)
                continue
            if attachment.mime.startswith("audio/"):
                try:
                    normalized = await normalize_audio(
                        attachment.data,
                        attachment.mime,
                    )
                except MediaProcessingError as exc:
                    raise MediaCapabilityError(exc.safe_message) from exc
                transcript = await self._transcribe_audio(
                    config,
                    normalized.data,
                    normalized.mime,
                )
                notes.append(f"[语音转写]\n{transcript}")
                continue
            if attachment.mime.startswith("video/"):
                video_count += 1
                if video_count > 1:
                    raise MediaCapabilityError(
                        "OpenAI Responses 每次最多处理一个视频附件。"
                    )
                try:
                    video = await process_video(
                        attachment.data,
                        attachment.mime,
                    )
                except MediaProcessingError as exc:
                    raise MediaCapabilityError(exc.safe_message) from exc
                prepared.extend(
                    MediaAttachment(
                        data=frame.data,
                        mime=frame.mime,
                        source=attachment.source,
                    )
                    for frame in video.frames
                )
                notes.append(
                    "[视频画面] 已按时间顺序抽取 "
                    f"{len(video.frames)} 帧（视频时长约 "
                    f"{video.duration_seconds:.1f} 秒）。"
                )
                if video.audio is not None:
                    transcript = await self._transcribe_audio(
                        config,
                        video.audio.data,
                        video.audio.mime,
                    )
                    notes.append(f"[视频音轨转写]\n{transcript}")
                continue
            raise MediaCapabilityError(
                f"OpenAI Responses 不支持附件格式 {attachment.mime}。"
            )
        message = request.message
        if notes:
            note_text = "\n\n".join(notes)
            message = f"{message}\n\n{note_text}" if message else note_text
        return replace(
            request,
            message=message,
            attachments=tuple(prepared),
        )

    async def _transcribe_audio(
        self,
        config: ModelConfig,
        data: bytes,
        mime: str,
    ) -> str:
        response = await self._request(
            "transcription",
            config,
            {
                "model": config.transcription_model,
                "file": {
                    "name": f"input.{_audio_format(mime)}",
                    "data": data,
                    "mime": mime,
                },
            },
        )
        text = _extract_transcription_text(response).strip()
        if not text:
            raise MediaCapabilityError("语音转写没有返回可识别的文字。")
        return text

    async def _request(
        self,
        provider: str,
        config: ModelConfig,
        payload: Mapping[str, Any],
    ) -> Any:
        try:
            if self._transport is not None:
                result = self._transport(provider, config, payload)
                return await result if inspect.isawaitable(result) else result
            if provider == "openai":
                return await _default_openai_request(config, payload)
            if provider == "responses":
                return await _default_responses_request(config, payload)
            if provider == "transcription":
                return await _default_transcription_request(config, payload)
            if provider == "gemini":
                return await _default_gemini_request(config, payload)
            if provider == "anthropic":
                return await _default_anthropic_request(config, payload)
            raise ModelConfigError(f"unsupported provider transport: {provider}")
        except ProviderError:
            raise
        except Exception as exc:
            if payload.get("tools") and _is_tools_unsupported_exception(exc):
                raise ToolsUnsupportedError(
                    f"{provider} endpoint rejected function tools for model {config.key!r}"
                ) from exc
            raise ProviderRequestError(
                f"{provider} request failed for model {config.key!r}"
            ) from exc


def load_model_config(path: str | Path) -> ModelConfig:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as stream:
        raw = json.load(stream)
    if not isinstance(raw, dict):
        raise ModelConfigError(f"{config_path.name} must contain a JSON object")
    key = config_path.name
    if key.casefold().endswith(".ai.json"):
        key = key[: -len(".ai.json")]
    provider = (
        raw.get("ResponseType")
        or raw.get("Provider")
        or raw.get("provider")
        or "openai"
    )
    api_key = raw.get("ApiKey")
    if api_key is None:
        api_key = raw.get("APIKey")
    base_url = raw.get("BaseUrl")
    if base_url is None:
        base_url = raw.get("BaseURL")
    extra_parameters = raw.get("other") or {}
    extra_body = raw.get("Extra_Body") or {}
    if not isinstance(extra_parameters, dict) or not isinstance(extra_body, dict):
        raise ModelConfigError(f"model config {key!r} has invalid extra parameters")
    try:
        return ModelConfig(
            key=key,
            friendly_name=raw.get("FriendlyName") or key,
            provider=provider,
            model=raw.get("Model") or "",
            api_key=api_key or "",
            base_url=base_url or "",
            temperature=float(raw.get("Temperature", 0.5)),
            max_tokens=int(raw.get("MaxTokens", 1000)),
            top_p=float(raw.get("TopP", 1)),
            personality=raw.get("Personality") or "",
            empty_response_text=raw.get(
                "if_return_none", "模型没有返回任何内容"
            ),
            max_history_messages=int(raw.get("max_history_messages", 20)),
            request_timeout_seconds=float(
                raw.get("RequestTimeoutSeconds", DEFAULT_REQUEST_TIMEOUT)
            ),
            endpoint_style=raw.get("EndpointStyle") or "",
            tools_enabled=raw.get("ToolsEnabled", "auto"),
            transcription_model=raw.get(
                "TranscriptionModel", "gpt-4o-mini-transcribe"
            ),
            thinking_level=raw.get("ThinkingLevel", ""),
            extra_parameters=extra_parameters,
            extra_body=extra_body,
        )
    except (TypeError, ValueError) as exc:
        raise ModelConfigError(f"model config {key!r} has invalid numeric values") from exc


def _normalize_provider(value: Any) -> str:
    normalized = re.sub(
        r"[\s_-]+",
        "-",
        str(value or "").strip().casefold(),
    )
    aliases = {
        "openai": "openai_chat_completions",
        "openai-compatible": "openai_chat_completions",
        "chat-completions": "openai_chat_completions",
        "openai-chat-completions": "openai_chat_completions",
        "responses": "openai_responses",
        "openai-responses": "openai_responses",
        "gemini": "google_generate_content",
        "google": "google_generate_content",
        "google-gemini": "google_generate_content",
        "google-generatecontent": "google_generate_content",
        "google-generate-content": "google_generate_content",
        "anthropic": "anthropic_messages",
        "claude": "anthropic_messages",
        "anthropic-messages": "anthropic_messages",
    }
    try:
        return aliases[normalized]
    except KeyError as exc:
        raise ModelConfigError(f"unsupported AI provider: {normalized or 'missing'}") from exc


def _history_messages(
    config: ModelConfig,
    history: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    valid: list[dict[str, str]] = []
    for item in history:
        if not isinstance(item, Mapping):
            continue
        role = str(item.get("role") or "").casefold()
        if role not in {"user", "assistant"}:
            continue
        content = item.get("content")
        if content is None:
            continue
        valid.append({"role": role, "content": str(content)})
    if config.max_history_messages:
        return valid[-config.max_history_messages :]
    return valid


def _final_system_prompt(config: ModelConfig, request: ChatRequest) -> str:
    if request.system_prompt.strip():
        return request.system_prompt.strip()
    return config.personality.strip()


def _build_openai_payload(
    config: ModelConfig,
    request: ChatRequest,
) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    system_prompt = _final_system_prompt(config, request)
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.extend(_history_messages(config, request.history))
    if request.attachments:
        content: list[dict[str, Any]] = [{"type": "text", "text": request.message}]
        for attachment in request.attachments:
            encoded = base64.b64encode(attachment.data).decode("ascii")
            if attachment.mime.startswith("image/"):
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{attachment.mime};base64,{encoded}"
                        },
                    }
                )
            elif attachment.mime.startswith("audio/"):
                content.append(
                    {
                        "type": "input_audio",
                        "input_audio": {
                            "data": encoded,
                            "format": _audio_format(attachment.mime),
                        },
                    }
                )
            elif attachment.mime.startswith("video/"):
                raise MediaCapabilityError(
                    "OpenAI Chat Completions 不支持视频输入，请切换到 "
                    "OpenAI Responses 或 Google GenerateContent。"
                )
        messages.append({"role": "user", "content": content})
    else:
        messages.append({"role": "user", "content": request.message})

    for turn in request.turns:
        if isinstance(turn, AssistantTurn):
            message: dict[str, Any] = {
                "role": "assistant",
                "content": turn.text or None,
            }
            if turn.tool_calls:
                message["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": _arguments_json(call.arguments),
                        },
                    }
                    for call in turn.tool_calls
                ]
            messages.append(message)
        else:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": turn.call_id,
                    "content": turn.content,
                }
            )

    payload: dict[str, Any] = {
        "model": config.model,
        "messages": messages,
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
        "top_p": config.top_p,
    }
    _merge_safe_extra(payload, config.extra_parameters)
    _merge_safe_extra(payload, config.extra_body)
    if request.tools:
        payload["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": dict(tool.parameters),
                    "strict": False,
                },
            }
            for tool in request.tools
        ]
        payload["tool_choice"] = "auto"
        payload["stream"] = False
    return payload


def _build_gemini_payload(
    config: ModelConfig,
    request: ChatRequest,
) -> dict[str, Any]:
    contents: list[dict[str, Any]] = []
    for item in _history_messages(config, request.history):
        role = "model" if item["role"] == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": item["content"]}]})
    parts: list[dict[str, Any]] = [{"text": request.message}]
    for attachment in request.attachments:
        parts.append(
            {
                "inlineData": {
                    "mimeType": attachment.mime,
                    "data": base64.b64encode(attachment.data).decode("ascii"),
                }
            }
        )
    contents.append({"role": "user", "parts": parts})
    pending_results: list[dict[str, Any]] = []
    for turn in request.turns:
        if isinstance(turn, ToolResultTurn):
            pending_results.append(_gemini_function_response(turn))
            continue
        if pending_results:
            contents.append({"role": "user", "parts": pending_results})
            pending_results = []
        if turn.provider_content is not None:
            provider_content = dict(turn.provider_content)
            provider_content.setdefault("role", "model")
            contents.append(provider_content)
            continue
        model_parts: list[dict[str, Any]] = []
        if turn.text:
            model_parts.append({"text": turn.text})
        model_parts.extend(
            {
                "functionCall": {
                    "id": call.id,
                    "name": call.name,
                    "args": _arguments_mapping(call.arguments),
                }
            }
            for call in turn.tool_calls
        )
        contents.append({"role": "model", "parts": model_parts})
    if pending_results:
        contents.append({"role": "user", "parts": pending_results})
    generation_config: dict[str, Any] = {
        "temperature": config.temperature,
        "topP": config.top_p,
        "maxOutputTokens": config.max_tokens,
    }
    thinking_level = config.thinking_level
    if not thinking_level and request.tools and _is_gemini_3_model(config.model):
        thinking_level = "medium"
    if thinking_level:
        generation_config["thinkingConfig"] = {
            "thinkingLevel": thinking_level,
        }
    payload: dict[str, Any] = {
        "contents": contents,
        "generationConfig": generation_config,
    }
    system_prompt = _final_system_prompt(config, request)
    if system_prompt:
        payload["systemInstruction"] = {
            "role": "system",
            "parts": [{"text": system_prompt}],
        }
    if request.tools:
        payload["tools"] = [
            {
                "functionDeclarations": [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "parametersJsonSchema": dict(tool.parameters),
                    }
                    for tool in request.tools
                ]
            }
        ]
    return payload


def _is_gemini_3_model(model: str) -> bool:
    normalized = str(model or "").strip().casefold().replace("_", "-")
    return "gemini-3" in normalized


def _build_responses_payload(
    config: ModelConfig,
    request: ChatRequest,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": config.model,
        "input": _responses_full_input(config, request),
        "store": False,
        "temperature": config.temperature,
        "top_p": config.top_p,
        "max_output_tokens": config.max_tokens,
    }
    system_prompt = _final_system_prompt(config, request)
    if system_prompt:
        payload["instructions"] = system_prompt
    _merge_safe_extra(payload, config.extra_parameters)
    _merge_safe_extra(payload, config.extra_body)
    if request.tools:
        payload["tools"] = [
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": dict(tool.parameters),
                "strict": False,
            }
            for tool in request.tools
        ]
        payload["tool_choice"] = "auto"
    return payload


def _responses_full_input(
    config: ModelConfig,
    request: ChatRequest,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = [
        {"role": item["role"], "content": item["content"]}
        for item in _history_messages(config, request.history)
    ]
    items.append(
        {"role": "user", "content": _responses_user_content(request)}
    )
    for turn in request.turns:
        if isinstance(turn, AssistantTurn):
            if turn.text:
                items.append(
                    {
                        "role": "assistant",
                        "content": turn.text,
                    }
                )
            items.extend(_responses_function_call(call) for call in turn.tool_calls)
        else:
            items.append(_responses_function_result(turn))
    return items


def _responses_user_content(
    request: ChatRequest,
) -> str | list[dict[str, Any]]:
    if not request.attachments:
        return request.message or "请回复当前请求。"
    content: list[dict[str, Any]] = []
    if request.message:
        content.append({"type": "input_text", "text": request.message})
    for attachment in request.attachments:
        encoded = base64.b64encode(attachment.data).decode("ascii")
        if attachment.mime.startswith("image/"):
            content.append(
                {
                    "type": "input_image",
                    "image_url": f"data:{attachment.mime};base64,{encoded}",
                    "detail": "auto",
                }
            )
            continue
        if attachment.mime.startswith("audio/"):
            raise MediaCapabilityError(
                "OpenAI Responses 的语音需要先完成转写。"
            )
        if attachment.mime.startswith("video/"):
            raise MediaCapabilityError(
                "OpenAI Responses 的视频需要先完成抽帧和音轨转写。"
            )
    if not content:
        content.append({"type": "input_text", "text": "请回复当前请求。"})
    return content


def _responses_function_call(call: ProviderToolCall) -> dict[str, Any]:
    return {
        "type": "function_call",
        "call_id": call.id,
        "name": call.name,
        "arguments": _arguments_json(call.arguments),
    }


def _responses_function_result(turn: ToolResultTurn) -> dict[str, Any]:
    return {
        "type": "function_call_output",
        "call_id": turn.call_id,
        "output": turn.content,
    }


def _build_anthropic_payload(
    config: ModelConfig,
    request: ChatRequest,
) -> dict[str, Any]:
    messages: list[dict[str, Any]] = [
        {
            "role": item["role"],
            "content": item["content"],
        }
        for item in _history_messages(config, request.history)
    ]
    user_content: list[dict[str, Any]] = []
    if request.message:
        user_content.append({"type": "text", "text": request.message})
    for attachment in request.attachments:
        if attachment.mime.startswith("image/"):
            if attachment.mime not in {
                "image/jpeg",
                "image/png",
                "image/gif",
                "image/webp",
            }:
                raise MediaCapabilityError(
                    f"Anthropic Messages 不支持图片格式 {attachment.mime}。"
                )
            user_content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": attachment.mime,
                        "data": base64.b64encode(attachment.data).decode("ascii"),
                    },
                }
            )
            continue
        if attachment.mime.startswith("audio/"):
            raise MediaCapabilityError(
                "Anthropic Messages 当前不支持语音输入，请切换到支持语音的模型。"
            )
        if attachment.mime.startswith("video/"):
            raise MediaCapabilityError(
                "Anthropic Messages 当前不支持视频输入，请切换到支持视频的模型。"
            )
    if not user_content:
        user_content.append({"type": "text", "text": "请回复当前请求。"})
    messages.append({"role": "user", "content": user_content})

    pending_results: list[dict[str, Any]] = []
    for turn in request.turns:
        if isinstance(turn, ToolResultTurn):
            pending_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": turn.call_id,
                    "content": turn.content,
                }
            )
            continue
        if pending_results:
            messages.append({"role": "user", "content": pending_results})
            pending_results = []
        stored = (
            turn.provider_content.get("anthropic_content")
            if turn.provider_content is not None
            else None
        )
        if isinstance(stored, Sequence) and not isinstance(
            stored, (str, bytes, bytearray)
        ):
            content = [
                dict(item) for item in stored if isinstance(item, Mapping)
            ]
        else:
            content = []
            if turn.text:
                content.append({"type": "text", "text": turn.text})
            content.extend(
                {
                    "type": "tool_use",
                    "id": call.id,
                    "name": call.name,
                    "input": _arguments_mapping(call.arguments),
                }
                for call in turn.tool_calls
            )
        messages.append({"role": "assistant", "content": content})
    if pending_results:
        messages.append({"role": "user", "content": pending_results})

    payload: dict[str, Any] = {
        "model": config.model,
        "messages": messages,
        "temperature": config.temperature,
        "top_p": config.top_p,
        "max_tokens": config.max_tokens,
    }
    system_prompt = _final_system_prompt(config, request)
    if system_prompt:
        payload["system"] = system_prompt
    if request.tools:
        payload["tools"] = [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": dict(tool.parameters),
            }
            for tool in request.tools
        ]
        payload["tool_choice"] = {"type": "auto"}
    _merge_safe_extra(payload, config.extra_parameters)
    _merge_safe_extra(payload, config.extra_body)
    return payload


def _arguments_json(arguments: Any) -> str:
    if isinstance(arguments, str):
        return arguments
    return json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))


def _arguments_mapping(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, Mapping):
        return dict(arguments)
    if isinstance(arguments, str):
        try:
            value = json.loads(arguments)
        except json.JSONDecodeError:
            return {}
        return dict(value) if isinstance(value, Mapping) else {}
    return {}


def _gemini_function_response(turn: ToolResultTurn) -> dict[str, Any]:
    try:
        decoded = json.loads(turn.content)
    except json.JSONDecodeError:
        decoded = {"result": turn.content}
    if not isinstance(decoded, Mapping):
        decoded = {"result": decoded}
    function_response: dict[str, Any] = {
        "name": turn.name,
        "response": dict(decoded),
    }
    if turn.call_id and not turn.call_id.startswith("local-gemini-"):
        function_response["id"] = turn.call_id
    return {"functionResponse": function_response}


def _merge_safe_extra(payload: dict[str, Any], extra: Mapping[str, Any]) -> None:
    for key, value in extra.items():
        key_text = str(key)
        if key_text not in _PROTECTED_PAYLOAD_KEYS:
            payload[key_text] = value


async def _default_openai_request(
    config: ModelConfig,
    payload: Mapping[str, Any],
) -> Any:
    try:
        from openai import AsyncOpenAI
    except ImportError as exc:
        raise ProviderRequestError(
            "OpenAI provider requires the 'openai' package"
        ) from exc
    client = AsyncOpenAI(
        api_key=config.api_key,
        base_url=config.base_url or DEFAULT_OPENAI_BASE_URL,
        timeout=config.request_timeout_seconds,
    )
    try:
        response = await client.chat.completions.create(**dict(payload))
        if hasattr(response, "__aiter__"):
            text = await _extract_openai_text(response)
            return {"choices": [{"message": {"content": text}}]}
        return response
    finally:
        close = getattr(client, "close", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result


async def _default_responses_request(
    config: ModelConfig,
    payload: Mapping[str, Any],
) -> Any:
    try:
        from openai import AsyncOpenAI
    except ImportError as exc:
        raise ProviderRequestError(
            "OpenAI Responses requires the 'openai' package"
        ) from exc
    client = AsyncOpenAI(
        api_key=config.api_key,
        base_url=config.base_url or DEFAULT_OPENAI_BASE_URL,
        timeout=config.request_timeout_seconds,
    )
    try:
        return await client.responses.create(**dict(payload))
    finally:
        close = getattr(client, "close", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result


async def _default_transcription_request(
    config: ModelConfig,
    payload: Mapping[str, Any],
) -> Any:
    try:
        from openai import AsyncOpenAI
    except ImportError as exc:
        raise ProviderRequestError(
            "OpenAI transcription requires the 'openai' package"
        ) from exc
    file_data = payload.get("file")
    if not isinstance(file_data, Mapping):
        raise ProviderRequestError("transcription request is missing audio bytes")
    data = file_data.get("data")
    if not isinstance(data, bytes) or not data:
        raise ProviderRequestError("transcription request has invalid audio bytes")
    client = AsyncOpenAI(
        api_key=config.api_key,
        base_url=config.base_url or DEFAULT_OPENAI_BASE_URL,
        timeout=config.request_timeout_seconds,
    )
    try:
        return await client.audio.transcriptions.create(
            model=str(payload.get("model") or config.transcription_model),
            file=(
                str(file_data.get("name") or "input.wav"),
                data,
                str(file_data.get("mime") or "audio/wav"),
            ),
        )
    finally:
        close = getattr(client, "close", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result


async def _default_anthropic_request(
    config: ModelConfig,
    payload: Mapping[str, Any],
) -> Any:
    try:
        from anthropic import AsyncAnthropic
    except ImportError as exc:
        raise ProviderRequestError(
            "Anthropic Messages requires the 'anthropic' package"
        ) from exc
    client = AsyncAnthropic(
        api_key=config.api_key,
        base_url=config.base_url or DEFAULT_ANTHROPIC_BASE_URL,
        timeout=config.request_timeout_seconds,
    )
    try:
        return await client.messages.create(**dict(payload))
    finally:
        close = getattr(client, "close", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result


async def _default_gemini_request(
    config: ModelConfig,
    payload: Mapping[str, Any],
) -> Any:
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise ProviderRequestError(
            "Google GenerateContent requires the 'google-genai' package"
        ) from exc
    contents = _google_sdk_contents(payload, types)
    generate_config = _google_sdk_generate_config(payload, types)
    http_options = _google_sdk_http_options(config, types)
    client = genai.Client(
        api_key=config.api_key,
        http_options=http_options,
    )
    async_client = client.aio
    try:
        response = await async_client.models.generate_content(
            model=config.model,
            contents=contents,
            config=generate_config,
        )
        return _google_sdk_response_payload(response)
    finally:
        await async_client.aclose()


def _google_sdk_contents(payload: Mapping[str, Any], types: Any) -> list[Any]:
    raw_contents = payload.get("contents")
    if not isinstance(raw_contents, Sequence) or isinstance(
        raw_contents,
        (str, bytes, bytearray),
    ):
        raise ProviderRequestError(
            "Google GenerateContent request is missing contents"
        )
    try:
        return [types.Content.model_validate(item) for item in raw_contents]
    except (TypeError, ValueError) as exc:
        raise ProviderRequestError(
            "Google GenerateContent request contains invalid content"
        ) from exc


def _google_sdk_generate_config(payload: Mapping[str, Any], types: Any) -> Any:
    raw_generation = payload.get("generationConfig")
    if not isinstance(raw_generation, Mapping):
        raise ProviderRequestError(
            "Google GenerateContent request is missing generation config"
        )
    raw_config: dict[str, Any] = dict(raw_generation)
    system_instruction = payload.get("systemInstruction")
    if system_instruction is not None:
        raw_config["systemInstruction"] = system_instruction
    tools = payload.get("tools")
    if tools is not None:
        raw_config["tools"] = _google_sdk_tools(tools)
    raw_config["automatic_function_calling"] = {"disable": True}
    raw_config["should_return_http_response"] = True
    try:
        return types.GenerateContentConfig.model_validate(raw_config)
    except (TypeError, ValueError) as exc:
        raise ProviderRequestError(
            "Google GenerateContent request contains invalid generation config"
        ) from exc


def _google_sdk_response_payload(response: Any) -> dict[str, Any]:
    if isinstance(response, Mapping):
        return dict(response)
    sdk_http_response = getattr(response, "sdk_http_response", None)
    raw_body = getattr(sdk_http_response, "body", None)
    if isinstance(raw_body, str) and raw_body.strip():
        try:
            data = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise ProviderRequestError(
                "google-genai returned a malformed raw JSON response"
            ) from exc
        if not isinstance(data, dict):
            raise ProviderRequestError(
                "google-genai returned an unexpected raw response shape"
            )
        return data
    dump = getattr(response, "model_dump", None)
    if not callable(dump):
        raise ProviderRequestError(
            "google-genai returned an unexpected response shape"
        )
    data = dump(mode="json", by_alias=True, exclude_none=True)
    if not isinstance(data, dict):
        raise ProviderRequestError(
            "google-genai returned an unexpected response shape"
        )
    return data


def _google_sdk_tools(tools: Any) -> Any:
    if not isinstance(tools, Sequence) or isinstance(
        tools,
        (str, bytes, bytearray),
    ):
        return tools
    converted_tools: list[Any] = []
    for raw_tool in tools:
        if not isinstance(raw_tool, Mapping):
            converted_tools.append(raw_tool)
            continue
        tool = dict(raw_tool)
        declarations = tool.get("functionDeclarations")
        if not isinstance(declarations, Sequence) or isinstance(
            declarations,
            (str, bytes, bytearray),
        ):
            converted_tools.append(tool)
            continue
        converted_declarations: list[Any] = []
        for raw_declaration in declarations:
            if not isinstance(raw_declaration, Mapping):
                converted_declarations.append(raw_declaration)
                continue
            declaration = dict(raw_declaration)
            parameters = declaration.pop("parameters", None)
            if parameters is not None:
                # google-genai's typed `parameters` field only accepts string
                # enum members. Its JSON Schema field preserves valid numeric
                # and boolean enums without changing tool argument semantics.
                declaration.setdefault("parametersJsonSchema", parameters)
            converted_declarations.append(declaration)
        tool["functionDeclarations"] = converted_declarations
        converted_tools.append(tool)
    return converted_tools


def _google_sdk_http_options(config: ModelConfig, types: Any) -> Any:
    try:
        import httpx
    except ImportError as exc:
        raise ProviderRequestError(
            "Google GenerateContent requires the 'httpx' package"
        ) from exc
    options: dict[str, Any] = {
        "timeout": max(1, int(config.request_timeout_seconds * 1000)),
        # JianerCore currently pins aiohttp 3.9.x. Supplying an explicit
        # transport keeps google-genai on its supported httpx path instead of
        # selecting an incompatible optional aiohttp transport.
        "async_client_args": {
            "transport": httpx.AsyncHTTPTransport(),
        },
    }
    base_url = config.base_url.rstrip("/")
    if base_url:
        api_version = ""
        for candidate in ("v1beta", "v1alpha", "v1"):
            suffix = f"/{candidate}"
            if base_url.casefold().endswith(suffix):
                base_url = base_url[: -len(suffix)].rstrip("/")
                api_version = candidate
                break
        if not base_url:
            raise ProviderRequestError(
                "Google GenerateContent BaseUrl is invalid"
            )
        options["base_url"] = base_url
        if api_version:
            options["api_version"] = api_version
    return types.HttpOptions.model_validate(options)


async def _extract_openai_text(response: Any) -> str:
    return (await _extract_openai_response(response)).text


async def _extract_openai_response(response: Any) -> ProviderResponse:
    if hasattr(response, "__aiter__"):
        chunks: list[str] = []
        async for chunk in response:
            text = _extract_openai_chunk_text(chunk)
            if text:
                chunks.append(text)
        text = "".join(chunks)
        turn = AssistantTurn(text=text)
        return ProviderResponse(text=text, tool_calls=(), turn=turn)
    if _is_sync_stream(response):
        text = "".join(
            text
            for item in response
            if (text := _extract_openai_chunk_text(item))
        )
        turn = AssistantTurn(text=text)
        return ProviderResponse(text=text, tool_calls=(), turn=turn)
    choices = _get_value(response, "choices") or ()
    if choices:
        message = _get_value(choices[0], "message") or {}
    else:
        message = response
    text = _normalize_content_text(_get_value(message, "content"))
    calls: list[ProviderToolCall] = []
    for index, item in enumerate(_get_value(message, "tool_calls") or ()):
        function = _get_value(item, "function") or {}
        name = str(_get_value(function, "name") or "").strip()
        if not name:
            continue
        calls.append(
            ProviderToolCall(
                id=str(_get_value(item, "id") or f"tool-call-{index}"),
                name=name,
                arguments=_get_value(function, "arguments") or {},
            )
        )
    turn = AssistantTurn(text=text, tool_calls=tuple(calls))
    return ProviderResponse(text=text, tool_calls=tuple(calls), turn=turn)


def _extract_responses_response(response: Any) -> ProviderResponse:
    plain = _plain_value(response)
    if not isinstance(plain, Mapping):
        turn = AssistantTurn()
        return ProviderResponse(text="", tool_calls=(), turn=turn)
    output = plain.get("output")
    output_items = (
        list(output)
        if isinstance(output, Sequence)
        and not isinstance(output, (str, bytes, bytearray))
        else []
    )
    texts: list[str] = []
    calls: list[ProviderToolCall] = []
    for index, item in enumerate(output_items):
        if not isinstance(item, Mapping):
            continue
        item_type = str(item.get("type") or "").casefold()
        if item_type == "function_call":
            name = str(item.get("name") or "").strip()
            if name:
                calls.append(
                    ProviderToolCall(
                        id=str(
                            item.get("call_id")
                            or item.get("id")
                            or f"responses-call-{index}"
                        ),
                        name=name,
                        arguments=item.get("arguments") or {},
                    )
                )
            continue
        if item_type != "message":
            continue
        content = item.get("content")
        if not isinstance(content, Sequence) or isinstance(
            content, (str, bytes, bytearray)
        ):
            continue
        for part in content:
            if not isinstance(part, Mapping):
                continue
            part_type = str(part.get("type") or "").casefold()
            if part_type == "output_text" and part.get("text") is not None:
                texts.append(str(part["text"]))
            elif part_type == "refusal" and part.get("refusal") is not None:
                texts.append(str(part["refusal"]))
    if not texts:
        output_text = plain.get("output_text")
        if isinstance(output_text, str):
            texts.append(output_text)
    text = "\n".join(item for item in texts if item).strip()
    provider_content = {
        "responses_output": [
            dict(item) for item in output_items if isinstance(item, Mapping)
        ]
    }
    turn = AssistantTurn(
        text=text,
        tool_calls=tuple(calls),
        provider_content=provider_content,
    )
    return ProviderResponse(
        text=text,
        tool_calls=tuple(calls),
        turn=turn,
    )


def _extract_anthropic_response(response: Any) -> ProviderResponse:
    plain = _plain_value(response)
    if not isinstance(plain, Mapping):
        turn = AssistantTurn()
        return ProviderResponse(text="", tool_calls=(), turn=turn)
    content = plain.get("content")
    blocks = (
        list(content)
        if isinstance(content, Sequence)
        and not isinstance(content, (str, bytes, bytearray))
        else []
    )
    texts: list[str] = []
    calls: list[ProviderToolCall] = []
    for index, item in enumerate(blocks):
        if not isinstance(item, Mapping):
            continue
        item_type = str(item.get("type") or "").casefold()
        if item_type == "text" and item.get("text") is not None:
            texts.append(str(item["text"]))
        elif item_type == "tool_use":
            name = str(item.get("name") or "").strip()
            if name:
                calls.append(
                    ProviderToolCall(
                        id=str(item.get("id") or f"anthropic-call-{index}"),
                        name=name,
                        arguments=item.get("input") or {},
                    )
                )
    text = "\n".join(item for item in texts if item).strip()
    turn = AssistantTurn(
        text=text,
        tool_calls=tuple(calls),
        provider_content={
            "anthropic_content": [
                dict(item) for item in blocks if isinstance(item, Mapping)
            ]
        },
    )
    return ProviderResponse(text=text, tool_calls=tuple(calls), turn=turn)


def _extract_transcription_text(response: Any) -> str:
    if isinstance(response, str):
        return response
    value = _get_value(response, "text")
    if value is not None:
        return str(value)
    return ""


def _extract_openai_message_text(response: Any) -> str:
    choices = _get_value(response, "choices") or ()
    if choices:
        message = _get_value(choices[0], "message") or {}
        content = _get_value(message, "content")
        return _normalize_content_text(content)
    content = _get_value(response, "content")
    return _normalize_content_text(content)


def _parse_dsml_tool_calls(text: str) -> tuple[ProviderToolCall, ...]:
    """Parse DeepSeek-style textual tool calls returned by some gateways."""

    normalized = _normalize_dsml_markup(str(text or ""))
    outer = re.fullmatch(
        r"\s*<DSML:tool_calls>\s*(.*?)\s*</DSML:tool_calls>\s*",
        normalized,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if outer is None:
        return ()
    body = outer.group(1)
    invoke_pattern = re.compile(
        r"<DSML:invoke\s+name\s*=\s*(['\"])([A-Za-z0-9_-]{1,64})\1\s*>"
        r"(.*?)</DSML:invoke>",
        flags=re.DOTALL | re.IGNORECASE,
    )
    matches = tuple(invoke_pattern.finditer(body))
    if not matches or invoke_pattern.sub("", body).strip():
        return ()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    calls: list[ProviderToolCall] = []
    for index, invoke in enumerate(matches):
        parameters_body = invoke.group(3)
        parameter_pattern = re.compile(
            r"<DSML:parameter\s+name\s*=\s*(['\"])([A-Za-z0-9_-]{1,64})\1"
            r"([^>]*)>(.*?)</DSML:parameter>",
            flags=re.DOTALL | re.IGNORECASE,
        )
        parameters = tuple(parameter_pattern.finditer(parameters_body))
        if parameter_pattern.sub("", parameters_body).strip():
            return ()
        arguments: dict[str, Any] = {}
        for parameter in parameters:
            name = parameter.group(2)
            if name in arguments:
                return ()
            attributes = parameter.group(3)
            string_match = re.fullmatch(
                r"\s*string\s*=\s*(['\"])(true|false)\1\s*",
                attributes,
                flags=re.IGNORECASE,
            )
            if attributes.strip() and string_match is None:
                return ()
            raw_value = parameter.group(4).strip()
            if string_match is not None and string_match.group(2).casefold() == "true":
                value: Any = raw_value
            else:
                try:
                    value = json.loads(raw_value)
                except json.JSONDecodeError:
                    value = raw_value
            arguments[name] = value
        calls.append(
            ProviderToolCall(
                id=f"local-dsml-{digest}-{index}",
                name=invoke.group(2),
                arguments=arguments,
            )
        )
    return tuple(calls)


def _looks_like_dsml_tool_call(text: str) -> bool:
    normalized = _normalize_dsml_markup(str(text or ""))
    return bool(
        re.search(
            r"</?DSML:(?:tool_calls|invoke|parameter)\b",
            normalized,
            flags=re.IGNORECASE,
        )
    )


def _normalize_dsml_markup(text: str) -> str:
    normalized = str(text).replace("｜", "|")
    marker = r"(?:\|\s*)+DSML\s*(?:\|\s*)+"
    normalized = re.sub(
        rf"<\s*/\s*{marker}",
        "</DSML:",
        normalized,
        flags=re.IGNORECASE,
    )
    return re.sub(
        rf"<\s*{marker}",
        "<DSML:",
        normalized,
        flags=re.IGNORECASE,
    )


def _extract_openai_chunk_text(chunk: Any) -> str:
    if isinstance(chunk, str):
        return chunk
    choices = _get_value(chunk, "choices") or ()
    if not choices:
        return ""
    delta = _get_value(choices[0], "delta") or {}
    return _normalize_content_text(_get_value(delta, "content"))


def _extract_gemini_text(response: Any) -> str:
    return _extract_gemini_response(response).text


def _extract_gemini_response(response: Any) -> ProviderResponse:
    if not isinstance(response, Mapping):
        turn = AssistantTurn()
        return ProviderResponse(text="", tool_calls=(), turn=turn)
    candidates = response.get("candidates") or ()
    if not candidates or not isinstance(candidates[0], Mapping):
        turn = AssistantTurn()
        return ProviderResponse(text="", tool_calls=(), turn=turn)
    content = candidates[0].get("content") or {}
    if not isinstance(content, Mapping):
        turn = AssistantTurn()
        return ProviderResponse(text="", tool_calls=(), turn=turn)
    parts = content.get("parts") or ()
    texts = [
        str(part["text"])
        for part in parts
        if isinstance(part, Mapping) and isinstance(part.get("text"), str)
    ]
    calls: list[ProviderToolCall] = []
    for index, part in enumerate(parts):
        if not isinstance(part, Mapping):
            continue
        function = part.get("functionCall") or part.get("function_call")
        if not isinstance(function, Mapping):
            continue
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        calls.append(
            ProviderToolCall(
                id=str(
                    function.get("id")
                    or f"local-gemini-function-call-{index}"
                ),
                name=name,
                arguments=function.get("args") or {},
            )
        )
    text = "\n".join(texts).strip()
    turn = AssistantTurn(
        text=text,
        tool_calls=tuple(calls),
        provider_content=content,
    )
    return ProviderResponse(text=text, tool_calls=tuple(calls), turn=turn)


def _gemini_empty_response_error(response: Any) -> EmptyProviderResponseError:
    details: list[str] = []
    if isinstance(response, Mapping):
        candidates = response.get("candidates")
        if isinstance(candidates, Sequence) and not isinstance(
            candidates,
            (str, bytes, bytearray),
        ):
            first = candidates[0] if candidates else None
            if isinstance(first, Mapping):
                reason = first.get("finishReason") or first.get("finish_reason")
                if reason:
                    details.append(f"finish_reason={reason}")
                content = first.get("content")
                if isinstance(content, Mapping):
                    parts = content.get("parts")
                    if isinstance(parts, Sequence) and not isinstance(
                        parts,
                        (str, bytes, bytearray),
                    ):
                        details.append(f"parts={len(parts)}")
                        part_fields = sorted(
                            {
                                str(key)[:40]
                                for part in parts
                                if isinstance(part, Mapping)
                                for key in part
                                if str(key)
                                not in {
                                    "thoughtSignature",
                                    "thought_signature",
                                }
                            }
                        )
                        if part_fields:
                            details.append(
                                "part_fields=" + "|".join(part_fields[:12])
                            )
        prompt_feedback = response.get("promptFeedback") or response.get(
            "prompt_feedback"
        )
        if isinstance(prompt_feedback, Mapping):
            block_reason = prompt_feedback.get(
                "blockReason"
            ) or prompt_feedback.get("block_reason")
            if block_reason:
                details.append(f"block_reason={block_reason}")
        usage = response.get("usageMetadata") or response.get("usage_metadata")
        if isinstance(usage, Mapping):
            for output_name, aliases in (
                ("prompt_tokens", ("promptTokenCount", "prompt_token_count")),
                (
                    "candidate_tokens",
                    ("candidatesTokenCount", "candidates_token_count"),
                ),
                (
                    "thoughts_tokens",
                    ("thoughtsTokenCount", "thoughts_token_count"),
                ),
                ("total_tokens", ("totalTokenCount", "total_token_count")),
            ):
                value = next(
                    (
                        usage.get(alias)
                        for alias in aliases
                        if usage.get(alias) is not None
                    ),
                    None,
                )
                if isinstance(value, int) and not isinstance(value, bool):
                    details.append(f"{output_name}={value}")
    suffix = f" ({', '.join(details)})" if details else ""
    return EmptyProviderResponseError(
        "Google GenerateContent returned no text or function call" + suffix
    )


def _normalize_content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes, bytearray)):
        texts: list[str] = []
        for item in content:
            if isinstance(item, Mapping):
                text = item.get("text")
            else:
                text = getattr(item, "text", None)
            if text is not None:
                texts.append(str(text))
        return "\n".join(texts)
    return str(content)


def _is_sync_stream(value: Any) -> bool:
    return (
        hasattr(value, "__iter__")
        and not isinstance(value, (str, bytes, bytearray, Mapping))
        and _get_value(value, "choices") is None
    )


def _audio_format(mime: str) -> str:
    return {
        "audio/mpeg": "mp3",
        "audio/mp3": "mp3",
        "audio/wav": "wav",
        "audio/x-wav": "wav",
        "audio/ogg": "ogg",
        "audio/flac": "flac",
    }.get(mime, mime.split("/", 1)[-1])


def _get_value(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, None)


def _plain_mapping(value: Any) -> dict[str, Any]:
    plain = _plain_value(value)
    if not isinstance(plain, Mapping):
        raise TypeError("provider content must be a mapping")
    return dict(plain)


def _plain_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _plain_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_plain_value(item) for item in value]
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return _plain_value(dump(exclude_none=True))
    if hasattr(value, "__dict__"):
        return {
            str(key): _plain_value(item)
            for key, item in vars(value).items()
            if not str(key).startswith("_")
        }
    return str(value)


def _is_tools_unsupported_exception(exc: BaseException) -> bool:
    candidates = [
        getattr(exc, "param", None),
        getattr(exc, "code", None),
        getattr(exc, "body", None),
        getattr(exc, "response", None),
        str(exc),
    ]
    return any(_is_tools_unsupported_value(item) for item in candidates)


def _is_tools_unsupported_value(value: Any) -> bool:
    if value is None:
        return False
    plain = _plain_value(value)
    try:
        text = json.dumps(plain, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(plain)
    normalized = text.casefold()
    mentions_tools = any(
        marker in normalized
        for marker in (
            "tools",
            "tool_choice",
            "functiondeclarations",
            "function_declarations",
        )
    )
    rejects_parameter = any(
        marker in normalized
        for marker in (
            "unsupported",
            "not supported",
            "unknown parameter",
            "unrecognized",
            "unexpected field",
            "invalid argument",
        )
    )
    return mentions_tools and rejects_parameter


def _safe_config_error(exc: BaseException) -> str:
    if isinstance(exc, json.JSONDecodeError):
        return f"invalid JSON at line {exc.lineno}, column {exc.colno}"
    if isinstance(exc, OSError):
        return exc.__class__.__name__
    return str(exc)
