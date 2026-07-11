import asyncio
import logging
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import plugin_state


def _write(path: Path, body: str):
    path.write_text(textwrap.dedent(body), encoding="utf-8")


def _configure(reminder: str = "~"):
    plugin_state.configure(
        config=SimpleNamespace(),
        logger=logging.getLogger("plugin_state_test"),
        reminder=reminder,
        bot_name="Jianer",
        bot_name_en="Jianer",
        one_slogan="test",
        confused_word="{bot_name} cannot do that",
        root_users=["1"],
        cooldowns={},
        cooldowns1={},
    )


def test_reload_plugins_uses_jianercore_plugin_manager(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    _write(
        plugins_dir / "hello.py",
        """
        from jianer.plugins import PluginMetadata

        __plugin_meta__ = PluginMetadata(
            name="jianerbot-plugin-hello",
            usage="{reminder}hello —> reply hello",
        )

        async def on_message(event, actions):
            return False
        """,
    )

    monkeypatch.chdir(tmp_path)
    _configure()

    result = plugin_state.reload_plugins()

    assert result.failed == []
    assert "jianerbot-plugin-hello" in result.loaded
    assert plugin_state.loaded_plugins() == result.loaded
    assert "~hello —> reply hello" in plugin_state.plugin_help_text()


def test_dispatch_plugins_uses_alconna_and_normalized_message_text(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    _write(
        plugins_dir / "command.py",
        """
        from jianer.plugins import PluginMetadata
        from jianer.plugins.builtin.alconna import Command

        __plugin_meta__ = PluginMetadata(
            name="jianerbot-plugin-command",
            requires={"jianerbot-plugin-alconna"},
        )

        @Command("~ping").handle()
        async def _(event, actions):
            actions.handled = event.msg_str
            actions.calls += 1
        """,
    )

    monkeypatch.chdir(tmp_path)
    _configure()
    plugin_state.reload_plugins()
    plugin_state.reload_plugins()
    actions = SimpleNamespace(handled=False, calls=0)

    handled = asyncio.run(
        plugin_state.dispatch_plugins(
            SimpleNamespace(msg_str="@bot ~ping"),
            actions,
            message_text="~ping",
        )
    )

    assert handled is True
    assert actions.handled == "~ping"
    assert actions.calls == 1


def test_disabled_plugins_and_metadata_lookup(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    _write(
        plugins_dir / "d_blocked.py",
        """
        from jianer.plugins import PluginMetadata

        __plugin_meta__ = PluginMetadata(name="jianerbot-plugin-blocked")

        async def on_message(event, actions):
            return True
        """,
    )

    monkeypatch.chdir(tmp_path)
    _configure()
    result = plugin_state.reload_plugins()

    assert result.failed == []
    assert "blocked" in plugin_state.disabled_plugins()
    assert plugin_state.find_plugin_path("jianerbot-plugin-blocked", enable=True).name == "d_blocked.py"
