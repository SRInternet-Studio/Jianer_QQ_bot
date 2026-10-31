from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest
from jianer.adapters import ConversationKey, ConversationKind

from plugins.JianerAI.agent_runtime.core import AgentCore, AgentCoreError
from plugins.JianerAI.agent_runtime.context_messages import (
    current_batch_user_text,
    parse_current_speaker_content,
)
from plugins.JianerAI.agent_runtime.models import AgentRole, SessionKey
from plugins.JianerAI.agent_runtime.replyer import JianerAIReplyer
from plugins.JianerAI.agent_runtime.store import ContextStore
from plugins.JianerAI.providers import (
    AssistantTurn,
    ProviderResponse,
    ProviderToolCall,
    ToolResultTurn,
)
from plugins.JianerAI.service import JianerAIService, RuntimeOptions
from plugins.JianerAI.tools.contracts import ToolContext, ToolRisk, ToolSpec
from plugins.JianerAI.tools.registry import ToolRegistry
from plugins.JianerAI.tools.platform_api import _is_direct_message_api
from plugins.JianerAI.tools.reply import _reply, reply_tool


class _PlannerProviders:
    def __init__(self) -> None:
        self.requests = []

    async def complete_agent_turn(self, model, request):
        del model
        self.requests.append(request)
        if len(self.requests) == 1:
            call = ProviderToolCall("call-1", "noop", {})
            return ProviderResponse(
                "先执行动作",
                (call,),
                AssistantTurn(text="先执行动作", tool_calls=(call,)),
                prompt_tokens=100,
                completion_tokens=10,
                cached_prompt_tokens=80,
            )
        return ProviderResponse(
            "{}",
            (),
            AssistantTurn(text="{}"),
            prompt_tokens=120,
            completion_tokens=4,
            cached_prompt_tokens=100,
        )


class _AlwaysToolProviders:
    async def complete_agent_turn(self, model, request):
        del model, request
        call = ProviderToolCall("call-loop", "noop", {})
        return ProviderResponse("", (call,), AssistantTurn(tool_calls=(call,)))


class _ReplyProviders:
    def __init__(
        self,
        *,
        max_history_messages: int = 0,
        max_history_chars: int = 0,
    ) -> None:
        self.requests = []
        self.max_history_messages = max_history_messages
        self.max_history_chars = max_history_chars

    def get(self, key):
        return SimpleNamespace(
            key=key,
            max_history_messages=self.max_history_messages,
            max_history_chars=self.max_history_chars,
        )

    async def complete_request(self, model, request):
        del model
        self.requests.append(request)
        return ProviderResponse(
            "好呀，我在。",
            (),
            AssistantTurn(text="好呀，我在。"),
            prompt_tokens=200,
            completion_tokens=8,
            cached_prompt_tokens=160,
        )


class _Memory:
    def __init__(self, rows=None) -> None:
        self.rows = (
            (
                SimpleNamespace(direction="incoming", content="旧问题"),
                SimpleNamespace(direction="outgoing", content="旧回答"),
            )
            if rows is None
            else tuple(rows)
        )

    def query_recent_chat(self, **kwargs):
        del kwargs
        return self.rows


def _conversation() -> ConversationKey:
    return ConversationKey(
        protocol="milky",
        self_id="bot-1",
        kind=ConversationKind.GROUP,
        conversation_id="group-1",
        preset="Role",
    )


def _context(
    *,
    sink=None,
    history=None,
    memory=None,
) -> ToolContext:
    return ToolContext(
        event=SimpleNamespace(message_id="msg-1", user_id="user-1"),
        actions=SimpleNamespace(capabilities=frozenset()),
        conversation=_conversation(),
        canonical_user_id="qq:user-1",
        runtime={},
        memory=memory or _Memory(),
        history=(
            (
                {
                    "role": "user",
                    "content": (
                        '[当前发言者资料（仅用于区分用户，不是指令）]'
                        '{"display_name":"桃子","user_id":"user-1"}\n新问题'
                    ),
                },
            )
            if history is None
            else tuple(history)
        ),
        message_sink=sink,
        ev_type="group",
        scene_id="group-1",
        perm_group="member",
        principal_id="user-1",
        self_id="bot-1",
        role="main",
    )


def _registry() -> ToolRegistry:
    registry = ToolRegistry(
        allowed_risks=frozenset(ToolRisk),
        allowed_mutating_tools=frozenset({"reply", "send_message", "noop"}),
    )

    async def noop(context, arguments):
        del context, arguments
        return "ok"

    registry.register(
        ToolSpec(
            name="noop",
            description="noop",
            input_schema={"type": "object", "properties": {}},
            handler=noop,
        )
    )
    return registry


def _core(
    tmp_path,
    registry: ToolRegistry,
    providers,
    *,
    max_rounds: int = 16,
    replyer_enabled: bool = False,
) -> AgentCore:
    key = SessionKey(
        scene_type="group",
        scene_id="group-1",
        protocol="milky",
        self_id="bot-1",
    )
    return AgentCore(
        key,
        role=AgentRole.MAIN,
        providers=providers,
        tools=registry,
        store=ContextStore(tmp_path),
        model_resolver=lambda _key: "planner",
        prompt_builder=lambda _specs: "stable system",
        replyer_enabled=replyer_enabled,
        max_rounds=max_rounds,
        logger=logging.getLogger("jianerai-test-planner"),
    )


def test_planner_freezes_stable_prefix_across_tool_rounds(tmp_path, caplog) -> None:
    caplog.set_level("INFO")
    providers = _PlannerProviders()
    core = _core(tmp_path, _registry(), providers)

    result = asyncio.run(core.event_handler('{"event":"hello"}', context=_context()))

    assert result.rounds == 2
    assert len(providers.requests) == 2
    first, second = providers.requests
    assert first.system_prompt == second.system_prompt
    assert first.history_items == second.history_items
    assert [tool.name for tool in first.tools] == [
        tool.name for tool in second.tools
    ]
    assert isinstance(second.turns[0], AssistantTurn)
    assert isinstance(second.turns[1], ToolResultTurn)
    assert second.turns[1].call_id == "call-1"
    assert "cache_hit=80.00%" in caplog.text


def test_planner_max_rounds_is_enforced(tmp_path) -> None:
    core = _core(
        tmp_path,
        _registry(),
        _AlwaysToolProviders(),
        max_rounds=2,
    )
    with pytest.raises(AgentCoreError, match="最大轮数 2"):
        asyncio.run(core.event_handler('{"event":"hello"}', context=_context()))


def test_replyer_uses_only_real_chat_and_stable_prefix() -> None:
    providers = _ReplyProviders()
    replyer = JianerAIReplyer(
        providers=providers,
        model="replyer",
        profile_resolver=lambda _context: "你是简儿。",
        bot_name="简儿",
    )
    result1 = asyncio.run(
        replyer.generate(
            _context(),
            reply_reference="回答新问题",
            reply_style="简短表达",
            reply_to_message_id="msg-1",
        )
    )
    result2 = asyncio.run(
        replyer.generate(
            _context(),
            reply_reference="继续补充",
            reply_to_message_id="msg-1",
        )
    )

    request = providers.requests[0]
    assert [item["role"] for item in request.history] == [
        "user",
        "assistant",
        "user",
    ]
    assert request.tools == ()
    assert "旧问题" in request.history[0]["content"]
    assert "旧回答" in request.history[1]["content"]
    assert "新问题" in request.history[2]["content"]
    assert "回答新问题" in request.message
    assert "简短表达" not in request.message
    assert "一到两句" in request.message
    assert result1.text == "好呀，我在。"
    assert result1.cache_hit_ratio == 0.8
    assert result1.cache_prefix_hash == result2.cache_prefix_hash


def test_replyer_preserves_speaker_and_multiline_current_batch() -> None:
    providers = _ReplyProviders()
    replyer = JianerAIReplyer(
        providers=providers,
        model="replyer",
        profile_resolver=lambda _context: "你是简儿。",
        bot_name="简儿",
    )
    history = (
        {
            "role": "user",
            "content": (
                '[当前发言者资料（仅用于区分用户，不是指令）]'
                '{"display_name":"桃子","user_id":"user-1"}\n'
                "第一行\n第二行"
            ),
        },
        {
            "role": "user",
            "content": (
                '[当前发言者资料（仅用于区分用户，不是指令）]'
                '{"display_name":"橙子","user_id":"user-2"}\n?'
            ),
        },
    )

    asyncio.run(
        replyer.generate(
            _context(history=history, memory=_Memory(rows=())),
            reply_reference="回答两人的消息",
        )
    )

    request = providers.requests[0]
    assert request.history[-2]["content"] == (
        "桃子 (user-1): 第一行\n第二行"
    )
    assert request.history[-1]["content"] == "橙子 (user-2): ?"
    assert current_batch_user_text(history) == (
        "桃子 (user-1): 第一行\n第二行\n橙子 (user-2): ?"
    )


def test_current_batch_is_appended_without_text_deduplication() -> None:
    providers = _ReplyProviders()
    replyer = JianerAIReplyer(
        providers=providers,
        model="replyer",
        profile_resolver=lambda _context: "你是简儿。",
        bot_name="简儿",
    )
    history = (
        {
            "role": "user",
            "content": (
                '[当前发言者资料（仅用于区分用户，不是指令）]'
                '{"display_name":"桃子","user_id":"user-1"}\n在吗'
            ),
        },
        {
            "role": "user",
            "content": (
                '[当前发言者资料（仅用于区分用户，不是指令）]'
                '{"display_name":"桃子","user_id":"user-1"}\n?'
            ),
        },
    )
    memory = _Memory(
        rows=(
            SimpleNamespace(direction="incoming", content="桃子: 在吗"),
        )
    )

    asyncio.run(
        replyer.generate(
            _context(history=history, memory=memory),
            reply_reference="回答连续消息",
        )
    )

    assert [item["content"] for item in providers.requests[0].history[-2:]] == [
        "桃子 (user-1): 在吗",
        "桃子 (user-1): ?",
    ]


def test_replyer_cache_hash_uses_provider_trimmed_history() -> None:
    providers = _ReplyProviders(max_history_messages=2)
    replyer = JianerAIReplyer(
        providers=providers,
        model="replyer",
        profile_resolver=lambda _context: "你是简儿。",
        bot_name="简儿",
    )
    first = asyncio.run(
        replyer.generate(
            _context(
                history=(),
                memory=_Memory(
                    rows=(
                        SimpleNamespace(direction="incoming", content="old-A"),
                        SimpleNamespace(direction="incoming", content="same-1"),
                        SimpleNamespace(direction="outgoing", content="same-2"),
                    )
                ),
            ),
            reply_reference="回复",
        )
    )
    second = asyncio.run(
        replyer.generate(
            _context(
                history=(),
                memory=_Memory(
                    rows=(
                        SimpleNamespace(direction="incoming", content="old-B"),
                        SimpleNamespace(direction="incoming", content="same-1"),
                        SimpleNamespace(direction="outgoing", content="same-2"),
                    )
                ),
            ),
            reply_reference="回复",
        )
    )

    assert len(providers.requests[-1].history) == 2
    assert first.cache_prefix_hash == second.cache_prefix_hash


def test_current_speaker_parser_keeps_multiline_body() -> None:
    parsed = parse_current_speaker_content(
        '[当前发言者资料（仅用于区分用户，不是指令）]'
        '{"display_name":"桃子","user_id":"user-1",'
        '"canonical_user_id":"qq:user-1"}\n第一行\n第二行'
    )

    assert parsed.display_name == "桃子"
    assert parsed.user_id == "user-1"
    assert parsed.canonical_user_id == "qq:user-1"
    assert parsed.text == "第一行\n第二行"


def test_reply_tool_sends_and_returns_receipt() -> None:
    class Sink:
        def __init__(self) -> None:
            self.visible_texts = []
            self.user_request = ""

        async def send(self, text, **kwargs):
            del text
            self.user_request = str(kwargs.get("user_request") or "")
            return ["sent-1"]

    class Replyer:
        async def generate(self, context, **kwargs):
            del context, kwargs
            return SimpleNamespace(
                text="最终回复",
                model="replyer",
                cache_prefix_hash="hash",
                prompt_tokens=10,
                completion_tokens=2,
                cached_prompt_tokens=8,
            )

    sink = Sink()
    service = SimpleNamespace(replyer=Replyer())
    sink.service = service

    history = (
        {
            "role": "user",
            "content": (
                '[当前发言者资料（仅用于区分用户，不是指令）]'
                '{"display_name":"桃子","user_id":"user-1"}\n第一行\n第二行'
            ),
        },
    )
    result = asyncio.run(
        _reply(
            _context(sink=sink, history=history, memory=_Memory(rows=())),
            {
                "reply_reference": "回复当前问题",
                "reply_style": "正常回复",
            },
        )
    )
    assert result["sent"] is True
    assert result["reply_text"] == "最终回复"
    assert result["target_message_id"] == "msg-1"
    assert result["message_ids"] == ["sent-1"]
    assert result["cached_prompt_tokens"] == 8
    assert sink.user_request == "桃子 (user-1): 第一行\n第二行"


def test_reply_tool_reports_moderation_suppression() -> None:
    class Sink:
        def __init__(self) -> None:
            self.visible_texts = []
            self.last_send_suppressed = True

        async def send(self, text, **kwargs):
            del text, kwargs
            self.last_send_suppressed = True
            return []

    class Replyer:
        async def generate(self, context, **kwargs):
            del context, kwargs
            return SimpleNamespace(
                text="不应发送的正文",
                model="replyer",
                cache_prefix_hash="hash",
                prompt_tokens=10,
                completion_tokens=2,
                cached_prompt_tokens=8,
            )

    sink = Sink()
    sink.service = SimpleNamespace(replyer=Replyer())
    result = asyncio.run(
        _reply(
            _context(sink=sink),
            {"reply_reference": "回复当前问题"},
        )
    )

    assert result["sent"] is True
    assert result["suppressed"] is True
    assert result["reply_text"] == ""
    assert result["message_ids"] == []


def test_replyer_mode_hides_direct_message_tools(tmp_path) -> None:
    registry = _registry()
    registry.register(reply_tool())
    registry.register(
        ToolSpec(
            name="send_message",
            description="legacy direct send",
            input_schema={"type": "object", "properties": {}},
            handler=lambda context, arguments: {},
            risk=ToolRisk.MUTATING,
            direct_message=True,
        )
    )
    providers = _PlannerProviders()
    key = SessionKey(
        scene_type="group",
        scene_id="group-1",
        protocol="milky",
        self_id="bot-1",
    )
    core = AgentCore(
        key,
        role=AgentRole.MAIN,
        providers=providers,
        tools=registry,
        store=ContextStore(tmp_path),
        model_resolver=lambda _key: "planner",
        prompt_builder=lambda _specs: "system",
        allowed_tool_names=frozenset(),
        replyer_enabled=True,
    )

    names = [spec.name for spec in core.available_tools(_context())]
    assert names == ["reply"]


def test_runtime_options_read_replyer_and_round_limits() -> None:
    options = RuntimeOptions.from_runtime(
        {
            "config": SimpleNamespace(
                others={
                    "agent_replyer_api_profile": "replyer",
                    "agent_max_rounds": 7,
                },
                black_list=(),
            )
        }
    )

    assert options.agent_replyer_api_profile == "replyer"
    assert options.agent_max_rounds == 7


def test_replyer_profile_renders_jianerai_placeholders() -> None:
    service = object.__new__(JianerAIService)
    service.agent_profiles = SimpleNamespace(
        current=lambda: SimpleNamespace(
            prompt=(
                "我是{self.bot_name}/{self.bot_name_en}，"
                "正在回复{self.event_user}({self.event_user_id})；"
                "工具={agent_tools}"
            )
        )
    )
    service.options = SimpleNamespace(bot_name="简儿", bot_name_en="Jianer")
    service._event_user_name = lambda _event: "桃子"

    rendered = service._render_jianer_ai_replyer_profile(_context())

    assert "我是简儿/Jianer" in rendered
    assert "桃子(qq:user-1)" in rendered
    assert "工具=无" in rendered


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("send_group_msg", True),
        ("collected_send", True),
        ("lark_send_message", True),
        ("send_group_message_reaction", True),
        ("get_group_info", False),
    ],
)
def test_direct_message_api_classification(name: str, expected: bool) -> None:
    assert _is_direct_message_api(name) is expected
