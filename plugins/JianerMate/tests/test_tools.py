import asyncio
import json
from types import SimpleNamespace

from jianer import common, segments
from jianer.adapters import ConversationKey, ConversationKind

from plugins.JianerMate.config import DEFAULT_TOOL_ALLOWLIST, ToolOptions
from plugins.JianerMate.store import MateStore
from plugins.JianerMate.tools import (
    MateMemoryFacade,
    MateToolRuntime,
    build_tool_context,
)


class _Actions:
    protocol = "milky"
    capabilities = frozenset()
    custom = None


def _context(tmp_path, *, actor=None):
    store = MateStore(tmp_path / "mate.db")
    conversation = store.ensure_conversation(
        "milky", "10001", "group", "222", now=1
    )
    store.append_message(
        conversation.ref,
        direction="incoming",
        content="中午吃啥呢",
        platform_message_id="m0",
        sender_id="5",
        sender_name="小明",
        now=100,
    )
    store.append_message(
        conversation.ref,
        direction="outgoing",
        content="吃面吧",
        platform_message_id="m1",
        now=101,
    )
    event = SimpleNamespace(
        protocol="milky",
        self_id="10001",
        user_id="5",
        group_id="222",
        message=common.Message(segments.Text("现在几点了")),
        message_id="m2",
        sender=SimpleNamespace(card=None, nickname="小明", role="admin"),
    )
    context = build_tool_context(
        event=event,
        actions=_Actions(),
        conversation=ConversationKey(
            protocol="milky",
            self_id="10001",
            kind=ConversationKind.GROUP,
            conversation_id="222",
            preset="mate",
        ),
        canonical_user_id="qq:5",
        runtime={"root_users": ["1"]},
        memory=MateMemoryFacade(store, conversation.ref),
        actor=actor or SimpleNamespace(is_privileged=False),
        model="test",
    )
    return store, context


def test_default_tool_allowlist_matches_jianer_ai_tools(tmp_path):
    runtime = MateToolRuntime(ToolOptions(), project_root=tmp_path)
    status = runtime.status()
    assert status["available"] is True, status.get("error")
    names = set(status["names"])
    assert names <= set(DEFAULT_TOOL_ALLOWLIST)
    for name in (
        "get_current_time",
        "calculate_expression",
        "read_recent_chat",
        "search_current_chat",
        "web_search",
        "github_repository",
        "render_information_card",
    ):
        assert name in names
    # Skill tools only appear when a skills directory actually exists.
    assert "list_agent_skills" not in names
    assert status["tools"] == len(names)
    asyncio.run(runtime.shutdown())


def test_schemas_follow_allowlist_and_empty_list_disables_tools(tmp_path):
    store, context = _context(tmp_path)
    single = MateToolRuntime(
        ToolOptions(allowlist=("get_current_time",)), project_root=tmp_path
    )
    schemas = single.schemas(context)
    assert [item["function"]["name"] for item in schemas] == ["get_current_time"]
    assert schemas[0]["type"] == "function"
    assert schemas[0]["function"]["parameters"]["type"] == "object"
    blocked = asyncio.run(
        single.execute("call-1", "calculate_expression", {"expression": "1+1"}, context)
    )
    assert "不可用" in blocked

    disabled = MateToolRuntime(ToolOptions(allowlist=()), project_root=tmp_path)
    assert disabled.schemas(context) == ()
    asyncio.run(single.shutdown())
    asyncio.run(disabled.shutdown())
    store.close()


def test_original_tools_execute_against_mate_context(tmp_path):
    store, context = _context(tmp_path)
    runtime = MateToolRuntime(
        ToolOptions(
            allowlist=(
                "get_current_time",
                "calculate_expression",
                "read_recent_chat",
                "list_my_memories",
            )
        ),
        project_root=tmp_path,
    )

    clock = asyncio.run(
        runtime.execute("call-1", "get_current_time", {}, context)
    )
    assert clock and "error" not in clock.lower()

    maths = asyncio.run(
        runtime.execute(
            "call-2", "calculate_expression", {"expression": "1+2*3"}, context
        )
    )
    assert "7" in maths

    history = asyncio.run(
        runtime.execute("call-3", "read_recent_chat", {"limit": 5}, context)
    )
    payload = json.loads(history)["data"]
    assert payload["count"] == 2
    texts = [item["text"] for item in payload["messages"]]
    assert "中午吃啥呢" in texts and "吃面吧" in texts
    assert payload["messages"][0]["platform_message_id"] == "m1"

    memory = asyncio.run(
        runtime.execute("call-4", "list_my_memories", {"scope": "person"}, context)
    )
    assert "长期记忆" in memory
    asyncio.run(runtime.shutdown())
    store.close()


def test_agent_skills_are_registered_when_present(tmp_path):
    skill_dir = tmp_path / ".agents" / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: 演示技能\n---\n\n按步骤做事。\n",
        encoding="utf-8",
    )
    store, context = _context(tmp_path)
    runtime = MateToolRuntime(ToolOptions(allowlist=None), project_root=tmp_path)
    status = runtime.status()
    assert "list_agent_skills" in status["names"]
    listing = asyncio.run(
        runtime.execute("call-1", "list_agent_skills", {}, context)
    )
    assert "demo" in listing
    asyncio.run(runtime.shutdown())
    store.close()


def test_readonly_platform_tools_need_privilege_but_no_mutating_flag(tmp_path):
    store, anonymous = _context(tmp_path)
    _store, privileged = _context(
        tmp_path / "priv", actor=SimpleNamespace(is_privileged=True)
    )
    runtime = MateToolRuntime(ToolOptions(allowlist=None), project_root=tmp_path)
    plain_names = {
        item["function"]["name"] for item in runtime.schemas(anonymous)
    }
    privileged_names = {
        item["function"]["name"] for item in runtime.schemas(privileged)
    }
    assert not any(name.startswith("milky_get_") for name in plain_names)
    assert any(name.startswith("milky_get_") for name in privileged_names)
    assert "send_message" not in privileged_names
    asyncio.run(runtime.shutdown())
    store.close()
