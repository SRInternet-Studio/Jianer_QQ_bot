from jianer.plugins import PluginManager


def test_plugin_loads_through_plugin_manager():
    manager = PluginManager()
    plugin = manager.load_plugin("plugins/JianerMate")
    assert plugin is not None
    assert plugin.metadata.name == "jianerbot-plugin-jianer-mate"
    assert plugin.metadata.requires == frozenset({"jianerbot-plugin-alconna"})
    module = plugin.module
    for name in (
        "setup",
        "shutdown",
        "on_message_observe",
        "on_message_fallback",
        "get_service",
    ):
        assert callable(getattr(module, name, None)), name
    assert "主动发言" in str(plugin.metadata.usage)
