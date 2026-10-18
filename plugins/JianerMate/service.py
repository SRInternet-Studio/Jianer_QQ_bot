"""JianerMate runtime: passive chat, proactive speech, and meme handling."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from jianer import common as Manager, segments as Segments
from jianer.adapters import (
    Capability,
    ConversationKey,
    ConversationKind,
    MediaKind,
    MediaPolicy,
    MediaRequest,
    MediaSourceKind,
    ResolutionStatus,
)

from plugins.JianerMate import style as reply_style
from plugins.JianerMate.config import RuntimeOptions
from plugins.JianerMate.memes import MemeLibrary
from plugins.JianerMate.memory import LongTermMemory
from plugins.JianerMate.moderation import ContentModerator
from plugins.JianerMate.persona import (
    MAX_MULTIPLE_STYLES,
    Persona,
    PersonaConverter,
    PersonaError,
    load_jianer_ai_presets,
    resolve_preset,
)
from plugins.JianerMate.plugin_config import (
    PluginConfigError,
    ensure_config_file,
    flatten_plugin_config,
    load_plugin_config,
)
from plugins.JianerMate.planner import Planner, PlannerDecision
from plugins.JianerMate.providers import (
    ChatImage,
    OpenAICompatibleClient,
    ProviderError,
    ResponseParseError,
    load_models,
)
from plugins.JianerMate.replyer import MemeCandidateView, ReplyPlan, Replyer
from plugins.JianerMate.store import ConversationRecord, MateStore, MessageRecord
from plugins.JianerMate.tools import (
    MateMemoryFacade,
    MateToolRuntime,
    build_tool_context,
    make_actor_resolver,
)


_LOGGER = logging.getLogger("jianer_mate")

JIANER_AI_PLUGIN_ID = "jianerbot-plugin-jianer-ai"
BARE_MENTION_PROMPT = "（对方只是 @ 了你，没有说别的）"
CONFIG_HINT = "（简儿还没配好模型，请管理员检查 aiconfig 与 jianer_mate_* 配置）"
IMAGE_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})
MAX_INPUT_IMAGES = 4
MAX_IMAGE_BYTES = 8 * 1024 * 1024
HINT_INTERVAL_SECONDS = 600.0
PRUNE_EVERY_MESSAGES = 50
PERSONA_OVERRIDE_STATE_KEY = "persona_override"


@dataclass(frozen=True, slots=True)
class ConversationRef:
    protocol: str
    self_id: str
    kind: str
    conversation_id: str

    @property
    def is_group(self) -> bool:
        return self.kind == ConversationKind.GROUP.value


@dataclass
class _PendingBatch:
    first_at: float
    last_at: float
    task: asyncio.Task[None] | None = None


@dataclass
class _ConversationState:
    writes_since_prune: int = 0
    last_hint_at: float = 0.0


class JianerMateService:
    """Single service instance owned by one PluginManager generation."""

    def __init__(
        self,
        options: RuntimeOptions,
        *,
        runtime: Mapping[str, Any] | None = None,
        store: MateStore | None = None,
        memes: MemeLibrary | None = None,
        client: OpenAICompatibleClient | None = None,
        planner: Planner | None = None,
        replyer: Replyer | None = None,
        moderator: ContentModerator | None = None,
        clock: Any = time.time,
        rng: random.Random | None = None,
        sleep: Any = asyncio.sleep,
        logger: Any | None = None,
        entries: Mapping[str, Any] | None = None,
    ) -> None:
        self.options = options
        self.runtime = runtime or {}
        self._logger = logger or self.runtime.get("logger") or _LOGGER
        self._clock = clock
        self._rng = rng or random.Random()
        self._sleep = sleep
        self._active = False
        self._closed = False
        self._conflict = False
        self._conflict_detail = ""
        self._reminder = str(self.runtime.get("reminder") or "~")
        self.store = store or MateStore(options.database_path)
        self.memes = memes or MemeLibrary(
            self.store,
            options.meme,
            directory=options.meme_dir,
            logger=self._logger,
        )
        if client is not None:
            self.client = client
        else:
            loaded, errors = load_models(options.ai_config_dir)
            if entries is not None:
                loaded = dict(entries)
            self._model_errors = errors
            self.client = OpenAICompatibleClient(loaded)
        self._model_errors = getattr(self, "_model_errors", {})
        self.planner = planner or Planner(
            self.client, options=options, logger=self._logger
        )
        self.replyer = replyer or Replyer(
            self.client, options=options, logger=self._logger
        )
        self.moderator = moderator
        if (
            self.moderator is None
            and options.moderation_enabled
            and options.moderation_model
        ):
            self.moderator = ContentModerator(
                self.client,
                options.moderation_model,
                logger=self._logger,
            )
        self._pending: dict[ConversationRef, _PendingBatch] = {}
        self._conversation_locks: dict[ConversationRef, asyncio.Lock] = {}
        self._conversation_states: dict[ConversationRef, _ConversationState] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._ai_semaphore = asyncio.Semaphore(max(1, options.max_concurrent_ai))
        self._notice_state: dict[str, Any] = {}
        self._silence_streak: dict[ConversationRef, int] = {}
        self._backoff_until: dict[ConversationRef, float] = {}
        self.persona_converter = PersonaConverter(self.client, logger=self._logger)
        self.persona_override: Persona | None = self._load_persona_override()
        self.tool_runtime = MateToolRuntime(
            options.tools,
            project_root=options.project_root,
            logger=self._logger,
        )
        self._actor_resolver = make_actor_resolver()
        self.long_term = LongTermMemory(
            options.memory_db_path,
            enabled=bool(options.memory_enabled),
            logger=self._logger,
            backend=options.memory_backend,
            data_dir=options.memory_data_dir,
            project_root=options.project_root,
            embedding_config_path=options.memory_embedding_config_path,
            bot_name=options.persona.name,
        )

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    @classmethod
    def from_runtime(cls, runtime: Mapping[str, Any] | None) -> "JianerMateService":
        options = RuntimeOptions.from_runtime(runtime)
        return cls(options, runtime=runtime or {})

    @property
    def active(self) -> bool:
        return bool(self._active and not self._closed and not self._conflict)

    @property
    def conflict(self) -> bool:
        return bool(self._conflict)

    @property
    def conflict_detail(self) -> str:
        return self._conflict_detail

    async def start_after_activation(
        self,
        manager: Any,
        previous_manager: Any = None,
    ) -> None:
        if self._closed:
            return
        self._conflict = self._detect_conflict(manager)
        if self._conflict:
            self._conflict_detail = (
                "检测到 jianerbot-plugin-jianer-ai 已加载：两个插件互斥，"
                "JianerMate 不会处理任何消息。请先禁用 JianerAI（~禁用插件 JianerAI）"
                "再重载插件。"
            )
            self._log_error(self._conflict_detail)
            return
        self._active = True
        models = [
            key
            for key, entry in self.client.models.items()
            if entry.supported
        ]
        self._log_info(
            "JianerMate 已激活 | planner=%s | replyer=%s | 可用模型=%s"
            % (
                self.options.planner_model or "(未配置)",
                self.options.replyer_model or "(未配置)",
                ",".join(sorted(models)) or "(无)",
            )
        )
        removed = 0
        if ensure_config_file(self.options.config_path, self.options):
            self._log_info(f"已生成插件配置文件：{self.options.config_path}")
            self._refresh_config_metadata()
        with contextlib.suppress(Exception):
            removed = self.memes.cleanup_orphans()
        if removed:
            self._log_info(f"清理了 {removed} 个孤立的表情包文件")
        if not self._planner_model_key():
            self._log_error(
                "JianerMate 没有可用的 planner 模型，主动发言不会触发。"
            )
        if not self._replyer_model_key():
            self._log_error(
                "JianerMate 没有可用的 replyer 模型，@ 与主动发言都不会回复。"
            )

    def _detect_conflict(self, manager: Any) -> bool:
        if manager is None:
            return False
        plugins = getattr(manager, "plugins", None)
        if isinstance(plugins, Mapping) and JIANER_AI_PLUGIN_ID in plugins:
            return True
        return False

    async def shutdown(self) -> None:
        self._closed = True
        self._active = False
        for batch in list(self._pending.values()):
            if batch.task is not None:
                batch.task.cancel()
        self._pending.clear()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        with contextlib.suppress(Exception):
            self.long_term.close()
        with contextlib.suppress(Exception):
            await self.tool_runtime.shutdown()
        with contextlib.suppress(Exception):
            await self.client.close()
        with contextlib.suppress(Exception):
            self.store.close()

    # ------------------------------------------------------------------ #
    # persona
    # ------------------------------------------------------------------ #
    def _load_persona_override(self) -> Persona | None:
        raw = ""
        with contextlib.suppress(Exception):
            raw = self.store.get_state(PERSONA_OVERRIDE_STATE_KEY, "")
        if not str(raw or "").strip():
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            self._log_error("人格覆盖数据不是合法 JSON，已忽略")
            return None
        try:
            return Persona.from_payload(payload, base=self.options.persona)
        except PersonaError as exc:
            self._log_error(f"人格覆盖数据无效，已忽略：{exc}")
            return None

    def _effective_persona(self) -> Persona:
        return self.persona_override or self.options.persona

    def _save_persona_override(self, persona: Persona) -> None:
        self.persona_override = persona
        with contextlib.suppress(Exception):
            self.store.set_state(PERSONA_OVERRIDE_STATE_KEY, persona.to_json())

    def _clear_persona_override(self) -> None:
        self.persona_override = None
        with contextlib.suppress(Exception):
            self.store.set_state(PERSONA_OVERRIDE_STATE_KEY, "")

    def _persona_text(self) -> str:
        persona = self._effective_persona()
        source = "人格转换结果" if self.persona_override else "config.json"
        return "当前人格（来源：%s）\n%s" % (source, "\n".join(persona.summary_lines()))

    def _refresh_config_metadata(self) -> None:
        """Refresh where the dedicated config lives and how many keys it holds."""

        path = self.options.config_path
        document, errors = load_plugin_config(path)
        keys: tuple[str, ...] = ()
        if document:
            with contextlib.suppress(PluginConfigError):
                keys = tuple(sorted(flatten_plugin_config(document)))
        self.options = replace(
            self.options,
            config_loaded=path.is_file() and not errors,
            config_keys=keys,
            config_errors=tuple(
                f"{source}: {message}"
                for source, message in sorted(errors.items())
            ),
        )

    # ------------------------------------------------------------------ #
    # observation
    # ------------------------------------------------------------------ #
    async def observe(self, event: Any, actions: Any) -> None:
        if not self.active or not self.options.enabled:
            return
        key = self._conversation_ref(event)
        if key is None:
            return
        if self._is_self_message(event):
            return
        text = self._event_text(event)
        if text.startswith(self._reminder):
            return
        conversation = self.store.ensure_conversation(
            key.protocol, key.self_id, key.kind, key.conversation_id,
            now=int(self._clock()),
        )
        try:
            self.store.append_message(
                conversation.ref,
                direction="incoming",
                content=text,
                platform_message_id=str(getattr(event, "message_id", "") or ""),
                sender_id=str(getattr(event, "user_id", "") or ""),
                sender_name=self._sender_name(event),
                message_type=self._message_type(event),
                segments_json=self._segments_json(event),
                has_image=self._has_image(event),
                now=int(self._clock()),
            )
        except Exception:
            self._log_exception("写入聊天记录失败")
            return
        state = self._conversation_states.setdefault(key, _ConversationState())
        state.writes_since_prune += 1
        if state.writes_since_prune >= PRUNE_EVERY_MESSAGES:
            state.writes_since_prune = 0
            with contextlib.suppress(Exception):
                self.store.prune_messages(
                    conversation.ref,
                    keep=self.options.history_keep_messages,
                    retention_days=self.options.history_retention_days,
                    now=int(self._clock()),
                )
        if (
            key.is_group
            and self.options.meme.enabled
            and self.options.meme.steal_emoji
            and self._has_image(event)
        ):
            self._spawn(self._capture_meme(event, actions, key))
        if key.is_group:
            self._schedule_proactive(key, event, actions, conversation)

    def _schedule_proactive(
        self,
        key: ConversationRef,
        event: Any,
        actions: Any,
        conversation: ConversationRecord,
    ) -> None:
        if not conversation.enabled:
            return
        now = self._clock()
        if now < self._backoff_until.get(key, 0.0):
            return
        batch = self._pending.get(key)
        max_wait = max(
            self.options.max_debounce_seconds,
            self.options.debounce_seconds + 1.0,
        )
        if batch is not None and now - batch.first_at >= max_wait:
            # The burst is already long enough: keep the pending deadline.
            batch.last_at = now
            return
        if batch is None:
            batch = _PendingBatch(first_at=now, last_at=now)
            self._pending[key] = batch
        else:
            batch.last_at = now
            if batch.task is not None:
                batch.task.cancel()
        delay = max(0.0, float(self.options.debounce_seconds))
        batch.task = self._spawn(self._run_proactive(key, event, actions, batch, delay))

    async def _run_proactive(
        self,
        key: ConversationRef,
        event: Any,
        actions: Any,
        batch: _PendingBatch,
        delay: float,
    ) -> None:
        try:
            if delay:
                await self._sleep(delay)
            if self._pending.get(key) is not batch:
                return
            self._pending.pop(key, None)
            await self._decide_and_speak(key, event, actions)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._log_exception("主动发言调度失败")

    async def _decide_and_speak(
        self,
        key: ConversationRef,
        event: Any,
        actions: Any,
    ) -> None:
        if not self.active:
            return
        conversation = self.store.get_conversation(
            key.protocol, key.self_id, key.kind, key.conversation_id
        )
        if conversation is None or not conversation.enabled:
            return
        recent = self.store.recent_messages(
            conversation.ref, limit=self.options.context_messages
        )
        now = self._clock()
        passed, reason = self.evaluate_gate(conversation, recent, now=now)
        if not passed:
            self.store.add_decision(
                conversation.ref,
                gate_passed=False,
                gate_reason=reason,
                now=int(now),
            )
            return
        planner_model = self._planner_model_key()
        replyer_model = self._replyer_model_key()
        if not planner_model or not replyer_model:
            self.store.add_decision(
                conversation.ref,
                gate_passed=True,
                gate_reason="model_missing",
                error="planner/replyer 模型未配置",
                now=int(now),
            )
            return
        try:
            persona = self._effective_persona()
            style_override, style_label = persona.pick_multiple_style(self._rng)
            tool_context = await self._tool_context(
                event, actions, key, conversation, model=replyer_model
            )
            async with self._ai_semaphore:
                decision = await self.planner.decide(
                    model_key=planner_model,
                    conversation_label=self._conversation_label(key),
                    recent=recent,
                    talk_value=self._talk_value(conversation),
                    custom_prompt=conversation.custom_prompt,
                    persona=persona,
                    style_override=style_override,
                    now=now,
                )
                decision = replace(decision, style=style_label)
                if not decision.speak:
                    self.store.add_decision(
                        conversation.ref,
                        gate_passed=True,
                        planner_json=decision.as_json(),
                        gate_reason=decision.reason,
                        now=int(now),
                    )
                    self._note_silence(key, now=now)
                    return
                candidate_views = self._meme_candidate_views(
                    now=int(now),
                    allow_images=self._replyer_supports_images(replyer_model),
                )
                plan = await self._compose_reply(
                    model_key=replyer_model,
                    mode="proactive",
                    recent=recent,
                    trigger_text="",
                    candidates=candidate_views,
                    topic=decision.topic,
                    want_meme=decision.want_meme,
                    custom_prompt=conversation.custom_prompt,
                    conversation_label=self._conversation_label(key),
                    persona=persona,
                    persona_mode="group",
                    style_override=style_override,
                    tools=self._tool_schemas(tool_context),
                    tool_executor=self._tool_executor(tool_context),
                    tool_max_rounds=self.options.tools.max_rounds,
                    now=now,
                )
        except ProviderError as exc:
            self.store.add_decision(
                conversation.ref,
                gate_passed=True,
                gate_reason="provider_error",
                error=str(exc)[:300],
                now=int(now),
            )
            self._log_info(f"主动发言模型调用失败: {exc}")
            return
        if plan.empty:
            self.store.add_decision(
                conversation.ref,
                gate_passed=True,
                planner_json=decision.as_json(),
                gate_reason="empty_plan",
                now=int(now),
            )
            return
        if conversation.debug_dry_run:
            self.store.add_decision(
                conversation.ref,
                gate_passed=True,
                planner_json=decision.as_json(),
                gate_reason="dry_run",
                spoke=False,
                now=int(now),
            )
            preview = reply_style.sanitize_messages(
                plan.messages, self.options.style, rng=self._rng
            )
            lines = [f"[调试] 这次本会主动发言（{decision.reason or '未说明'}）"]
            if decision.style:
                lines.append(f"临时风格：{decision.style}")
            for index, message in enumerate(preview, start=1):
                lines.append(f"{index}. {message}")
            if plan.meme_id is not None:
                lines.append(f"表情包：#{plan.meme_id}")
            await self._reply(actions, event, "\n".join(lines))
            return
        decision_text = await self._moderate(
            plan, context=self._transcript_text(recent)
        )
        if not decision_text.allowed:
            self.store.add_decision(
                conversation.ref,
                gate_passed=True,
                planner_json=decision.as_json(),
                gate_reason="moderation_blocked",
                error=decision_text.reason,
                now=int(now),
            )
            return
        sent = await self._deliver(conversation, event, actions, plan)
        if sent > 0:
            self._reset_backoff(key)
        self.store.add_decision(
            conversation.ref,
            gate_passed=True,
            planner_json=decision.as_json(),
            gate_reason=decision.reason or "spoken",
            spoke=sent > 0,
            now=int(now),
        )

    def evaluate_gate(
        self,
        conversation: ConversationRecord | None,
        recent: Sequence[MessageRecord],
        *,
        now: float,
    ) -> tuple[bool, str]:
        """Deterministic pre-filter that runs before any model call."""

        if not self.options.enabled:
            return False, "plugin_disabled"
        if conversation is None:
            return False, "no_conversation"
        if str(conversation.conversation_id) in set(self.options.group_blacklist):
            return False, "blacklisted"
        if not conversation.enabled:
            return False, "conversation_disabled"
        if self._in_quiet_hours(conversation, now=now):
            return False, "quiet_hours"
        if not recent or all(
            record.direction == "outgoing" for record in recent[-3:]
        ):
            return False, "no_human_topic"
        min_interval = (
            conversation.min_interval_seconds
            if conversation.min_interval_seconds is not None
            else self.options.min_interval_seconds
        )
        if min_interval > 0:
            last_outgoing = self.store.last_outgoing_at(conversation.ref)
            if last_outgoing is not None and now - last_outgoing < min_interval:
                return False, "cooldown"
        if self._rng.random() >= self._talk_value(conversation):
            if not self._mentioned_in(recent):
                return False, "talk_value"
        return True, "ok"

    def _mentioned_in(self, recent: Sequence[MessageRecord]) -> bool:
        """A name/alias mention skips the talk_value roll, like MaiBot."""

        if not self.options.mentioned_reply_enabled:
            return False
        persona = self._effective_persona()
        for record in list(recent)[-6:]:
            if record.direction != "incoming":
                continue
            if persona.mentions_name(record.content):
                return True
        return False

    def _note_silence(self, key: ConversationRef, *, now: float) -> None:
        """Slow down checks after consecutive planner silences (MaiBot backoff)."""

        streak = self._silence_streak.get(key, 0) + 1
        self._silence_streak[key] = streak
        start = max(1, int(self.options.backoff_start_count))
        if streak < start:
            return
        exponent = min(streak - start, 6)
        delay = min(
            max(0.0, self.options.backoff_base_seconds) * (2 ** exponent),
            max(0.0, self.options.backoff_cap_seconds),
        )
        if delay <= 0:
            return
        self._backoff_until[key] = now + delay
        self._log_info(
            "主动发言连续 %d 次保持沉默，%.0f 秒后再检查" % (streak, delay)
        )

    def _reset_backoff(self, key: ConversationRef) -> None:
        self._silence_streak.pop(key, None)
        self._backoff_until.pop(key, None)

    # ------------------------------------------------------------------ #
    # tool calling
    # ------------------------------------------------------------------ #
    async def _tool_context(
        self,
        event: Any,
        actions: Any,
        key: ConversationRef,
        conversation: ConversationRecord,
        *,
        model: str,
    ) -> Any | None:
        if not self.tool_runtime.available:
            return None
        actor = None
        if self._actor_resolver is not None:
            with contextlib.suppress(Exception):
                actor = await self._actor_resolver.resolve(
                    event, actions, self.runtime
                )
        return build_tool_context(
            event=event,
            actions=actions,
            conversation=self._framework_key(key),
            canonical_user_id=f"{key.protocol}:{getattr(event, 'user_id', '')}",
            runtime=self.runtime,
            memory=MateMemoryFacade(
                self.store,
                conversation.ref,
                long_term=self.long_term,
                preset=self._effective_persona().name,
            ),
            actor=actor,
            model=str(model or ""),
        )

    def _tool_schemas(self, tool_context: Any) -> tuple[Mapping[str, Any], ...]:
        if tool_context is None:
            return ()
        return self.tool_runtime.schemas(tool_context)

    def _tool_executor(self, tool_context: Any) -> Any:
        async def execute(
            call_id: str,
            name: str,
            arguments: Mapping[str, Any],
        ) -> str:
            return await self.tool_runtime.execute(
                call_id, name, arguments, tool_context
            )

        return execute

    def _tool_status_text(self) -> str:
        status = self.tool_runtime.status()
        if not status.get("enabled"):
            return "关闭"
        if not status.get("available"):
            return f"不可用（{status.get('error') or '未知原因'}）"
        return f"开（{status.get('tools', 0)} 个可用）"

    # ------------------------------------------------------------------ #
    # passive fallback
    # ------------------------------------------------------------------ #
    async def handle_fallback(self, event: Any, actions: Any) -> bool:
        if not self.active or not self.options.enabled:
            return False
        key = self._conversation_ref(event)
        if key is None or self._is_self_message(event):
            return False
        is_group = key.is_group
        if is_group:
            if not self._mentions_self(event):
                return False
            prompt = self._text_without_mentions(event)
        else:
            raw = self._event_text(event)
            if raw.startswith(self._reminder):
                return False
            prompt = raw
        if not prompt.strip():
            if not self._has_media(event):
                if not is_group:
                    return False
                prompt = BARE_MENTION_PROMPT
            else:
                prompt = "（对方发来了图片）"
        replyer_model = self._replyer_model_key()
        if not replyer_model:
            await self._maybe_send_config_hint(key, event, actions)
            return True
        conversation = self.store.ensure_conversation(
            key.protocol, key.self_id, key.kind, key.conversation_id,
            now=int(self._clock()),
        )
        lock = self._conversation_locks.setdefault(key, asyncio.Lock())
        async with lock:
            now = self._clock()
            recent = self.store.recent_messages(
                conversation.ref, limit=self.options.context_messages
            )
            try:
                persona = self._effective_persona()
                style_override, _style_label = persona.pick_multiple_style(self._rng)
                tool_context = await self._tool_context(
                    event,
                    actions,
                    key,
                    conversation,
                    model=replyer_model,
                )
                async with self._ai_semaphore:
                    images, reference_text = await self._resolve_inputs(
                        event, actions, key
                    )
                    candidate_views = self._meme_candidate_views(
                        now=int(now),
                        allow_images=self._replyer_supports_images(replyer_model),
                    )
                    plan = await self._compose_reply(
                        model_key=replyer_model,
                        mode="passive",
                        recent=recent,
                        trigger_text=prompt,
                        reference_text=reference_text,
                        images=images,
                        candidates=candidate_views,
                        custom_prompt=conversation.custom_prompt,
                        conversation_label=self._conversation_label(key),
                        persona=persona,
                        persona_mode="group" if is_group else "private",
                        style_override=style_override,
                        tools=self._tool_schemas(tool_context),
                        tool_executor=self._tool_executor(tool_context),
                        tool_max_rounds=self.options.tools.max_rounds,
                        now=now,
                    )
            except ProviderError as exc:
                self._log_info(f"被动回复模型调用失败: {exc}")
                return True
            if plan.empty:
                return True
            review = await self._moderate(plan, context=prompt)
            if not review.allowed:
                self._log_info(f"被动回复被审核拦截: {review.reason}")
                return True
            at_user_ids: tuple[str, ...] = ()
            if is_group and self.options.reply_at_enabled:
                at_user_ids = (str(getattr(event, "user_id", "") or ""),)
            await self._deliver(
                conversation,
                event,
                actions,
                plan,
                at_user_ids=at_user_ids,
            )
        return True

    # ------------------------------------------------------------------ #
    # delivery
    # ------------------------------------------------------------------ #
    async def _deliver(
        self,
        conversation: ConversationRecord,
        event: Any,
        actions: Any,
        plan: ReplyPlan,
        *,
        at_user_ids: Sequence[str] = (),
    ) -> int:
        texts = reply_style.sanitize_messages(
            plan.messages, self.options.style, rng=self._rng
        )
        if not texts:
            return 0
        capabilities = self._capabilities(actions)
        typo_index = len(texts) - 1
        sent: list[tuple[str, str]] = []
        typo_original = ""
        reply_to = plan.reply_to if plan.reply_to else None
        for index, text in enumerate(texts):
            content = reply_style.decorate(
                text, self.options.style, rng=self._rng
            )
            correct_content = content
            if index == typo_index:
                content, typo = reply_style.inject_typo(
                    content,
                    probability=self.options.style.typo_probability,
                    rng=self._rng,
                )
                if typo:
                    typo_original = correct_content
            if index > 0:
                delay_ms = reply_style.sample_delay_ms(
                    self.options.style, rng=self._rng
                )
                if delay_ms:
                    await self._sleep(delay_ms / 1000.0)
            message_id = await self._send_text(
                actions,
                event,
                content,
                reply_to=reply_to if index == 0 else None,
                at_user_ids=at_user_ids if index == 0 else (),
                capabilities=capabilities,
            )
            if message_id is None:
                continue
            sent.append((content, message_id))
            self._record_outgoing(
                conversation, content, message_id, now=int(self._clock())
            )
        if typo_original and self.options.style.typo_correction and sent:
            last_id = sent[-1][1]
            await self._sleep(
                reply_style.sample_delay_ms(self.options.style, rng=self._rng)
                / 1000.0
            )
            correction = await self._send_text(
                actions,
                event,
                typo_original,
                reply_to=last_id or None,
                capabilities=capabilities,
            )
            if correction is not None:
                self._record_outgoing(
                    conversation, typo_original, correction, now=int(self._clock())
                )
        if (
            plan.meme_id is not None
            and sent
            and Capability.SEND_IMAGE in capabilities
        ):
            uri = self.memes.file_uri(plan.meme_id)
            if uri:
                await self._send_image(actions, event, uri)
                self._record_outgoing(
                    conversation, "[表情包]", "", now=int(self._clock())
                )
                self.memes.mark_used(plan.meme_id, now=int(self._clock()))
        return len(sent)

    async def _compose_reply(
        self,
        *,
        model_key: str,
        mode: str,
        images: Sequence[ChatImage] = (),
        candidates: Sequence[MemeCandidateView] = (),
        **kwargs: Any,
    ) -> ReplyPlan:
        """Compose a reply; fall back to text-only when media input fails."""

        try:
            return await self.replyer.compose(
                model_key=model_key,
                mode=mode,
                images=images,
                candidates=candidates,
                **kwargs,
            )
        except ResponseParseError:
            raise
        except ProviderError:
            if not images and not candidates:
                raise
            failures = int(self._notice_state.get("vision_failures", 0)) + 1
            self._notice_state["vision_failures"] = failures
            self._log_info(
                "模型不接受图片输入，降级为纯文本重试（第 %d 次）" % failures
            )
            return await self.replyer.compose(
                model_key=model_key,
                mode=mode,
                images=(),
                candidates=(),
                **kwargs,
            )

    async def _send_text(
        self,
        actions: Any,
        event: Any,
        text: str,
        *,
        reply_to: str | None = None,
        at_user_ids: Sequence[str] = (),
        capabilities: frozenset[Any] | None = None,
    ) -> str | None:
        """Send one text message; ``None`` means the adapter rejected it."""

        caps = capabilities if capabilities is not None else self._capabilities(actions)
        segments: list[Any] = []
        if reply_to and Capability.SEND_REPLY in caps:
            segments.append(Segments.Reply(str(reply_to)))
        for user_id in at_user_ids:
            mention = str(user_id or "").strip()
            if mention:
                segments.append(Segments.At(mention))
        segments.append(Segments.Text(str(text)))
        try:
            result = await actions.send(
                message=Manager.Message(*segments),
                **self._target_kwargs(event),
            )
        except Exception:
            self._log_exception("发送消息失败")
            return None
        data = getattr(result, "data", None)
        message_id = getattr(data, "message_id", None)
        return "" if message_id is None else str(message_id)

    async def _send_image(self, actions: Any, event: Any, uri: str) -> bool:
        try:
            await actions.send(
                message=Manager.Message(Segments.Image(uri)),
                **self._target_kwargs(event),
            )
        except Exception:
            self._log_exception("发送表情包失败")
            return False
        return True

    def _record_outgoing(
        self,
        conversation: ConversationRecord,
        content: str,
        message_id: str,
        *,
        now: int,
    ) -> None:
        with contextlib.suppress(Exception):
            self.store.append_message(
                conversation.ref,
                direction="outgoing",
                content=content,
                platform_message_id=message_id,
                sender_id="self",
                sender_name=self._effective_persona().name,
                message_type="image" if content == "[表情包]" else "text",
                segments_json=json.dumps(
                    [{"type": "text", "text": content}],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                now=now,
            )

    async def _moderate(self, plan: ReplyPlan, *, context: str) -> Any:
        from plugins.JianerMate.moderation import ModerationDecision

        if self.moderator is None:
            return ModerationDecision(allowed=True)
        texts = reply_style.sanitize_messages(
            plan.messages, self.options.style, rng=self._rng
        )
        return await self.moderator.review_many(texts, context=context)

    # ------------------------------------------------------------------ #
    # media helpers
    # ------------------------------------------------------------------ #
    async def _resolve_inputs(
        self,
        event: Any,
        actions: Any,
        key: ConversationRef,
    ) -> tuple[tuple[ChatImage, ...], str]:
        caps = self._capabilities(actions)
        segments: list[Any] = list(getattr(event, "message", ()) or ())
        reference_text = ""
        reply_segment = next(
            (item for item in segments if isinstance(item, Segments.Reply)),
            None,
        )
        if reply_segment is not None and Capability.RESOLVE_REFERENCE in caps:
            try:
                resolved = await actions.resolve_reference(
                    str(reply_segment.id),
                    conversation=self._framework_key(key),
                )
                if (
                    getattr(resolved, "status", None) is ResolutionStatus.OK
                    and getattr(resolved, "segments", None)
                ):
                    segments = list(resolved.segments) + segments
                    reference_text = self._segments_text(resolved.segments)
            except Exception:
                self._log_exception("引用消息解析失败")
        if Capability.RESOLVE_MEDIA not in caps:
            return (), reference_text
        images: list[ChatImage] = []
        for segment in segments:
            if len(images) >= MAX_INPUT_IMAGES:
                break
            if not isinstance(segment, Segments.Image):
                continue
            request = self._media_request(segment, event)
            if request is None:
                continue
            try:
                resolution = await actions.resolve_media(
                    request,
                    conversation=self._framework_key(key),
                    policy=self._media_policy(request),
                )
            except Exception:
                self._log_exception("图片解析失败")
                continue
            if (
                getattr(resolution, "status", None) is ResolutionStatus.OK
                and getattr(resolution, "data", None)
            ):
                images.append(
                    ChatImage(
                        data=bytes(resolution.data),
                        mime=str(getattr(resolution, "mime", "") or "image/jpeg"),
                    )
                )
        return tuple(images), reference_text

    async def _capture_meme(
        self,
        event: Any,
        actions: Any,
        key: ConversationRef,
    ) -> None:
        caps = self._capabilities(actions)
        if Capability.RESOLVE_MEDIA not in caps:
            return
        for segment in getattr(event, "message", ()) or ():
            if not isinstance(segment, Segments.Image):
                continue
            request = self._media_request(segment, event)
            if request is None:
                continue
            try:
                resolution = await actions.resolve_media(
                    request,
                    conversation=self._framework_key(key),
                    policy=self._media_policy(request),
                )
            except Exception:
                return
            if (
                getattr(resolution, "status", None) is not ResolutionStatus.OK
                or not getattr(resolution, "data", None)
            ):
                return
            if len(bytes(resolution.data)) > self.options.meme.max_size_bytes:
                return
            result = self.memes.ingest(
                bytes(resolution.data),
                source_group=key.conversation_id,
                source_user=str(getattr(event, "user_id", "") or ""),
                now=int(self._clock()),
            )
            if result.status == "added":
                self._log_info(f"已收藏表情包 #{result.meme_id}")
            return

    def _media_request(self, segment: Any, event: Any) -> MediaRequest | None:
        locator = str(
            getattr(segment, "url", None)
            or getattr(segment, "file", None)
            or ""
        ).strip()
        if not locator:
            return None
        lowered = locator.casefold()
        if lowered.startswith(("http://", "https://")):
            kind = MediaSourceKind.REMOTE_URL
        elif lowered.startswith("data:"):
            kind = MediaSourceKind.DATA_URI
        elif lowered.startswith("file:"):
            kind = MediaSourceKind.LOCAL_FILE
        else:
            kind = MediaSourceKind.ADAPTER_RESOURCE
        return MediaRequest(
            kind=kind,
            media_kind=MediaKind.IMAGE,
            locator=locator,
            message_id=str(getattr(event, "message_id", "") or "") or None,
        )

    @staticmethod
    def _media_policy(request: MediaRequest) -> MediaPolicy:
        origins: frozenset[str] = frozenset()
        if request.kind is MediaSourceKind.REMOTE_URL:
            parsed = urlsplit(request.locator)
            if parsed.scheme.casefold() in {"http", "https"} and parsed.hostname:
                port = f":{parsed.port}" if parsed.port else ""
                origins = frozenset(
                    {
                        f"{parsed.scheme.casefold()}://{parsed.hostname.casefold()}{port}"
                    }
                )
        return MediaPolicy(
            max_bytes=MAX_IMAGE_BYTES,
            connect_timeout_seconds=3.0,
            total_timeout_seconds=15.0,
            max_redirects=3,
            allowed_remote_origins=origins,
            allowed_local_roots=(),
            allowed_mime_types=IMAGE_MIME_TYPES,
        )

    # ------------------------------------------------------------------ #
    # commands
    # ------------------------------------------------------------------ #
    async def handle_active_command(
        self,
        event: Any,
        actions: Any,
        action: str = "",
        value: str = "",
    ) -> bool:
        key = self._conversation_ref(event)
        if key is None or not key.is_group:
            await self._reply(actions, event, "该命令只能在群里使用。")
            return True
        normalized = str(action or "").strip()
        if normalized in {"", "状态"}:
            await self._reply(actions, event, self._status_text(key))
            return True
        if not self._can_manage(event):
            await self._reply(actions, event, "只有群主/管理员或机器人管理员能改这个设置。")
            return True
        conversation = self.store.ensure_conversation(
            key.protocol, key.self_id, key.kind, key.conversation_id,
            now=int(self._clock()),
        )
        if normalized in {"开启", "打开", "启用"}:
            self.store.update_conversation(
                conversation.ref, enabled=True, now=int(self._clock())
            )
            await self._reply(
                actions,
                event,
                "主动发言已开启（默认频率 %.2f，免打扰 %s）。"
                % (
                    self._talk_value(conversation),
                    conversation.quiet_hours or self.options.quiet_hours_default,
                ),
            )
            return True
        if normalized in {"关闭", "关掉", "停用"}:
            self.store.update_conversation(
                conversation.ref, enabled=False, now=int(self._clock())
            )
            await self._reply(actions, event, "主动发言已关闭。")
            return True
        if normalized in {"频率", "talk_value"}:
            try:
                parsed = float(str(value).strip())
            except (TypeError, ValueError):
                await self._reply(actions, event, "用法：~主动发言 频率 0.5（0 到 1）")
                return True
            parsed = max(0.0, min(1.0, parsed))
            self.store.update_conversation(
                conversation.ref, talk_value=parsed, now=int(self._clock())
            )
            await self._reply(actions, event, f"发言频率已设为 {parsed:.2f}。")
            return True
        if normalized in {"免打扰", "静默", "静音"}:
            window = str(value or "").strip()
            if window in {"关闭", "关", "off", "无"}:
                self.store.update_conversation(
                    conversation.ref, quiet_hours="", now=int(self._clock())
                )
                await self._reply(actions, event, "免打扰时段已关闭。")
                return True
            if not valid_hours_window(window):
                await self._reply(
                    actions,
                    event,
                    "用法：~主动发言 免打扰 23:00-08:00，或 ~主动发言 免打扰 关闭",
                )
                return True
            self.store.update_conversation(
                conversation.ref, quiet_hours=window, now=int(self._clock())
            )
            await self._reply(actions, event, f"免打扰时段已设为 {window}。")
            return True
        if normalized in {"提示", "查看提示"}:
            current = conversation.custom_prompt or "（未设置）"
            await self._reply(
                actions,
                event,
                f"本群补充要求：{current}\n"
                "修改用 ~设置提示 <文本>，清空用 ~清空提示。",
            )
            return True
        if normalized in {"调试", "dry_run"}:
            if not self._is_bot_admin(event):
                await self._reply(actions, event, "调试模式只有机器人管理员能开关。")
                return True
            enabled = not conversation.debug_dry_run
            self.store.update_conversation(
                conversation.ref, debug_dry_run=enabled, now=int(self._clock())
            )
            await self._reply(
                actions,
                event,
                "调试模式已开启：只记录决策，不会真的发言。"
                if enabled
                else "调试模式已关闭。",
            )
            return True
        if normalized == "重载":
            if not self._is_bot_admin(event):
                await self._reply(actions, event, "重载只有机器人管理员能执行。")
                return True
            await self.reload()
            await self._reply(
                actions,
                event,
                "配置与 aiconfig 已重载。\n插件配置："
                f"{self.options.config_path}"
                f"（{'已加载' if self.options.config_loaded else '尚未创建'}）",
            )
            return True
        if normalized in {"决策", "日志"}:
            try:
                limit = max(1, min(10, int(str(value).strip() or "5")))
            except ValueError:
                limit = 5
            records = self.store.recent_decisions(
                conversation.ref, limit=limit
            )
            if not records:
                await self._reply(actions, event, "还没有决策记录。")
                return True
            lines = [f"最近的主动发言决策（{len(records)} 条）："]
            for record in records:
                stamp = time.strftime(
                    "%m-%d %H:%M", time.localtime(record.created_at)
                )
                detail = record.gate_reason or "-"
                if record.planner_json:
                    with contextlib.suppress(Exception):
                        payload = json.loads(record.planner_json)
                        planner_reason = str(payload.get("reason") or "").strip()
                        if planner_reason:
                            detail = planner_reason
                if record.error:
                    detail = f"{detail}｜{record.error}"
                status = (
                    "已发言"
                    if record.spoke
                    else ("已通过" if record.gate_passed else "未通过")
                )
                lines.append(f"{stamp}｜{status}｜{detail[:60]}")
            await self._reply(actions, event, "\n".join(lines))
            return True
        await self._reply(
            actions,
            event,
            "用法：~主动发言 [状态|开启|关闭|频率 0.5|免打扰 23:00-08:00|"
            "提示|调试|决策|重载]",
        )
        return True

    # ------------------------------------------------------------------ #
    # persona commands
    # ------------------------------------------------------------------ #
    async def handle_persona_command(
        self,
        event: Any,
        actions: Any,
        action: str = "",
        value: str = "",
    ) -> bool:
        if not self._is_bot_admin(event):
            await self._reply(actions, event, "人格设置只有机器人管理员能修改。")
            return True
        normalized = str(action or "").strip()
        if normalized in {"", "状态", "查看", "查看状态"}:
            await self._reply(actions, event, self._persona_text())
            return True
        if normalized in {"转换", "转写", "导入"}:
            await self._convert_persona_command(
                event, actions, normalized, value
            )
            return True
        if normalized in {"重置", "清空"}:
            self._clear_persona_override()
            await self._reply(
                actions, event, "已恢复为 config.json 里的人格设定。"
            )
            return True
        if normalized in {"概率", "切换概率"}:
            try:
                parsed = float(str(value).strip())
            except (TypeError, ValueError):
                await self._reply(
                    actions, event, "用法：~人格 概率 0.2（0 到 1）"
                )
                return True
            persona = self._effective_persona().with_multiple_probability(parsed)
            self._save_persona_override(persona)
            warning = (
                "\n注意：当前没有备用说话风格，概率不会生效。"
                if not persona.multiple_reply_styles
                else ""
            )
            await self._reply(
                actions,
                event,
                f"备用风格切换概率已设为 {persona.multiple_probability:.2f}。{warning}",
            )
            return True
        if normalized in {"风格", "风格列表"}:
            persona = self._effective_persona()
            if not persona.multiple_reply_styles:
                await self._reply(actions, event, "当前没有备用说话风格。")
                return True
            lines = [
                f"{index}. {style}"
                for index, style in enumerate(
                    persona.multiple_reply_styles, start=1
                )
            ]
            await self._reply(
                actions,
                event,
                "备用说话风格：\n" + "\n".join(lines),
            )
            return True
        if normalized in {"风格清空", "清空风格"}:
            persona = replace(
                self._effective_persona(), multiple_reply_styles=()
            )
            self._save_persona_override(persona)
            await self._reply(actions, event, "备用说话风格已清空。")
            return True
        if normalized in {"风格添加", "添加风格"}:
            text = " ".join(str(value or "").split())[:400]
            if not text:
                await self._reply(
                    actions, event, "用法：~人格 风格添加 用1-2个字回复"
                )
                return True
            persona = self._effective_persona()
            styles = list(persona.multiple_reply_styles)
            if text in styles:
                await self._reply(actions, event, "这条备用风格已经存在了。")
                return True
            if len(styles) >= MAX_MULTIPLE_STYLES:
                await self._reply(
                    actions,
                    event,
                    f"备用风格最多 {MAX_MULTIPLE_STYLES} 条，"
                    "可以用 ~人格 风格删除 <编号> 腾位置。",
                )
                return True
            styles.append(text)
            self._save_persona_override(
                replace(persona, multiple_reply_styles=tuple(styles))
            )
            await self._reply(
                actions, event, f"已添加备用风格 #{len(styles)}。"
            )
            return True
        if normalized in {"风格删除", "删除风格"}:
            try:
                index = int(str(value).strip())
            except (TypeError, ValueError):
                await self._reply(
                    actions, event, "用法：~人格 风格删除 <编号>"
                )
                return True
            persona = self._effective_persona()
            styles = list(persona.multiple_reply_styles)
            if not 1 <= index <= len(styles):
                await self._reply(
                    actions,
                    event,
                    f"编号超出范围，当前共有 {len(styles)} 条备用风格。",
                )
                return True
            removed = styles.pop(index - 1)
            self._save_persona_override(
                replace(persona, multiple_reply_styles=tuple(styles))
            )
            await self._reply(
                actions, event, f"已删除备用风格：{removed}"
            )
            return True
        await self._reply(
            actions,
            event,
            "用法：~人格 [状态|转换 <文本>|导入 <旧预设名>|重置|概率 0.2|"
            "风格|风格清空]",
        )
        return True

    async def _convert_persona_command(
        self,
        event: Any,
        actions: Any,
        action: str,
        value: str,
    ) -> None:
        model_key = self._planner_model_key() or self._replyer_model_key()
        if not model_key:
            await self._reply(
                actions, event, "没有可用的 aiconfig 模型，无法转写人格。"
            )
            return
        source_name = ""
        source_text = ""
        if action == "导入":
            catalog, errors = load_jianer_ai_presets(self.options.project_root)
            if not catalog:
                detail = (
                    "；".join(sorted(errors.values()))[:200]
                    if errors
                    else "没有找到 prerequisites/current.json"
                )
                await self._reply(actions, event, f"没有可导入的旧预设：{detail}")
                return
            entry = resolve_preset(catalog, value)
            if entry is None:
                choices = "，".join(
                    f"{item.name}({key})"
                    for key, item in sorted(catalog.items())
                )
                await self._reply(
                    actions, event, f"没有找到这个预设。可用：{choices}"
                )
                return
            source_name = entry.name or entry.key
            source_text = entry.template
        else:
            source_text = str(value or "").strip()
            if not source_text:
                key = self._conversation_ref(event)
                if key is not None:
                    _images, reference_text = await self._resolve_inputs(
                        event, actions, key
                    )
                    source_text = reference_text
            if not source_text:
                await self._reply(
                    actions,
                    event,
                    "用法：~人格 转换 <原设定文本>，"
                    "或回复一条包含原设定的消息再发 ~人格 转换",
                )
                return
            source_name = "消息内容"
        await self._reply(actions, event, "正在用模型转写人格，请稍等…")
        try:
            persona = await self.persona_converter.convert(
                model_key=model_key,
                source_text=source_text,
                source_name=source_name,
                base=self.options.persona,
            )
        except PersonaError as exc:
            await self._reply(actions, event, f"转写失败：{exc}")
            return
        except ProviderError as exc:
            await self._reply(actions, event, f"转写失败：{exc}")
            return
        self._save_persona_override(persona)
        await self._reply(
            actions,
            event,
            "人格已更新（来源：%s）\n%s"
            % (source_name, "\n".join(persona.summary_lines()[:6])),
        )

    # ------------------------------------------------------------------ #
    # conversation prompts
    # ------------------------------------------------------------------ #
    async def set_group_prompt(self, event: Any, actions: Any, text: str) -> bool:
        return await self.set_conversation_prompt(event, actions, text)

    async def set_conversation_prompt(
        self,
        event: Any,
        actions: Any,
        text: str,
    ) -> bool:
        """Extra prompt for the current chat; groups or private both allowed."""

        key = self._conversation_ref(event)
        if key is None:
            await self._reply(actions, event, "无法识别当前会话。")
            return True
        if not self._can_manage(event):
            await self._reply(actions, event, "只有群主/管理员或机器人管理员能改这个设置。")
            return True
        prompt = " ".join(str(text or "").split())[:400]
        if not prompt:
            await self._reply(
                actions, event, "用法：~设置提示 <文本>（群里需要群主/管理员）"
            )
            return True
        conversation = self.store.ensure_conversation(
            key.protocol, key.self_id, key.kind, key.conversation_id,
            now=int(self._clock()),
        )
        self.store.update_conversation(
            conversation.ref, custom_prompt=prompt, now=int(self._clock())
        )
        label = "本群" if key.is_group else "本会话"
        await self._reply(actions, event, f"{label}补充要求已更新。")
        return True

    async def clear_group_prompt(self, event: Any, actions: Any) -> bool:
        return await self.clear_conversation_prompt(event, actions)

    async def clear_conversation_prompt(self, event: Any, actions: Any) -> bool:
        key = self._conversation_ref(event)
        if key is None:
            await self._reply(actions, event, "无法识别当前会话。")
            return True
        if not self._can_manage(event):
            await self._reply(actions, event, "只有群主/管理员或机器人管理员能改这个设置。")
            return True
        conversation = self.store.ensure_conversation(
            key.protocol, key.self_id, key.kind, key.conversation_id,
            now=int(self._clock()),
        )
        self.store.update_conversation(
            conversation.ref, custom_prompt="", now=int(self._clock())
        )
        label = "本群" if key.is_group else "本会话"
        await self._reply(actions, event, f"{label}补充要求已清空。")
        return True

    async def steal_meme_command(self, event: Any, actions: Any) -> bool:
        key = self._conversation_ref(event)
        if key is None:
            return True
        if not self.options.meme.enabled:
            await self._reply(actions, event, "表情包功能当前被关闭了。")
            return True
        images = await self._collect_command_images(event, actions, key)
        if not images:
            await self._reply(actions, event, "没有找到图片：回复一张图片再发 ~偷表情。")
            return True
        result = self.memes.ingest(
            images[0].data,
            source_group=key.conversation_id,
            source_user=str(getattr(event, "user_id", "") or ""),
            now=int(self._clock()),
        )
        if result.status == "added":
            await self._reply(actions, event, f"已收藏表情包 #{result.meme_id}。")
        elif result.status == "duplicate":
            await self._reply(actions, event, "这张表情包里已经有了。")
        else:
            await self._reply(actions, event, f"收藏失败：{result.reason or '未知原因'}。")
        return True

    async def meme_command(
        self,
        event: Any,
        actions: Any,
        action: str = "",
        argument: str = "",
        extra: str = "",
    ) -> bool:
        normalized = str(action or "").strip()
        if normalized in {"", "状态"}:
            status = self.memes.status()
            await self._reply(
                actions,
                event,
                "表情包库：%s 张（启用 %s 张，%.1f MB）\n目录：%s"
                % (
                    status["count"],
                    status["enabled_count"],
                    status["bytes"] / (1024 * 1024),
                    status["directory"],
                ),
            )
            return True
        if normalized in {"列表", "list"}:
            page = 1
            try:
                page = max(1, int(str(argument).strip() or "1"))
            except ValueError:
                page = 1
            page_size = 10
            records = self.store.list_memes(
                limit=page_size, offset=(page - 1) * page_size
            )
            if not records:
                await self._reply(actions, event, "这一页没有表情包。")
                return True
            lines = [
                f"#{record.id} {'✅' if record.enabled else '⛔'} "
                f"{record.tags or '无标签'} ({record.width}x{record.height})"
                for record in records
            ]
            await self._reply(actions, event, f"表情包第 {page} 页：\n" + "\n".join(lines))
            return True
        if normalized in {"查看", "show"}:
            if not self._can_manage(event):
                await self._reply(
                    actions,
                    event,
                    "只有群主/管理员或机器人管理员能查看表情包详情。",
                )
                return True
            try:
                meme_id = int(str(argument).strip())
            except ValueError:
                await self._reply(actions, event, "用法：~表情包 查看 <编号>")
                return True
            record = self.store.get_meme(meme_id)
            if record is None:
                await self._reply(actions, event, f"没有找到 #{meme_id}。")
                return True
            caption = (
                f"#{meme_id}｜{record.width}x{record.height}"
                f"｜{max(1, record.size // 1024)}KB"
                f"｜标签：{record.tags or '无标签'}"
                f"｜已用 {record.use_count} 次"
            )
            uri = self.memes.file_uri(meme_id)
            if uri is None or Capability.SEND_IMAGE not in self._capabilities(
                actions
            ):
                await self._reply(
                    actions, event, caption + "（当前无法发送图片）"
                )
                return True
            if await self._send_image(actions, event, uri):
                await self._reply(actions, event, caption)
            else:
                await self._reply(actions, event, "发送预览失败，请稍后再试。")
            return True
        if normalized in {"删除", "disable"}:
            if not self._is_bot_admin(event):
                await self._reply(actions, event, "删除表情包只有机器人管理员能执行。")
                return True
            try:
                meme_id = int(str(argument).strip())
            except ValueError:
                await self._reply(actions, event, "用法：~表情包 删除 <编号>")
                return True
            deleted = self.memes.delete(meme_id)
            await self._reply(
                actions,
                event,
                f"已删除表情包 #{meme_id}。" if deleted else f"没有找到 #{meme_id}。",
            )
            return True
        if normalized in {"标签", "tag"}:
            if not self._is_bot_admin(event):
                await self._reply(actions, event, "改标签只有机器人管理员能执行。")
                return True
            try:
                meme_id = int(str(argument).strip())
            except ValueError:
                await self._reply(actions, event, "用法：~表情包 标签 <编号> 猫,无语")
                return True
            tags = " ".join(str(extra or "").split())[:120]
            updated = self.store.update_meme_tags(meme_id, tags)
            await self._reply(
                actions,
                event,
                f"已更新 #{meme_id} 的标签。" if updated else f"没有找到 #{meme_id}。",
            )
            return True
        if normalized in {"清空", "clear"}:
            if not self._is_bot_admin(event):
                await self._reply(actions, event, "清空表情包库只有机器人管理员能执行。")
                return True
            if str(argument).strip() != "确认":
                await self._reply(actions, event, "确认清空请输入：~表情包 清空 确认")
                return True
            removed = self.memes.clear()
            await self._reply(actions, event, f"已清空 {removed} 张表情包。")
            return True
        await self._reply(
            actions,
            event,
            "用法：~表情包 [状态|列表 1|删除 <编号>|标签 <编号> <标签>|清空 确认]",
        )
        return True

    async def _collect_command_images(
        self,
        event: Any,
        actions: Any,
        key: ConversationRef,
    ) -> tuple[ChatImage, ...]:
        images, _ = await self._resolve_inputs(event, actions, key)
        return images

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    async def reload(self) -> None:
        options = RuntimeOptions.from_runtime(self.runtime)
        entries, errors = load_models(options.ai_config_dir)
        self.options = options
        self._model_errors = errors
        ensure_config_file(options.config_path, options)
        with contextlib.suppress(Exception):
            await self.tool_runtime.shutdown()
        self.tool_runtime = MateToolRuntime(
            options.tools,
            project_root=options.project_root,
            logger=self._logger,
        )
        with contextlib.suppress(Exception):
            self.long_term.close()
        self.long_term = LongTermMemory(
            options.memory_db_path,
            enabled=bool(options.memory_enabled),
            logger=self._logger,
            backend=options.memory_backend,
            data_dir=options.memory_data_dir,
            project_root=options.project_root,
            embedding_config_path=options.memory_embedding_config_path,
            bot_name=options.persona.name,
        )
        if hasattr(self.client, "reload"):
            self.client.reload(entries)
        self.memes.options = options.meme
        self.memes.directory = Path(options.meme_dir)
        self.memes.directory.mkdir(parents=True, exist_ok=True)
        self._notice_state.pop("vision_failures", None)
        self.moderator = None
        if options.moderation_enabled and options.moderation_model:
            self.moderator = ContentModerator(
                self.client, options.moderation_model, logger=self._logger
            )

    def _planner_model_key(self) -> str:
        candidate = self.options.planner_model or self.options.replyer_model
        return candidate if self.client.has_model(candidate) else ""

    def _replyer_model_key(self) -> str:
        candidate = self.options.replyer_model or self.options.planner_model
        return candidate if self.client.has_model(candidate) else ""

    def _replyer_supports_images(self, model_key: str) -> bool:
        """Assume vision support and degrade after repeated failures."""

        if not self.options.meme.enabled:
            return False
        failures = int(self._notice_state.get("vision_failures", 0))
        return failures < 3

    def _meme_candidate_views(
        self,
        *,
        now: int,
        allow_images: bool,
    ) -> tuple[MemeCandidateView, ...]:
        if not allow_images:
            return ()
        candidates = self.memes.candidates(now=now)
        views: list[MemeCandidateView] = []
        for candidate in candidates:
            thumbnail = self.memes.thumbnail_data_uri(candidate)
            if not thumbnail:
                continue
            views.append(
                MemeCandidateView(
                    meme_id=candidate.id,
                    tags=candidate.tags,
                    thumbnail=thumbnail,
                )
            )
        return tuple(views)

    def _conversation_ref(self, event: Any) -> ConversationRef | None:
        message = getattr(event, "message", None)
        if message is None:
            return None
        protocol = str(getattr(event, "protocol", "") or "").strip()
        self_id = str(getattr(event, "self_id", "") or "").strip()
        if not protocol or not self_id:
            return None
        group_id = getattr(event, "group_id", None)
        user_id = getattr(event, "user_id", None)
        if group_id is not None:
            return ConversationRef(
                protocol=protocol,
                self_id=self_id,
                kind=ConversationKind.GROUP.value,
                conversation_id=str(group_id),
            )
        if user_id is not None:
            return ConversationRef(
                protocol=protocol,
                self_id=self_id,
                kind=ConversationKind.PRIVATE.value,
                conversation_id=str(user_id),
            )
        return None

    @staticmethod
    def _framework_key(key: ConversationRef) -> ConversationKey:
        return ConversationKey(
            protocol=key.protocol,
            self_id=key.self_id,
            kind=ConversationKind(key.kind),
            conversation_id=key.conversation_id,
            preset="mate",
        )

    def _is_self_message(self, event: Any) -> bool:
        user_id = str(getattr(event, "user_id", "") or "")
        self_id = str(getattr(event, "self_id", "") or "")
        return bool(user_id) and user_id == self_id

    @staticmethod
    def _capabilities(actions: Any) -> frozenset[Any]:
        return frozenset(getattr(actions, "capabilities", ()) or ())

    @staticmethod
    def _target_kwargs(event: Any) -> dict[str, Any]:
        group_id = getattr(event, "group_id", None)
        if group_id is not None:
            return {"group_id": group_id}
        return {"user_id": getattr(event, "user_id", None)}

    def _conversation_label(self, key: ConversationRef) -> str:
        if key.is_group:
            return f"群 {key.conversation_id}"
        return f"私聊 {key.conversation_id}"

    @staticmethod
    def _sender_name(event: Any) -> str:
        sender = getattr(event, "sender", None)
        for attribute in ("card", "nickname", "name"):
            value = getattr(sender, attribute, None) if sender is not None else None
            if value:
                return str(value)
        return str(getattr(event, "user_id", "") or "")

    @staticmethod
    def _segments_text(segments: Sequence[Any]) -> str:
        parts: list[str] = []
        for segment in segments:
            if isinstance(segment, Segments.Text):
                text = str(getattr(segment, "text", "") or "").strip()
                if text:
                    parts.append(text)
            elif isinstance(segment, Segments.Image):
                parts.append("[图片]")
        return " ".join(parts).strip()

    def _event_text(self, event: Any) -> str:
        parts: list[str] = []
        for segment in getattr(event, "message", ()) or ():
            if isinstance(segment, Segments.Text):
                text = str(getattr(segment, "text", "") or "").strip()
                if text:
                    parts.append(text)
            elif isinstance(segment, Segments.At):
                parts.append(f"@{getattr(segment, 'qq', '')}")
            elif isinstance(segment, Segments.Image):
                parts.append("[图片]")
            elif isinstance(segment, Segments.Record):
                parts.append("[语音]")
            elif isinstance(segment, Segments.Video):
                parts.append("[视频]")
            elif isinstance(segment, Segments.Faces):
                parts.append("[表情]")
            elif isinstance(segment, Segments.Reply):
                continue
            else:
                parts.append(f"[{segment.__class__.__name__}]")
        return " ".join(parts).strip()

    def _text_without_mentions(self, event: Any) -> str:
        parts: list[str] = []
        for segment in getattr(event, "message", ()) or ():
            if isinstance(segment, Segments.Text):
                text = str(getattr(segment, "text", "") or "").strip()
                if text:
                    parts.append(text)
        return " ".join(parts).strip()

    def _mentions_self(self, event: Any) -> bool:
        self_id = str(getattr(event, "self_id", "") or "")
        if bool(getattr(event, "is_mentioned", False)):
            return True
        return any(
            isinstance(segment, Segments.At)
            and str(getattr(segment, "qq", "")) == self_id
            for segment in (getattr(event, "message", ()) or ())
        )

    @staticmethod
    def _has_image(event: Any) -> bool:
        return any(
            isinstance(segment, Segments.Image)
            for segment in (getattr(event, "message", ()) or ())
        )

    def _message_type(self, event: Any) -> str:
        kinds: set[str] = set()
        for segment in getattr(event, "message", ()) or ():
            if isinstance(segment, Segments.Text):
                kinds.add("text")
            elif isinstance(segment, Segments.Image):
                kinds.add("image")
            elif isinstance(segment, Segments.Record):
                kinds.add("audio")
            elif isinstance(segment, Segments.Video):
                kinds.add("video")
            else:
                kinds.add("other")
        if not kinds:
            return "text"
        if len(kinds) == 1:
            return next(iter(kinds))
        return "mixed"

    @staticmethod
    def _segments_json(event: Any) -> str:
        output: list[dict[str, str]] = []
        for segment in getattr(event, "message", ()) or ():
            item: dict[str, str] = {
                "type": segment.__class__.__name__.casefold()
            }
            if isinstance(segment, Segments.Text):
                item["text"] = str(getattr(segment, "text", ""))
            elif isinstance(segment, Segments.At):
                item["target"] = str(getattr(segment, "qq", ""))
            elif isinstance(segment, Segments.Reply):
                item["message_id"] = str(getattr(segment, "id", ""))
            else:
                for attribute in ("id", "url", "name", "summary"):
                    value = getattr(segment, attribute, None)
                    if value is not None:
                        item[attribute] = str(value)[:500]
            output.append(item)
        return json.dumps(output, ensure_ascii=False, separators=(",", ":"))

    def _talk_value(self, conversation: ConversationRecord) -> float:
        if conversation.talk_value is None:
            return float(self.options.talk_value_default)
        return max(0.0, min(1.0, float(conversation.talk_value)))

    def _in_quiet_hours(self, conversation: ConversationRecord, *, now: float) -> bool:
        window = (
            conversation.quiet_hours
            if conversation.quiet_hours is not None
            else self.options.quiet_hours_default
        )
        return in_quiet_hours(window, now=now)

    async def _maybe_send_config_hint(
        self,
        key: ConversationRef,
        event: Any,
        actions: Any,
    ) -> None:
        now = self._clock()
        last = float(self._notice_state.get(f"hint:{key}", 0.0))
        if now - last < HINT_INTERVAL_SECONDS:
            return
        self._notice_state[f"hint:{key}"] = now
        await self._reply(actions, event, CONFIG_HINT)

    async def _reply(self, actions: Any, event: Any, text: str) -> None:
        await self._send_text(actions, event, text)

    def _status_text(self, key: ConversationRef) -> str:
        conversation = self.store.get_conversation(
            key.protocol, key.self_id, key.kind, key.conversation_id
        )
        enabled = "开启" if conversation is not None and conversation.enabled else "关闭"
        talk_value = (
            self._talk_value(conversation)
            if conversation is not None
            else self.options.talk_value_default
        )
        quiet = (
            conversation.quiet_hours
            if conversation is not None and conversation.quiet_hours is not None
            else self.options.quiet_hours_default
        )
        interval = (
            conversation.min_interval_seconds
            if conversation is not None and conversation.min_interval_seconds is not None
            else self.options.min_interval_seconds
        )
        debug = "开" if conversation is not None and conversation.debug_dry_run else "关"
        planner = self._planner_model_key() or "未配置"
        replyer = self._replyer_model_key() or "未配置"
        unsupported = [
            entry.key for entry in self.client.models.values() if not entry.supported
        ]
        lines = [
            "JianerMate 状态",
            f"人格：{self._effective_persona().name}"
            f"（来源：{'转换结果' if self.persona_override else 'config.json'}）"
            f"｜备用风格 {len(self._effective_persona().multiple_reply_styles)} 条"
            f"｜切换概率 {self._effective_persona().multiple_probability:.2f}",
            f"插件配置：{self.options.config_path}"
            + (
                f"（已加载 {len(self.options.config_keys)} 项）"
                if self.options.config_loaded
                else "（未创建或未加载，使用 config.others）"
            ),
            f"主动发言：{enabled}｜频率：{talk_value:.2f}｜免打扰：{quiet or '无'}｜"
            f"最小间隔：{interval}s｜调试：{debug}",
            f"Planner：{planner}｜Replyer：{replyer}",
            f"工具调用：{self._tool_status_text()}",
            f"回复时 @ 发言人：{'开' if self.options.reply_at_enabled else '关'}"
            f"｜点名更容易接话：{'开' if self.options.mentioned_reply_enabled else '关'}",
            f"表情包：{self.memes.status()['count']} 张",
            f"补充要求：{conversation.custom_prompt if conversation and conversation.custom_prompt else '（未设置）'}",
        ]
        vision_failures = int(self._notice_state.get("vision_failures", 0))
        if vision_failures >= 3:
            lines.append(
                f"注意：Replyer 图片输入已连续失败 {vision_failures} 次，"
                "表情包发送已暂停，重载或更换视觉模型后恢复。"
            )
        if self.conflict:
            lines.append(f"冲突：{self._conflict_detail}")
        if unsupported:
            lines.append("不支持协议的模型：" + "，".join(sorted(unsupported)))
        if self._model_errors:
            lines.append(
                "配置错误：" + "；".join(sorted(self._model_errors))[:200]
            )
        if self.options.config_errors:
            lines.append(
                "插件配置文件错误：" + "；".join(self.options.config_errors)[:300]
            )
        return "\n".join(lines)

    def _is_bot_admin(self, event: Any) -> bool:
        user_id = str(getattr(event, "user_id", "") or "")
        if not user_id:
            return False
        admins: set[str] = set()
        for name in ("root_users", "super_users", "manage_users", "admins"):
            values = self.runtime.get(name) or ()
            admins.update(str(value) for value in values)
        owner_ids = self.runtime.get("owner") or ()
        admins.update(str(value) for value in owner_ids)
        return user_id in admins

    def _can_manage(self, event: Any) -> bool:
        if self._is_bot_admin(event):
            return True
        sender = getattr(event, "sender", None)
        role = str(getattr(sender, "role", "") or "").casefold()
        if role in {"owner", "admin"}:
            return True
        return bool(getattr(event, "is_owner", False))

    def _spawn(self, coroutine: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _transcript_text(self, recent: Sequence[MessageRecord]) -> str:
        parts = [
            f"{record.sender_name or record.sender_id}: {record.content}"
            for record in recent[-8:]
            if record.content
        ]
        return "\n".join(parts)[-800:]

    def _log_info(self, message: str) -> None:
        for method in ("info", "log"):
            handler = getattr(self._logger, method, None)
            if callable(handler):
                handler(message)
                return

    def _log_error(self, message: str) -> None:
        for method in ("error", "log"):
            handler = getattr(self._logger, method, None)
            if callable(handler):
                handler(message)
                return

    def _log_exception(self, message: str) -> None:
        handler = getattr(self._logger, "exception", None)
        if callable(handler):
            handler(message)
            return
        self._log_error(message)


def parse_hours_window(value: Any) -> tuple[int, int] | None:
    """Parse ``HH:MM-HH:MM`` (cross-midnight allowed) into minute offsets."""

    text = str(value or "").strip()
    if not text or text in {"关闭", "关", "off", "无", "-"}:
        return None
    if "-" not in text:
        return None
    start_text, _, end_text = text.partition("-")
    start = _parse_clock(start_text)
    end = _parse_clock(end_text)
    if start is None or end is None:
        return None
    return start, end


def _parse_clock(text: str) -> int | None:
    value = str(text or "").strip()
    if ":" not in value:
        return None
    hours_text, _, minutes_text = value.partition(":")
    try:
        hours = int(hours_text)
        minutes = int(minutes_text)
    except ValueError:
        return None
    if not 0 <= hours <= 23 or not 0 <= minutes <= 59:
        return None
    return hours * 60 + minutes


def valid_hours_window(value: Any) -> bool:
    return parse_hours_window(value) is not None


def in_quiet_hours(value: Any, *, now: float) -> bool:
    window = parse_hours_window(value)
    if window is None:
        return False
    start, end = window
    local = time.localtime(now)
    minute = local.tm_hour * 60 + local.tm_min
    if start == end:
        return False
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end
