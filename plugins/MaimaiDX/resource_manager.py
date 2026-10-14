"""Automatic preparation of MaimaiDX static assets and Chromium."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

from .config import log, maiconfig
from .resources import ensure_runtime_dirs, resource_issues, resources_ready, static


class ResourceInstallError(RuntimeError):
    """Static resource preparation failed in a recoverable way."""


@dataclass(frozen=True)
class ResourceArchive:
    url: str
    sha256: str | None = None


def _archive_list() -> tuple[ResourceArchive, ...]:
    archives = [
        ResourceArchive(maiconfig.resource_url, maiconfig.resource_sha256),
    ]
    if maiconfig.resource_update_url:
        archives.append(
            ResourceArchive(
                maiconfig.resource_update_url,
                maiconfig.resource_update_sha256,
            )
        )
    return tuple(archives)


def _safe_member_name(name: str) -> str:
    normalised = str(name).replace("\\", "/")
    if normalised.startswith("/") or normalised.startswith("../"):
        raise ResourceInstallError(f"资源包包含不安全路径：{name}")
    parts = Path(normalised).parts
    if ".." in parts:
        raise ResourceInstallError(f"资源包包含不安全路径：{name}")
    return normalised


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _download(
    archive: ResourceArchive,
    destination: Path,
    *,
    client_factory: Callable[..., Any] = httpx.AsyncClient,
) -> Path:
    if archive.url.startswith("file://"):
        source = Path(archive.url[7:])
        destination.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.copy2, source, destination)
        if archive.sha256:
            digest = await asyncio.to_thread(_sha256, destination)
            if digest.casefold() != archive.sha256.casefold():
                destination.unlink(missing_ok=True)
                raise ResourceInstallError("资源包 SHA-256 校验失败")
        return destination

    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = Path(f"{destination}.part")
    headers: dict[str, str] = {}
    if partial.exists():
        headers["Range"] = f"bytes={partial.stat().st_size}-"

    timeout = httpx.Timeout(connect=30, read=None, write=60, pool=30)
    async with client_factory(timeout=timeout, follow_redirects=True) as client:
        async with client.stream("GET", archive.url, headers=headers) as response:
            if partial.exists() and response.status_code != 206:
                partial.unlink(missing_ok=True)
                response = await client.get(archive.url)
                response.raise_for_status()
                payload = response.iter_bytes()
            else:
                response.raise_for_status()
                payload = response.aiter_bytes()
            mode = "ab" if partial.exists() else "wb"
            with partial.open(mode) as stream:
                async for chunk in payload:
                    if chunk:
                        stream.write(chunk)

    if archive.sha256:
        digest = await asyncio.to_thread(_sha256, partial)
        if digest.casefold() != archive.sha256.casefold():
            partial.unlink(missing_ok=True)
            raise ResourceInstallError("资源包 SHA-256 校验失败")
    partial.replace(destination)
    return destination


def _extract_archive(archive: Path, destination: Path) -> Path:
    try:
        import py7zr
    except ImportError as error:
        raise ResourceInstallError(
            "缺少 py7zr，无法自动解压舞萌静态资源"
        ) from error

    destination.mkdir(parents=True, exist_ok=True)
    try:
        with py7zr.SevenZipFile(archive, mode="r") as handle:
            for name in handle.getnames():
                _safe_member_name(name)
            handle.extractall(path=destination)
    except ResourceInstallError:
        raise
    except Exception as error:
        raise ResourceInstallError(f"资源包解压失败：{error}") from error

    nested = destination / "static"
    return nested if nested.is_dir() else destination


def _merge_static_tree(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        target = destination / child.name
        if child.is_dir():
            shutil.copytree(child, target, dirs_exist_ok=True)
        else:
            shutil.copy2(child, target)


def _run_chromium_install() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        check=False,
        capture_output=True,
        text=True,
        timeout=900,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-500:]
        raise ResourceInstallError(
            "Playwright Chromium 自动安装失败；"
            "若为 Linux 系统依赖缺失，请执行 "
            "`python -m playwright install --with-deps chromium`。"
            + (f" 详情：{detail}" if detail else "")
        )


async def ensure_static_resources(
    *,
    download: Callable[..., Any] = _download,
    extract: Callable[[Path, Path], Path] = _extract_archive,
    chromium_install: Callable[[], Any] = _run_chromium_install,
) -> bool:
    """Install missing resource archives and Chromium in one background task."""

    ensure_runtime_dirs()
    chromium_marker = static.parent / "chromium-ready"
    if resources_ready() and (
        not maiconfig.auto_install_chromium or chromium_marker.exists()
    ):
        return True
    if not resources_ready() and not maiconfig.auto_download:
        return False

    archives = list(_archive_list())
    if not resources_ready():
        install_root = static.parent / ".maimaidx-install"
        install_root.mkdir(parents=True, exist_ok=True)
        run_id = uuid.uuid4().hex
        with tempfile.TemporaryDirectory(
            prefix=f"{run_id}-",
            dir=install_root,
        ) as temp_name:
            temp = Path(temp_name)
            if maiconfig.resource_archive:
                local = Path(maiconfig.resource_archive).expanduser()
                if not local.is_absolute():
                    local = Path.cwd() / local
                archives = [ResourceArchive(local.resolve().as_uri())]

            for index, archive in enumerate(archives, 1):
                target = temp / f"archive-{index}.7z"
                await download(archive, target)
                extracted = await asyncio.to_thread(
                    extract, target, temp / f"x{index}"
                )
                await asyncio.to_thread(_merge_static_tree, extracted, static)

    if maiconfig.auto_install_chromium:
        await asyncio.to_thread(chromium_install)
        chromium_marker.write_text("ok\n", encoding="utf-8")

    marker = static.parent / "resource-install.json"
    marker.write_text(
        json.dumps(
            {
                "version": "CN1.55+CN1.56",
                "resources": [item.url for item in archives],
                "chromium": maiconfig.auto_install_chromium,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    issues = resource_issues()
    if issues:
        raise ResourceInstallError(
            "静态资源安装后校验仍未通过：\n" + "\n".join(issues)
        )
    log.success("舞萌DX静态资源与 Chromium 已准备完成")
    return True

