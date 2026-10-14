"""Automatic preparation of MaimaiDX static assets and Chromium."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.parse import urlparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

from .config import log, maiconfig
from .resources import ensure_runtime_dirs, resource_issues, resources_ready, static


class ResourceInstallError(RuntimeError):
    """Static resource preparation failed in a recoverable way."""


RESOURCE_DOWNLOAD_RETRIES = 5
RESOURCE_RETRY_DELAY_SECONDS = 3.0


@dataclass(frozen=True)
class ResourceArchive:
    url: str
    sha256: str | None = None
    target: str | None = None


def _archive_list() -> tuple[ResourceArchive, ...]:
    archives = [
        ResourceArchive(maiconfig.resource_url, maiconfig.resource_sha256),
    ]
    if maiconfig.resource_update_url:
        archives.append(
            ResourceArchive(
                maiconfig.resource_update_url,
                maiconfig.resource_update_sha256,
                target="mai/pic",
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
    def log_progress(
        downloaded: int,
        total: int | None,
        started_at: float,
        *,
        force: bool = False,
        bucket: int | None = None,
    ) -> int | None:
        if total and total > 0:
            percent = min(downloaded / total * 100, 100.0)
            current_bucket = int(percent // 5) * 5
            if not force and bucket is not None and current_bucket <= bucket:
                return bucket
            elapsed = max(time.monotonic() - started_at, 0.001)
            speed = downloaded / elapsed / 1024 / 1024
            log.info(
                "MaimaiDX 资源下载 "
                f"{percent:5.1f}% "
                f"({downloaded / 1024 / 1024:.1f}/"
                f"{total / 1024 / 1024:.1f} MiB, "
                f"{speed:.1f} MiB/s) {destination.name}"
            )
            return current_bucket
        if force:
            log.info(
                "MaimaiDX 资源下载 "
                f"({downloaded / 1024 / 1024:.1f} MiB) {destination.name}"
            )
        return bucket

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

    if destination.exists():
        if not archive.sha256:
            log.info(f"MaimaiDX 资源包已缓存：{destination.name}")
            return destination
        digest = await asyncio.to_thread(_sha256, destination)
        if digest.casefold() == archive.sha256.casefold():
            log.info(f"MaimaiDX 资源包已缓存且校验通过：{destination.name}")
            return destination
        destination.unlink(missing_ok=True)

    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = Path(f"{destination}.part")
    headers: dict[str, str] = {
        "Accept-Encoding": "identity",
        "User-Agent": "JianerBot-MaimaiDX/3.1.4",
    }
    if partial.exists():
        headers["Range"] = f"bytes={partial.stat().st_size}-"

    started_at = time.monotonic()
    downloaded = partial.stat().st_size if partial.exists() else 0
    progress_bucket: int | None = None
    if downloaded:
        log.info(
            "MaimaiDX 检测到下载断点，将从 "
            f"{downloaded / 1024 / 1024:.1f} MiB 继续：{destination.name}"
        )

    timeout = httpx.Timeout(connect=30, read=None, write=60, pool=30)
    async with client_factory(timeout=timeout, follow_redirects=True) as client:
        async with client.stream("GET", archive.url, headers=headers) as response:
            if partial.exists() and response.status_code != 206:
                partial.unlink(missing_ok=True)
                downloaded = 0
                response = await client.get(archive.url)
                response.raise_for_status()
                payload = response.iter_bytes()
            else:
                response.raise_for_status()
                payload = response.aiter_bytes()
            content_length = int(response.headers.get("content-length", 0) or 0)
            total = downloaded + content_length if content_length else None
            progress_bucket = log_progress(
                downloaded,
                total,
                started_at,
                force=True,
                bucket=progress_bucket,
            )
            mode = "ab" if partial.exists() else "wb"
            with partial.open(mode) as stream:
                async for chunk in payload:
                    if chunk:
                        stream.write(chunk)
                        downloaded += len(chunk)
                        progress_bucket = log_progress(
                            downloaded,
                            total,
                            started_at,
                            bucket=progress_bucket,
                        )
            log_progress(
                downloaded,
                total,
                started_at,
                force=True,
                bucket=progress_bucket,
            )

    if archive.sha256:
        digest = await asyncio.to_thread(_sha256, partial)
        if digest.casefold() != archive.sha256.casefold():
            partial.unlink(missing_ok=True)
            raise ResourceInstallError("资源包 SHA-256 校验失败")
    partial.replace(destination)
    return destination


def _cache_name(index: int, archive: ResourceArchive) -> str:
    digest = hashlib.sha256(archive.url.encode("utf-8")).hexdigest()[:16]
    name = Path(urlparse(archive.url).path).name or f"archive-{index}.7z"
    return f"{index}-{digest}-{name}"


async def _download_with_retries(
    archive: ResourceArchive,
    destination: Path,
    download: Callable[..., Any],
) -> Path:
    delay = RESOURCE_RETRY_DELAY_SECONDS
    last_error: BaseException | None = None
    for attempt in range(1, RESOURCE_DOWNLOAD_RETRIES + 1):
        try:
            return await download(archive, destination)
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, OSError, ResourceInstallError) as error:
            last_error = error
            if attempt >= RESOURCE_DOWNLOAD_RETRIES:
                break
            log.warning(
                "舞萌静态资源下载中断，将从断点重试 "
                f"({attempt}/{RESOURCE_DOWNLOAD_RETRIES})："
                f"{type(error).__name__}: {error}"
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)
    raise ResourceInstallError(
        "舞萌静态资源下载失败，已尝试断点续传："
        f"{type(last_error).__name__ if last_error else 'unknown'}"
    ) from last_error


def _extract_archive(archive: Path, destination: Path) -> Path:
    try:
        import py7zr
    except ImportError as error:
        extractor = shutil.which("7z") or shutil.which("7zz")
        if extractor is None:
            raise ResourceInstallError(
                "缺少 py7zr 或系统 7z，无法自动解压舞萌静态资源"
            ) from error
        destination.mkdir(parents=True, exist_ok=True)
        listing = subprocess.run(
            [extractor, "l", "-slt", str(archive)],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if listing.returncode != 0:
            raise ResourceInstallError(
                f"7z 读取资源包失败：{(listing.stderr or listing.stdout)[-500:]}"
            ) from error
        for line in listing.stdout.splitlines():
            if line.startswith("Path = "):
                _safe_member_name(line[7:])
        extracted = subprocess.run(
            [extractor, "x", "-y", f"-o{destination}", str(archive)],
            check=False,
            capture_output=True,
            text=True,
            timeout=1800,
        )
        if extracted.returncode != 0:
            raise ResourceInstallError(
                f"7z 解压资源包失败：{(extracted.stderr or extracted.stdout)[-500:]}"
            ) from error
        nested = destination / "static"
        return nested if nested.is_dir() else destination

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

    for nested in destination.rglob("static"):
        if nested.is_dir():
            return nested
    return destination


def _merge_static_tree(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        target = destination / child.name
        if child.is_dir():
            shutil.copytree(child, target, dirs_exist_ok=True)
        else:
            shutil.copy2(child, target)


def _remove_misplaced_resource_dirs(destination: Path) -> None:
    if not destination.is_dir():
        return
    for child in destination.iterdir():
        if child.is_dir() and child.name.startswith("Resource CN"):
            shutil.rmtree(child)


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
        cache_root = static.parent / ".maimaidx-cache"
        install_root.mkdir(parents=True, exist_ok=True)
        cache_root.mkdir(parents=True, exist_ok=True)
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
                target = cache_root / _cache_name(index, archive)
                await _download_with_retries(archive, target, download)
                extracted = await asyncio.to_thread(
                    extract, target, temp / f"x{index}"
                )
                destination = (
                    static / archive.target
                    if archive.target
                    else static
                )
                await asyncio.to_thread(
                    _merge_static_tree,
                    extracted,
                    destination,
                )
        await asyncio.to_thread(_remove_misplaced_resource_dirs, static)

    if maiconfig.auto_install_chromium:
        await asyncio.to_thread(chromium_install)

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
    if maiconfig.auto_install_chromium:
        chromium_marker.write_text("ok\n", encoding="utf-8")
    log.success("舞萌DX静态资源与 Chromium 已准备完成")
    return True

