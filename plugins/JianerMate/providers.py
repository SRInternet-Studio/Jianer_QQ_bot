"""Minimal OpenAI-compatible chat client used by JianerMate.

The plugin reads the shared ``aiconfig/*.ai.json`` model definitions but only
drives the OpenAI chat-completions protocol.  Entries using Gemini,
Anthropic, or the OpenAI Responses protocol stay visible in ``~主动发言 状态``
and are reported as unsupported instead of being silently ignored.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import aiohttp


_LOGGER = logging.getLogger("jianer_mate.providers")


OPENAI_CHAT_COMPLETIONS = "openai_chat_completions"

_OPENAI_ALIASES = frozenset(
    {
        "openai",
        "openai-compatible",
        "openai-chat",
        "chat-completions",
        "openai-chatcompletions",
        "openai-chat-completions",
    }
)
_OTHER_ALIASES = {
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


class ProviderError(RuntimeError):
    """Raised when a model request cannot produce a usable answer."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(str(message))
        self.status = status


class ModelConfigError(ProviderError):
    """Raised when a model definition is unusable."""


class ResponseParseError(ProviderError):
    """Raised when the provider answer is not the JSON we asked for."""


def normalize_response_type(value: Any) -> str:
    normalized = str(value or "").strip().casefold()
    normalized = normalized.replace("_", "-").replace(" ", "-")
    if normalized in _OPENAI_ALIASES:
        return OPENAI_CHAT_COMPLETIONS
    return _OTHER_ALIASES.get(normalized, normalized or "unknown")


@dataclass(frozen=True, slots=True)
class ModelEntry:
    key: str
    friendly_name: str
    response_type: str
    model: str
    api_key: str = field(repr=False)
    base_url: str = ""
    temperature: float = 0.5
    max_tokens: int = 800
    timeout_seconds: float = 60.0
    extra_parameters: Mapping[str, Any] = field(default_factory=dict, repr=False)
    extra_body: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @property
    def supported(self) -> bool:
        return self.response_type == OPENAI_CHAT_COMPLETIONS

    @property
    def unsupported_reason(self) -> str:
        if self.supported:
            return ""
        if self.response_type in {
            "google_generate_content",
            "anthropic_messages",
            "openai_responses",
        }:
            return f"ResponseType={self.response_type} 不在本插件支持范围内"
        return "缺少或无法识别的 ResponseType"

    @property
    def endpoint(self) -> str:
        base = str(self.base_url or "https://api.openai.com/v1").strip().rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"


@dataclass(frozen=True, slots=True)
class ChatImage:
    """Inline image bytes that are safe to send to the model."""

    data: bytes
    mime: str = "image/jpeg"

    def data_uri(self) -> str:
        payload = base64.b64encode(bytes(self.data)).decode("ascii")
        return f"data:{self.mime or 'image/jpeg'};base64,{payload}"


def load_models(config_dir: str | Path) -> tuple[dict[str, ModelEntry], dict[str, str]]:
    """Load every ``*.ai.json`` entry, keeping unsupported ones for status."""

    directory = Path(config_dir)
    entries: dict[str, ModelEntry] = {}
    errors: dict[str, str] = {}
    if not directory.is_dir():
        return entries, errors
    for path in sorted(directory.glob("*.ai.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors[path.name] = f"配置读取失败: {exc.__class__.__name__}"
            continue
        if not isinstance(raw, Mapping):
            errors[path.name] = "配置必须是 JSON 对象"
            continue
        key = str(path.name)[: -len(".ai.json")]
        try:
            entries[key] = _model_entry(key, raw)
        except (TypeError, ValueError) as exc:
            errors[path.name] = f"配置无效: {exc}"
    return entries, errors


def _model_entry(key: str, raw: Mapping[str, Any]) -> ModelEntry:
    model = str(raw.get("Model") or raw.get("model") or "").strip()
    if not model:
        raise ValueError("缺少 Model")
    extra_body = raw.get("Extra_Body") or raw.get("extra_body") or {}
    if not isinstance(extra_body, Mapping):
        extra_body = {}
    extra_parameters = raw.get("other") or raw.get("Other") or {}
    if not isinstance(extra_parameters, Mapping):
        extra_parameters = {}
    return ModelEntry(
        key=str(key),
        friendly_name=str(raw.get("FriendlyName") or raw.get("friendly_name") or key),
        response_type=normalize_response_type(
            raw.get("ResponseType") or raw.get("response_type")
        ),
        model=model,
        api_key=str(raw.get("ApiKey") or raw.get("api_key") or "").strip(),
        base_url=str(raw.get("BaseUrl") or raw.get("base_url") or "").strip(),
        temperature=_float_value(raw.get("Temperature"), 0.5),
        max_tokens=_int_value(raw.get("MaxTokens"), 800),
        timeout_seconds=_float_value(
            raw.get("TimeoutSeconds") or raw.get("timeout_seconds"), 60.0
        ),
        extra_parameters=dict(extra_parameters),
        extra_body=dict(extra_body),
    )


def _float_value(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int_value(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def extract_json_object(text: str) -> str:
    """Return the first balanced JSON object found in a model answer."""

    value = str(text or "").strip()
    if value.startswith("```"):
        value = value.strip("`")
        if value[:4].casefold() == "json":
            value = value[4:]
    start = value.find("{")
    if start < 0:
        raise ValueError("no JSON object in model answer")
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(value)):
        char = value[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return value[start : index + 1]
    raise ValueError("unbalanced JSON object in model answer")


def parse_json_object(text: str) -> dict[str, Any]:
    payload = json.loads(extract_json_object(text))
    if not isinstance(payload, dict):
        raise ValueError("model answer is not a JSON object")
    return payload


class JianerAIBridge:
    """Serve non OpenAI-compatible aiconfig entries via JianerAI's registry.

    Models configured as ``Google GenerateContent``, ``Anthropic Messages`` or
    ``OpenAI Responses`` cannot be driven by JianerMate's chat-completions
    client.  Instead of re-implementing those protocols, the bridge reuses the
    provider layer that already ships in ``plugins.JianerAI`` (module import
    only; the JianerAI plugin itself does not have to be enabled).
    """

    def __init__(self, config_dir: str | Path) -> None:
        self._registry: Any | None = None
        self._chat_request: Any | None = None
        self._media_attachment: Any | None = None
        self._error = ""
        try:
            from plugins.JianerAI.providers import (  # noqa: PLC0415
                ChatRequest,
                FunctionTool,
                MediaAttachment,
                ProviderRegistry,
                ToolResultTurn,
            )
        except Exception as exc:  # noqa: BLE001 - optional dependency
            self._error = f"{exc.__class__.__name__}: {exc}"
            return
        try:
            self._registry = ProviderRegistry(Path(config_dir))
        except Exception as exc:  # noqa: BLE001
            self._error = f"{exc.__class__.__name__}: {exc}"
            return
        self._chat_request = ChatRequest
        self._media_attachment = MediaAttachment
        self._function_tool = FunctionTool
        self._tool_result_turn = ToolResultTurn

    @property
    def available(self) -> bool:
        return self._registry is not None

    @property
    def error(self) -> str:
        return self._error

    def supports(self, key: str) -> bool:
        if self._registry is None:
            return False
        try:
            self._registry.get(str(key or "").strip())
        except Exception:  # noqa: BLE001 - unknown model
            return False
        return True

    def model_keys(self) -> tuple[str, ...]:
        if self._registry is None:
            return ()
        try:
            return tuple(sorted(self._registry.list_models()))
        except Exception:  # noqa: BLE001 - keep the bridge usable
            return ()

    def supports_tools(self, key: str) -> bool:
        if self._registry is None:
            return False
        check = getattr(self._registry, "supports_tools", None)
        if not callable(check):
            return False
        try:
            return bool(check(str(key or "").strip()))
        except Exception:  # noqa: BLE001
            return False

    async def complete_text_with_tools(
        self,
        key: str,
        *,
        system_prompt: str,
        user_text: str,
        tools: Sequence[Mapping[str, Any]],
        executor: Any = None,
        max_rounds: int = 3,
        images: Sequence[ChatImage] = (),
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Tool loop for bridged models that declare tool support."""

        if (
            self._registry is None
            or self._chat_request is None
            or self._function_tool is None
            or self._tool_result_turn is None
            or not self.supports_tools(key)
        ):
            return await self.complete_text(
                key,
                system_prompt=system_prompt,
                user_text=user_text,
                images=images,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        declarations = tuple(
            self._function_tool(
                name=str((item.get("function") or {}).get("name") or ""),
                description=str(
                    (item.get("function") or {}).get("description") or ""
                ),
                parameters=dict(
                    (item.get("function") or {}).get("parameters") or {}
                ),
            )
            for item in tools
            if (item.get("function") or {}).get("name")
        )
        attachments = self._attachments(images)
        turns: list[Any] = []
        for _round in range(1, max(1, int(max_rounds)) + 1):
            request = self._chat_request(
                message=str(user_text or ""),
                system_prompt=str(system_prompt or ""),
                attachments=attachments,
                tools=declarations,
                turns=tuple(turns),
                temperature=temperature,
                max_output_tokens=max_tokens,
            )
            try:
                response = await self._registry.complete_agent_turn(
                    key, request
                )
            except Exception as exc:  # noqa: BLE001
                raise ProviderError(
                    f"多协议调用失败：{exc.__class__.__name__}"
                ) from exc
            calls = tuple(getattr(response, "tool_calls", ()) or ())
            text = str(getattr(response, "text", "") or "").strip()
            if not calls:
                if not text:
                    raise ProviderError("模型返回了空内容")
                return text
            turn = getattr(response, "turn", None)
            if turn is not None:
                turns.append(turn)
            for call in calls:
                if executor is None:
                    result = "工具执行器不可用。"
                else:
                    try:
                        result = await executor(
                            str(getattr(call, "id", "")),
                            str(getattr(call, "name", "")),
                            dict(getattr(call, "arguments", {}) or {}),
                        )
                    except Exception as exc:  # noqa: BLE001
                        result = f"工具执行失败：{exc.__class__.__name__}"
                turns.append(
                    self._tool_result_turn(
                        call_id=str(getattr(call, "id", "")),
                        name=str(getattr(call, "name", "")),
                        content=str(result)[:8000],
                    )
                )
        raise ProviderError("工具调用轮次超限")

    def _attachments(self, images: Sequence[ChatImage]) -> tuple[Any, ...]:
        if self._media_attachment is None:
            return ()
        return tuple(
            self._media_attachment(
                data=bytes(image.data),
                mime=str(image.mime or "image/jpeg"),
                source="jianer-mate",
            )
            for image in images
            if image.data
        )
        try:
            return tuple(sorted(self._registry.list_models()))
        except Exception:  # noqa: BLE001
            return ()

    async def complete_text(
        self,
        key: str,
        *,
        system_prompt: str,
        user_text: str,
        images: Sequence[ChatImage] = (),
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        if (
            self._registry is None
            or self._chat_request is None
            or self._media_attachment is None
        ):
            raise ProviderError("多协议桥接不可用")
        attachments = tuple(
            self._media_attachment(
                data=bytes(image.data),
                mime=str(image.mime or "image/jpeg"),
                source="jianer-mate",
            )
            for image in images
            if image.data
        )
        request = self._chat_request(
            message=str(user_text or ""),
            system_prompt=str(system_prompt or ""),
            attachments=attachments,
            temperature=temperature,
            max_output_tokens=max_tokens,
        )
        try:
            response = await self._registry.complete_request(key, request)
        except Exception as exc:  # noqa: BLE001 - normalize for our callers
            raise ProviderError(
                f"多协议调用失败：{exc.__class__.__name__}"
            ) from exc
        text = str(getattr(response, "text", "") or "").strip()
        if not text:
            raise ProviderError("模型返回了空内容")
        return text


class OpenAICompatibleClient:
    """Async chat-completions client with image input and JSON answers."""

    def __init__(
        self,
        entries: Mapping[str, ModelEntry] | None = None,
        *,
        session: Any | None = None,
        sleep: Any = asyncio.sleep,
        max_retries: int = 2,
        bridge: Any | None = None,
    ) -> None:
        self._entries: dict[str, ModelEntry] = dict(entries or {})
        self._bridge = bridge
        self._session = session
        self._owns_session = session is None
        self._sleep = sleep
        self._max_retries = max(0, int(max_retries))
        self._closed = False

    @property
    def models(self) -> Mapping[str, ModelEntry]:
        return dict(self._entries)

    def reload(self, entries: Mapping[str, ModelEntry]) -> None:
        self._entries = dict(entries)

    def has_model(self, key: str) -> bool:
        return self.serves(key)

    def serves(self, key: str) -> bool:
        """Whether this client can drive ``key`` (OpenAI-compatible or bridged)."""

        entry = self._entries.get(str(key or "").strip())
        if entry is not None and entry.supported and entry.api_key:
            return True
        bridge = self._bridge
        return bool(
            bridge is not None
            and getattr(bridge, "available", False)
            and bridge.supports(str(key or "").strip())
        )

    def set_bridge(self, bridge: Any | None) -> None:
        self._bridge = bridge

    def _bridge_for(self, key: str) -> Any | None:
        """Return a bridge when ``key`` is not an OpenAI-compatible entry."""

        entry = self._entries.get(str(key or "").strip())
        if entry is not None and entry.supported and entry.api_key:
            return None
        bridge = self._bridge
        if bridge is None or not getattr(bridge, "available", False):
            return None
        return bridge if bridge.supports(str(key or "").strip()) else None

    def entry(self, key: str) -> ModelEntry:
        normalized = str(key or "").strip()
        entry = self._entries.get(normalized)
        if entry is None:
            raise ModelConfigError(f"未找到 aiconfig 模型: {normalized or '(空)'}")
        if not entry.supported:
            raise ModelConfigError(
                f"模型 {normalized} 不受支持：{entry.unsupported_reason}"
            )
        if not entry.api_key:
            raise ModelConfigError(f"模型 {normalized} 缺少 ApiKey")
        return entry

    def close_sync(self) -> None:
        self._closed = True

    async def close(self) -> None:
        self._closed = True
        session, self._session = self._session, None
        if session is not None and self._owns_session:
            await session.close()

    async def complete_text(
        self,
        key: str,
        *,
        system_prompt: str,
        user_text: str,
        images: Sequence[ChatImage] = (),
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        bridge = self._bridge_for(key)
        if bridge is not None:
            return await bridge.complete_text(
                key,
                system_prompt=system_prompt,
                user_text=user_text,
                images=images,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        entry = self.entry(key)
        payload = _build_payload(
            entry,
            system_prompt=system_prompt,
            user_text=user_text,
            images=images,
            max_tokens=max_tokens,
            temperature=temperature,
            json_mode=False,
        )
        response = await self._post(entry, payload)
        return _extract_message_text(response)

    async def complete_json(
        self,
        key: str,
        *,
        system_prompt: str,
        user_text: str,
        images: Sequence[ChatImage] = (),
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        bridge = self._bridge_for(key)
        if bridge is not None:
            text = await bridge.complete_text(
                key,
                system_prompt=system_prompt,
                user_text=user_text,
                images=images,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            try:
                return parse_json_object(text)
            except ValueError as exc:
                raise ResponseParseError("模型返回的 JSON 无法解析") from exc
        entry = self.entry(key)
        payload = _build_payload(
            entry,
            system_prompt=system_prompt,
            user_text=user_text,
            images=images,
            max_tokens=max_tokens,
            temperature=temperature,
            json_mode=True,
        )
        response = await self._post(entry, payload)
        text = _extract_message_text(response)
        try:
            return parse_json_object(text)
        except ValueError as exc:
            raise ResponseParseError("模型返回的 JSON 无法解析") from exc

    async def complete_json_with_tools(
        self,
        key: str,
        *,
        system_prompt: str,
        user_text: str,
        tools: Sequence[Mapping[str, Any]] = (),
        executor: Any = None,
        max_rounds: int = 3,
        images: Sequence[ChatImage] = (),
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        """Chat-completions loop that runs tool calls before the JSON answer."""

        text = await self._run_tool_loop(
            key,
            system_prompt=system_prompt,
            user_text=user_text,
            tools=tools,
            executor=executor,
            max_rounds=max_rounds,
            images=images,
            max_tokens=max_tokens,
            temperature=temperature,
            json_mode=True,
        )
        try:
            return parse_json_object(text)
        except ValueError as exc:
            raise ResponseParseError("模型返回的 JSON 无法解析") from exc

    async def complete_text_with_tools(
        self,
        key: str,
        *,
        system_prompt: str,
        user_text: str,
        tools: Sequence[Mapping[str, Any]] = (),
        executor: Any = None,
        max_rounds: int = 3,
        images: Sequence[ChatImage] = (),
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        """Same loop as :meth:`complete_json_with_tools` but plain text output."""

        return await self._run_tool_loop(
            key,
            system_prompt=system_prompt,
            user_text=user_text,
            tools=tools,
            executor=executor,
            max_rounds=max_rounds,
            images=images,
            max_tokens=max_tokens,
            temperature=temperature,
            json_mode=False,
        )

    async def _run_tool_loop(
        self,
        key: str,
        *,
        system_prompt: str,
        user_text: str,
        tools: Sequence[Mapping[str, Any]],
        executor: Any,
        max_rounds: int,
        images: Sequence[ChatImage],
        max_tokens: int | None,
        temperature: float | None,
        json_mode: bool,
    ) -> str:
        tool_schemas = [dict(item) for item in tools]
        bridge = self._bridge_for(key)
        if bridge is not None:
            if tool_schemas:
                runner = getattr(bridge, "complete_text_with_tools", None)
                if callable(runner):
                    return await runner(
                        key,
                        system_prompt=system_prompt,
                        user_text=user_text,
                        tools=tool_schemas,
                        executor=executor,
                        max_rounds=max_rounds,
                        images=images,
                        temperature=temperature,
                        max_tokens=max_tokens,
                    )
                _LOGGER.info(
                    "模型 %s 走多协议桥接且不支持工具，本轮按普通对话处理", key
                )
            return await bridge.complete_text(
                key,
                system_prompt=system_prompt,
                user_text=user_text,
                images=images,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        entry = self.entry(key)
        payload = _build_payload(
            entry,
            system_prompt=system_prompt,
            user_text=user_text,
            images=images,
            max_tokens=max_tokens,
            temperature=temperature,
            json_mode=json_mode,
        )
        messages = list(payload.get("messages") or [])
        rounds = 0
        while True:
            current = dict(payload)
            current["messages"] = list(messages)
            if tool_schemas:
                current["tools"] = tool_schemas
                current.setdefault("tool_choice", "auto")
            response = await self._post(entry, current)
            message = _extract_choice_message(response)
            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, list) and tool_calls:
                rounds += 1
                if rounds > max(1, int(max_rounds)):
                    raise ProviderError("工具调用轮次超限")
                messages.append(
                    {
                        "role": "assistant",
                        "content": message.get("content") or "",
                        "tool_calls": tool_calls,
                    }
                )
                for call in tool_calls:
                    function = call.get("function") if isinstance(call, Mapping) else None
                    name = str((function or {}).get("name") or "")
                    arguments = _tool_arguments((function or {}).get("arguments"))
                    if executor is None:
                        result = "工具执行器不可用。"
                    else:
                        try:
                            result = await executor(
                                str(call.get("id") or ""), name, arguments
                            )
                        except Exception as exc:  # noqa: BLE001 - surfaced to model
                            result = f"工具执行失败：{exc.__class__.__name__}"
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": str(call.get("id") or ""),
                            "content": str(result)[:8000],
                        }
                    )
                continue
            return _message_text(message)

    async def _post(self, entry: ModelEntry, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if self._closed:
            raise ProviderError("模型客户端已关闭")
        attempt = 0
        last_error: Exception | None = None
        current_payload: dict[str, Any] = dict(payload)
        while attempt <= self._max_retries:
            attempt += 1
            try:
                return await self._post_once(entry, current_payload)
            except _RetryWithoutJsonMode as exc:
                current_payload.pop("response_format", None)
                last_error = exc
                continue
            except ProviderError as exc:
                last_error = exc
                if attempt > self._max_retries or not _retryable(exc):
                    raise
                await self._sleep(0.4 * attempt)
        raise ProviderError("模型请求失败") from last_error

    async def _post_once(
        self,
        entry: ModelEntry,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        session = await self._ensure_session()
        headers = {"Content-Type": "application/json"}
        if entry.api_key:
            headers["Authorization"] = f"Bearer {entry.api_key}"
        timeout = aiohttp.ClientTimeout(total=entry.timeout_seconds)
        try:
            async with session.post(
                entry.endpoint,
                json=dict(payload),
                headers=headers,
                timeout=timeout,
            ) as response:
                body = await response.text()
                if response.status == 400 and "response_format" in payload:
                    raise _RetryWithoutJsonMode()
                if response.status >= 400:
                    raise ProviderError(
                        f"模型返回 HTTP {response.status}: {_shorten(body)}",
                        status=response.status,
                    )
                try:
                    parsed = json.loads(body)
                except json.JSONDecodeError as exc:
                    raise ProviderError("模型返回的不是合法 JSON") from exc
                if not isinstance(parsed, Mapping):
                    raise ProviderError("模型返回结构异常")
                return parsed
        except aiohttp.ClientError as exc:
            raise ProviderError(f"模型网络错误: {exc.__class__.__name__}") from exc
        except asyncio.TimeoutError as exc:
            raise ProviderError("模型请求超时") from exc

    async def _ensure_session(self) -> Any:
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session


class _RetryWithoutJsonMode(Exception):
    pass


def _retryable(error: Exception) -> bool:
    status = getattr(error, "status", None)
    if status is None:
        return True
    return int(status) >= 500 or int(status) == 429


def _shorten(value: str, limit: int = 400) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _build_payload(
    entry: ModelEntry,
    *,
    system_prompt: str,
    user_text: str,
    images: Sequence[ChatImage],
    max_tokens: int | None,
    temperature: float | None,
    json_mode: bool,
) -> dict[str, Any]:
    content: Any = str(user_text or "")
    inline_images = [image for image in images if image.data]
    if inline_images:
        content = [{"type": "text", "text": str(user_text or "")}]
        content.extend(
            {
                "type": "image_url",
                "image_url": {"url": image.data_uri()},
            }
            for image in inline_images
        )
    payload: dict[str, Any] = {
        "model": entry.model,
        "messages": [
            {"role": "system", "content": str(system_prompt or "")},
            {"role": "user", "content": content},
        ],
        "temperature": (
            entry.temperature if temperature is None else float(temperature)
        ),
        "max_tokens": int(max_tokens or entry.max_tokens),
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    payload.update(dict(entry.extra_body))
    parameters = dict(entry.extra_parameters)
    if parameters:
        payload.update(parameters)
    return payload


def _extract_message_text(response: Mapping[str, Any]) -> str:
    return _message_text(_extract_choice_message(response))


def _extract_choice_message(response: Mapping[str, Any]) -> Mapping[str, Any]:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProviderError("模型没有返回任何候选内容")
    first = choices[0]
    if not isinstance(first, Mapping):
        raise ProviderError("模型返回结构异常")
    message = first.get("message")
    if not isinstance(message, Mapping):
        message = {"content": first.get("text")}
    return message


def _message_text(message: Mapping[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, Mapping) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        text = "".join(parts)
    else:
        text = ""
    text = str(text or "").strip()
    if not text:
        raise ProviderError("模型返回了空内容")
    return text


def _tool_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "{}")
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, Mapping):
            return dict(parsed)
    return {}
