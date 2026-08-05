from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path


MAX_MEDIA_DURATION_SECONDS = 5 * 60
MAX_VIDEO_FRAMES = 8
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
