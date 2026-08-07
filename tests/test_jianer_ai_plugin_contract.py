import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_fresh_manager_loads_agent_command_extension_api_and_shutdown(tmp_path):
    script = r'''
import asyncio
import logging
import os
from types import SimpleNamespace

from bot import plugin_state
from plugins.JianerAI.tools import ToolSpec


class FakeActions:
    protocol = "onebot"
    capabilities = frozenset()

    def __init__(self):
        self.sent = []

    async def send(self, message, **target):
        self.sent.append((target, message))
        return SimpleNamespace(data=SimpleNamespace(message_id="sent-1"))


def event(text):
    return SimpleNamespace(
        protocol="onebot",
        self_id="bot-1",
        user_id="user-1",
        group_id="group-1",
        conversation_id="group-1",
        message_id="message-1",
        msg_str=text,
        message=[],
        sender={"nickname": "tester"},
        time=1_900_000_000,
    )


plugin_state.PLUGIN_FOLDER = os.path.join(os.getcwd(), "plugins")
plugin_state.configure(
    config=SimpleNamespace(
        others={
            "jianer_ai_db_path": os.environ["JIANER_AGENT_TEST_DB"],
            "agent_enabled_default": True,
            "default_mode": "example",
        },
        black_list=[],
    ),
    logger=logging.getLogger("jianer_ai_plugin_contract"),
    reminder="~",
    bot_name="Jianer",
    bot_name_en="Jianer",
    one_slogan="test",
    confused_word="cannot do that",
    root_users=["1"],
    cooldowns={},
    cooldowns1={},
)
plugin_state.set_auth_snapshot(
    admins=["1"],
    supers=["1"],
    root_users=["1"],
    super_users=["1"],
    manage_users=[],
)

result = plugin_state.reload_plugins()
assert result.failed == []
assert "jianerbot-plugin-jianer-ai" in result.loaded
assert "jianerbot-plugin-alconna" in result.dependency_order
module = plugin_state.get_plugin_module("jianerbot-plugin-jianer-ai")
service = module.get_service()
assert service is not None

registration = module.register_tool(
    ToolSpec(
        name="plugin_contract_probe",
        description="test-only read-only tool",
        input_schema={
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        handler=lambda *_: {"ok": True},
    )
)


async def scenario():
    actions = FakeActions()
    handled = await plugin_state.dispatch_plugins(
        event("~Agent 工具"),
        actions,
        message_text="~Agent 工具",
    )
    assert handled is True
    assert "plugin_contract_probe" in str(actions.sent[-1][1])

    untouched = FakeActions()
    assert await plugin_state.dispatch_plugins(
        event("~definitely-not-an-agent-command"),
        untouched,
        message_text="~definitely-not-an-agent-command",
    ) is False
    assert untouched.sent == []

    assert module.unregister_tool(registration) is True
    report = await plugin_state.shutdown_plugins()
    assert report.completed
    assert service._closed is True
    assert service.tools.available(
        SimpleNamespace(
            conversation=SimpleNamespace(protocol="onebot"),
            actions=SimpleNamespace(capabilities=frozenset()),
        )
    ) == ()


asyncio.run(scenario())
'''
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env["PYTHONUTF8"] = "1"
    env["JIANER_AGENT_TEST_DB"] = str(tmp_path / "agent-plugin.db")
    for key in (
        "QWEATHER_API_HOST",
        "QWEATHER_PROJECT_ID",
        "QWEATHER_CREDENTIAL_ID",
        "QWEATHER_PRIVATE_KEY_PATH",
    ):
        env[key] = ""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
