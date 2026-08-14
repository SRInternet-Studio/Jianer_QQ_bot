import asyncio
from pathlib import Path
from types import SimpleNamespace

from bot import group_commands


class FakeSegments:
    class Image:
        def __init__(self, path):
            self.path = path


class FakeManager:
    @staticmethod
    def Message(*segments):
        return tuple(segments)


class FakeActions:
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

    async def send(self, message, **target):
        self.sent.append((target, message))


def test_about_renders_installed_jianercore_version(monkeypatch, tmp_path):
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

    requested_distributions = []

    def fake_version(distribution_name):
        requested_distributions.append(distribution_name)
        return "9.8.7"

    monkeypatch.setattr(group_commands.metadata, "version", fake_version)

    rendered = {}
    image_path = tmp_path / "about_image_0.png"

    async def fake_capture_screenshot(url, output_path_base, extension):
        temp_files = list(static_dir.glob("about_temp_*.html"))
        assert len(temp_files) == 1
        rendered["html"] = temp_files[0].read_text(encoding="utf-8")
        rendered["url"] = url
        rendered["output_path_base"] = output_path_base
        rendered["extension"] = extension
        image_path.write_bytes(b"png")
        return str(image_path)

    monkeypatch.setattr(
        group_commands,
        "capture_screenshot",
        fake_capture_screenshot,
    )

    actions = FakeActions()
    event = SimpleNamespace(group_id=100)
    asyncio.run(
        group_commands.cmd_about(
            actions,
            FakeManager,
            FakeSegments,
            event,
            "简儿",
            "Jianer",
            "测试口号",
            "测试构建",
        )
    )

    assert requested_distributions == ["jianer-bot"]
    assert (
        "简儿|Jianer|测试口号|测试构建|Milky|1.0|2.0|JianerCore 9.8.7"
        in rendered["html"]
    )
    assert "{{" not in rendered["html"]
    assert rendered["url"].startswith("file:///")
    assert rendered["output_path_base"] == "about_image"
    assert rendered["extension"] == "png"
    assert actions.sent[0][0] == {"group_id": 100}
    assert actions.sent[0][1][0].path == str(image_path)
    assert list(static_dir.glob("about_temp_*.html")) == []
    assert not image_path.exists()


def test_jianercore_version_falls_back_when_metadata_is_missing(monkeypatch):
    def missing_distribution(_distribution_name):
        raise group_commands.metadata.PackageNotFoundError

    monkeypatch.setattr(group_commands.metadata, "version", missing_distribution)

    assert group_commands._jianercore_version() == "Unknown"


def test_about_template_names_jianercore():
    template_path = (
        Path(group_commands.__file__).resolve().parents[1]
        / "static"
        / "about_template.html"
    )
    template = template_path.read_text(encoding="utf-8")

    assert "基于 JianerCore 版本 {{jianercore_version}} 开发" in template
    assert "HypeR Bot" not in template
