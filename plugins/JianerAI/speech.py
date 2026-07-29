from __future__ import annotations

import asyncio
import inspect
import re
import shutil
import tempfile
import unicodedata
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class SpeechError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SpeechOptions:
    voice: str = "zh-CN-XiaoyiNeural"
    rate: str = "+0%"
    volume: str = "+0%"
    pitch: str = "+0Hz"

    @classmethod
    def from_settings(
        cls,
        settings: "SpeechOptions | Mapping[str, Any] | None",
    ) -> "SpeechOptions":
        if settings is None:
            return cls()
        if isinstance(settings, cls):
            return settings
        if not isinstance(settings, Mapping):
            raise TypeError("speech settings must be SpeechOptions or a mapping")
        defaults = cls()
        return cls(
            voice=str(
                settings.get("voice")
                or settings.get("voiceColor")
                or defaults.voice
            ),
            rate=str(settings.get("rate") or defaults.rate),
            volume=str(settings.get("volume") or defaults.volume),
            pitch=str(settings.get("pitch") or defaults.pitch),
        )


@dataclass(frozen=True, slots=True)
class SpeechArtifact:
    path: Path
    mime: str
    size: int


class SpeechBackend(Protocol):
    def __call__(
        self,
        text: str,
        output_path: Path,
        options: SpeechOptions,
    ) -> Awaitable[Any] | Any:
        ...


class SpeechSynthesizer:
    """Per-plugin-instance Edge TTS workspace with deterministic shutdown."""

    def __init__(
        self,
        *,
        temp_parent: str | Path | None = None,
        backend: SpeechBackend | None = None,
        sanitizer: Callable[[str], str] | None = None,
    ) -> None:
        parent = Path(temp_parent) if temp_parent is not None else None
        if parent is not None:
            parent.mkdir(parents=True, exist_ok=True)
        self.temp_dir = Path(
            tempfile.mkdtemp(
                prefix="jianerai-tts-",
                dir=str(parent) if parent is not None else None,
            )
        )
        self._backend = backend or _edge_tts_backend
        self._sanitizer = sanitizer or sanitize_for_speech
        self._state_lock = asyncio.Lock()
        self._idle = asyncio.Event()
        self._idle.set()
        self._active = 0
        self._closed = False
        self._shutdown_task: asyncio.Task[None] | None = None

    @property
    def closed(self) -> bool:
        return self._closed

    async def synthesize(
        self,
        text: str,
        settings: SpeechOptions | Mapping[str, Any] | None = None,
    ) -> Path | None:
        artifact = await self.synthesize_artifact(text, settings)
        return artifact.path if artifact is not None else None

    async def synthesize_artifact(
        self,
        text: str,
        settings: SpeechOptions | Mapping[str, Any] | None = None,
    ) -> SpeechArtifact | None:
        sanitized = self._sanitizer(str(text or ""))
        if not sanitized:
            return None
        options = SpeechOptions.from_settings(settings)
        async with self._state_lock:
            if self._closed:
                raise SpeechError("speech synthesizer is closed")
            self._active += 1
            self._idle.clear()
        output_path = self.temp_dir / f"{uuid.uuid4().hex}.mp3"
        try:
            result = self._backend(sanitized, output_path, options)
            if inspect.isawaitable(result):
                await result
            if not output_path.is_file():
                raise SpeechError("speech backend did not produce an audio file")
            size = output_path.stat().st_size
            if size <= 0:
                output_path.unlink(missing_ok=True)
                raise SpeechError("speech backend produced an empty audio file")
            return SpeechArtifact(
                path=output_path,
                mime="audio/mpeg",
                size=size,
            )
        except SpeechError:
            raise
        except Exception as exc:
            output_path.unlink(missing_ok=True)
            raise SpeechError("speech synthesis failed") from exc
        finally:
            async with self._state_lock:
                self._active -= 1
                if self._active == 0:
                    self._idle.set()

    async def shutdown(self) -> None:
        async with self._state_lock:
            if self._shutdown_task is None:
                self._closed = True
                self._shutdown_task = asyncio.create_task(
                    self._finish_shutdown()
                )
            shutdown_task = self._shutdown_task
        await asyncio.shield(shutdown_task)

    async def _finish_shutdown(self) -> None:
        await self._idle.wait()
        await asyncio.to_thread(shutil.rmtree, self.temp_dir, True)

    async def __aenter__(self) -> "SpeechSynthesizer":
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.shutdown()


async def _edge_tts_backend(
    text: str,
    output_path: Path,
    options: SpeechOptions,
) -> None:
    try:
        import edge_tts
    except ImportError as exc:
        raise SpeechError("TTS requires the 'edge_tts' package") from exc
    communicator = edge_tts.Communicate(
        text,
        options.voice,
        rate=options.rate,
        volume=options.volume,
        pitch=options.pitch,
    )
    await communicator.save(str(output_path))


def sanitize_for_speech(text: str) -> str:
    """Remove formatting and pictographs while preserving readable math."""

    value = str(text or "")
    value = re.sub(r"```[\s\S]*?```", " ", value)
    value = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"`([^`]*)`", r"\1", value)
    value = re.sub(r"[*_]{1,2}(.+?)[*_]{1,2}", r"\1", value)
    value = re.sub(r"^\s{0,3}(?:#{1,6}|>|[-*]|\d+\.)\s+", "", value, flags=re.MULTILINE)
    cleaned: list[str] = []
    for char in value:
        category = unicodedata.category(char)
        if category in {"So", "Sk"}:
            continue
        cleaned.append(char)
    value = "".join(cleaned)
    value = re.sub(r"\s+", " ", value).strip()
    return value
