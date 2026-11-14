from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAIN_SOURCE = (ROOT / "main.py").read_text(encoding="utf-8")


def test_host_keeps_restart_sleep_and_core_dispatch_only():
    assert "dispatch_core_commands" in MAIN_SOURCE
    assert "重启" in MAIN_SOURCE
    assert "休眠" in MAIN_SOURCE

    for removed in (
        "_bot_plugin_ops",
        "_bot_admin_commands",
        "_bot_group_commands",
        "_bot_broadcast",
        "_bot_event_handlers",
        "_bot_help_view",
    ):
        assert removed not in MAIN_SOURCE

    for core_command in (
        "~重载插件",
        "~禁用插件",
        "~启用插件",
        "~插件视角",
    ):
        assert core_command not in MAIN_SOURCE


def test_native_plugins_do_not_reimplement_core_management_commands():
    native_plugins = (
        ROOT / "plugins" / "InteractionEvents" / "setup.py",
        ROOT / "plugins" / "HelpInfo" / "setup.py",
        ROOT / "plugins" / "Admin" / "setup.py",
        ROOT / "plugins" / "Broadcast" / "setup.py",
        ROOT / "plugins" / "FeishuBinding" / "setup.py",
    )
    source = "\n".join(path.read_text(encoding="utf-8") for path in native_plugins)
    for core_command in (
        "重载插件",
        "禁用插件",
        "启用插件",
        "插件视角",
    ):
        assert core_command not in source


def test_native_plugins_use_shared_unimessage_support():
    source = (ROOT / "bot" / "plugin_support.py").read_text(encoding="utf-8")
    assert "UniMessage" in source
    assert "actions.send(" not in source
