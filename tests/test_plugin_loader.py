"""plugin_loader 单元测试：构造临时 plugins 目录，验证 loaded/disabled/failed 三类收集。"""
import logging
import os
import sys
import textwrap
from types import SimpleNamespace
from pathlib import Path

import pytest

# 确保项目根在 sys.path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import importlib
plugin_loader = importlib.import_module("bot.plugin_loader")


def _make_config(protocol: str = "OneBot"):
    return SimpleNamespace(protocol=protocol)


def _silent_logger():
    lg = logging.getLogger("plugin_loader_test")
    lg.addHandler(logging.NullHandler())
    lg.setLevel(logging.CRITICAL)
    return lg


@pytest.fixture
def plugins_dir(tmp_path, monkeypatch):
    """临时 plugins 目录 + 切换 cwd，让 load_plugins 扫描到它。"""
    pdir = tmp_path / "plugins"
    pdir.mkdir()
    monkeypatch.chdir(tmp_path)
    return pdir


def _write(p: Path, body: str):
    p.write_text(textwrap.dedent(body), encoding="utf-8")


def test_loaded_ok_file_plugin(plugins_dir):
    _write(plugins_dir / "hello.py", """
        TRIGGHT_KEYWORD = "你好"
        HELP_MESSAGE = "hello help"
        async def on_message():
            return True
    """)
    result = plugin_loader.load_plugins(_make_config(), _silent_logger())
    assert len(result.plugins) == 1
    assert len(result.loaded) == 1
    assert result.loaded_display == ["hello"]
    assert "hello help" in result.help_text
    assert result.failed == []


def test_disabled_prefix(plugins_dir):
    _write(plugins_dir / "d_blocked.py", "TRIGGHT_KEYWORD = 'x'\nasync def on_message(): return True\n")
    result = plugin_loader.load_plugins(_make_config(), _silent_logger())
    assert result.disabled == ["blocked"]
    assert result.loaded == []


def test_failed_missing_keyword(plugins_dir):
    _write(plugins_dir / "bad.py", "x = 1\n")
    result = plugin_loader.load_plugins(_make_config(), _silent_logger())
    assert len(result.failed) == 1
    assert "缺少 TRIGGHT_KEYWORD" in result.failed[0]
    assert result.loaded == []


def test_failed_wrong_keyword_type(plugins_dir):
    _write(plugins_dir / "wrong.py", """
        TRIGGHT_KEYWORD = 123
        async def on_message():
            return True
    """)
    result = plugin_loader.load_plugins(_make_config(), _silent_logger())
    assert any("TRIGGHT_KEYWORD 必须是字符串" in f for f in result.failed)


def test_failed_import_error(plugins_dir):
    _write(plugins_dir / "broken.py", "import this_module_does_not_exist_xyz\n")
    result = plugin_loader.load_plugins(_make_config(), _silent_logger())
    assert len(result.failed) == 1
    assert result.loaded == []


def test_folder_plugin_with_setup(plugins_dir):
    sub = plugins_dir / "myplug"
    sub.mkdir()
    _write(sub / "setup.py", """
        TRIGGHT_KEYWORD = "cmd"
        async def on_message():
            return True
    """)
    result = plugin_loader.load_plugins(_make_config(), _silent_logger())
    assert "myplug" in result.loaded_display
    assert result.disabled == []


def test_folder_plugin_missing_setup(plugins_dir):
    sub = plugins_dir / "noentry"
    sub.mkdir()
    result = plugin_loader.load_plugins(_make_config(), _silent_logger())
    assert any("缺少 setup.py" in f for f in result.failed)


def test_feishu_incompatible_filtered(plugins_dir):
    _write(plugins_dir / "CheckAccount.py", "TRIGGHT_KEYWORD = 'x'\nasync def on_message(): return True\n")
    result = plugin_loader.load_plugins(_make_config(protocol="Feishu"), _silent_logger())
    assert "CheckAccount" in result.disabled
    assert result.loaded == []


def test_pycache_skipped(plugins_dir):
    (plugins_dir / "__pycache__").mkdir()
    result = plugin_loader.load_plugins(_make_config(), _silent_logger())
    assert result.loaded == []
    assert result.disabled == []
    assert result.failed == []


def test_pyw_extension_handled(plugins_dir):
    _write(plugins_dir / "winonly.pyw", "TRIGGHT_KEYWORD = 'x'\nasync def on_message(): return True\n")
    result = plugin_loader.load_plugins(_make_config(), _silent_logger())
    assert result.loaded_display == ["winonly"]


def test_help_text_format(plugins_dir):
    _write(plugins_dir / "p1.py", """
        TRIGGHT_KEYWORD = "a"
        HELP_MESSAGE = "line1\\nline2"
        async def on_message():
            return True
    """)
    result = plugin_loader.load_plugins(_make_config(), _silent_logger())
    assert "\n       line1" in result.help_text
    assert "\n       line2" in result.help_text
