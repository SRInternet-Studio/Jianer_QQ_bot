from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
import zipfile
from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Any, Mapping

from ..host import logger
from ..host import localstore
from pilmoji import Pilmoji
from pilmoji.helpers import EMOJI_REGEX, getsize as pilmoji_getsize
from pilmoji.source import BaseSource
from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps, ImageSequence

RESOURCE_DIR = Path(__file__).parent / "resource"


CANVAS_SIZE = (800, 800)
CONTENT_WIDTH = 720
CONTENT_SAFE_HEIGHT = 760
AVATAR_SIZE = 240
AVATAR_CORNER_RADIUS = 30
AVATAR_CACHE_MAXSIZE = 192
GIF_TARGET_FRAMES = 60
# 源帧按“逻辑画布像素 × 帧数”限制解码工作量，比固定帧数更贴近 CPU 开销。
# 240×240 资源可容纳约 277 帧，480×480 资源约 69 帧；无论源帧多少，最终最多保留 60 帧。
GIF_MAX_DECODE_WORK_PIXELS = 16_000_000
# 极小画布可能绕过像素预算，仍需绝对帧数兜底，防止异常文件长时间占用解码线程。
GIF_ABSOLUTE_MAX_SOURCE_FRAMES = 600
GIF_MIN_FRAME_DURATION_MS = 20
GIF_MAX_FRAME_DURATION_MS = 2000
GIF_FALLBACK_FRAME_DURATION_MS = 100
GIF_PALETTE_SAMPLE_SIZE = 96
GIF_RENDER_CONCURRENCY = 2
# 动态文案只保留复用价值较高的规整头像帧；字节上限通常会先于条目上限触发。
GIF_SOURCE_CACHE_MAX_ENTRIES = 8
GIF_SOURCE_CACHE_MAX_BYTES = 24 * 1024 * 1024
# 固定卡片只按磁盘总字节淘汰，不另设条目数上限。
CARD_DISK_CACHE_MAX_BYTES = 64 * 1024 * 1024
# 修改卡片布局或编码规则时必须递增；源图、文案、字体和 Emoji 已各自包含内容指纹。
CARD_CACHE_VERSION = 4
CARD_DISK_CACHE_MAGIC = b"ROLLPIG-CARD-CACHE-V1\n"
CARD_DISK_CACHE_HEADER_MAX_BYTES = 4096
CARD_CACHE_DIR = localstore.get_plugin_cache_dir() / "cards"

NAME_FONT_SIZE = 48
DESC_FONT_SIZE = 30
ANALYSIS_FONT_MAX_SIZE = 28
ANALYSIS_FONT_MIN_SIZE = 24

NAME_MARGIN_TOP = 20
DESC_MARGIN_TOP = 20
ANALYSIS_MARGIN_TOP = 30

NAME_LINE_HEIGHT = 58
DESC_LINE_HEIGHT = 38
ANALYSIS_LINE_HEIGHT_FACTOR = 1.5

BACKGROUND_COLOR = (255, 255, 255, 255)
NAME_COLOR = (32, 32, 32, 255)
DESC_COLOR = (85, 85, 85, 255)
ANALYSIS_COLOR = (51, 51, 51, 255)
PLACEHOLDER_BG = (255, 226, 239, 255)
PLACEHOLDER_FG = (154, 92, 135, 255)
EMOJI_SCALE_FACTOR = 1.08
EMOJI_POSITION_OFFSET = (0, -2)
EXTRA_EMOJI_SYMBOLS = {
    # pig.json 中少量文本音乐符号不是标准 Emoji，中文字体又可能缺字；渲染时映射为 Noto Emoji 音符，不修改原始文案数据。
    "\u266a": "\U0001f3b5",
}
PACKAGE_FONT_DIR = RESOURCE_DIR / "fonts"


class PigCardImageLoadError(RuntimeError):
    """严格头像模式下，指定图片未能解码为可用头像时抛出的异常。"""


@dataclass(frozen=True)
class PigCardRenderResult:
    """普通小猪卡片渲染结果；保留格式字段方便后续接入 GIF。"""

    data: bytes
    image_format: str
    renderer: str
    analysis_font_size: int
    analysis_lines: int
    emoji_enabled: bool


@dataclass(frozen=True)
class _TextLayout:
    name_line: str
    desc_line: str
    analysis_lines: list[str]
    analysis_font: ImageFont.ImageFont
    analysis_font_size: int
    analysis_line_height: int
    total_height: int


@dataclass(frozen=True)
class _PreparedCard:
    """不含头像的卡片底层；GIF 渲染时逐帧复用，避免重复排版和绘制文字。"""

    canvas: Image.Image
    layout: _TextLayout
    avatar_y: int
    emoji_enabled: bool


# ================================ 卡片缓存与 GIF 并发状态 ================================ #
# 固定文案 PNG/GIF 把最终编码结果放到 64 MiB 磁盘 LRU，重启后仍能命中；
# 动态文案卡片只在内存中缓存规整后的头像帧，避免把低复用文案持续写入磁盘。
_source_gif_frame_cache: OrderedDict[
    tuple[object, ...],
    tuple[tuple[Image.Image, int], ...],
] = OrderedDict()
_source_gif_frame_cache_bytes = 0
_card_cache_lock = threading.RLock()

_gif_render_semaphore: asyncio.Semaphore | None = None
_gif_render_semaphore_loop: asyncio.AbstractEventLoop | None = None
_gif_render_semaphore_guard = threading.Lock()

# 同一个固定卡片键只生成一次；GIF 另受全局双并发限制。
_card_render_tasks: dict[tuple[object, ...], asyncio.Task[PigCardRenderResult]] = {}
_card_render_tasks_lock = asyncio.Lock()


# ================================ 字体与 Emoji 后端 ================================ #


# ================================ 彩色 Emoji 渲染 ================================ #
NOTO_EMOJI_ZIP_PATH = RESOURCE_DIR / "emoji" / "google-emoji.zip"
_VARIATION_SELECTORS = {0xFE0E, 0xFE0F}


def _emoji_to_noto_codepoint(emoji_text: str, *, strip_variation: bool) -> str:
    """把 Unicode Emoji 转成 Noto Emoji PNG 使用的 `emoji_uxxxx` 资源名片段。"""

    codepoints: list[str] = []
    for char in emoji_text:
        codepoint = ord(char)
        if strip_variation and codepoint in _VARIATION_SELECTORS:
            continue
        # Noto PNG 对 BMP 字符使用 4 位补零，例如 #️⃣ 对应 emoji_u0023_20e3.png。
        codepoints.append(f"{codepoint:04x}")
    return "_".join(codepoints)


def _noto_asset_candidates(emoji_text: str) -> tuple[str, ...]:
    """按 Noto Emoji 命名规则给出候选资源名，兼容 FE0F/FE0E 变体选择符。

    Noto 的 png/128 目录通常会省略 emoji presentation 的 FE0F，
    但不能在输入阶段粗暴删除；这里保留“精确匹配 -> 去变体选择符”的顺序，
    能兼容普通 emoji、keycap 和 ZWJ 组合。
    """

    exact = _emoji_to_noto_codepoint(emoji_text, strip_variation=False)
    stripped = _emoji_to_noto_codepoint(emoji_text, strip_variation=True)
    exact_asset = f"emoji_u{exact}.png"
    stripped_asset = f"emoji_u{stripped}.png"
    if exact_asset == stripped_asset:
        return (exact_asset,)
    return (exact_asset, stripped_asset)


class ZipNotoEmojiSource(BaseSource):
    """从内置 ZIP 按需读取 Noto Emoji PNG，避免运行时联网和仓库碎文件。"""

    def __init__(self, zip_path: Path = NOTO_EMOJI_ZIP_PATH) -> None:
        self.zip_path = zip_path
        self._zip_file = zipfile.ZipFile(zip_path, "r")
        self._lock = threading.RLock()

    @lru_cache(maxsize=1024)
    def _read_asset(self, asset_name: str) -> bytes | None:
        """读取并缓存常见 Emoji PNG 字节；每次返回时再包装成新的 BytesIO。"""

        try:
            with self._lock:
                return self._zip_file.read(asset_name)
        except KeyError:
            return None

    def get_emoji(self, emoji: str, /) -> BytesIO | None:
        for asset_name in _noto_asset_candidates(emoji):
            data = self._read_asset(asset_name)
            if data is not None:
                return BytesIO(data)
        logger.debug(f"RollPig Noto Emoji 资源缺失，回退普通字体绘制: emoji={emoji!r}")
        return None

    def get_discord_emoji(self, id: int, /) -> BytesIO | None:
        return None

    def close(self) -> None:
        """释放 Emoji ZIP 句柄和已读取资源，供插件关闭或重载时调用。"""

        with self._lock:
            self._read_asset.cache_clear()
            self._zip_file.close()


@lru_cache(maxsize=1)
def get_noto_emoji_source() -> ZipNotoEmojiSource | None:
    """加载本地 Noto Emoji 源；资源缺失时只降级，不影响普通卡片生成。"""

    if not NOTO_EMOJI_ZIP_PATH.exists():
        logger.warning(f"RollPig Noto Emoji ZIP 不存在，Emoji 将按普通字体降级绘制: {NOTO_EMOJI_ZIP_PATH}")
        return None
    try:
        return ZipNotoEmojiSource(NOTO_EMOJI_ZIP_PATH)
    except Exception as error:
        logger.warning(f"RollPig Noto Emoji ZIP 加载失败，Emoji 将按普通字体降级绘制: {error}")
        return None


def _resolve_font_path(value: str | None) -> Path | None:
    """解析用户配置的字体路径；相对路径按 Bot 运行目录而不是插件目录处理。"""

    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    raw_path = Path(text).expanduser()
    return raw_path if raw_path.is_absolute() else Path.cwd() / raw_path


def _configured_font_candidates(*, bold: bool) -> list[Path]:
    """读取用户显式指定的字体；插件配置已在 config.py 集中加载一次。"""

    from .config import plugin_config

    candidates: list[Path] = []
    # 保留字体配置项：Pillow 无完整字体族管理，标题和正文共享同一字体更可预测。
    configured_path = _resolve_font_path(plugin_config.rollpig_card_font_path)
    if configured_path is not None:
        candidates.append(configured_path)
    return list(dict.fromkeys(candidates))


def _font_candidates(*, bold: bool) -> list[Path]:
    """按优先级列出候选字体；用户配置最高，系统字体兜底。"""

    packaged_fonts = [
        PACKAGE_FONT_DIR / "SourceHanSansSC-Medium.otf",
        PACKAGE_FONT_DIR / ("msyhbd.ttc" if bold else "msyh.ttc"),
        PACKAGE_FONT_DIR / "msyh.ttc",
        PACKAGE_FONT_DIR / "msyhbd.ttc",
        # 必须显式列路径，否则容器里仍会退回默认位图字体。
        Path.cwd() / "fonts" / ("msyhbd.ttc" if bold else "msyh.ttc"),
        Path.cwd() / "fonts" / "msyh.ttc",
        Path.cwd() / "fonts" / "msyhbd.ttc",
        Path.cwd() / "fonts" / "MicrosoftYaHei" / "Microsoft Yahei.ttf",
        Path("/app/fonts") / ("msyhbd.ttc" if bold else "msyh.ttc"),
        Path("/app/fonts/msyh.ttc"),
        Path("/app/fonts/msyhbd.ttc"),
        Path("/app/fonts/MicrosoftYaHei/Microsoft Yahei.ttf"),
        Path("/root/.fonts") / ("msyhbd.ttc" if bold else "msyh.ttc"),
        Path("/root/.fonts/msyh.ttc"),
        Path("/root/.fonts/msyhbd.ttc"),
        Path("/root/.fonts/MicrosoftYaHei/Microsoft Yahei.ttf"),
    ]
    windows_fonts = [
        Path("C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("C:/Windows/Fonts/simsun.ttc"),
    ]
    linux_fonts = [
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJKsc-Bold.otf" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJKjp-Bold.otf" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJKjp-Regular.otf"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJKkr-Bold.otf" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJKkr-Regular.otf"),
        Path("/usr/share/fonts/opentype/source-han-sans/SourceHanSansSC-Bold.otf" if bold else "/usr/share/fonts/opentype/source-han-sans/SourceHanSansSC-Regular.otf"),
        Path("/usr/share/fonts/opentype/source-han-sans/SourceHanSansCN-Bold.otf" if bold else "/usr/share/fonts/opentype/source-han-sans/SourceHanSansCN-Regular.otf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
        Path("/usr/share/fonts/truetype/arphic/uming.ttc"),
    ]
    return [*_configured_font_candidates(bold=bold), *packaged_fonts, *windows_fonts, *linux_fonts]


def _file_content_digest(file_path: Path) -> str:
    """流式计算文件内容摘要；缓存键不能只依赖可能被保留的 mtime 和文件大小。"""

    hasher = hashlib.sha256()
    with file_path.open("rb") as file_handle:
        while chunk := file_handle.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def _first_font_file_signature(*, bold: bool) -> tuple[object, ...]:
    """记录实际会选中的字体，防止换字体后继续命中旧卡片成品。"""

    for font_path in _font_candidates(bold=bold):
        try:
            stat = font_path.stat()
            if font_path.is_file():
                return str(font_path.resolve()), stat.st_size, _file_content_digest(font_path)
        except OSError:
            continue
    return ("pillow-default",)


@lru_cache(maxsize=1)
def _card_render_asset_signature() -> tuple[object, ...]:
    """汇总跨重启稳定的字体与 Emoji 指纹，作为磁盘成品缓存键的一部分。"""

    try:
        emoji_stat = NOTO_EMOJI_ZIP_PATH.stat()
        emoji_signature: tuple[object, ...] = (
            str(NOTO_EMOJI_ZIP_PATH.resolve()),
            emoji_stat.st_size,
            _file_content_digest(NOTO_EMOJI_ZIP_PATH),
        )
    except OSError:
        emoji_signature = ("emoji-missing",)
    return (
        _first_font_file_signature(bold=False),
        _first_font_file_signature(bold=True),
        emoji_signature,
    )


@lru_cache(maxsize=32)
def _load_font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    """加载指定字号字体；找不到中文字体时降级但不中断渲染。"""

    for font_path in _font_candidates(bold=bold):
        if not font_path.exists():
            continue
        try:
            return ImageFont.truetype(str(font_path), size=size)
        except Exception as error:
            logger.debug(f"RollPig Pillow 字体加载失败: path={font_path}, error={error}")

    logger.warning("RollPig Pillow 未找到可用中文字体，已退回 Pillow 默认字体。")
    return ImageFont.load_default()


# ================================ 文本测量与换行 ================================ #


def _measure_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> int:
    """按实际像素测量文本宽度；Emoji 使用 pilmoji 的贴图宽度计入。"""

    if not text:
        return 0

    if _has_emoji_candidate(text) and get_noto_emoji_source() is not None:
        try:
            render_text = _normalize_extra_emoji_symbols(text)
            width, _ = pilmoji_getsize(render_text, font=font, spacing=0, emoji_scale_factor=EMOJI_SCALE_FACTOR)
            return int(width)
        except Exception as error:
            logger.debug(f"RollPig pilmoji 文本测量失败，回退普通测量: text={text!r}, error={error}")

    return _measure_plain_text(draw, text, font)


def _measure_plain_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> int:
    """测量非 Emoji 文本；Pillow 版本差异导致 textlength 失败时回退 bbox。"""

    if not text:
        return 0
    try:
        return int(draw.textlength(text, font=font))
    except Exception:
        bbox = draw.textbbox((0, 0), text, font=font)
        return max(0, bbox[2] - bbox[0])


def _text_height(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> int:
    sample = text or "国"
    bbox = draw.textbbox((0, 0), sample, font=font)
    return max(1, bbox[3] - bbox[1])


def _font_line_top(font: ImageFont.ImageFont, y: int, line_height: int) -> int:
    """按字体整体指标定位行顶；正文多行用它固定基线，避免每行按内容 bbox 上下漂移。"""

    try:
        ascent, descent = font.getmetrics()
        font_height = ascent + descent
    except Exception:
        # Pillow 默认字体等少数对象没有 getmetrics；用中英文混合样本估一个稳定高度。
        probe = "国Hg"
        mask = font.getmask(probe)
        bbox = mask.getbbox() or (0, 0, 1, line_height)
        font_height = max(1, bbox[3] - bbox[1])
    return int(y + max(0, line_height - font_height) / 2)


_TOKEN_RE = re.compile(r"[A-Za-z0-9_./:+#@%-]+|[^\S\n]+|\n|.", re.S)


def _has_emoji_candidate(text: str) -> bool:
    """快速判断文本是否可能含 Emoji，避免普通中文卡片无谓加载 emoji 包。"""

    if any(symbol in text for symbol in EXTRA_EMOJI_SYMBOLS):
        return True
    if any(mark in text for mark in ("\ufe0f", "\u200d", "\u20e3")):
        return True
    return any(
        0x1F000 <= ord(char) <= 0x1FAFF
        or 0x2600 <= ord(char) <= 0x27BF
        or 0x2B00 <= ord(char) <= 0x2BFF
        or ord(char) in (0x00A9, 0x00AE, 0x3030, 0x303D, 0x3297, 0x3299)
        for char in text
    )


def _normalize_extra_emoji_symbols(text: str) -> str:
    """把少量 emoji 风格文本符号转换为 Noto Emoji 可识别字符。"""

    if not text or not any(symbol in text for symbol in EXTRA_EMOJI_SYMBOLS):
        return text
    return "".join(EXTRA_EMOJI_SYMBOLS.get(char, char) for char in text)


@lru_cache(maxsize=4096)
def _emoji_spans(text: str) -> tuple[tuple[int, int], ...]:
    """返回文本中的 Emoji 片段范围；与 pilmoji 使用同一套解析规则。"""

    if not _has_emoji_candidate(text):
        return ()

    normalized_text = _normalize_extra_emoji_symbols(text)
    try:
        spans = [match.span() for match in EMOJI_REGEX.finditer(normalized_text)]
    except Exception as error:
        logger.debug(f"RollPig Emoji 分段失败，回退普通文字: text={text!r}, error={error}")
        return ()

    # 防御第三方库异常返回，避免错位范围破坏中文切片。
    valid_spans: list[tuple[int, int]] = []
    last_end = 0
    for start, end in sorted(spans):
        if 0 <= start < end <= len(text) and start >= last_end:
            valid_spans.append((start, end))
            last_end = end
    return tuple(valid_spans)


def _tokenize_text(text: str) -> list[str]:
    """中文逐字、英文数字成词；Emoji 按完整簇成词，避免换行切碎。"""

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    spans = _emoji_spans(normalized)
    if not spans:
        return _TOKEN_RE.findall(normalized)

    tokens: list[str] = []
    cursor = 0
    for start, end in spans:
        if start > cursor:
            tokens.extend(_TOKEN_RE.findall(normalized[cursor:start]))
        tokens.append(normalized[start:end])
        cursor = end
    if cursor < len(normalized):
        tokens.extend(_TOKEN_RE.findall(normalized[cursor:]))
    return tokens


def _drop_last_text_unit(text: str) -> str:
    """删除最后一个显示单元；末尾是 Emoji 簇时整簇删除，避免截断出残缺符号。"""

    if not text:
        return ""

    spans = _emoji_spans(text)
    if spans and spans[-1][1] == len(text):
        return text[:spans[-1][0]]
    return text[:-1]


def _truncate_to_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
) -> str:
    """把单行文本收敛到给定宽度，末尾使用中文省略号。"""

    if _measure_text(draw, text, font) <= max_width:
        return text

    ellipsis = "…"
    result = text
    while result and _measure_text(draw, f"{result}{ellipsis}", font) > max_width:
        result = _drop_last_text_unit(result)
    return f"{result}{ellipsis}" if result else ellipsis


def _append_ellipsis_to_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
) -> str:
    """强制给截断行追加省略号，并保证追加后仍不超出最大宽度。"""

    ellipsis = "…"
    result = text.rstrip()
    while result and _measure_text(draw, f"{result}{ellipsis}", font) > max_width:
        result = _drop_last_text_unit(result)
    return f"{result}{ellipsis}" if result else ellipsis


def _wrap_text_by_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
    *,
    max_lines: int | None = None,
) -> list[str]:
    """按像素宽度换行；超过最大行数时在最后一行省略。"""

    if not text:
        return []

    lines: list[str] = []
    current = ""

    def push_line(line: str) -> bool:
        lines.append(line.rstrip())
        return max_lines is not None and len(lines) >= max_lines

    for token in _tokenize_text(text):
        if token == "\n":
            if push_line(current):
                break
            current = ""
            continue

        candidate = f"{current}{token}"
        if current and _measure_text(draw, candidate.rstrip(), font) > max_width:
            if push_line(current):
                break
            current = token.lstrip()
            continue
        current = candidate
    else:
        if current or not lines:
            push_line(current)

    if max_lines is not None and len(lines) >= max_lines:
        consumed = "".join(lines)
        source_compact = text.replace("\n", "")
        if len(consumed) < len(source_compact):
            lines[-1] = _append_ellipsis_to_width(draw, lines[-1].rstrip(), font, max_width)

    return lines


# ================================ 图片与布局生成 ================================ #


def _make_canvas() -> Image.Image:
    """创建满版白底卡片画布；外框不再做透明裁切、描边或圆角，避免产生内收感。"""

    return Image.new("RGBA", CANVAS_SIZE, BACKGROUND_COLOR)


def _load_avatar(
    image_file: Path | None,
    *,
    cache_source_avatar: bool,
) -> Image.Image | None:
    """载入并裁切小猪图；只有动态文案才保留源头像，固定卡冷渲染后即可释放。"""

    if image_file is None:
        return None

    if not cache_source_avatar:
        return _load_avatar_file(image_file)

    try:
        stat = image_file.stat()
        content_digest = _file_content_digest(image_file)
    except OSError as error:
        logger.warning(f"RollPig 小猪图片状态读取失败，使用占位图: file={image_file}, error={error}")
        return None

    cached = _load_avatar_cached(str(image_file), stat.st_size, content_digest)
    return cached.copy() if cached is not None else None


def _fit_avatar_frame(frame: Image.Image) -> Image.Image:
    """把任意来源图片统一规整为 240×240 RGBA 头像帧。"""

    frame = ImageOps.exif_transpose(frame)
    if frame.mode not in ("RGBA", "LA"):
        frame = frame.convert("RGBA")
    else:
        frame = frame.convert("RGBA")

    return ImageOps.fit(
        frame,
        (AVATAR_SIZE, AVATAR_SIZE),
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    )


@lru_cache(maxsize=AVATAR_CACHE_MAXSIZE)
def _load_avatar_cached(path: str, file_size: int, content_digest: str) -> Image.Image | None:
    """读取并缩放头像资源；大小和摘要参数只用于构成 LRU 缓存失效键。"""

    return _load_avatar_file(Path(path))


def _load_avatar_file(image_file: Path) -> Image.Image | None:
    """直接读取一个静态头像；调用方决定结果是否进入进程内 LRU。"""

    try:
        with Image.open(image_file) as opened:
            frame = opened.copy()
    except Exception as error:
        logger.warning(f"RollPig 小猪图片读取失败，使用占位图: file={image_file}, error={error}")
        return None

    return _fit_avatar_frame(frame)


def _normalize_gif_duration(raw_duration: object) -> int:
    """规整 GIF 帧间隔；防御 0ms/异常值让客户端播放过快或卡住。"""

    try:
        duration = int(raw_duration)
    except (TypeError, ValueError):
        duration = GIF_FALLBACK_FRAME_DURATION_MS
    if duration <= 0:
        duration = GIF_FALLBACK_FRAME_DURATION_MS
    return min(max(duration, GIF_MIN_FRAME_DURATION_MS), GIF_MAX_FRAME_DURATION_MS)


def _card_text_values(pig_data: Mapping[str, Any]) -> tuple[str, str, str]:
    """统一卡片实际绘制文案，保证缓存键与渲染输入完全一致。"""

    return (
        str(pig_data.get("name") or "未知小猪"),
        str(pig_data.get("description") or ""),
        str(pig_data.get("analysis") or "你今天是只神秘小猪。"),
    )


def _card_image_signature(image_file: Path | None) -> tuple[object, ...] | None:
    """生成源图内容指纹；同名资源被覆盖且时间戳不变时也必须让旧成品失效。"""

    if image_file is None:
        # 缺图卡片也可以缓存；资源补齐后 image_file 会变为真实路径，键自然改变。
        return ("image-missing",)
    try:
        stat = image_file.stat()
        if not image_file.is_file():
            return None
        return (
            str(image_file.resolve()),
            stat.st_size,
            _file_content_digest(image_file),
        )
    except OSError:
        # 文件正被资源更新流程替换时放弃缓存，本次仍可走渲染回退。
        return None


def _gif_file_signature(image_file: Path | None) -> tuple[object, ...] | None:
    """返回 GIF 内容指纹，供动态卡片的源帧缓存和同键任务复用。"""

    if image_file is None or image_file.suffix.lower() != ".gif":
        return None
    return _card_image_signature(image_file)


def _fixed_card_cache_key(
    pig_data: Mapping[str, Any],
    image_file: Path | None,
    *,
    require_avatar: bool = False,
) -> tuple[object, ...] | None:
    """生成固定卡片键；严格头像模式必须与可显示占位图的普通模式隔离。"""

    signature = _card_image_signature(image_file)
    if signature is None:
        return None
    return (
        CARD_CACHE_VERSION,
        *signature,
        _card_render_asset_signature(),
        *_card_text_values(pig_data),
        "require-avatar",
        bool(require_avatar),
    )


def _fixed_card_cache_path(key: tuple[object, ...]) -> Path:
    """把完整渲染输入摘要成稳定文件名，避免路径和中文直接进入缓存文件名。"""

    serialized = json.dumps(key, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    digest = hashlib.sha256(serialized).hexdigest()
    return CARD_CACHE_DIR / f"v{CARD_CACHE_VERSION}-{digest}.cache"


def _serialize_card_disk_cache(result: PigCardRenderResult) -> bytes:
    """把成品图与渲染指标写入单个缓存文件，避免图片和 sidecar 元数据不同步。"""

    header = json.dumps(
        {
            "image_format": result.image_format,
            "renderer": result.renderer,
            "analysis_font_size": result.analysis_font_size,
            "analysis_lines": result.analysis_lines,
            "emoji_enabled": result.emoji_enabled,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return CARD_DISK_CACHE_MAGIC + len(header).to_bytes(4, "big") + header + result.data


def _deserialize_card_disk_cache(payload: bytes) -> PigCardRenderResult:
    """校验并读取本插件生成的 PNG/GIF 缓存容器；损坏文件由调用方丢弃。"""

    prefix_size = len(CARD_DISK_CACHE_MAGIC)
    if not payload.startswith(CARD_DISK_CACHE_MAGIC) or len(payload) < prefix_size + 4:
        raise ValueError("缓存魔数不匹配")

    header_size = int.from_bytes(payload[prefix_size : prefix_size + 4], "big")
    if not 0 < header_size <= CARD_DISK_CACHE_HEADER_MAX_BYTES:
        raise ValueError(f"缓存头长度非法: {header_size}")

    header_start = prefix_size + 4
    data_start = header_start + header_size
    if data_start >= len(payload):
        raise ValueError("缓存缺少图片数据")

    metadata = json.loads(payload[header_start:data_start].decode("utf-8"))
    if not isinstance(metadata, dict):
        raise ValueError("缓存头不是 object")
    image_data = payload[data_start:]
    if image_data.startswith((b"GIF87a", b"GIF89a")):
        detected_format = "gif"
    elif image_data.startswith(b"\x89PNG\r\n\x1a\n"):
        detected_format = "png"
    else:
        raise ValueError("缓存正文不是 PNG 或 GIF")

    metadata_format = str(metadata.get("image_format") or detected_format).lower()
    if metadata_format != detected_format:
        raise ValueError(f"缓存格式不一致: metadata={metadata_format}, data={detected_format}")

    return PigCardRenderResult(
        data=image_data,
        image_format=detected_format,
        renderer=str(metadata.get("renderer") or f"pillow-{detected_format}"),
        analysis_font_size=int(metadata.get("analysis_font_size") or 0),
        analysis_lines=int(metadata.get("analysis_lines") or 0),
        emoji_enabled=bool(metadata.get("emoji_enabled")),
    )


def _remove_invalid_card_disk_cache(cache_file: Path, error: Exception) -> None:
    """删除单个损坏缓存；删除失败不影响回退到即时渲染。"""

    logger.warning(f"RollPig 卡片磁盘缓存损坏，已忽略: file={cache_file}, error={error}")
    try:
        cache_file.unlink(missing_ok=True)
    except OSError:
        pass


def _get_fixed_card(key: tuple[object, ...]) -> PigCardRenderResult | None:
    """从磁盘读取固定卡片成品，并用 mtime 维护近似 LRU。"""

    cache_file = _fixed_card_cache_path(key)
    with _card_cache_lock:
        try:
            file_size = cache_file.stat().st_size
            if file_size > CARD_DISK_CACHE_MAX_BYTES:
                raise ValueError(f"缓存文件超过总容量: {file_size}")
            result = _deserialize_card_disk_cache(cache_file.read_bytes())
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as error:
            _remove_invalid_card_disk_cache(cache_file, error)
            return None

        try:
            os.utime(cache_file, None)
        except OSError:
            # 命中时间只用于淘汰顺序；只读文件系统仍可正常读取已有缓存。
            pass
        return result


def _card_disk_cache_files() -> list[tuple[Path, int, int]]:
    """列出磁盘缓存的路径、大小和 mtime；无法读取的条目交给后续自然淘汰。"""

    entries: list[tuple[Path, int, int]] = []
    try:
        candidates = CARD_CACHE_DIR.glob("*.cache")
        for path in candidates:
            try:
                stat = path.stat()
            except OSError:
                continue
            entries.append((path, stat.st_size, stat.st_mtime_ns))
    except OSError:
        return []
    return entries


def _trim_card_disk_cache() -> None:
    """按最近使用时间把磁盘成品清到 64 MiB 内；条目数量不设上限。"""

    entries = _card_disk_cache_files()
    total_bytes = sum(size for _, size, _ in entries)
    if total_bytes <= CARD_DISK_CACHE_MAX_BYTES:
        return

    for path, size, _ in sorted(entries, key=lambda item: item[2]):
        if total_bytes <= CARD_DISK_CACHE_MAX_BYTES:
            break
        try:
            path.unlink(missing_ok=True)
        except OSError:
            continue
        total_bytes -= size


def _store_fixed_card(key: tuple[object, ...], result: PigCardRenderResult) -> bool:
    """原子写入固定卡片成品；缓存不可写时只影响加速，不影响本次响应。"""

    is_valid_gif = result.image_format == "gif" and result.data.startswith((b"GIF87a", b"GIF89a"))
    is_valid_png = result.image_format == "png" and result.data.startswith(b"\x89PNG\r\n\x1a\n")
    if not (is_valid_gif or is_valid_png):
        return False

    payload = _serialize_card_disk_cache(result)
    if len(payload) > CARD_DISK_CACHE_MAX_BYTES:
        logger.warning(
            "RollPig 卡片成品超过磁盘缓存总上限，本次不缓存: "
            f"bytes={len(payload)}/{CARD_DISK_CACHE_MAX_BYTES}"
        )
        return False

    cache_file = _fixed_card_cache_path(key)
    temporary_file = cache_file.with_name(
        f".{cache_file.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        with _card_cache_lock:
            CARD_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            temporary_file.write_bytes(payload)
            os.replace(temporary_file, cache_file)
            _trim_card_disk_cache()
    except OSError as error:
        logger.warning(f"RollPig 卡片磁盘缓存写入失败，本次继续发送即时结果: file={cache_file}, error={error}")
        try:
            temporary_file.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True


def _avatar_frames_bytes(frames: tuple[tuple[Image.Image, int], ...]) -> int:
    """按解码像素缓冲区估算头像帧真实内存，不使用压缩文件大小。"""

    return sum(frame.width * frame.height * len(frame.getbands()) for frame, _ in frames)


def _get_source_gif_frames(
    key: tuple[object, ...],
) -> tuple[tuple[Image.Image, int], ...] | None:
    """读取动态文案使用的源帧 LRU；调用方只读使用缓存帧。"""

    with _card_cache_lock:
        cached = _source_gif_frame_cache.pop(key, None)
        if cached is not None:
            _source_gif_frame_cache[key] = cached
        return cached


def _store_source_gif_frames(
    key: tuple[object, ...],
    frames: tuple[tuple[Image.Image, int], ...],
) -> bool:
    """把动态文案所需头像帧放入字节受限 LRU，返回是否由缓存接管。"""

    global _source_gif_frame_cache_bytes

    frame_bytes = _avatar_frames_bytes(frames)
    if not frames or frame_bytes > GIF_SOURCE_CACHE_MAX_BYTES:
        return False

    with _card_cache_lock:
        previous = _source_gif_frame_cache.pop(key, None)
        if previous is not None:
            _source_gif_frame_cache_bytes -= _avatar_frames_bytes(previous)
        _source_gif_frame_cache[key] = frames
        _source_gif_frame_cache_bytes += frame_bytes

        while (
            len(_source_gif_frame_cache) > GIF_SOURCE_CACHE_MAX_ENTRIES
            or _source_gif_frame_cache_bytes > GIF_SOURCE_CACHE_MAX_BYTES
        ):
            _, evicted = _source_gif_frame_cache.popitem(last=False)
            _source_gif_frame_cache_bytes -= _avatar_frames_bytes(evicted)
    return True


def _get_gif_render_semaphore() -> asyncio.Semaphore:
    """每个事件循环创建一个内置双并发锁，限制多个首次生成峰值叠加。"""

    global _gif_render_semaphore, _gif_render_semaphore_loop

    loop = asyncio.get_running_loop()
    with _gif_render_semaphore_guard:
        if _gif_render_semaphore is None or _gif_render_semaphore_loop is not loop:
            _gif_render_semaphore = asyncio.Semaphore(GIF_RENDER_CONCURRENCY)
            _gif_render_semaphore_loop = loop
        return _gif_render_semaphore


def _gif_frame_groups(frame_count: int) -> tuple[tuple[int, int, int], ...]:
    """把完整动画均匀分组到目标上限，返回每组起点、终点和代表帧。"""

    output_count = min(frame_count, GIF_TARGET_FRAMES)
    groups: list[tuple[int, int, int]] = []
    for output_index in range(output_count):
        start = output_index * frame_count // output_count
        end = (output_index + 1) * frame_count // output_count
        # 选分组中点而不是固定取前 40 帧，确保动画末尾也被保留。
        representative = (start + end) // 2
        groups.append((start, end, representative))
    return tuple(groups)


def _decode_animated_avatar_frames(image_file: Path) -> tuple[tuple[Image.Image, int], ...]:
    """流式读取 GIF，只保留均匀抽样后的头像帧，并把被合并帧的时长累加。"""

    decoded: list[Image.Image] = []
    try:
        with Image.open(image_file) as opened:
            frame_count = int(getattr(opened, "n_frames", 1) or 1)
            if not getattr(opened, "is_animated", False) or frame_count <= 1:
                return ()
            width, height = opened.size
            decode_work_pixels = width * height * frame_count
            if frame_count > GIF_ABSOLUTE_MAX_SOURCE_FRAMES:
                logger.warning(
                    "RollPig GIF 超过绝对源帧兜底，已降级为静态首帧: "
                    f"file={image_file}, frames={frame_count}/{GIF_ABSOLUTE_MAX_SOURCE_FRAMES}"
                )
                return ()
            if decode_work_pixels > GIF_MAX_DECODE_WORK_PIXELS:
                logger.warning(
                    "RollPig GIF 超过解码工作量预算，已降级为静态首帧: "
                    f"file={image_file}, size={width}x{height}, frames={frame_count}, "
                    f"pixel_frames={decode_work_pixels}/{GIF_MAX_DECODE_WORK_PIXELS}"
                )
                return ()

            groups = _gif_frame_groups(frame_count)
            durations = [0] * len(groups)
            group_index = 0
            for index, frame in enumerate(ImageSequence.Iterator(opened)):
                while group_index + 1 < len(groups) and index >= groups[group_index][1]:
                    group_index += 1
                duration = _normalize_gif_duration(frame.info.get("duration", opened.info.get("duration")))
                durations[group_index] += duration
                if index != groups[group_index][2]:
                    continue

                source_frame = frame.copy()
                try:
                    decoded.append(_fit_avatar_frame(source_frame))
                finally:
                    source_frame.close()

            if len(decoded) != len(groups):
                raise ValueError(f"GIF 抽样帧数量异常: decoded={len(decoded)}, expected={len(groups)}")
            if frame_count > len(groups):
                logger.info(
                    "RollPig GIF 已在完整周期内均匀抽帧: "
                    f"file={image_file}, frames={frame_count}->{len(groups)}"
                )
            return tuple(zip(decoded, durations))
    except Exception as error:
        for frame in decoded:
            frame.close()
        logger.warning(f"RollPig GIF 图片读取失败，回退静态渲染: file={image_file}, error={error}")
        return ()


def _load_animated_avatar_frames(
    image_file: Path | None,
    *,
    cache_source_frames: bool,
) -> tuple[tuple[tuple[Image.Image, int], ...], bool]:
    """载入 GIF 头像帧，并返回帧是否已由动态源帧缓存接管。"""

    signature = _gif_file_signature(image_file)
    if signature is None or image_file is None:
        return (), False

    if cache_source_frames:
        cached = _get_source_gif_frames(signature)
        if cached is not None:
            return cached, True

    frames = _decode_animated_avatar_frames(image_file)
    retained = cache_source_frames and _store_source_gif_frames(signature, frames)
    return frames, retained


@lru_cache(maxsize=8)
def _avatar_corner_mask() -> Image.Image:
    """生成抗锯齿圆角蒙版；透明小猪图不受影响，实底图片会获得柔和圆角。"""

    scale = 3
    mask = Image.new("L", (AVATAR_SIZE * scale, AVATAR_SIZE * scale), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle(
        (0, 0, AVATAR_SIZE * scale, AVATAR_SIZE * scale),
        radius=AVATAR_CORNER_RADIUS * scale,
        fill=255,
    )
    return mask.resize((AVATAR_SIZE, AVATAR_SIZE), Image.Resampling.LANCZOS)


def _prepare_avatar_for_draw(avatar: Image.Image) -> Image.Image:
    """给头像套圆角边界；使用 ImageChops 保留原始透明通道。"""

    prepared = avatar.copy()
    alpha = prepared.getchannel("A")
    prepared.putalpha(ImageChops.multiply(alpha, _avatar_corner_mask()))
    return prepared


def _draw_avatar(canvas: Image.Image, avatar: Image.Image | None, y: int) -> None:
    x = (CANVAS_SIZE[0] - AVATAR_SIZE) // 2
    if avatar is not None:
        prepared_avatar = _prepare_avatar_for_draw(avatar)
        try:
            canvas.alpha_composite(prepared_avatar, (x, y))
        finally:
            prepared_avatar.close()
        return

    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle(
        (x, y, x + AVATAR_SIZE, y + AVATAR_SIZE),
        radius=36,
        fill=PLACEHOLDER_BG,
    )
    placeholder_font = _load_font(86, bold=True)
    _draw_text_line(
        canvas,
        "猪",
        placeholder_font,
        y + (AVATAR_SIZE - 100) // 2,
        100,
        PLACEHOLDER_FG,
        max_width=AVATAR_SIZE,
    )


def _build_text_layout(
    draw: ImageDraw.ImageDraw,
    *,
    name: str,
    desc: str,
    analysis: str,
) -> _TextLayout:
    """计算普通卡片排版；分析正文优先缩字号，最后才省略。"""

    name_font = _load_font(NAME_FONT_SIZE, bold=True)
    desc_font = _load_font(DESC_FONT_SIZE)

    name_line = _truncate_to_width(draw, name, name_font, CONTENT_WIDTH)
    desc_line = _truncate_to_width(draw, desc, desc_font, CONTENT_WIDTH) if desc else ""

    static_height = AVATAR_SIZE + NAME_MARGIN_TOP + NAME_LINE_HEIGHT
    if desc_line:
        static_height += DESC_MARGIN_TOP + DESC_LINE_HEIGHT
    if analysis:
        static_height += ANALYSIS_MARGIN_TOP

    for analysis_font_size in range(ANALYSIS_FONT_MAX_SIZE, ANALYSIS_FONT_MIN_SIZE - 1, -1):
        analysis_font = _load_font(analysis_font_size)
        analysis_line_height = round(analysis_font_size * ANALYSIS_LINE_HEIGHT_FACTOR)
        analysis_lines = _wrap_text_by_width(draw, analysis, analysis_font, CONTENT_WIDTH) if analysis else []
        total_height = static_height + len(analysis_lines) * analysis_line_height
        if total_height <= CONTENT_SAFE_HEIGHT:
            return _TextLayout(
                name_line=name_line,
                desc_line=desc_line,
                analysis_lines=analysis_lines,
                analysis_font=analysis_font,
                analysis_font_size=analysis_font_size,
                analysis_line_height=analysis_line_height,
                total_height=total_height,
            )

    analysis_font = _load_font(ANALYSIS_FONT_MIN_SIZE)
    analysis_line_height = round(ANALYSIS_FONT_MIN_SIZE * ANALYSIS_LINE_HEIGHT_FACTOR)
    available_height = max(analysis_line_height, CONTENT_SAFE_HEIGHT - static_height)
    max_lines = max(1, available_height // analysis_line_height)
    analysis_lines = _wrap_text_by_width(
        draw,
        analysis,
        analysis_font,
        CONTENT_WIDTH,
        max_lines=max_lines,
    ) if analysis else []
    total_height = static_height + len(analysis_lines) * analysis_line_height
    return _TextLayout(
        name_line=name_line,
        desc_line=desc_line,
        analysis_lines=analysis_lines,
        analysis_font=analysis_font,
        analysis_font_size=ANALYSIS_FONT_MIN_SIZE,
        analysis_line_height=analysis_line_height,
        total_height=total_height,
    )


# ================================ 绘制与对外入口 ================================ #


def _draw_text_line(
    canvas: Image.Image,
    text: str,
    font: ImageFont.ImageFont,
    y: int,
    line_height: int,
    fill: tuple[int, int, int, int],
    *,
    max_width: int,
    align_by_baseline: bool = False,
) -> None:
    """水平居中绘制单行文本；正文可按统一基线绘制，避免混合字符导致行距视觉漂移。"""

    if not text:
        return

    draw = ImageDraw.Draw(canvas)
    width = min(_measure_text(draw, text, font), max_width)
    x = (CANVAS_SIZE[0] - width) // 2

    if _has_emoji_candidate(text):
        source = get_noto_emoji_source()
        if source is not None:
            try:
                render_text = _normalize_extra_emoji_symbols(text)
                _, measured_height = pilmoji_getsize(
                    render_text,
                    font=font,
                    spacing=0,
                    emoji_scale_factor=EMOJI_SCALE_FACTOR,
                )
                text_y = _font_line_top(font, y, line_height) if align_by_baseline else int(y + (line_height - measured_height) / 2)
                with Pilmoji(
                    canvas,
                    source=source,
                    render_discord_emoji=False,
                    emoji_scale_factor=EMOJI_SCALE_FACTOR,
                    emoji_position_offset=EMOJI_POSITION_OFFSET,
                ) as pilmoji:
                    pilmoji.text(
                        (x, text_y),
                        render_text,
                        fill=fill,
                        font=font,
                        emoji_scale_factor=EMOJI_SCALE_FACTOR,
                        emoji_position_offset=EMOJI_POSITION_OFFSET,
                    )
                return
            except Exception as error:
                logger.debug(f"RollPig pilmoji 文本绘制失败，回退普通绘制: text={text!r}, error={error}")

    bbox = draw.textbbox((0, 0), text, font=font)
    text_h = max(1, bbox[3] - bbox[1])
    if align_by_baseline:
        text_y = _font_line_top(font, y, line_height)
    else:
        text_y = int(y + (line_height - text_h) / 2 - bbox[1])
    draw.text((x, text_y), text, fill=fill, font=font)


def _prepare_card_without_avatar(pig_data: Mapping[str, Any]) -> _PreparedCard:
    """生成不含头像的静态卡片层；动态头像逐帧贴在同一位置。"""

    name, desc, analysis = _card_text_values(pig_data)

    canvas = _make_canvas()
    draw = ImageDraw.Draw(canvas)
    layout = _build_text_layout(draw, name=name, desc=desc, analysis=analysis)

    start_y = max(20, (CANVAS_SIZE[1] - layout.total_height) // 2)
    y = start_y + AVATAR_SIZE + NAME_MARGIN_TOP

    name_font = _load_font(NAME_FONT_SIZE, bold=True)
    _draw_text_line(canvas, layout.name_line, name_font, y, NAME_LINE_HEIGHT, NAME_COLOR, max_width=CONTENT_WIDTH)
    y += NAME_LINE_HEIGHT

    if layout.desc_line:
        y += DESC_MARGIN_TOP
        desc_font = _load_font(DESC_FONT_SIZE)
        _draw_text_line(canvas, layout.desc_line, desc_font, y, DESC_LINE_HEIGHT, DESC_COLOR, max_width=CONTENT_WIDTH)
        y += DESC_LINE_HEIGHT

    if layout.analysis_lines:
        y += ANALYSIS_MARGIN_TOP
        for line in layout.analysis_lines:
            _draw_text_line(
                canvas,
                line,
                layout.analysis_font,
                y,
                layout.analysis_line_height,
                ANALYSIS_COLOR,
                max_width=CONTENT_WIDTH,
                align_by_baseline=True,
            )
            y += layout.analysis_line_height

    contains_emoji = any(
        _has_emoji_candidate(text)
        for text in (layout.name_line, layout.desc_line, *layout.analysis_lines)
    )
    return _PreparedCard(
        canvas=canvas,
        layout=layout,
        avatar_y=start_y,
        emoji_enabled=contains_emoji and get_noto_emoji_source() is not None,
    )


def _encode_png_card(
    prepared: _PreparedCard,
    image_file: Path | None,
    *,
    cache_source_avatar: bool,
    require_avatar: bool,
) -> PigCardRenderResult:
    """把静态卡片编码为 PNG；静态 GIF 也会走这里取首帧。"""

    canvas = prepared.canvas.copy()
    avatar = _load_avatar(image_file, cache_source_avatar=cache_source_avatar)
    try:
        if avatar is None and require_avatar and image_file is not None:
            raise PigCardImageLoadError(f"指定头像无法读取: {image_file}")
        _draw_avatar(canvas, avatar, prepared.avatar_y)
        rgb_canvas = canvas.convert("RGB")
        try:
            output = BytesIO()
            rgb_canvas.save(output, format="PNG", optimize=True)
        finally:
            rgb_canvas.close()
    finally:
        if avatar is not None:
            avatar.close()
        canvas.close()
    return PigCardRenderResult(
        data=output.getvalue(),
        image_format="png",
        renderer="pillow",
        analysis_font_size=prepared.layout.analysis_font_size,
        analysis_lines=len(prepared.layout.analysis_lines),
        emoji_enabled=prepared.emoji_enabled,
    )


def _build_gif_palette(
    prepared: _PreparedCard,
    avatar_frames: tuple[tuple[Image.Image, int], ...],
) -> Image.Image:
    """用小尺寸样本构造全局调色板，避免为了取色常驻整批 800×800 RGB 帧。"""

    sample_size = GIF_PALETTE_SAMPLE_SIZE
    palette_source = Image.new(
        "RGB",
        (sample_size, sample_size * (len(avatar_frames) + 1)),
        BACKGROUND_COLOR[:3],
    )

    # 第一块放静态卡片缩略图，确保文字、白底和 Emoji 色彩进入全局调色板。
    static_rgb = prepared.canvas.convert("RGB")
    try:
        full_card_sample = static_rgb.resize((sample_size, sample_size), Image.Resampling.LANCZOS)
        try:
            palette_source.paste(full_card_sample, (0, 0))
        finally:
            full_card_sample.close()
    finally:
        static_rgb.close()

    # 后续只在 240×240 头像区合成取样；逐帧释放临时图，不建立整卡 RGB 列表。
    avatar_box = (
        (CANVAS_SIZE[0] - AVATAR_SIZE) // 2,
        prepared.avatar_y,
        (CANVAS_SIZE[0] + AVATAR_SIZE) // 2,
        prepared.avatar_y + AVATAR_SIZE,
    )
    avatar_background = prepared.canvas.crop(avatar_box)
    try:
        for index, (avatar, _) in enumerate(avatar_frames, 1):
            sample = avatar_background.copy()
            prepared_avatar = _prepare_avatar_for_draw(avatar)
            try:
                sample.alpha_composite(prepared_avatar, (0, 0))
            finally:
                prepared_avatar.close()

            sample_rgb = sample.convert("RGB")
            sample.close()
            resized = sample_rgb.resize((sample_size, sample_size), Image.Resampling.LANCZOS)
            sample_rgb.close()
            try:
                palette_source.paste(resized, (0, sample_size * index))
            finally:
                resized.close()
    finally:
        avatar_background.close()

    try:
        return palette_source.quantize(colors=256, method=Image.Quantize.MEDIANCUT)
    finally:
        palette_source.close()


def _encode_gif_card(prepared: _PreparedCard, avatar_frames: tuple[tuple[Image.Image, int], ...]) -> PigCardRenderResult:
    """逐帧合成并立即量化，仅保留单字节索引帧，压低首次生成峰值。"""

    palette = _build_gif_palette(prepared, avatar_frames)
    output_frames: list[Image.Image] = []
    durations: list[int] = []
    try:
        for avatar, duration in avatar_frames:
            frame = prepared.canvas.copy()
            try:
                _draw_avatar(frame, avatar, prepared.avatar_y)
                rgb_frame = frame.convert("RGB")
                try:
                    output_frames.append(rgb_frame.quantize(palette=palette, dither=Image.Dither.NONE))
                finally:
                    rgb_frame.close()
            finally:
                frame.close()
            durations.append(duration)

        output = BytesIO()
        output_frames[0].save(
            output,
            format="GIF",
            save_all=True,
            append_images=output_frames[1:],
            duration=durations,
            loop=0,
            disposal=2,
            optimize=False,
        )
        result_data = output.getvalue()
    finally:
        palette.close()
        for frame in output_frames:
            frame.close()
    return PigCardRenderResult(
        data=result_data,
        image_format="gif",
        renderer="pillow-gif",
        analysis_font_size=prepared.layout.analysis_font_size,
        analysis_lines=len(prepared.layout.analysis_lines),
        emoji_enabled=prepared.emoji_enabled,
    )


def _render_pig_card_image_sync(
    pig_data: Mapping[str, Any],
    image_file: Path | None,
    *,
    cache_final_card: bool = True,
    require_avatar: bool = False,
    _final_cache_key: tuple[object, ...] | None = None,
) -> PigCardRenderResult:
    """同步生成卡片；固定文案缓存最终成品，动态文案 GIF 仅缓存受限源帧。"""

    final_cache_key = _final_cache_key
    if cache_final_card and final_cache_key is None:
        final_cache_key = _fixed_card_cache_key(
            pig_data,
            image_file,
            require_avatar=require_avatar,
        )
    if final_cache_key is not None:
        cached = _get_fixed_card(final_cache_key)
        if cached is not None:
            return cached

    prepared = _prepare_card_without_avatar(pig_data)
    avatar_frames: tuple[tuple[Image.Image, int], ...] = ()
    frames_retained = False
    try:
        avatar_frames, frames_retained = _load_animated_avatar_frames(
            image_file,
            cache_source_frames=not cache_final_card,
        )
        result = (
            _encode_gif_card(prepared, avatar_frames)
            if avatar_frames
            else _encode_png_card(
                prepared,
                image_file,
                cache_source_avatar=not cache_final_card,
                require_avatar=require_avatar,
            )
        )
    finally:
        prepared.canvas.close()
        if avatar_frames and not frames_retained:
            for frame, _ in avatar_frames:
                frame.close()

    if final_cache_key is not None:
        # 资源同步可能在渲染期间切换 active 目录；只把结果写回仍与当前源图一致的键，
        # 避免极短竞态把新图片成品记到旧图片摘要名下。
        current_cache_key = _fixed_card_cache_key(
            pig_data,
            image_file,
            require_avatar=require_avatar,
        )
        if current_cache_key == final_cache_key:
            _store_fixed_card(final_cache_key, result)
    return result


def _read_fixed_card_cache(
    pig_data: Mapping[str, Any],
    image_file: Path | None,
    *,
    require_avatar: bool = False,
) -> tuple[tuple[object, ...] | None, PigCardRenderResult | None]:
    """在线程中计算源图摘要并查磁盘，避免文件 I/O 阻塞 NoneBot 事件循环。"""

    cache_key = _fixed_card_cache_key(
        pig_data,
        image_file,
        require_avatar=require_avatar,
    )
    cached = _get_fixed_card(cache_key) if cache_key is not None else None
    return cache_key, cached


async def render_pig_card_image(
    pig_data: Mapping[str, Any],
    image_file: Path | None,
    *,
    cache_final_card: bool = True,
    cache_final_gif: bool | None = None,
    require_avatar: bool = False,
) -> PigCardRenderResult:
    """异步入口：固定卡片查磁盘；首次 GIF 双并发，同一成品只生成一次。"""

    if cache_final_gif is not None:
        # 兼容 0.9.0 预发布代码中的旧关键字；新代码统一使用更准确的 card 命名。
        cache_final_card = cache_final_gif

    final_cache_key: tuple[object, ...] | None = None
    if cache_final_card:
        final_cache_key, cached = await asyncio.to_thread(
            _read_fixed_card_cache,
            pig_data,
            image_file,
            require_avatar=require_avatar,
        )
        if cached is not None:
            return cached

    is_gif = image_file is not None and image_file.suffix.lower() == ".gif"
    if final_cache_key is not None or is_gif:
        if final_cache_key is not None:
            task_key: tuple[object, ...] = ("fixed-card", *final_cache_key)
        else:
            # 动态 GIF 不写最终成品，但同一文案的并发请求仍可共享这一次生成。
            signature = await asyncio.to_thread(_gif_file_signature, image_file)
            task_key = (
                "dynamic-card" if not cache_final_card else "uncached-card",
                *(signature or (str(image_file),)),
                *_card_text_values(pig_data),
                "require-avatar",
                bool(require_avatar),
            )

        async with _card_render_tasks_lock:
            render_task = _card_render_tasks.get(task_key)
            if render_task is None:
                render_task = asyncio.create_task(
                    _render_card_once(
                        task_key,
                        pig_data,
                        image_file,
                        cache_final_card=cache_final_card,
                        require_avatar=require_avatar,
                        final_cache_key=final_cache_key,
                        limit_gif=is_gif,
                    )
                )
                _card_render_tasks[task_key] = render_task
        # 调用方取消时不取消共享任务，其他等待相同卡片的请求仍可获得结果。
        return await asyncio.shield(render_task)

    return await asyncio.to_thread(
        _render_pig_card_image_sync,
        pig_data,
        image_file,
        cache_final_card=cache_final_card,
        require_avatar=require_avatar,
    )


async def _render_card_once(
    task_key: tuple[object, ...],
    pig_data: Mapping[str, Any],
    image_file: Path | None,
    *,
    cache_final_card: bool,
    require_avatar: bool,
    final_cache_key: tuple[object, ...] | None,
    limit_gif: bool,
) -> PigCardRenderResult:
    """执行一次共享卡片生成；只有 GIF 进入全局双并发预算。"""

    try:
        if limit_gif:
            async with _get_gif_render_semaphore():
                return await asyncio.to_thread(
                    _render_pig_card_image_sync,
                    pig_data,
                    image_file,
                    cache_final_card=cache_final_card,
                    require_avatar=require_avatar,
                    _final_cache_key=final_cache_key,
                )
        return await asyncio.to_thread(
            _render_pig_card_image_sync,
            pig_data,
            image_file,
            cache_final_card=cache_final_card,
            require_avatar=require_avatar,
            _final_cache_key=final_cache_key,
        )
    finally:
        current_task = asyncio.current_task()
        async with _card_render_tasks_lock:
            if _card_render_tasks.get(task_key) is current_task:
                _card_render_tasks.pop(task_key, None)


def get_card_renderer_cache_stats() -> dict[str, int]:
    """返回固定卡片磁盘成品与动态 GIF 源帧占用，便于诊断和回归测试。"""

    disk_entries = _card_disk_cache_files()
    with _card_cache_lock:
        return {
            "final_entries": len(disk_entries),
            "final_bytes": sum(size for _, size, _ in disk_entries),
            "source_entries": len(_source_gif_frame_cache),
            "source_bytes": _source_gif_frame_cache_bytes,
        }


def clear_card_renderer_caches() -> None:
    """释放进程内 GIF 源帧、字体、头像与 Emoji；磁盘成品跨重启保留。"""

    global _source_gif_frame_cache_bytes
    global _gif_render_semaphore, _gif_render_semaphore_loop

    with _card_cache_lock:
        source_frames = list(_source_gif_frame_cache.values())
        _source_gif_frame_cache.clear()
        _source_gif_frame_cache_bytes = 0
    for frame_set in source_frames:
        for frame, _ in frame_set:
            frame.close()

    for cached_function in (
        _load_avatar_cached,
        _avatar_corner_mask,
        _load_font,
        _emoji_spans,
        _card_render_asset_signature,
    ):
        cached_function.cache_clear()

    # 只有已初始化时才取实例，避免单纯 shutdown 反而打开 Emoji ZIP。
    if get_noto_emoji_source.cache_info().currsize:
        emoji_source = get_noto_emoji_source()
        if emoji_source is not None:
            emoji_source.close()
    get_noto_emoji_source.cache_clear()

    with _gif_render_semaphore_guard:
        _gif_render_semaphore = None
        _gif_render_semaphore_loop = None


async def shutdown_card_renderer() -> None:
    """NoneBot 关闭入口；等待在途卡片后释放可能常驻数十 MiB 的内存缓存。"""

    async with _card_render_tasks_lock:
        render_tasks = list(_card_render_tasks.values())
    if render_tasks:
        await asyncio.gather(*render_tasks, return_exceptions=True)
    clear_card_renderer_caches()


# ================================ 本周小猪长图 ================================ #
ITEM_SIZE = (150, 150)
PADDING = 20
BOTTOM_TEXT_SPACE = 80


def _render_weekly_pig_image_sync(image_paths: list[Path]) -> bytes:
    """同步绘制本周小猪长图；外层必须丢到线程，避免阻塞 NoneBot 事件循环。"""

    item_width, item_height = ITEM_SIZE
    total_width = (item_width + PADDING) * len(image_paths) + PADDING
    total_height = item_height + BOTTOM_TEXT_SPACE

    canvas = Image.new("RGB", (total_width, total_height), (255, 255, 255))
    for index, image_path in enumerate(image_paths):
        with Image.open(image_path) as opened:
            # 本周小猪是静态合照：GIF 只使用首帧，不读取或缓存整段动画。
            if getattr(opened, "is_animated", False):
                opened.seek(0)
            image = opened.convert("RGBA").resize(ITEM_SIZE)
            x = PADDING + index * (item_width + PADDING)
            y = PADDING
            canvas.paste(image, (x, y), image)
            image.close()

    output = BytesIO()
    canvas.save(output, format="PNG")
    result = output.getvalue()
    canvas.close()
    return result


async def render_weekly_pig_image(image_paths: list[Path]) -> bytes:
    """异步渲染本周小猪长图。"""

    return await asyncio.to_thread(_render_weekly_pig_image_sync, image_paths)
