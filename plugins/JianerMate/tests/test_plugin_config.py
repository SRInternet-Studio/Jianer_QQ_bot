import json
from pathlib import Path
from types import SimpleNamespace

from plugins.JianerMate.config import RuntimeOptions
from plugins.JianerMate.plugin_config import (
    build_config_document,
    ensure_config_file,
    flatten_plugin_config,
    load_plugin_config,
    resolve_config_path,
    write_config_file,
)


_EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.json"


def _runtime(others=None):
    config = SimpleNamespace(others=dict(others or {}), black_list=[])
    return {"config": config, "reminder": "~"}


def test_shipped_example_maps_to_host_style_keys():
    document = json.loads(_EXAMPLE.read_text(encoding="utf-8"))
    flat = flatten_plugin_config(document)
    assert flat["jianer_mate_planner_model"] == "deepseek"
    assert flat["jianer_mate_replyer_model"] == "vision-primary"
    assert flat["jianer_mate_persona"]["name"] == "简儿"
    assert flat["jianer_mate_persona"]["aliases"] == ["小简", "Jianer"]
    assert flat["jianer_mate_reply_max_parts"] == 3
    assert flat["jianer_mate_reply_delay_ms"] == [600, 1800]
    assert flat["jianer_mate_talk_value_default"] == 0.5
    assert flat["jianer_mate_quiet_hours_default"] == "23:00-08:00"
    assert flat["jianer_mate_mentioned_reply"] is True
    assert flat["jianer_mate_context_messages"] == 40
    assert flat["jianer_mate_meme_max_entries"] == 128
    assert flat["jianer_mate_max_concurrent_ai"] == 2
    assert flat["jianer_mate_group_blacklist"] == []
    assert flat["jianer_mate_memory_backend"] == "memorix"
    assert flat["jianer_mate_memory_console_enabled"] is False
    assert flat["jianer_mate_memory_console_port"] == 8787
    assert flat["jianer_mate_suffix_config_path"] == "suffix_config.json"
    assert flat["jianer_mate_tts_enabled"] is False
    assert flat["jianer_mate_tool_bash_enabled"] is False
    assert flat["jianer_mate_tool_subagent_max_tasks"] == 4
    assert flat["jianer_mate_tool_mcp_enabled"] is False
    assert "jianer_mate_config_path" not in flat


def test_flat_keys_pass_through_but_config_path_is_ignored():
    flat = flatten_plugin_config(
        {
            "jianer_mate_planner_model": "x",
            "jianer_mate_config_path": "/tmp/other.json",
            "unknown": 1,
        }
    )
    assert flat == {"jianer_mate_planner_model": "x"}


def test_load_plugin_config_handles_missing_and_broken_files(tmp_path):
    missing, errors = load_plugin_config(tmp_path / "nope.json")
    assert missing == {} and errors == {}

    broken = tmp_path / "broken.json"
    broken.write_text("{ not json", encoding="utf-8")
    document, errors = load_plugin_config(broken)
    assert document == {}
    assert any("解析失败" in message for message in errors.values())

    not_object = tmp_path / "list.json"
    not_object.write_text("[]", encoding="utf-8")
    document, errors = load_plugin_config(not_object)
    assert document == {}
    assert any("必须是 JSON 对象" in message for message in errors.values())


def test_resolve_config_path_defaults_to_data_dir(tmp_path):
    default = resolve_config_path(tmp_path, {})
    assert default == tmp_path / "data" / "jianer_mate" / "config.json"
    custom = resolve_config_path(
        tmp_path, {"jianer_mate_config_path": "conf/mate.json"}
    )
    assert custom == tmp_path / "conf" / "mate.json"


def test_ensure_config_file_writes_reloadable_document(tmp_path):
    config_path = tmp_path / "mate-config.json"
    options = RuntimeOptions.from_runtime(
        _runtime(
            {
                "jianer_mate_config_path": str(config_path),
                "jianer_mate_planner_model": "planner",
                "jianer_mate_replyer_model": "vision",
                "jianer_mate_talk_value_default": 0.25,
            }
        )
    )
    assert ensure_config_file(config_path, options) is True
    assert config_path.is_file()
    document = json.loads(config_path.read_text(encoding="utf-8"))
    assert document["models"]["planner"] == "planner"
    assert document["proactive"]["talk_value"] == 0.25
    # Creating again is a no-op so user edits are never overwritten.
    assert ensure_config_file(config_path, options) is False

    reloaded = RuntimeOptions.from_runtime(
        _runtime({"jianer_mate_config_path": str(config_path)})
    )
    assert reloaded.config_loaded is True
    assert reloaded.planner_model == "planner"
    assert reloaded.replyer_model == "vision"
    assert reloaded.talk_value_default == 0.25
    assert reloaded.config_errors == ()

    # build/write round trip keeps the same document shape.
    assert write_config_file(tmp_path / "copy.json", build_config_document(options))
    copy = json.loads((tmp_path / "copy.json").read_text(encoding="utf-8"))
    assert copy["persona"]["name"] == document["persona"]["name"]


def test_plugin_config_overrides_host_others(tmp_path):
    config_path = tmp_path / "mate-config.json"
    config_path.write_text(
        json.dumps(
            {
                "models": {"planner": "file-model"},
                "proactive": {"talk_value": 0.9},
            }
        ),
        encoding="utf-8",
    )
    options = RuntimeOptions.from_runtime(
        _runtime(
            {
                "jianer_mate_config_path": str(config_path),
                "jianer_mate_planner_model": "host-model",
                "jianer_mate_talk_value_default": 0.1,
                "jianer_mate_group_blacklist": ["999"],
            }
        )
    )
    assert options.planner_model == "file-model"
    assert options.talk_value_default == 0.9
    # Keys only present in the host config keep working.
    assert options.group_blacklist == ("999",)
    assert options.config_loaded is True
    assert "jianer_mate_planner_model" in options.config_keys
    assert "jianer_mate_talk_value_default" in options.config_keys


def test_broken_plugin_config_falls_back_to_host_values(tmp_path):
    config_path = tmp_path / "mate-config.json"
    config_path.write_text("{ broken", encoding="utf-8")
    options = RuntimeOptions.from_runtime(
        _runtime(
            {
                "jianer_mate_config_path": str(config_path),
                "jianer_mate_planner_model": "host-model",
            }
        )
    )
    assert options.planner_model == "host-model"
    assert options.config_loaded is False
    assert options.config_errors
    assert "解析失败" in options.config_errors[0]
