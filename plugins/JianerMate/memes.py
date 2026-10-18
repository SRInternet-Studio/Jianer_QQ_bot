"""Meme collection, normalisation, storage, and vision-pick helpers."""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

from plugins.JianerMate.config import MemeOptions
from plugins.JianerMate.store import MateStore, MemeRecord


_LOGGER = logging.getLogger("jianer_mate.memes")
_ENCODE_ATTEMPTS = 3
_STORED_NAME = re.compile(r"^[0-9a-f]{32}\.(?:jpg|png)$")
_ORPHAN_MIN_AGE_SECONDS = 3600


class MemeError(RuntimeError):
    """Raised when an image cannot become a usable meme."""


@dataclass(frozen=True, slots=True)
class IngestResult:
    status: str
    meme_id: int | None = None
    reason: str = ""

    @property
    def accepted(self) -> bool:
        return self.status == "added"


@dataclass(frozen=True, slots=True)
class MemeCandidate:
    id: int
    tags: str
    path: Path
    use_count: int
    last_used_at: int | None


class MemeLibrary:
    """Global meme pool shared by every conversation."""

    def __init__(
        self,
        store: MateStore,
        options: MemeOptions,
        *,
        directory: str | Path | None = None,
        logger: Any | None = None,
    ) -> None:
        self.store = store
        self.options = options
        if directory is None:
            raise ValueError("MemeLibrary requires an explicit directory")
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._logger = logger or _LOGGER

    # ------------------------------------------------------------------ #
    # ingestion
    # ------------------------------------------------------------------ #
    def ingest(
        self,
        data: bytes,
        *,
        source_group: str = "",
        source_user: str = "",
        now: int | None = None,
    ) -> IngestResult:
        if not self.options.enabled:
            return IngestResult("rejected", reason="表情包功能已关闭")
        raw = bytes(data or b"")
        if not raw:
            return IngestResult("rejected", reason="空图片")
        if len(raw) > self.options.max_size_bytes:
            return IngestResult("rejected", reason="图片过大")
        try:
            encoded, mime, width, height = self._normalize(raw)
        except MemeError as exc:
            return IngestResult("rejected", reason=str(exc))
        digest = hashlib.sha256(encoded).hexdigest()
        if self.store.get_meme_by_sha(digest) is not None:
            return IngestResult("duplicate", reason="已经收藏过这张表情")
        suffix = ".png" if mime == "image/png" else ".jpg"
        target = self.directory / f"{digest[:32]}{suffix}"
        try:
            target.write_bytes(encoded)
        except OSError as exc:
            return IngestResult("rejected", reason=f"写入失败: {exc.__class__.__name__}")
        meme_id = self.store.add_meme(
            sha256=digest,
            path=str(target),
            mime=mime,
            width=width,
            height=height,
            size=len(encoded),
            source_group=source_group,
            source_user=source_user,
            now=now,
        )
        if meme_id is None:
            target.unlink(missing_ok=True)
            return IngestResult("duplicate", reason="已经收藏过这张表情")
        self._enforce_limit()
        return IngestResult("added", meme_id=meme_id)

    def _normalize(self, data: bytes) -> tuple[bytes, str, int, int]:
        Image, ImageOps = _load_pillow()
        try:
            with Image.open(BytesIO(data)) as image:
                image.load()
                frame = ImageOps.exif_transpose(image)
                if getattr(frame, "is_animated", False):
                    frame.seek(0)
                width, height = frame.size
                if min(width, height) < int(self.options.min_dimension):
                    raise MemeError("图片太小")
                if max(width, height) > int(self.options.max_dimension):
                    frame = frame.copy()
                    frame.thumbnail(
                        (self.options.max_dimension, self.options.max_dimension)
                    )
                has_alpha = frame.mode in {"RGBA", "LA"} or (
                    frame.mode == "P" and "transparency" in frame.info
                )
                working = frame.convert("RGBA" if has_alpha else "RGB")
                mime = "image/png" if has_alpha else "image/jpeg"
                encoded = _encode(working, mime)
                attempts = 0
                while len(encoded) > self.options.max_size_bytes and attempts < _ENCODE_ATTEMPTS:
                    attempts += 1
                    width, height = working.size
                    if width <= 64 or height <= 64:
                        raise MemeError("图片压缩后仍然过大")
                    working = working.resize(
                        (max(1, int(width * 0.7)), max(1, int(height * 0.7)))
                    )
                    encoded = _encode(working, mime)
                if len(encoded) > self.options.max_size_bytes:
                    raise MemeError("图片压缩后仍然过大")
                return encoded, mime, working.width, working.height
        except MemeError:
            raise
        except Exception as exc:  # noqa: BLE001 - Pillow raises many subclasses
            raise MemeError("无法解析这张图片") from exc

    def _enforce_limit(self) -> None:
        overflow = self.store.overflow_memes(self.options.max_entries)
        for record in overflow:
            removed = self.store.delete_meme(record.id)
            if removed is not None:
                _safe_unlink(Path(removed.path))

    # ------------------------------------------------------------------ #
    # selection and sending
    # ------------------------------------------------------------------ #
    def candidates(self, *, limit: int | None = None, now: int) -> tuple[MemeCandidate, ...]:
        if not self.options.enabled:
            return ()
        cooldown = int(self.options.reuse_cooldown_seconds)
        exclude_used_after = now - cooldown if cooldown > 0 else None
        records = self.store.meme_candidates(
            limit=int(limit or self.options.candidates),
            now=now,
            exclude_used_after=exclude_used_after,
        )
        return tuple(
            MemeCandidate(
                id=record.id,
                tags=record.tags,
                path=Path(record.path),
                use_count=record.use_count,
                last_used_at=record.last_used_at,
            )
            for record in records
        )

    def thumbnail_data_uri(self, candidate: MemeCandidate) -> str | None:
        data = self.thumbnail_bytes(candidate)
        if not data:
            return None
        payload = base64.b64encode(data).decode("ascii")
        return f"data:image/jpeg;base64,{payload}"

    def thumbnail_bytes(self, candidate: MemeCandidate) -> bytes | None:
        Image, _ = _load_pillow()
        path = Path(candidate.path)
        if not path.is_file():
            return None
        try:
            with Image.open(path) as image:
                image.load()
                frame = image.convert("RGB")
                frame.thumbnail(
                    (int(self.options.thumbnail_px), int(self.options.thumbnail_px))
                )
                return _encode(frame, "image/jpeg", quality=80)
        except Exception:  # noqa: BLE001 - corrupt library files are skipped
            return None

    def file_uri(self, meme_id: int) -> str | None:
        record = self.store.get_meme(meme_id)
        if record is None or not record.enabled:
            return None
        path = Path(record.path).resolve()
        root = self.directory.resolve()
        if root not in path.parents:
            self._logger.warning("拒绝发送库外文件：%s", path)
            return None
        if not path.is_file():
            return None
        return path.as_uri()

    def mark_used(self, meme_id: int, *, now: int) -> None:
        self.store.mark_meme_used(meme_id, now=now)

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.options.enabled,
            "count": self.store.count_memes(),
            "enabled_count": self.store.count_memes(enabled_only=True),
            "bytes": self.store.total_meme_bytes(),
            "directory": str(self.directory),
        }

    def delete(self, meme_id: int) -> bool:
        record = self.store.delete_meme(meme_id)
        if record is None:
            return False
        _safe_unlink(Path(record.path))
        return True

    def clear(self) -> int:
        records = self.store.clear_memes()
        for record in records:
            _safe_unlink(Path(record.path))
        return len(records)

    def existing_records(self) -> tuple[MemeRecord, ...]:
        return self.store.list_memes(limit=200)

    def cleanup_orphans(
        self,
        *,
        older_than_seconds: int = _ORPHAN_MIN_AGE_SECONDS,
        now: int | None = None,
    ) -> int:
        """Delete stored files that no longer have a database row."""

        timestamp = int(now if now is not None else time.time())
        known = {
            Path(path).resolve() for path in self.store.all_meme_paths()
        }
        removed = 0
        try:
            children = list(self.directory.iterdir())
        except OSError:
            return 0
        for child in children:
            if not child.is_file() or not _STORED_NAME.match(child.name):
                continue
            resolved = child.resolve()
            if resolved in known:
                continue
            try:
                if timestamp - int(child.stat().st_mtime) < older_than_seconds:
                    continue
                child.unlink()
            except OSError:
                continue
            removed += 1
        return removed


def _load_pillow() -> tuple[Any, Any]:
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:  # pragma: no cover - dependency is in requirements
        raise MemeError("缺少 Pillow 依赖") from exc
    return Image, ImageOps


def _encode(image: Any, mime: str, *, quality: int = 88) -> bytes:
    buffer = BytesIO()
    if mime == "image/png":
        image.save(buffer, format="PNG", optimize=True)
    else:
        image.save(buffer, format="JPEG", quality=quality, optimize=True)
    return buffer.getvalue()


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
