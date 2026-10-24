"""Global sticker library ported from HypeR_Bot."""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import time
from collections import Counter
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import RLock
from typing import Any


@dataclass(frozen=True, slots=True)
class StickerOptions:
    directory: Path = Path("data/jianer_ai/stickers")
    enabled: bool = True
    max_entries: int = 500
    max_size_bytes: int = 5 * 1024 * 1024
    max_dimension: int = 4096
    send_interval_seconds: float = 10.0


@dataclass(slots=True)
class StickerEntry:
    id: int
    file: str
    md5: str
    description: str
    keywords: str
    adder: str = ""
    created_at: int = 0
    use_count: int = 0
    last_used_at: int = 0

    def public(self, *, score: float | None = None) -> dict[str, Any]:
        value = asdict(self)
        value["id"] = int(self.id)
        if score is not None:
            value["score"] = round(float(score), 4)
        return value


@dataclass(frozen=True, slots=True)
class StickerIngestResult:
    status: str
    entry: StickerEntry | None = None
    reason: str = ""


class StickerError(RuntimeError):
    pass


class StickerStore:
    """Persistent global sticker library with lightweight Chinese BM25 search."""

    def __init__(
        self,
        options: StickerOptions,
        *,
        clock: Any = time.time,
    ) -> None:
        self.options = options
        self.root = Path(options.directory).resolve()
        self.files_dir = self.root / "files"
        self.index_path = self.root / "stickers.json"
        self._clock = clock
        self._lock = RLock()
        self._entries: list[StickerEntry] = []
        self._next_id = 1
        self.root.mkdir(parents=True, exist_ok=True)
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.reload()

    def reload(self) -> None:
        with self._lock:
            try:
                raw = json.loads(self.index_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                self._entries = []
                self._next_id = 1
                return
            except (OSError, json.JSONDecodeError) as exc:
                raise StickerError("表情包索引读取失败") from exc
            entries: list[StickerEntry] = []
            raw_entries = raw.get("entries") if isinstance(raw, Mapping) else None
            for item in raw_entries or ():
                if not isinstance(item, Mapping):
                    continue
                try:
                    entry = StickerEntry(
                        id=int(item["id"]),
                        file=str(item["file"]),
                        md5=str(item["md5"]),
                        description=str(item.get("description") or ""),
                        keywords=str(item.get("keywords") or ""),
                        adder=str(item.get("adder") or ""),
                        created_at=int(item.get("created_at") or 0),
                        use_count=int(item.get("use_count") or 0),
                        last_used_at=int(item.get("last_used_at") or 0),
                    )
                except (KeyError, TypeError, ValueError):
                    continue
                if Path(entry.file).is_file():
                    entries.append(entry)
            self._entries = entries
            self._next_id = max(
                int(raw.get("next_id") or 1)
                if isinstance(raw, Mapping)
                else 1,
                max((entry.id for entry in entries), default=0) + 1,
            )

    def add(
        self,
        raw: bytes,
        description: str,
        keywords: str = "",
        adder: str = "",
    ) -> StickerIngestResult:
        if not self.options.enabled:
            return StickerIngestResult("rejected", reason="表情包功能已关闭")
        try:
            normalized, _mime, extension = normalize_sticker_image(
                raw,
                max_dimension=self.options.max_dimension,
                max_size_bytes=self.options.max_size_bytes,
            )
        except StickerError as exc:
            return StickerIngestResult("rejected", reason=str(exc))
        digest = hashlib.sha256(normalized).hexdigest()
        with self._lock:
            existing = next(
                (item for item in self._entries if item.md5 == digest),
                None,
            )
            if existing is not None:
                return StickerIngestResult("duplicate", entry=existing)
            path = self.files_dir / f"{digest}{extension}"
            _atomic_write(path, normalized)
            now = int(self._clock())
            entry = StickerEntry(
                id=self._next_id,
                file=str(path),
                md5=digest,
                description=str(description or "").strip()[:1000],
                keywords=str(keywords or "").strip()[:500],
                adder=str(adder or ""),
                created_at=now,
            )
            self._next_id += 1
            self._entries.append(entry)
            removed = self._trim_locked()
            self._save_locked()
        for old in removed:
            self._delete_file(old.file)
        return StickerIngestResult("added", entry=entry)

    def get(self, sticker_id: int) -> StickerEntry | None:
        with self._lock:
            return next(
                (
                    entry
                    for entry in self._entries
                    if entry.id == int(sticker_id)
                ),
                None,
            )

    def delete(self, sticker_id: int) -> StickerEntry | None:
        with self._lock:
            entry = next(
                (
                    item
                    for item in self._entries
                    if item.id == int(sticker_id)
                ),
                None,
            )
            if entry is None:
                return None
            self._entries.remove(entry)
            self._save_locked()
        self._delete_file(entry.file)
        return entry

    def clear(self) -> int:
        with self._lock:
            entries = list(self._entries)
            self._entries.clear()
            self._next_id = 1
            self._save_locked()
        for entry in entries:
            self._delete_file(entry.file)
        return len(entries)

    def mark_used(self, sticker_id: int) -> None:
        with self._lock:
            entry = next(
                (
                    item
                    for item in self._entries
                    if item.id == int(sticker_id)
                ),
                None,
            )
            if entry is None:
                return
            entry.use_count += 1
            entry.last_used_at = int(self._clock())
            self._save_locked()

    def list_recent(self, limit: int = 10) -> tuple[StickerEntry, ...]:
        with self._lock:
            return tuple(
                sorted(
                    self._entries,
                    key=lambda item: (item.created_at, item.id),
                    reverse=True,
                )[: max(1, int(limit))]
            )

    def search(
        self,
        query: str,
        top_k: int = 5,
    ) -> tuple[tuple[StickerEntry, float], ...]:
        text = str(query or "").strip()
        if not text:
            return tuple((entry, 0.0) for entry in self.list_recent(top_k))
        with self._lock:
            entries = list(self._entries)
        if not entries:
            return ()
        documents = [
            _tokens(f"{entry.keywords} {entry.description}")
            for entry in entries
        ]
        query_tokens = _tokens(text)
        if not query_tokens:
            return ()
        document_count = len(documents)
        average_length = sum(len(tokens) for tokens in documents) / max(
            1,
            document_count,
        )
        document_frequency = Counter(
            token
            for tokens in documents
            for token in set(tokens)
        )
        normalized_query = re.sub(r"\s+", "", text.casefold())
        scored: list[tuple[StickerEntry, float]] = []
        for entry, tokens in zip(entries, documents, strict=True):
            frequencies = Counter(tokens)
            length = max(1, len(tokens))
            score = 0.0
            for token in query_tokens:
                frequency = frequencies.get(token, 0)
                if not frequency:
                    continue
                df = document_frequency.get(token, 0)
                inverse = math.log(
                    1 + (document_count - df + 0.5) / (df + 0.5)
                )
                denominator = frequency + 1.5 * (
                    1 - 0.75 + 0.75 * length / max(1.0, average_length)
                )
                score += inverse * frequency * 2.5 / denominator
            haystack = re.sub(
                r"\s+",
                "",
                f"{entry.keywords}{entry.description}".casefold(),
            )
            if normalized_query and normalized_query in haystack:
                score += 4.0
            if score > 0:
                scored.append((entry, score))
        scored.sort(
            key=lambda item: (
                item[1],
                item[0].last_used_at,
                item[0].created_at,
            ),
            reverse=True,
        )
        return tuple(scored[: max(1, int(top_k))])

    def count(self) -> int:
        with self._lock:
            return len(self._entries)

    def total_bytes(self) -> int:
        with self._lock:
            return sum(
                Path(entry.file).stat().st_size
                for entry in self._entries
                if Path(entry.file).is_file()
            )

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.options.enabled,
            "count": self.count(),
            "bytes": self.total_bytes(),
            "directory": str(self.root),
        }

    def _trim_locked(self) -> list[StickerEntry]:
        limit = max(1, int(self.options.max_entries))
        if len(self._entries) <= limit:
            return []
        ordered = sorted(
            self._entries,
            key=lambda item: (item.created_at, item.id),
        )
        removed = ordered[: len(ordered) - limit]
        removed_ids = {entry.id for entry in removed}
        self._entries = [
            entry for entry in self._entries if entry.id not in removed_ids
        ]
        return removed

    def _save_locked(self) -> None:
        payload = {
            "next_id": self._next_id,
            "entries": [asdict(entry) for entry in self._entries],
        }
        _atomic_write(
            self.index_path,
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        )

    @staticmethod
    def _delete_file(path: str) -> None:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass


def normalize_sticker_image(
    raw: bytes,
    *,
    max_dimension: int = 4096,
    max_size_bytes: int = 5 * 1024 * 1024,
) -> tuple[bytes, str, str]:
    if not raw:
        raise StickerError("图片为空")
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise StickerError("缺少 Pillow 依赖") from exc
    try:
        with Image.open(io.BytesIO(raw)) as source:
            source.seek(0)
            image = ImageOps.exif_transpose(source).convert("RGBA")
    except Exception as exc:
        raise StickerError("无法解析这张图片") from exc
    limit = max(128, int(max_dimension))
    if max(image.size) > limit:
        image.thumbnail((limit, limit), Image.Resampling.LANCZOS)
    has_alpha = image.getextrema()[3][0] < 255
    if has_alpha:
        output = io.BytesIO()
        image.save(output, format="PNG", optimize=True)
        data = output.getvalue()
        mime = "image/png"
        extension = ".png"
    else:
        output = io.BytesIO()
        image.convert("RGB").save(
            output,
            format="JPEG",
            quality=88,
            optimize=True,
        )
        data = output.getvalue()
        mime = "image/jpeg"
        extension = ".jpg"
    if len(data) > max_size_bytes:
        raise StickerError("图片压缩后仍然过大")
    return data, mime, extension


def _tokens(text: str) -> list[str]:
    normalized = str(text or "").casefold().strip()
    if not normalized:
        return []
    try:
        import jieba

        words = [
            item.strip()
            for item in jieba.cut_for_search(normalized)
            if item.strip()
        ]
    except ImportError:
        words = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", normalized)
    if not words:
        words = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", normalized)
    return words


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    with temporary.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
