import asyncio
from pathlib import Path
from types import SimpleNamespace


class FakeActions:
    protocol = "onebot"
    capabilities = frozenset()

    def __init__(self):
        self.sent = []

    async def get_version_info(self):
        return SimpleNamespace(
            data=SimpleNamespace(
                raw={
                    "app_name": "Milky",
                    "protocol_version": "1.0",
                    "app_version": "2.0",
                }
            )
        )

    async def send(self, message, group_id=None, user_id=None):
        self.sent.append((message, group_id, user_id))
        return SimpleNamespace(data={"message_id": "1"})


def test_about_template_names_jianercore():
    template_path = Path(__file__).resolve().parents[1] / "static" / "about_template.html"
    template = template_path.read_text(encoding="utf-8")

    assert "基于 JianerCore 版本 {{jianercore_version}} 开发" in template
    assert "HypeR Bot" not in template


def test_help_info_about_renders_and_cleans_temp_files(
    monkeypatch,
    tmp_path,
):
    from tests.test_native_plugins import (
        _event,
        _manager,
        _run,
    )

    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "about_template.html").write_text(
        "|".join(
            (
                "{{bot_name}}",
                "{{bot_name_en}}",
                "{{ONE_SLOGAN}}",
                "{{version_name}}",
                "{{app_name}}",
                "{{protocol_version}}",
                "{{app_version}}",
                "JianerCore {{jianercore_version}}",
                "{{year}}",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    rendered = {}

    async def fake_capture(url, output_path_base, extension):
        temp_files = list(static_dir.glob("about_temp_*.html"))
        assert len(temp_files) == 1
        rendered["html"] = temp_files[0].read_text(encoding="utf-8")
        image_path = tmp_path / "about_image_0.png"
        image_path.write_bytes(b"png")
        return str(image_path)

    monkeypatch.setattr(
        "Tools.capture_screenshot.capture_screenshot",
        fake_capture,
    )
    monkeypatch.setattr(
        "importlib.metadata.version",
        lambda name: "9.8.7",
    )
    manager, client, actions = _manager("HelpInfo", tmp_path)
    assert _run(manager, _event("~关于"), actions) is True
    assert "简儿|Jianer|测试|JianerQQ机器人 版本 NEXT5|TestBot|v11|1.0|JianerCore 9.8.7" in rendered["html"]
    assert "{{" not in rendered["html"]
    assert actions.sent
    assert list(static_dir.glob("about_temp_*.html")) == []
    assert not (tmp_path / "about_image_0.png").exists()
    asyncio.run(manager.shutdown())
    asyncio.run(client.aclose())
