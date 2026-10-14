import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.MaimaiDX import resource_manager


def test_safe_member_name_rejects_parent_and_absolute_paths():
    with pytest.raises(resource_manager.ResourceInstallError):
        resource_manager._safe_member_name("../secret")
    with pytest.raises(resource_manager.ResourceInstallError):
        resource_manager._safe_member_name("/absolute")
    assert resource_manager._safe_member_name("static/mai/pic/1.png") == (
        "static/mai/pic/1.png"
    )


def test_auto_install_downloads_extracts_and_installs_chromium(
    monkeypatch,
    tmp_path: Path,
):
    target = tmp_path / "static"
    downloads = []
    chromium = []

    monkeypatch.setattr(
        resource_manager,
        "maiconfig",
        SimpleNamespace(
            auto_download=True,
            auto_install_chromium=True,
            resource_url="https://example.invalid/base.7z",
            resource_sha256=None,
            resource_update_url="https://example.invalid/update.7z",
            resource_update_sha256=None,
            resource_archive=None,
        ),
    )
    monkeypatch.setattr(resource_manager, "static", target)
    monkeypatch.setattr(resource_manager, "ensure_runtime_dirs", lambda: None)
    monkeypatch.setattr(resource_manager, "resources_ready", lambda: False)
    monkeypatch.setattr(resource_manager, "resource_issues", lambda: ())

    async def fake_download(archive, destination, **kwargs):
        downloads.append(archive.url)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"archive")
        return destination

    def fake_extract(archive, destination):
        destination.mkdir(parents=True)
        static = destination / "static"
        static.mkdir()
        (static / "marker").write_text("ok", encoding="utf-8")
        return static

    def fake_chromium():
        chromium.append(True)

    assert asyncio.run(
        resource_manager.ensure_static_resources(
            download=fake_download,
            extract=fake_extract,
            chromium_install=fake_chromium,
        )
    )
    assert downloads == [
        "https://example.invalid/base.7z",
        "https://example.invalid/update.7z",
    ]
    assert chromium == [True]
    assert (target / "marker").read_text(encoding="utf-8") == "ok"


def test_auto_download_can_be_disabled(monkeypatch):
    monkeypatch.setattr(
        resource_manager,
        "maiconfig",
        SimpleNamespace(auto_download=False),
    )
    monkeypatch.setattr(resource_manager, "ensure_runtime_dirs", lambda: None)
    monkeypatch.setattr(resource_manager, "resources_ready", lambda: False)
    assert asyncio.run(resource_manager.ensure_static_resources()) is False

