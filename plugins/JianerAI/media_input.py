from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any


MAX_MEDIA_DURATION_SECONDS = 5 * 60
MAX_VIDEO_FRAMES = 8
MAX_IMAGE_EDGE = 2048
MAX_IMAGE_BYTES = 2 * 1024 * 1024
_STATIC_IMAGE_MIMES = frozenset({"image/jpeg", "image/png", "image/webp"})
_PROCESS_TIMEOUT_SECONDS = 60.0


class MediaProcessingError(RuntimeError):
    def __init__(self, safe_message: str) -> None:
        self.safe_message = str(safe_message).strip()
        super().__init__(self.safe_message)


@dataclass(frozen=True, slots=True)
class ProcessedMedia:
    data: bytes
    mime: str


@dataclass(frozen=True, slots=True)
class ProcessedVideo:
    frames: tuple[ProcessedMedia, ...]
    audio: ProcessedMedia | None
    duration_seconds: float


async def normalize_audio(data: bytes, mime: str) -> ProcessedMedia:
    ffmpeg, ffprobe = _require_ffmpeg()
    suffix = _media_suffix(mime, fallback=".audio")
    with tempfile.TemporaryDirectory(prefix="jianerai-audio-") as raw_dir:
        work_dir = Path(raw_dir)
        source = work_dir / f"input{suffix}"
        output = work_dir / "audio.wav"
        await asyncio.to_thread(source.write_bytes, data)
        duration = await _probe_duration(ffprobe, source)
        _validate_duration(duration, "语音")
        await _run_checked(
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(output),
        )
        if not output.is_file() or output.stat().st_size <= 44:
            raise MediaProcessingError("语音附件中没有可识别的音轨。")
        return ProcessedMedia(
            data=await asyncio.to_thread(output.read_bytes),
            mime="audio/wav",
        )


async def process_video(data: bytes, mime: str) -> ProcessedVideo:
    ffmpeg, ffprobe = _require_ffmpeg()
    suffix = _media_suffix(mime, fallback=".video")
    with tempfile.TemporaryDirectory(prefix="jianerai-video-") as raw_dir:
        work_dir = Path(raw_dir)
        source = work_dir / f"input{suffix}"
        frame_pattern = work_dir / "frame-%02d.jpg"
        audio_path = work_dir / "audio.wav"
        await asyncio.to_thread(source.write_bytes, data)
        duration = await _probe_duration(ffprobe, source)
        _validate_duration(duration, "视频")

        frames_per_second = min(
            float(MAX_VIDEO_FRAMES),
            max(0.01, MAX_VIDEO_FRAMES / duration),
        )
        await _run_checked(
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-i",
            str(source),
            "-an",
            "-vf",
            (
                f"fps={frames_per_second:.8f},"
                "scale=1280:1280:force_original_aspect_ratio=decrease"
            ),
            "-frames:v",
            str(MAX_VIDEO_FRAMES),
            "-q:v",
            "4",
            str(frame_pattern),
        )
        frame_paths = tuple(sorted(work_dir.glob("frame-*.jpg")))
        if not frame_paths:
            raise MediaProcessingError("视频附件中没有可识别的画面。")
        frame_items: list[ProcessedMedia] = []
        for path in frame_paths[:MAX_VIDEO_FRAMES]:
            frame_items.append(
                ProcessedMedia(
                    data=await asyncio.to_thread(path.read_bytes),
                    mime="image/jpeg",
                )
            )
        frames = tuple(frame_items)

        audio_result = await _run_process(
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-i",
            str(source),
            "-map",
            "0:a:0?",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(audio_path),
        )
        audio: ProcessedMedia | None = None
        if (
            audio_result.returncode == 0
            and audio_path.is_file()
            and audio_path.stat().st_size > 44
        ):
            audio = ProcessedMedia(
                data=await asyncio.to_thread(audio_path.read_bytes),
                mime="audio/wav",
            )
        return ProcessedVideo(
            frames=frames,
            audio=audio,
            duration_seconds=duration,
        )


async def normalize_image(data: bytes, mime: str) -> ProcessedMedia:
    """Return one static, size-bounded frame the chat providers accept.

    OpenAI-compatible gateways reject animated GIF with an upstream 400 and
    stall on multi-megabyte base64 bodies, so forwarding the resolved bytes
    untouched turns a single sticker or camera photo into a failed turn. Only
    images that are already safe static bitmaps inside the provider budget are
    passed through unchanged.
    """

    return await asyncio.to_thread(_normalize_image, data, mime)


def _normalize_image(data: bytes, mime: str) -> ProcessedMedia:
    normalized_mime = str(mime or "").split(";", 1)[0].strip().casefold()
    image = _open_image(data)
    try:
        animated = bool(getattr(image, "is_animated", False))
        if (
            not animated
            and normalized_mime in _STATIC_IMAGE_MIMES
            and max(image.size) <= MAX_IMAGE_EDGE
            and len(data) <= MAX_IMAGE_BYTES
        ):
            return ProcessedMedia(data=data, mime=normalized_mime)
        with suppress(AttributeError, EOFError, OSError, ValueError):
            image.seek(0)
        return _transcode_image(image)
    except MediaProcessingError:
        raise
    except Exception as exc:
        raise MediaProcessingError("无法解析该图片附件。") from exc
    finally:
        with suppress(Exception):
            image.close()


def _open_image(data: bytes) -> Any:
    try:
        from PIL import Image
    except ImportError as exc:
        raise MediaProcessingError(
            "服务器未安装 Pillow，暂时无法处理图片附件。"
        ) from exc
    try:
        image = Image.open(BytesIO(data))
        image.load()
    except Exception as exc:
        raise MediaProcessingError("无法解析该图片附件。") from exc
    return image


def _transcode_image(image: Any) -> ProcessedMedia:
    from PIL import Image, ImageOps

    frame = ImageOps.exif_transpose(image) or image
    if max(frame.size) > MAX_IMAGE_EDGE:
        frame = frame.copy()
        frame.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE), Image.LANCZOS)
    if _has_alpha(frame):
        payload = _encode_png(frame.convert("RGBA"))
        if len(payload) <= MAX_IMAGE_BYTES:
            return ProcessedMedia(data=payload, mime="image/png")
        frame = _flatten_alpha(frame)
    rgb = frame.convert("RGB")
    for quality in (85, 75, 65):
        payload = _encode_jpeg(rgb, quality)
        if len(payload) <= MAX_IMAGE_BYTES:
            return ProcessedMedia(data=payload, mime="image/jpeg")
    while max(rgb.size) > 320:
        rgb = rgb.resize(
            (max(1, rgb.width // 2), max(1, rgb.height // 2)),
            Image.LANCZOS,
        )
        payload = _encode_jpeg(rgb, 70)
        if len(payload) <= MAX_IMAGE_BYTES:
            return ProcessedMedia(data=payload, mime="image/jpeg")
    raise MediaProcessingError("图片附件太大，无法转换后交给模型。")


def _has_alpha(image: Any) -> bool:
    return image.mode in {"RGBA", "LA"} or (
        image.mode == "P" and "transparency" in image.info
    )


def _flatten_alpha(image: Any) -> Any:
    from PIL import Image

    rgba = image.convert("RGBA")
    background = Image.new("RGB", rgba.size, (255, 255, 255))
    background.paste(rgba, mask=rgba.getchannel("A"))
    return background


def _encode_png(image: Any) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _encode_jpeg(image: Any, quality: int) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=quality, optimize=True)
    return buffer.getvalue()


def _require_ffmpeg() -> tuple[str, str]:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise MediaProcessingError(
            "服务器未安装 FFmpeg，暂时无法读取该语音或视频。"
        )
    return ffmpeg, ffprobe


async def _probe_duration(ffprobe: str, source: Path) -> float:
    result = await _run_checked(
        ffprobe,
        "-v",
        "error",
        "-protocol_whitelist",
        "file,pipe",
        "-show_entries",
        "format=duration:stream=duration",
        "-of",
        "json",
        str(source),
    )
    try:
        payload = json.loads(result.stdout.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise MediaProcessingError("无法读取附件时长。") from exc
    candidates: list[float] = []
    format_data = payload.get("format") if isinstance(payload, dict) else None
    if isinstance(format_data, dict):
        _append_duration(candidates, format_data.get("duration"))
    streams = payload.get("streams") if isinstance(payload, dict) else None
    if isinstance(streams, list):
        for stream in streams:
            if isinstance(stream, dict):
                _append_duration(candidates, stream.get("duration"))
    if not candidates:
        raise MediaProcessingError("无法读取附件时长。")
    return max(candidates)


def _append_duration(candidates: list[float], raw: object) -> None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return
    if value > 0:
        candidates.append(value)


def _validate_duration(duration: float, label: str) -> None:
    if duration <= 0:
        raise MediaProcessingError(f"无法读取{label}时长。")
    if duration > MAX_MEDIA_DURATION_SECONDS:
        raise MediaProcessingError(
            f"{label}超过 {MAX_MEDIA_DURATION_SECONDS // 60} 分钟，暂不处理。"
        )


@dataclass(frozen=True, slots=True)
class _ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes


async def _run_checked(executable: str, *args: str) -> _ProcessResult:
    result = await _run_process(executable, *args)
    if result.returncode != 0:
        raise MediaProcessingError("无法解码该语音或视频附件。")
    return result


async def _run_process(executable: str, *args: str) -> _ProcessResult:
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=_PROCESS_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            process.kill()
            await process.communicate()
            raise MediaProcessingError("语音或视频处理超时。")
    except OSError as exc:
        raise MediaProcessingError("无法启动 FFmpeg 处理附件。") from exc
    return _ProcessResult(
        returncode=int(process.returncode or 0),
        stdout=stdout[:1024 * 1024],
        stderr=stderr[:4096],
    )


def _media_suffix(mime: str, *, fallback: str) -> str:
    return {
        "audio/mpeg": ".mp3",
        "audio/mp3": ".mp3",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "audio/ogg": ".ogg",
        "audio/flac": ".flac",
        "audio/mp4": ".m4a",
        "audio/x-m4a": ".m4a",
        "audio/webm": ".webm",
        "video/mp4": ".mp4",
        "video/webm": ".webm",
        "video/quicktime": ".mov",
        "video/mpeg": ".mpeg",
    }.get(str(mime).casefold(), fallback)
