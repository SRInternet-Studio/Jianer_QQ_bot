from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import math
import os
import random
import re
import shutil
import threading
from collections import Counter
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Literal, Sequence

from ..host import logger
from ..host import localstore
from pilmoji import Pilmoji
from pilmoji.helpers import getsize as pilmoji_getsize
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageSequence

from .card_renderer import (
    EMOJI_SCALE_FACTOR,
    GIF_ABSOLUTE_MAX_SOURCE_FRAMES,
    GIF_MAX_DECODE_WORK_PIXELS,
    GIF_PALETTE_SAMPLE_SIZE,
    _gif_frame_groups,
    _has_emoji_candidate,
    _normalize_gif_duration,
    _normalize_extra_emoji_symbols,
    _resolve_font_path,
    _tokenize_text,
    get_noto_emoji_source,
)
from .catalog_renderer import catalog_render_budget
from .config import plugin_config
from .runtime import rollpig_today
from .yesterday_recap import YesterdayRecap


# ================================ 数据模型 ================================ #

RGB = tuple[int, int, int]
SectionKind = Literal["highlight", "summary", "aftereffect"]
HeroDecorMode = Literal["fixed", "random", "none"]
HeroDecorDensity = Literal["auto", "none", "minimal", "low", "normal", "high"]
CardSurfaceStyle = Literal["opaque", "glass"]
DEFAULT_GLASS_OPACITY = 0.55
PACKAGE_RESOURCE_DIR = Path(__file__).parent / "resource"
YESTERDAY_CARD_RESOURCE_DIR = PACKAGE_RESOURCE_DIR / "yesterday_card"
YESTERDAY_CARD_DEFAULT_TITLE_FONT = PACKAGE_RESOURCE_DIR / "fonts" / "ZCOOLKuaiLe-Regular.ttf"
YESTERDAY_CARD_DEFAULT_BODY_FONT = PACKAGE_RESOURCE_DIR / "fonts" / "SourceHanSansSC-Medium.otf"
YESTERDAY_CARD_WIDTH = 720
YESTERDAY_CARD_SUPERSAMPLE = 3
YESTERDAY_CARD_DISK_CACHE_MAX_BYTES = 64 * 1024 * 1024
YESTERDAY_CARD_CACHE_VERSION = 3
YESTERDAY_CARD_CACHE_MAGIC = b"ROLLPIG-YESTERDAY-CACHE-V2\n"
YESTERDAY_CARD_CACHE_HEADER_MAX_BYTES = 4096
YESTERDAY_CARD_CACHE_DIR = localstore.get_plugin_cache_dir() / "yesterday_cards"


@dataclass(frozen=True)
class TextSpan:
    """描述一段可独立设置颜色和字号的富文本。"""

    text: str
    color: RGB
    size: float | None = None


@dataclass(frozen=True)
class StatItem:
    """统计栏的一项；icon 对应 assets 目录中的同名 PNG 资源。"""

    icon: str
    label: str
    value: str
    suffix: str


@dataclass(frozen=True)
class InfoSection:
    """高光、小结或余波卡片；body 使用富文本以保留强调色。"""

    kind: SectionKind
    title: str
    body: tuple[TextSpan, ...]


@dataclass(frozen=True)
class CardData:
    """卡片的全部动态数据，列表长度和文字长度都可以变化。"""

    title: str
    date: str
    role_prefix: str
    role: str
    stats: tuple[StatItem, ...]
    sections: tuple[InfoSection, ...]
    # 抽取结果只显示一个事实槽位；为空时不占据角色区高度。
    outcome_text: str = ""
    # 可选外层背景；支持 assets 下的资源名或调用方传入的绝对图片路径。
    background_asset: str | None = None
    # None 表示不绘制中间角色；项目不再捆绑设计阶段的占位猪。
    hero_asset: str | None = None
    show_hero_ambience: bool = False
    hero_decor_mode: HeroDecorMode = "random"
    hero_decor_seed: str = ""
    hero_decor_density: HeroDecorDensity = "auto"
    # 放在可选字段末尾以兼容旧的 positional 构造；glass 仅改变材质，不参与布局计算。
    card_surface: CardSurfaceStyle = "opaque"
    # 仅 glass 使用：0 完全显示模糊背景，1 接近原版暖米白；默认取两者之间的折中值。
    glass_opacity: float = DEFAULT_GLASS_OPACITY


@dataclass(frozen=True)
class YesterdayCardRenderResult:
    """昨日回顾卡片成品及实际图片回退状态。"""

    data: bytes
    image_format: str
    renderer: str
    width: int
    height: int
    used_fallback_image: bool


# ================================ 当日缓存与并发状态 ================================ #
# 昨日回顾只在当天有重复查看价值：磁盘缓存跨进程复用，但业务日期一变化就整体清理。
_yesterday_card_cache_lock = threading.RLock()
_yesterday_card_cache_active_root: Path | None = None
_yesterday_card_cache_active_date: str | None = None
_yesterday_card_render_tasks: dict[
    tuple[object, ...],
    asyncio.Task[YesterdayCardRenderResult],
] = {}
_yesterday_card_render_tasks_lock = asyncio.Lock()


@dataclass(frozen=True)
class HeroVisualMetrics:
    """用于解释装饰密度决策的角色视觉复杂度指标。"""

    coverage: float
    spread: float
    component_count: int
    edge_density: float
    color_entropy: float
    score: float

    def to_dict(self) -> dict[str, float | int]:
        return asdict(self)


@dataclass(frozen=True)
class Box:
    x: float
    y: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height


@dataclass
class StatRowLayout:
    box: Box
    cells: list[Box]
    text_widths: list[float]
    text_heights: list[float]


@dataclass
class SectionLayout:
    index: int
    section: InfoSection
    box: Box
    header_height: float
    title_height: float
    body_height: float


@dataclass
class CardLayout:
    width: float
    height: float
    card: Box
    boxes: dict[str, Box]
    stat_rows: list[StatRowLayout] = field(default_factory=list)
    sections: list[SectionLayout] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        """返回便于测试和接入方记录的纯 JSON 布局信息。"""

        return {
            "width": self.width,
            "height": self.height,
            "card": asdict(self.card),
            "boxes": {name: asdict(box) for name, box in self.boxes.items()},
            "stat_rows": [
                {
                    "box": asdict(row.box),
                    "cells": [asdict(cell) for cell in row.cells],
                    "text_widths": row.text_widths,
                    "text_heights": row.text_heights,
                }
                for row in self.stat_rows
            ],
            "sections": [
                {
                    "index": section.index,
                    "kind": section.section.kind,
                    "box": asdict(section.box),
                    "header_height": section.header_height,
                    "title_height": section.title_height,
                    "body_height": section.body_height,
                }
                for section in self.sections
            ],
        }


# ================================ 设计常量 ================================ #

BASE_WIDTH = 1122.0

PINK_BG = (247, 202, 191)
CARD_BG = (252, 244, 230)
GLASS_BLUR_RADIUS = 28.0
GLASS_BORDER = (255, 253, 247, 190)
INK = (63, 33, 21)
BODY_INK = (62, 40, 30)
DATE_INK = (88, 60, 46)
ACCENT = (214, 82, 88)
STAT_BG = (252, 241, 222)
STAT_BORDER = (227, 197, 162)
STAT_DIVIDER = (218, 181, 141)
HIGHLIGHT_BG = (252, 230, 217)
HIGHLIGHT_BORDER = (236, 170, 154)
SUMMARY_BG = (252, 238, 213)
SUMMARY_BORDER = (228, 190, 141)
AFTEREFFECT_BG = (236, 242, 250)
AFTEREFFECT_BORDER = (155, 177, 207)
AFTEREFFECT_DIVIDER = (177, 194, 217)

ICON_SIZES: dict[str, tuple[float, float]] = {
    "flame": (38.0, 46.0),
    "runner": (43.0, 46.0),
}

RANDOM_HERO_DECOR_ASSETS = (
    "decor/sparkle_gold_large",
    "decor/sparkle_pink",
    "decor/flower_pink",
    "decor/sparkle_gold_small",
    "decor/braces_pink",
)

# 固定模式直接复用已经拆分的透明小图；这些位置可逐像素还原旧的无笔刷大装饰层。
FIXED_HERO_DECOR_PLACEMENTS: tuple[tuple[str, tuple[int, int, int, int]], ...] = (
    ("decor/sparkle_gold_large", (11, 351, 58, 61)),
    ("decor/sparkle_pink", (44, 147, 40, 43)),
    ("decor/flower_pink", (472, 336, 74, 66)),
    ("decor/sparkle_gold_small", (504, 52, 51, 53)),
    ("decor/braces_pink", (522, 256, 59, 64)),
)

# 锚点刻意分布在角色舞台两侧；实际位置还会在碰撞检测通过后做小幅随机偏移。
RANDOM_HERO_DECOR_LEFT_ANCHORS = ((65, 105), (55, 245), (70, 375))
RANDOM_HERO_DECOR_RIGHT_ANCHORS = ((545, 85), (560, 240), (530, 370))

HERO_DECOR_DENSITY_COUNTS: dict[str, int] = {
    "none": 0,
    "minimal": 1,
    "low": 2,
    "normal": 4,
    "high": 5,
}

# 自动评分无法完全表达语义；这些素材自带完整场景，因此显式降低通用装饰密度。
HERO_DECOR_COUNT_OVERRIDES: dict[str, int] = {
    "astro-pig": 1,
    "christmas-pig": 1,
}


# ================================ 富文本排版 ================================ #

@dataclass(frozen=True)
class _StyledToken:
    text: str
    color: RGB
    size: float


@dataclass(frozen=True)
class _Line:
    tokens: tuple[_StyledToken, ...]
    width_px: float


_BRACKETED_NAME_PATTERN = re.compile(r"【[^】]*】", re.DOTALL)
_CLOSING_PUNCTUATION = set("，。！？；：、）》】」』…,.!?;:")


class PillowCardRenderer:
    """按照 Figma 设计单位测量并绘制卡片，运行时仅依赖 Pillow。"""

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        display_font_path: Path | str | None = None,
        body_font_path: Path | str | None = None,
    ) -> None:
        self.root = Path(root or Path(__file__).resolve().parent)
        self._default_font_paths: dict[Literal["display", "body"], Path] = {
            "display": YESTERDAY_CARD_DEFAULT_TITLE_FONT,
            "body": YESTERDAY_CARD_DEFAULT_BODY_FONT,
        }
        self._font_paths: dict[Literal["display", "body"], Path] = {
            "display": (
                Path(display_font_path).expanduser()
                if display_font_path
                else self._default_font_paths["display"]
            ),
            "body": (
                Path(body_font_path).expanduser()
                if body_font_path
                else self._default_font_paths["body"]
            ),
        }
        self._font_fallback_warned: set[Literal["display", "body"]] = set()
        self._font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}
        self._image_cache: dict[str, Image.Image] = {}
        self._hero_logical_cache: dict[str, Image.Image] = {}
        self._hero_metrics_cache: dict[str, HeroVisualMetrics] = {}

    def prime_external_asset(self, name: str, image: Image.Image) -> None:
        """复用已经完成的严格解码结果，避免装饰分析再次读取 PNG/GIF。"""

        if not Path(name).is_absolute():
            raise ValueError("只有外部绝对路径角色才能预热")
        previous = self._image_cache.pop(name, None)
        if previous is not None:
            previous.close()
        self._image_cache[name] = image.convert("RGBA")

    def release_external_asset(self, name: str | None) -> None:
        """释放单次渲染持有的角色图及分析缓存，避免遍历猪池后常驻全部立绘。"""

        if not name or not Path(name).is_absolute():
            return
        image = self._image_cache.pop(name, None)
        logical = self._hero_logical_cache.pop(name, None)
        self._hero_metrics_cache.pop(name, None)
        if image is not None:
            image.close()
        if logical is not None:
            logical.close()

    def clear_caches(self) -> None:
        """释放字体与图片缓存；只允许在所有昨日卡片渲染结束后调用。"""

        images = [*self._image_cache.values(), *self._hero_logical_cache.values()]
        self._image_cache.clear()
        self._hero_logical_cache.clear()
        self._hero_metrics_cache.clear()
        self._font_cache.clear()
        self._font_fallback_warned.clear()
        closed_ids: set[int] = set()
        for image in images:
            if id(image) in closed_ids:
                continue
            image.close()
            closed_ids.add(id(image))

    def _font(self, role: Literal["display", "body"], size: float, unit: float) -> ImageFont.FreeTypeFont:
        """按角色加载可配置字体；外部字体失效时回退内置默认字体。"""

        pixel_size = max(1, int(round(size * unit)))
        key = (role, pixel_size)
        if key not in self._font_cache:
            font_path = self._font_paths[role]
            fallback_path = self._default_font_paths[role]
            try:
                self._font_cache[key] = ImageFont.truetype(str(font_path), pixel_size)
            except (OSError, ValueError) as error:
                if font_path == fallback_path:
                    raise
                # 用户字体只影响视觉风格，路径失效不能让昨日小猪命令整体失败。
                if role not in self._font_fallback_warned:
                    logger.warning(
                        "RollPig 昨日卡片自定义字体加载失败，已回退内置字体: "
                        f"role={role} file={font_path} fallback={fallback_path} error={error}"
                    )
                    self._font_fallback_warned.add(role)
                self._font_paths[role] = fallback_path
                self._font_cache[key] = ImageFont.truetype(str(fallback_path), pixel_size)
        return self._font_cache[key]

    def _asset(self, name: str) -> Image.Image:
        """读取并缓存内置资源或调用方传入的绝对路径图片。"""

        if name not in self._image_cache:
            requested_path = Path(name)
            vector_assets = {
                "header_book",
                "header_divider",
                "flame",
                "runner",
                "sparkle",
                "tape",
                "highlight_divider",
                "pig",
                "heart",
                "shield",
                "summary_divider",
            }
            # 角色库位于项目外部，因此只对显式绝对路径开放读取；其他资源仍被约束在 assets。
            if requested_path.is_absolute():
                asset_path = requested_path
            elif name in vector_assets:
                asset_path = self.root / f"{name}@4x.png"
            elif requested_path.suffix:
                asset_path = self.root / requested_path
            else:
                asset_path = self.root / f"{name}.png"
            if not asset_path.is_file():
                raise FileNotFoundError(f"找不到图片资源: {asset_path}")
            self._image_cache[name] = Image.open(asset_path).convert("RGBA")
        return self._image_cache[name]

    def _span_tokens(
        self,
        spans: Sequence[TextSpan],
        default_size: float,
    ) -> list[_StyledToken]:
        """按富文本颜色分段，同时保留完整昵称与 Unicode Emoji 簇。"""

        tokens: list[_StyledToken] = []
        for span in spans:
            size = span.size if span.size is not None else default_size
            normalized = span.text.replace("\r\n", "\n").replace("\r", "\n")
            cursor = 0
            span_tokens: list[str] = []
            for match in _BRACKETED_NAME_PATTERN.finditer(normalized):
                if match.start() > cursor:
                    span_tokens.extend(_tokenize_text(normalized[cursor : match.start()]))
                # 昵称整体测量和换行；其中的 Emoji 交给 Pilmoji 混排。
                span_tokens.append(match.group(0))
                cursor = match.end()
            if cursor < len(normalized):
                span_tokens.extend(_tokenize_text(normalized[cursor:]))
            for token in span_tokens:
                tokens.append(_StyledToken(token, span.color, size))
        return tokens

    def _token_width(self, token: _StyledToken, role: Literal["display", "body"], unit: float) -> float:
        font = self._font(role, token.size, unit)
        if _has_emoji_candidate(token.text) and get_noto_emoji_source() is not None:
            try:
                render_text = _normalize_extra_emoji_symbols(token.text)
                width, _height = pilmoji_getsize(
                    render_text,
                    font=font,
                    spacing=0,
                    emoji_scale_factor=EMOJI_SCALE_FACTOR,
                )
                return float(width)
            except Exception as error:
                logger.debug(
                    "RollPig 昨日卡片 Emoji 测量失败，回退字体宽度: "
                    f"text={token.text!r} error={error}"
                )
        return float(font.getlength(token.text))

    def _split_oversized_token(
        self,
        token: _StyledToken,
        role: Literal["display", "body"],
        max_width_px: float,
        unit: float,
    ) -> list[_StyledToken]:
        """超长 ASCII 或括号串无法整体容纳时才按字符拆分，防止死循环。"""

        pieces: list[_StyledToken] = []
        buffer = ""
        units = _tokenize_text(token.text) if _has_emoji_candidate(token.text) else list(token.text)
        for text_unit in units:
            candidate = _StyledToken(buffer + text_unit, token.color, token.size)
            if buffer and self._token_width(candidate, role, unit) > max_width_px:
                pieces.append(_StyledToken(buffer, token.color, token.size))
                buffer = text_unit
            else:
                buffer += text_unit
        if buffer:
            pieces.append(_StyledToken(buffer, token.color, token.size))
        return pieces

    def _layout_text(
        self,
        spans: Sequence[TextSpan],
        role: Literal["display", "body"],
        default_size: float,
        max_width: float,
        unit: float,
    ) -> tuple[_Line, ...]:
        """基于真实像素宽度进行中英文混排换行，并尽量避免行首闭合标点。"""

        max_width_px = max_width * unit
        source_tokens = self._span_tokens(spans, default_size)
        expanded: list[_StyledToken] = []
        for token in source_tokens:
            if token.text in {"\r\n", "\r", "\n"}:
                expanded.append(token)
                continue
            if self._token_width(token, role, unit) > max_width_px:
                expanded.extend(self._split_oversized_token(token, role, max_width_px, unit))
            else:
                expanded.append(token)

        lines: list[_Line] = []
        current: list[_StyledToken] = []
        current_width = 0.0
        for token in expanded:
            if token.text in {"\r\n", "\r", "\n"}:
                # 经历列表使用显式换行分隔；换行必须参与测量，不能只在绘制时处理。
                lines.append(_Line(tuple(current), current_width))
                current = []
                current_width = 0.0
                continue
            token_width = self._token_width(token, role, unit)
            is_space = token.text.isspace()
            if not current and is_space:
                continue
            should_wrap = bool(current) and current_width + token_width > max_width_px
            if should_wrap and token.text[0] in _CLOSING_PUNCTUATION:
                should_wrap = False
            if should_wrap:
                lines.append(_Line(tuple(current), current_width))
                current = []
                current_width = 0.0
                if is_space:
                    continue
            current.append(token)
            current_width += token_width
        if current or not lines:
            lines.append(_Line(tuple(current), current_width))
        return tuple(lines)

    def _measure_text(
        self,
        spans: Sequence[TextSpan],
        role: Literal["display", "body"],
        default_size: float,
        max_width: float,
        line_height: float,
        unit: float,
    ) -> tuple[float, tuple[_Line, ...]]:
        lines = self._layout_text(spans, role, default_size, max_width, unit)
        return len(lines) * line_height, lines

    def _draw_text(
        self,
        canvas: Image.Image,
        draw: ImageDraw.ImageDraw,
        spans: Sequence[TextSpan],
        role: Literal["display", "body"],
        default_size: float,
        box: Box,
        line_height: float,
        unit: float,
        align: Literal["left", "center", "right"] = "left",
    ) -> None:
        """用统一基线绘制富文本；不同字号在同一行时不会上下漂移。"""

        lines = self._layout_text(spans, role, default_size, box.width, unit)
        has_emoji = any(
            _has_emoji_candidate(token.text)
            for line in lines
            for token in line.tokens
        )
        emoji_source = get_noto_emoji_source() if has_emoji else None
        emoji_context = (
            Pilmoji(
                canvas,
                source=emoji_source,
                draw=draw,
                render_discord_emoji=False,
                emoji_scale_factor=EMOJI_SCALE_FACTOR,
                emoji_position_offset=(0, self._px(-2, unit)),
            )
            if emoji_source is not None
            else nullcontext(None)
        )

        with emoji_context as emoji_drawer:
            for line_index, line in enumerate(lines):
                if align == "center":
                    cursor_x = box.x * unit + (box.width * unit - line.width_px) / 2
                elif align == "right":
                    cursor_x = box.right * unit - line.width_px
                else:
                    cursor_x = box.x * unit

                max_size = max((token.size for token in line.tokens), default=default_size)
                reference_font = self._font(role, max_size, unit)
                ascent, descent = reference_font.getmetrics()
                line_top = (box.y + line_index * line_height) * unit
                baseline = line_top + (line_height * unit - ascent - descent) / 2 + ascent
                for token in line.tokens:
                    font = self._font(role, token.size, unit)
                    if emoji_drawer is not None and _has_emoji_candidate(token.text):
                        try:
                            token_ascent, _token_descent = font.getmetrics()
                            emoji_drawer.text(
                                (int(round(cursor_x)), int(round(baseline - token_ascent))),
                                _normalize_extra_emoji_symbols(token.text),
                                fill=(*token.color, 255),
                                font=font,
                                spacing=0,
                                emoji_scale_factor=EMOJI_SCALE_FACTOR,
                                emoji_position_offset=(0, self._px(-2, unit)),
                            )
                        except Exception as error:
                            logger.debug(
                                "RollPig 昨日卡片 Emoji 绘制失败，回退普通字体: "
                                f"text={token.text!r} error={error}"
                            )
                            draw.text(
                                (cursor_x, baseline),
                                token.text,
                                font=font,
                                fill=(*token.color, 255),
                                anchor="ls",
                            )
                    else:
                        draw.text(
                            (cursor_x, baseline),
                            token.text,
                            font=font,
                            fill=(*token.color, 255),
                            anchor="ls",
                        )
                    cursor_x += self._token_width(token, role, unit)


# ================================ 动态布局计算 ================================ #

    def build_layout(self, data: CardData, width: int = 1122, supersample: int = 3) -> CardLayout:
        """先测量全部动态内容，再返回没有绝对文本死坐标的纵向流式布局。"""

        if width < 360:
            raise ValueError("width 不能小于 360px")
        if supersample < 1:
            raise ValueError("supersample 必须大于等于 1")
        unit = width / BASE_WIDTH * supersample

        boxes: dict[str, Box] = {}
        stat_rows: list[StatRowLayout] = []
        section_layouts: list[SectionLayout] = []

        title_height, _ = self._measure_text(
            (TextSpan(data.title, INK),), "display", 58, 430, 70, unit
        )
        date_height, _ = self._measure_text(
            (TextSpan(data.date, DATE_INK),), "display", 34, 150, 44, unit
        )
        header_row_height = max(108.0, title_height, date_height)
        header_height = header_row_height + 12 + 8

        role_spans = (
            TextSpan(data.role_prefix, INK, 35),
            TextSpan(data.role, ACCENT, 39),
        )
        role_height, _ = self._measure_text(role_spans, "display", 35, 700, 48, unit)
        outcome_height = 0.0
        if data.outcome_text:
            outcome_height, _ = self._measure_text(
                (TextSpan(data.outcome_text, ACCENT),),
                "body",
                25,
                700,
                36,
                unit,
            )
        hero_height = (
            450
            + (8 if role_height else 0)
            + role_height
            + (8 if outcome_height else 0)
            + outcome_height
        )

        stat_height = 0.0
        if data.stats:
            columns = 1 if len(data.stats) == 1 else 2
            row_gap = 12.0
            inner_width = 794.0
            divider_width = 2.0 if columns == 2 else 0.0
            cell_width = (inner_width - divider_width * (columns - 1)) / columns
            # Figma 的 1.5px 描边参与外部尺寸，视觉内容区上下各落在 19px。
            y_cursor = 19.0
            for row_start in range(0, len(data.stats), columns):
                items = data.stats[row_start : row_start + columns]
                text_widths: list[float] = []
                text_heights: list[float] = []
                cell_boxes: list[Box] = []
                row_height = 0.0
                for item in items:
                    icon_width, icon_height = ICON_SIZES.get(item.icon, (38.0, 46.0))
                    available = max(120.0, cell_width - icon_width - 14.0)
                    wrap_width = min(245.0 if columns == 2 else 520.0, available)
                    text_spans = (
                        TextSpan(item.label, BODY_INK),
                        TextSpan(item.value, ACCENT),
                        TextSpan(item.suffix, BODY_INK),
                    )
                    text_height, text_lines = self._measure_text(
                        text_spans, "body", 29, wrap_width, 40, unit
                    )
                    # 居中时使用真实最长行宽，避免固定 245px 文本槽把短文案整体推向左侧。
                    content_width = max((line.width_px for line in text_lines), default=unit) / unit
                    text_widths.append(min(wrap_width, content_width) + 0.01)
                    text_heights.append(text_height)
                    row_height = max(row_height, icon_height, text_height)
                row_index = row_start // columns
                row_box = Box(22.0, y_cursor, inner_width, row_height)
                for item_index in range(len(items)):
                    cell_x = 22.0 + item_index * (cell_width + divider_width)
                    cell_boxes.append(Box(cell_x, y_cursor, cell_width, row_height))
                stat_rows.append(StatRowLayout(row_box, cell_boxes, text_widths, text_heights))
                y_cursor += row_height
                if row_start + columns < len(data.stats):
                    y_cursor += row_gap
            stat_height = y_cursor + 19.0

        block_heights: list[tuple[str, float]] = [("header", header_height), ("hero", hero_height)]
        if data.stats:
            block_heights.append(("stats", stat_height))

        for index, section in enumerate(data.sections):
            if section.kind == "highlight":
                title_width = 571.0
                title_height, _ = self._measure_text(
                    (TextSpan(section.title, INK),), "display", 36, title_width, 46, unit
                )
                body_height, _ = self._measure_text(section.body, "body", 28, 754, 44, unit)
                header_height_for_section = max(55.0, title_height)
                section_height = 34 + header_height_for_section + 13 + 5 + 13 + body_height + 36
            else:
                title_width = 624.0
                title_height, _ = self._measure_text(
                    (TextSpan(section.title, INK),), "display", 36, title_width, 46, unit
                )
                body_height, _ = self._measure_text(section.body, "body", 28, 754, 44, unit)
                header_height_for_section = max(47.0, title_height)
                section_height = 32 + header_height_for_section + 13 + 5 + 13 + body_height + 36
            block_heights.append((f"section_{index}", section_height))
            section_layouts.append(
                SectionLayout(
                    index=index,
                    section=section,
                    box=Box(0, 0, 838, section_height),
                    header_height=header_height_for_section,
                    title_height=title_height,
                    body_height=body_height,
                )
            )

        card_height = 42 + sum(height for _, height in block_heights) + 24 * (len(block_heights) - 1) + 46
        outer_height = 52 + card_height + 58
        card = Box(90, 52, 942, card_height)
        content_x = card.x + 52
        y_cursor = card.y + 42
        for name, height in block_heights:
            boxes[name] = Box(content_x, y_cursor, 838, height)
            if name.startswith("section_"):
                section_index = int(name.split("_")[1])
                section_layouts[section_index].box = boxes[name]
            y_cursor += height + 24

        return CardLayout(
            width=BASE_WIDTH,
            height=outer_height,
            card=card,
            boxes=boxes,
            stat_rows=stat_rows,
            sections=section_layouts,
        )


# ================================ Pillow 绘制 ================================ #

    @staticmethod
    def _px(value: float, unit: float) -> int:
        return int(round(value * unit))

    def _px_box(self, box: Box, unit: float) -> tuple[int, int, int, int]:
        return (
            self._px(box.x, unit),
            self._px(box.y, unit),
            self._px(box.right, unit),
            self._px(box.bottom, unit),
        )

    def _paste_asset(
        self,
        canvas: Image.Image,
        name: str,
        box: Box,
        unit: float,
        shift_x: float = 0,
    ) -> None:
        asset = self._asset(name)
        target_size = (max(1, self._px(box.width, unit)), max(1, self._px(box.height, unit)))
        resized = asset.resize(target_size, Image.Resampling.LANCZOS)
        target_position = (self._px(box.x, unit), self._px(box.y, unit))
        if shift_x:
            # Figma 会将 dash pattern 在整段路径上居中，普通 SVG 栅格器默认从首端起画。
            # 在固定宽度的透明视口内平移并裁切，能同时保留两端的半段虚线。
            clipped = Image.new("RGBA", target_size, (0, 0, 0, 0))
            clipped.alpha_composite(resized, (self._px(shift_x, unit), 0))
            canvas.alpha_composite(clipped, target_position)
        else:
            canvas.alpha_composite(resized, target_position)

    def _background_canvas(self, name: str, size: tuple[int, int]) -> Image.Image:
        """将可替换背景按 cover 规则居中裁切，并在透明区域下保留默认粉色。"""

        source = self._asset(name)
        target_width, target_height = size
        scale = max(target_width / source.width, target_height / source.height)
        rendered_size = (
            max(target_width, math.ceil(source.width * scale)),
            max(target_height, math.ceil(source.height * scale)),
        )
        rendered = source.resize(rendered_size, Image.Resampling.LANCZOS)
        left = (rendered.width - target_width) // 2
        top = (rendered.height - target_height) // 2
        cropped = rendered.crop((left, top, left + target_width, top + target_height))
        canvas = Image.new("RGBA", size, (*PINK_BG, 255))
        canvas.alpha_composite(cropped)
        return canvas

    def _draw_card_surface(
        self,
        canvas: Image.Image,
        card: Box,
        unit: float,
        style: CardSurfaceStyle,
        backdrop: Image.Image | None = None,
        glass_opacity: float = DEFAULT_GLASS_OPACITY,
    ) -> None:
        """绘制卡片底材；轻透模式先模糊卡片背后的真实背景，再覆暖米白纸色。"""

        card_box = self._px_box(card, unit)
        radius = self._px(82, unit)
        if style == "opaque":
            ImageDraw.Draw(canvas).rounded_rectangle(
                card_box,
                radius=radius,
                fill=(*CARD_BG, 255),
            )
            return
        if style != "glass":
            raise ValueError(f"不支持的卡片表面样式: {style}")
        if backdrop is None:
            raise ValueError("轻透卡片缺少阴影绘制前的背景快照")

        # ================================ 轻透纸张合成 ================================ #
        # 模糊在超采样画布上执行，最终缩小时仍保持约 18px（720px 输出）的柔和扩散。
        card_mask = Image.new("L", canvas.size, 0)
        ImageDraw.Draw(card_mask).rounded_rectangle(card_box, radius=radius, fill=255)
        softened_backdrop = backdrop.filter(
            ImageFilter.GaussianBlur(max(1, self._px(GLASS_BLUR_RADIUS, unit)))
        )

        # opacity 表示暖米白覆层的强度；数值越低，模糊后的背景越明显。
        tint_alpha = int(round(255 * glass_opacity))
        warm_veil = Image.new("RGBA", canvas.size, (*CARD_BG, tint_alpha))
        softened_backdrop.alpha_composite(warm_veil)
        canvas.paste(softened_backdrop, (0, 0), card_mask)

        # 极淡高光边界用于恢复玻璃/覆膜的边缘感，同时不会改变卡片内部排版。
        ImageDraw.Draw(canvas).rounded_rectangle(
            card_box,
            radius=radius,
            outline=GLASS_BORDER,
            width=max(1, self._px(2, unit)),
        )

    def _paste_hero_asset(
        self,
        canvas: Image.Image,
        name: str,
        stage: Box,
        unit: float,
    ) -> None:
        """将可替换角色放入 620×450 舞台；标准素材逐像素对齐，其他比例自动 contain。"""

        asset = self._asset(name)
        self._paste_hero_image(canvas, asset, name=name, stage=stage, unit=unit)

    def _paste_hero_image(
        self,
        canvas: Image.Image,
        asset: Image.Image,
        *,
        name: str,
        stage: Box,
        unit: float,
    ) -> None:
        """把已经解码的静态帧放入角色舞台，供 PNG 与 GIF 共用同一套 contain 规则。"""

        stage_size = (self._px(stage.width, unit), self._px(stage.height, unit))
        stage_position = (self._px(stage.x, unit), self._px(stage.y, unit))
        if asset.size == (620, 450):
            rendered = asset.resize(stage_size, Image.Resampling.LANCZOS)
            try:
                canvas.alpha_composite(rendered, stage_position)
            finally:
                rendered.close()
            return

        alpha_box = asset.getchannel("A").getbbox()
        if alpha_box is None:
            raise ValueError(f"角色素材没有可见像素: {name}")
        cropped = asset.crop(alpha_box)
        try:
            max_width = self._px(500, unit)
            max_height = self._px(448, unit)
            ratio = min(max_width / cropped.width, max_height / cropped.height)
            target_size = (
                max(1, int(round(cropped.width * ratio))),
                max(1, int(round(cropped.height * ratio))),
            )
            rendered = cropped.resize(target_size, Image.Resampling.LANCZOS)
            try:
                position = (
                    stage_position[0] + (stage_size[0] - target_size[0]) // 2,
                    stage_position[1] + stage_size[1] - target_size[1],
                )
                canvas.alpha_composite(rendered, position)
            finally:
                rendered.close()
        finally:
            cropped.close()

    def compose_hero_image(
        self,
        base_image: Image.Image,
        layout: CardLayout,
        hero_image: Image.Image,
        *,
        width: int,
        name: str,
    ) -> Image.Image:
        """把一帧角色叠到已完成的静态卡片上；GIF 无需逐帧重画文字和背景。"""

        canvas = base_image.convert("RGBA")
        prepared_hero = hero_image.convert("RGBA")
        try:
            hero_box = layout.boxes["hero"]
            stage = Box(hero_box.x + 109, hero_box.y, 620, 450)
            self._paste_hero_image(
                canvas,
                prepared_hero,
                name=name,
                stage=stage,
                unit=width / BASE_WIDTH,
            )
            return canvas.convert("RGB")
        finally:
            prepared_hero.close()
            canvas.close()

    # ================================ 随机装饰布局 ================================ #

    @staticmethod
    def _expand_mask(mask: Image.Image, iterations: int) -> Image.Image:
        """用多次小核膨胀替代超大卷积核，给碰撞区域增加稳定安全边距。"""

        expanded = mask
        for _ in range(iterations):
            expanded = expanded.filter(ImageFilter.MaxFilter(5))
        return expanded

    def _hero_logical_image(self, name: str | None) -> Image.Image:
        """按正式 contain 规则把角色还原到 620×450 设计画布，并缓存结果。"""

        stage_size = (620, 450)
        if name is None:
            return Image.new("RGBA", stage_size, (0, 0, 0, 0))
        if name in self._hero_logical_cache:
            return self._hero_logical_cache[name]

        asset = self._asset(name)
        if asset.size == stage_size:
            logical = asset.copy()
        else:
            alpha_box = asset.getchannel("A").getbbox()
            if alpha_box is None:
                raise ValueError(f"角色素材没有可见像素: {name}")
            cropped = asset.crop(alpha_box)
            ratio = min(500 / cropped.width, 448 / cropped.height)
            target_size = (
                max(1, int(round(cropped.width * ratio))),
                max(1, int(round(cropped.height * ratio))),
            )
            rendered = cropped.resize(target_size, Image.Resampling.LANCZOS)
            position = ((stage_size[0] - target_size[0]) // 2, stage_size[1] - target_size[1])
            logical = Image.new("RGBA", stage_size, (0, 0, 0, 0))
            logical.alpha_composite(rendered, position)
        self._hero_logical_cache[name] = logical
        return logical

    def _hero_binary_mask(self, name: str | None) -> Image.Image:
        """将抗锯齿 Alpha 转为稳定二值轮廓，供复杂度分析和碰撞检测共用。"""

        alpha = self._hero_logical_image(name).getchannel("A")
        return alpha.point([0 if value < 12 else 255 for value in range(256)])

    @staticmethod
    def _mask_component_count(mask: Image.Image) -> int:
        """在低分辨率二值遮罩上统计有效连通块，忽略单像素抗锯齿噪声。"""

        sample = mask.resize((62, 45), Image.Resampling.NEAREST)
        width, height = sample.size
        pixels = sample.load()
        visited = bytearray(width * height)
        component_count = 0
        for start_y in range(height):
            for start_x in range(width):
                start_index = start_y * width + start_x
                if visited[start_index] or pixels[start_x, start_y] == 0:
                    continue
                stack = [(start_x, start_y)]
                visited[start_index] = 1
                area = 0
                while stack:
                    x, y = stack.pop()
                    area += 1
                    for offset_x, offset_y in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                        next_x, next_y = x + offset_x, y + offset_y
                        if not (0 <= next_x < width and 0 <= next_y < height):
                            continue
                        next_index = next_y * width + next_x
                        if visited[next_index]:
                            continue
                        visited[next_index] = 1
                        if pixels[next_x, next_y] > 0:
                            stack.append((next_x, next_y))
                if area >= 2:
                    component_count += 1
        return component_count

    @staticmethod
    def _mask_spread(mask: Image.Image) -> float:
        """计算角色在 10×8 网格中的空间扩散程度，而非只看像素总量。"""

        columns, rows = 10, 8
        occupied = 0
        for row in range(rows):
            top = round(row * mask.height / rows)
            bottom = round((row + 1) * mask.height / rows)
            for column in range(columns):
                left = round(column * mask.width / columns)
                right = round((column + 1) * mask.width / columns)
                cell = mask.crop((left, top, right, bottom))
                histogram = cell.histogram()
                visible = cell.width * cell.height - histogram[0]
                if visible >= max(2, round(cell.width * cell.height * 0.04)):
                    occupied += 1
        return occupied / (columns * rows)

    def _hero_visual_metrics(self, name: str | None) -> HeroVisualMetrics:
        """综合空间、结构、边缘和颜色指标估算视觉复杂度，结果按素材缓存。"""

        if name is None:
            return HeroVisualMetrics(0.0, 0.0, 0, 0.0, 0.0, 0.0)
        if name in self._hero_metrics_cache:
            return self._hero_metrics_cache[name]

        logical = self._hero_logical_image(name)
        mask = self._hero_binary_mask(name)
        histogram = mask.histogram()
        total_pixels = mask.width * mask.height
        visible_pixels = total_pixels - histogram[0]
        coverage = visible_pixels / total_pixels
        spread = self._mask_spread(mask)
        component_count = self._mask_component_count(mask)

        sample_size = (155, 112)
        sample = logical.resize(sample_size, Image.Resampling.LANCZOS)
        sample_alpha = sample.getchannel("A").point([0 if value < 24 else 255 for value in range(256)])
        composite = Image.new("RGB", sample_size, CARD_BG)
        composite.paste(sample.convert("RGB"), (0, 0), sample.getchannel("A"))
        edges = composite.convert("L").filter(ImageFilter.FIND_EDGES)
        # Pillow 12 已弃用 getdata()，但其替代接口要到后续版本才提供。
        # 这里直接读取稳定的原始字节，避免版本切换影响复杂度判定。
        edge_bytes = edges.tobytes()
        alpha_bytes = sample_alpha.tobytes()
        edge_pixels = sum(
            1
            for edge_value, alpha_value in zip(edge_bytes, alpha_bytes)
            if alpha_value and edge_value >= 28
        )
        sample_visible = max(
            1,
            sum(1 for value in alpha_bytes if value),
        )
        edge_density = edge_pixels / sample_visible

        color_bins: Counter[tuple[int, int, int]] = Counter()
        rgba_bytes = sample.tobytes()
        for offset in range(0, len(rgba_bytes), 4):
            red, green, blue, alpha = rgba_bytes[offset : offset + 4]
            if alpha >= 32:
                color_bins[(red // 64, green // 64, blue // 64)] += 1
        color_total = sum(color_bins.values())
        entropy = 0.0
        if color_total:
            for count in color_bins.values():
                probability = count / color_total
                entropy -= probability * math.log2(probability)
        color_entropy = min(1.0, entropy / 6.0)

        coverage_factor = min(1.0, coverage / 0.55)
        component_factor = min(1.0, max(0, component_count - 1) / 7)
        edge_factor = min(1.0, edge_density / 0.42)
        score = (
            0.08 * coverage_factor
            + 0.30 * spread
            + 0.27 * component_factor
            + 0.25 * edge_factor
            + 0.10 * color_entropy
        )
        metrics = HeroVisualMetrics(
            coverage=round(coverage, 6),
            spread=round(spread, 6),
            component_count=component_count,
            edge_density=round(edge_density, 6),
            color_entropy=round(color_entropy, 6),
            score=round(score, 6),
        )
        self._hero_metrics_cache[name] = metrics
        return metrics

    def hero_decor_diagnostics(self, data: CardData) -> dict[str, object]:
        """返回随机装饰数量的可解释决策，供批量清单、日志和测试使用。"""

        metrics = self._hero_visual_metrics(data.hero_asset)
        hero_key = Path(data.hero_asset).stem if data.hero_asset else "empty"
        if data.hero_decor_density != "auto":
            target_count = HERO_DECOR_DENSITY_COUNTS[data.hero_decor_density]
            reason = f"explicit:{data.hero_decor_density}"
        elif data.hero_asset is None:
            target_count = 5
            reason = "empty_stage"
        elif hero_key in HERO_DECOR_COUNT_OVERRIDES:
            target_count = HERO_DECOR_COUNT_OVERRIDES[hero_key]
            reason = "asset_override"
        elif metrics.score >= 0.72:
            target_count = 1
            reason = "auto:very_complex"
        elif metrics.score >= 0.60:
            target_count = 2
            reason = "auto:complex"
        elif metrics.score >= 0.47:
            target_count = 3
            reason = "auto:moderate"
        else:
            target_count = 4
            reason = "auto:simple"
        return {
            "hero_key": hero_key,
            "metrics": metrics.to_dict(),
            "target_count": target_count,
            "reason": reason,
        }

    def _hero_collision_mask(self, name: str | None) -> Image.Image:
        """返回向外扩张约 10px 的角色禁入区。"""

        return self._expand_mask(self._hero_binary_mask(name), 5)

    @staticmethod
    def _decor_random(data: CardData) -> random.Random:
        """基于角色、日期和显式种子创建可复现随机源。"""

        hero_key = Path(data.hero_asset).stem if data.hero_asset else "empty"
        seed_text = f"{hero_key}|{data.date}|{data.role}|{data.hero_decor_seed}"
        digest = hashlib.sha256(seed_text.encode("utf-8")).digest()
        return random.Random(int.from_bytes(digest[:8], "big"))

    @staticmethod
    def _alternating_decor_anchors(rng: random.Random) -> list[tuple[int, int]]:
        """交替返回左右锚点，防止随机装饰全部堆在同一侧。"""

        left = list(RANDOM_HERO_DECOR_LEFT_ANCHORS)
        right = list(RANDOM_HERO_DECOR_RIGHT_ANCHORS)
        rng.shuffle(left)
        rng.shuffle(right)
        sides = (left, right) if rng.random() < 0.5 else (right, left)
        anchors: list[tuple[int, int]] = []
        for index in range(max(len(left), len(right))):
            for side in sides:
                if index < len(side):
                    anchors.append(side[index])
        return anchors

    @staticmethod
    def _decor_anchor_allowed(sprite_name: str, anchor: tuple[int, int]) -> bool:
        """约束具象装饰的语义位置：花朵靠下，花括号位于左右中部。"""

        _, anchor_y = anchor
        if sprite_name.endswith("flower_pink"):
            return anchor_y >= 320
        if sprite_name.endswith("braces_pink"):
            return 170 <= anchor_y <= 300
        return True

    def _draw_random_hero_decor(
        self,
        canvas: Image.Image,
        data: CardData,
        stage: Box,
        unit: float,
    ) -> None:
        """随机摆放独立装饰，同时避开角色 Alpha、安全边距和其他装饰。"""

        logical_size = (620, 450)
        blocked = self._hero_collision_mask(data.hero_asset)
        layer = Image.new("RGBA", logical_size, (0, 0, 0, 0))
        rng = self._decor_random(data)
        target_count = int(self.hero_decor_diagnostics(data)["target_count"])

        if target_count <= 2:
            # 自带复杂场景时只添加轻量星光，不再叠加花朵或花括号。
            sprite_names = [
                "decor/sparkle_gold_large",
                "decor/sparkle_pink",
                "decor/sparkle_gold_small",
            ]
        else:
            sprite_names = list(RANDOM_HERO_DECOR_ASSETS)
        rng.shuffle(sprite_names)
        selected_sprites = sprite_names[:target_count]
        # 受位置约束的素材优先选位，避免底部或中部锚点先被通用星光占用。
        selected_sprites.sort(
            key=lambda name: 0 if name.endswith(("flower_pink", "braces_pink")) else 1
        )
        anchors = self._alternating_decor_anchors(rng)
        placed_count = 0

        for sprite_name in selected_sprites:
            source = self._asset(sprite_name)
            scale = rng.uniform(0.82, 1.12)
            target_size = (
                max(1, int(round(source.width * scale))),
                max(1, int(round(source.height * scale))),
            )
            sprite = source.resize(target_size, Image.Resampling.LANCZOS)
            max_angle = 4 if sprite_name.endswith("braces_pink") else 12
            angle = rng.uniform(-max_angle, max_angle)
            sprite = sprite.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True)
            alpha = sprite.getchannel("A").point([0 if value < 12 else 255 for value in range(256)])

            chosen_position: tuple[int, int] | None = None
            chosen_anchor_index: int | None = None
            for anchor_index, (anchor_x, anchor_y) in enumerate(anchors):
                if not self._decor_anchor_allowed(sprite_name, (anchor_x, anchor_y)):
                    continue
                for _ in range(12):
                    x = int(round(anchor_x - sprite.width / 2 + rng.uniform(-28, 28)))
                    y = int(round(anchor_y - sprite.height / 2 + rng.uniform(-20, 20)))
                    x = min(max(12, x), logical_size[0] - sprite.width - 12)
                    y = min(max(12, y), logical_size[1] - sprite.height - 12)
                    candidate = Image.new("L", logical_size, 0)
                    candidate.paste(alpha, (x, y))
                    if ImageChops.multiply(candidate, blocked).getbbox() is not None:
                        continue
                    chosen_position = (x, y)
                    chosen_anchor_index = anchor_index
                    layer.alpha_composite(sprite, chosen_position)
                    # 装饰之间也留出约 6px 间距，避免花朵、星星互相粘连。
                    blocked = ImageChops.lighter(blocked, self._expand_mask(candidate, 3))
                    placed_count += 1
                    break
                if chosen_position is not None:
                    break
            if chosen_anchor_index is not None:
                anchors.pop(chosen_anchor_index)
            if not anchors:
                break

        if placed_count == 0 and target_count > 0 and data.hero_asset is None:
            raise RuntimeError("空角色舞台未能放置任何随机装饰")
        target_size = (self._px(stage.width, unit), self._px(stage.height, unit))
        rendered = layer.resize(target_size, Image.Resampling.LANCZOS)
        canvas.alpha_composite(rendered, (self._px(stage.x, unit), self._px(stage.y, unit)))

    def _draw_fixed_hero_decor(self, canvas: Image.Image, stage: Box, unit: float) -> None:
        """按原设计坐标组合五张透明装饰小图，避免依赖 620×450 烘焙大图。"""

        for asset_name, (x, y, width, height) in FIXED_HERO_DECOR_PLACEMENTS:
            self._paste_asset(
                canvas,
                asset_name,
                Box(stage.x + x, stage.y + y, width, height),
                unit,
            )

    def _draw_rounded_panel(
        self,
        draw: ImageDraw.ImageDraw,
        box: Box,
        fill: RGB,
        outline: RGB,
        stroke: float,
        radius: float,
        unit: float,
    ) -> None:
        draw.rounded_rectangle(
            self._px_box(box, unit),
            radius=self._px(radius, unit),
            fill=(*fill, 255),
            outline=(*outline, 255),
            width=max(1, self._px(stroke, unit)),
        )

    def _draw_header(
        self,
        canvas: Image.Image,
        draw: ImageDraw.ImageDraw,
        data: CardData,
        layout: CardLayout,
        unit: float,
    ) -> None:
        box = layout.boxes["header"]
        row_height = box.height - 20
        book = Box(box.x, box.y, 104, 108)
        self._paste_asset(canvas, "header_book", book, unit)

        title_height, _ = self._measure_text((TextSpan(data.title, INK),), "display", 58, 430, 70, unit)
        title_box = Box(box.x + 120, box.y + (row_height - title_height) / 2, 430, title_height)
        self._draw_text(canvas, draw, (TextSpan(data.title, INK),), "display", 58, title_box, 70, unit)

        date_height, _ = self._measure_text((TextSpan(data.date, DATE_INK),), "display", 34, 150, 44, unit)
        date_box = Box(box.x + 688, box.y + (row_height - date_height) / 2, 150, date_height)
        self._draw_text(
            canvas,
            draw,
            (TextSpan(data.date, DATE_INK),),
            "display",
            34,
            date_box,
            44,
            unit,
            align="right",
        )

        divider = Box(box.x, box.y + row_height + 12, 838, 8)
        self._paste_asset(canvas, "header_divider", divider, unit, shift_x=-7)

    def _draw_hero(
        self,
        canvas: Image.Image,
        draw: ImageDraw.ImageDraw,
        data: CardData,
        layout: CardLayout,
        unit: float,
        *,
        include_hero_asset: bool,
    ) -> None:
        box = layout.boxes["hero"]
        art = Box(box.x + 109, box.y, 620, 450)
        # 背景氛围、周围装饰和角色是三个独立层；换角色时不会露出旧猪。
        if data.show_hero_ambience:
            draw.ellipse(
                self._px_box(Box(art.x + 63, art.y + 74, 494, 336), unit),
                fill=(251, 236, 225, 255),
            )
        if data.hero_decor_mode == "fixed":
            self._draw_fixed_hero_decor(canvas, art, unit)
        elif data.hero_decor_mode == "random":
            self._draw_random_hero_decor(canvas, data, art, unit)
        elif data.hero_decor_mode != "none":
            raise ValueError(f"不支持的角色装饰模式: {data.hero_decor_mode}")
        if include_hero_asset and data.hero_asset is not None:
            self._paste_hero_asset(canvas, data.hero_asset, art, unit)
        role_spans = (
            TextSpan(data.role_prefix, INK, 35),
            TextSpan(data.role, ACCENT, 39),
        )
        role_height, _ = self._measure_text(role_spans, "display", 35, 700, 48, unit)
        role_box = Box(box.x + 69, box.y + 458, 700, role_height)
        self._draw_text(canvas, draw, role_spans, "display", 35, role_box, 48, unit, align="center")
        if data.outcome_text:
            outcome_height, _ = self._measure_text(
                (TextSpan(data.outcome_text, ACCENT),),
                "body",
                25,
                700,
                36,
                unit,
            )
            outcome_box = Box(
                box.x + 69,
                role_box.bottom + 8,
                700,
                outcome_height,
            )
            self._draw_text(
                canvas,
                draw,
                (TextSpan(data.outcome_text, ACCENT),),
                "body",
                25,
                outcome_box,
                36,
                unit,
                align="center",
            )

    def _draw_stats(
        self,
        canvas: Image.Image,
        draw: ImageDraw.ImageDraw,
        data: CardData,
        layout: CardLayout,
        unit: float,
    ) -> None:
        if not data.stats:
            return
        panel = layout.boxes["stats"]
        self._draw_rounded_panel(draw, panel, STAT_BG, STAT_BORDER, 1.5, 34, unit)
        columns = 1 if len(data.stats) == 1 else 2
        item_offset = 0
        for row in layout.stat_rows:
            row_items = data.stats[item_offset : item_offset + len(row.cells)]
            for cell_index, (cell, item) in enumerate(zip(row.cells, row_items)):
                global_cell = Box(panel.x + cell.x, panel.y + cell.y, cell.width, cell.height)
                icon_width, icon_height = ICON_SIZES.get(item.icon, (38.0, 46.0))
                text_width = row.text_widths[cell_index]
                text_height = row.text_heights[cell_index]
                packed_width = icon_width + 14 + text_width
                icon_box = Box(
                    global_cell.x + (global_cell.width - packed_width) / 2,
                    global_cell.y + (global_cell.height - icon_height) / 2,
                    icon_width,
                    icon_height,
                )
                self._paste_asset(canvas, item.icon, icon_box, unit)
                text_box = Box(
                    icon_box.right + 14,
                    global_cell.y + (global_cell.height - text_height) / 2,
                    text_width,
                    text_height,
                )
                spans = (
                    TextSpan(item.label, BODY_INK),
                    TextSpan(item.value, ACCENT),
                    TextSpan(item.suffix, BODY_INK),
                )
                self._draw_text(canvas, draw, spans, "body", 29, text_box, 40, unit)

            if columns == 2 and len(row.cells) == 2:
                divider_x = panel.x + row.cells[0].right
                divider = Box(divider_x, panel.y + row.box.y + (row.box.height - 48) / 2, 2, 48)
                draw.rectangle(self._px_box(divider, unit), fill=(*STAT_DIVIDER, 166))
            item_offset += len(row.cells)

    def _draw_section(
        self,
        canvas: Image.Image,
        draw: ImageDraw.ImageDraw,
        section_layout: SectionLayout,
        unit: float,
    ) -> None:
        section = section_layout.section
        box = section_layout.box
        if section.kind == "highlight":
            shadow_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
            shadow_draw = ImageDraw.Draw(shadow_layer)
            shadow_box = Box(box.x, box.y + 5, box.width, box.height)
            shadow_draw.rounded_rectangle(
                self._px_box(shadow_box, unit),
                radius=self._px(34, unit),
                fill=(115, 59, 38, 31),
            )
            canvas.alpha_composite(shadow_layer)
            self._draw_rounded_panel(draw, box, HIGHLIGHT_BG, HIGHLIGHT_BORDER, 2, 34, unit)
            padding_top = 34.0
            icon_name, icon_size = "sparkle", (47.0, 42.0)
            trailing_name, trailing_size = "tape", (108.0, 55.0)
            title_width = 571.0
        elif section.kind == "summary":
            self._draw_rounded_panel(draw, box, SUMMARY_BG, SUMMARY_BORDER, 1.5, 34, unit)
            padding_top = 32.0
            icon_name, icon_size = "pig", (52.0, 44.0)
            trailing_name, trailing_size = "heart", (50.0, 47.0)
            title_width = 624.0
        else:
            self._draw_rounded_panel(
                draw,
                box,
                AFTEREFFECT_BG,
                AFTEREFFECT_BORDER,
                1.5,
                34,
                unit,
            )
            padding_top = 32.0
            icon_name, icon_size = "shield", (52.0, 52.0)
            trailing_name, trailing_size = "sparkle", (47.0, 42.0)
            title_width = 624.0

        header_y = box.y + padding_top
        inner_x = box.x + 42
        icon_box = Box(
            inner_x,
            header_y + (section_layout.header_height - icon_size[1]) / 2,
            icon_size[0],
            icon_size[1],
        )
        trailing_box = Box(
            box.right - 42 - trailing_size[0],
            header_y + (section_layout.header_height - trailing_size[1]) / 2,
            trailing_size[0],
            trailing_size[1],
        )
        self._paste_asset(canvas, icon_name, icon_box, unit)
        self._paste_asset(canvas, trailing_name, trailing_box, unit)

        title_box = Box(
            icon_box.right + 14,
            header_y + (section_layout.header_height - section_layout.title_height) / 2,
            title_width,
            section_layout.title_height,
        )
        self._draw_text(
            canvas,
            draw,
            (TextSpan(section.title, INK),),
            "display",
            36,
            title_box,
            46,
            unit,
        )

        divider_y = header_y + section_layout.header_height + 13
        if section.kind == "aftereffect":
            # 余波使用冷色虚线，与“昨日小结”的暖色装饰明确区分。
            start_x = self._px(inner_x, unit)
            end_x = self._px(inner_x + 754, unit)
            divider_y_px = self._px(divider_y + 2.5, unit)
            dash_width = max(1, self._px(13, unit))
            dash_gap = max(1, self._px(8, unit))
            stroke_width = max(1, self._px(1.5, unit))
            cursor_x = start_x
            while cursor_x < end_x:
                draw.line(
                    (cursor_x, divider_y_px, min(cursor_x + dash_width, end_x), divider_y_px),
                    fill=(*AFTEREFFECT_DIVIDER, 255),
                    width=stroke_width,
                )
                cursor_x += dash_width + dash_gap
        else:
            divider_name = "highlight_divider" if section.kind == "highlight" else "summary_divider"
            self._paste_asset(canvas, divider_name, Box(inner_x, divider_y, 754, 5), unit, shift_x=-3)
        body_y = divider_y + 5 + 13
        body_box = Box(inner_x, body_y, 754, section_layout.body_height)
        self._draw_text(canvas, draw, section.body, "body", 28, body_box, 44, unit)

    def render(
        self,
        data: CardData,
        width: int = 1122,
        supersample: int = 3,
        *,
        include_hero_asset: bool = True,
    ) -> tuple[Image.Image, CardLayout]:
        """渲染卡片并返回布局快照；输出高度始终由当前内容决定。"""

        if not math.isfinite(data.glass_opacity) or not 0.0 <= data.glass_opacity <= 1.0:
            raise ValueError("glass_opacity 必须位于 0.0 到 1.0 之间")
        layout = self.build_layout(data, width=width, supersample=supersample)
        output_scale = width / BASE_WIDTH
        unit = output_scale * supersample
        high_width = width * supersample
        output_height = int(round(layout.height * output_scale))
        high_height = output_height * supersample
        if data.background_asset is None:
            canvas = Image.new("RGBA", (high_width, high_height), (*PINK_BG, 255))
        else:
            canvas = self._background_canvas(data.background_asset, (high_width, high_height))
        # 毛玻璃只能模糊外层背景，不能把卡片自己的投影再次卷进材质内部。
        glass_backdrop = canvas.copy() if data.card_surface == "glass" else None

        shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        shadow_draw = ImageDraw.Draw(shadow)
        shadow_box = Box(layout.card.x, layout.card.y + 14, layout.card.width, layout.card.height)
        shadow_draw.rounded_rectangle(
            self._px_box(shadow_box, unit),
            radius=self._px(82, unit),
            fill=(77, 41, 26, 56),
        )
        shadow = shadow.filter(ImageFilter.GaussianBlur(self._px(11, unit)))
        canvas.alpha_composite(shadow)

        self._draw_card_surface(
            canvas,
            layout.card,
            unit,
            data.card_surface,
            glass_backdrop,
            data.glass_opacity,
        )
        draw = ImageDraw.Draw(canvas)

        self._draw_header(canvas, draw, data, layout, unit)
        self._draw_hero(
            canvas,
            draw,
            data,
            layout,
            unit,
            include_hero_asset=include_hero_asset,
        )
        self._draw_stats(canvas, draw, data, layout, unit)
        for section_layout in layout.sections:
            self._draw_section(canvas, draw, section_layout, unit)

        output = canvas.resize((width, output_height), Image.Resampling.LANCZOS).convert("RGB")
        return output, layout


# ================================ RollPig 生产适配 ================================ #
# 原型负责响应式布局；本板块只处理业务 ViewModel、图片严格解码、GIF 合成与回退。

_ACCENT_TEXT_PATTERN = re.compile(r"【[^】]+】|\d+")
_FOOTPRINT_ICON = {
    "escaped_count": "runner",
}
_FOOTPRINT_SUFFIX = {
    "reservation_result_count": " 场",
    "success_count": " 次",
    "roasted_count": " 次",
    "escaped_count": " 次",
    "backfire_count": " 次",
    "self_roast_count": " 次",
    "bot_backfire_count": " 次",
}


@dataclass
class _DecodedHeroFrames:
    """一次严格解码得到的角色帧；调用方必须在编码结束后主动关闭。"""

    frames: tuple[tuple[Image.Image, int], ...]
    animated: bool

    def close(self) -> None:
        for frame, _duration in self.frames:
            frame.close()


def _format_recap_date(date_str: str) -> str:
    try:
        parsed = dt.date.fromisoformat(date_str)
    except ValueError:
        return date_str
    return f"{parsed.month}月{parsed.day}日"


def _accent_text_spans(text: str) -> tuple[TextSpan, ...]:
    """只强调人物名与数字，避免整段经历全部染成强调色。"""

    spans: list[TextSpan] = []
    cursor = 0
    for match in _ACCENT_TEXT_PATTERN.finditer(text):
        if match.start() > cursor:
            spans.append(TextSpan(text[cursor : match.start()], BODY_INK))
        spans.append(TextSpan(match.group(0), ACCENT))
        cursor = match.end()
    if cursor < len(text):
        spans.append(TextSpan(text[cursor:], BODY_INK))
    return tuple(spans) or (TextSpan("", BODY_INK),)


def _experience_spans(recap: YesterdayRecap) -> tuple[TextSpan, ...]:
    spans: list[TextSpan] = []
    for index, experience in enumerate(recap.experiences):
        if index:
            spans.append(TextSpan("\n", BODY_INK))
        spans.append(TextSpan("• ", ACCENT))
        spans.extend(_accent_text_spans(experience.text))
    return tuple(spans)


def _fallback_outcome_text(outcome_text: str, *, image_fallback: bool) -> str:
    """差分图片失效时撤回外观宣称，但保留真实发生的等级或文案成长。"""

    if not image_fallback:
        return outcome_text
    if outcome_text.endswith(" · 新立绘与介绍已解锁"):
        return outcome_text.removesuffix(" · 新立绘与介绍已解锁") + " · 新介绍已解锁"
    if outcome_text.endswith(" · 新立绘已解锁"):
        return outcome_text.removesuffix(" · 新立绘已解锁")
    return outcome_text


def build_yesterday_card_data(
    recap: YesterdayRecap,
    *,
    hero_path: Path | None,
    image_fallback: bool = False,
) -> CardData:
    """把昨日回顾 ViewModel 转成与绘图实现解耦的卡片数据。"""

    stats = tuple(
        StatItem(
            icon=_FOOTPRINT_ICON.get(footprint.kind, "flame"),
            label=f"{footprint.label} ",
            value=str(footprint.count),
            suffix=_FOOTPRINT_SUFFIX.get(footprint.kind, " 次"),
        )
        for footprint in recap.footprints
    )

    sections: list[InfoSection] = []
    if recap.roll.is_makeup and (recap.pig_description or recap.pig_analysis):
        sections.append(
            InfoSection(
                kind="summary",
                title=recap.pig_description or "小猪介绍",
                body=(TextSpan(recap.pig_analysis, BODY_INK),),
            )
        )
    if recap.experiences:
        highlight_title = "昨日高光" if recap.scope == "group" else "昨日高光 · 跨群"
        sections.append(
            InfoSection(
                kind="highlight",
                title=highlight_title,
                body=_experience_spans(recap),
            )
        )
    if recap.summary is not None:
        sections.append(
            InfoSection(
                kind="summary",
                title="昨日小结",
                body=(TextSpan(recap.summary.text, BODY_INK),),
            )
        )
    if recap.aftereffect_text:
        sections.append(
            InfoSection(
                kind="aftereffect",
                title="今日余波",
                body=(TextSpan(recap.aftereffect_text, BODY_INK),),
            )
        )

    return CardData(
        title="昨日小猪 · 补签" if recap.roll.is_makeup else "昨日小猪",
        date=_format_recap_date(recap.date_str),
        role_prefix="补签抽到 " if recap.roll.is_makeup else "昨天你是 ",
        role=f"【{recap.pig_name}】",
        stats=stats,
        sections=tuple(sections),
        outcome_text=_fallback_outcome_text(
            recap.outcome_text,
            image_fallback=image_fallback,
        ),
        background_asset="backgrounds/pink_dream.jpg",
        hero_asset=str(hero_path) if hero_path is not None else None,
        show_hero_ambience=False,
        hero_decor_mode="random",
        hero_decor_seed=f"{recap.date_str}|{recap.group_id}|{recap.roll.pig_id}",
        card_surface="opaque",
    )


def _first_visible_frame(opened: Image.Image, image_path: Path) -> Image.Image:
    opened.seek(0)
    frame = opened.convert("RGBA")
    if frame.getchannel("A").getbbox() is None:
        frame.close()
        raise ValueError(f"角色图片没有可见像素: {image_path}")
    return frame


def _decode_hero_frames(image_path: Path) -> _DecodedHeroFrames:
    """严格读取静态图或 GIF；异常动图按现有普通卡预算降级为静态首帧。"""

    if not image_path.is_file():
        raise FileNotFoundError(f"角色图片不存在: {image_path}")

    decoded: list[Image.Image] = []
    try:
        with Image.open(image_path) as opened:
            frame_count = int(getattr(opened, "n_frames", 1) or 1)
            animated = bool(getattr(opened, "is_animated", False) and frame_count > 1)
            if not animated:
                return _DecodedHeroFrames(((_first_visible_frame(opened, image_path), 0),), False)

            width, height = opened.size
            decode_work_pixels = width * height * frame_count
            if (
                frame_count > GIF_ABSOLUTE_MAX_SOURCE_FRAMES
                or decode_work_pixels > GIF_MAX_DECODE_WORK_PIXELS
            ):
                logger.warning(
                    "RollPig 昨日卡片 GIF 超出解码预算，已使用静态首帧: "
                    f"file={image_path} size={width}x{height} frames={frame_count} "
                    f"pixel_frames={decode_work_pixels}/{GIF_MAX_DECODE_WORK_PIXELS}"
                )
                return _DecodedHeroFrames(((_first_visible_frame(opened, image_path), 0),), False)

            groups = _gif_frame_groups(frame_count)
            durations = [0] * len(groups)
            group_index = 0
            visible_frames = 0
            for index, frame in enumerate(ImageSequence.Iterator(opened)):
                while group_index + 1 < len(groups) and index >= groups[group_index][1]:
                    group_index += 1
                durations[group_index] += _normalize_gif_duration(
                    frame.info.get("duration", opened.info.get("duration"))
                )
                if index != groups[group_index][2]:
                    continue
                rgba_frame = frame.convert("RGBA")
                if rgba_frame.getchannel("A").getbbox() is not None:
                    visible_frames += 1
                decoded.append(rgba_frame)

            if len(decoded) != len(groups) or visible_frames <= 0:
                raise ValueError(
                    "GIF 没有得到完整可见帧: "
                    f"decoded={len(decoded)} expected={len(groups)} visible={visible_frames}"
                )
            return _DecodedHeroFrames(
                tuple(zip(decoded, durations)),
                len(decoded) > 1,
            )
    except Exception:
        for frame in decoded:
            frame.close()
        raise


def _encode_png(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(output, format="PNG", optimize=True, compress_level=9)
    return output.getvalue()


def _build_gif_palette(
    renderer: PillowCardRenderer,
    base_image: Image.Image,
    layout: CardLayout,
    frames: tuple[tuple[Image.Image, int], ...],
    *,
    hero_name: str,
) -> Image.Image:
    """用整卡缩略图构造共享调色板，避免常驻一批 RGB 全尺寸帧。"""

    sample_size = GIF_PALETTE_SAMPLE_SIZE
    palette_source = Image.new(
        "RGB",
        (sample_size, sample_size * (len(frames) + 1)),
        PINK_BG,
    )
    try:
        base_sample = base_image.resize((sample_size, sample_size), Image.Resampling.LANCZOS)
        try:
            palette_source.paste(base_sample, (0, 0))
        finally:
            base_sample.close()

        for index, (hero_frame, _duration) in enumerate(frames, start=1):
            composed = renderer.compose_hero_image(
                base_image,
                layout,
                hero_frame,
                width=YESTERDAY_CARD_WIDTH,
                name=hero_name,
            )
            try:
                sample = composed.resize((sample_size, sample_size), Image.Resampling.LANCZOS)
                try:
                    palette_source.paste(sample, (0, sample_size * index))
                finally:
                    sample.close()
            finally:
                composed.close()
        return palette_source.quantize(colors=256, method=Image.Quantize.MEDIANCUT)
    finally:
        palette_source.close()


def _encode_animated_card(
    renderer: PillowCardRenderer,
    base_image: Image.Image,
    layout: CardLayout,
    frames: tuple[tuple[Image.Image, int], ...],
    *,
    hero_name: str,
) -> bytes:
    """逐帧叠加角色并立即量化；只保留单字节索引帧控制内存峰值。"""

    palette = _build_gif_palette(
        renderer,
        base_image,
        layout,
        frames,
        hero_name=hero_name,
    )
    output_frames: list[Image.Image] = []
    durations: list[int] = []
    try:
        for hero_frame, duration in frames:
            composed = renderer.compose_hero_image(
                base_image,
                layout,
                hero_frame,
                width=YESTERDAY_CARD_WIDTH,
                name=hero_name,
            )
            try:
                output_frames.append(
                    composed.quantize(palette=palette, dither=Image.Dither.NONE)
                )
            finally:
                composed.close()
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
        return output.getvalue()
    finally:
        palette.close()
        for frame in output_frames:
            frame.close()


_YESTERDAY_CARD_RENDERER = PillowCardRenderer(
    YESTERDAY_CARD_RESOURCE_DIR,
    display_font_path=_resolve_font_path(plugin_config.rollpig_yesterday_card_title_font_path),
    body_font_path=_resolve_font_path(plugin_config.rollpig_yesterday_card_body_font_path),
)
_YESTERDAY_CARD_RENDER_LOCK = threading.RLock()


def _render_with_hero(
    recap: YesterdayRecap,
    hero_path: Path | None,
    decoded: _DecodedHeroFrames | None,
    *,
    image_fallback: bool,
) -> YesterdayCardRenderResult:
    data = build_yesterday_card_data(
        recap,
        hero_path=hero_path,
        image_fallback=image_fallback,
    )
    hero_name = str(hero_path) if hero_path is not None else None
    if hero_name is not None and decoded is not None:
        _YESTERDAY_CARD_RENDERER.prime_external_asset(hero_name, decoded.frames[0][0])

    base_image: Image.Image | None = None
    try:
        base_image, layout = _YESTERDAY_CARD_RENDERER.render(
            data,
            width=YESTERDAY_CARD_WIDTH,
            supersample=YESTERDAY_CARD_SUPERSAMPLE,
            include_hero_asset=False,
        )
        if decoded is None:
            payload = _encode_png(base_image)
            image_format = "png"
            renderer_name = "pillow-yesterday"
        elif decoded.animated:
            payload = _encode_animated_card(
                _YESTERDAY_CARD_RENDERER,
                base_image,
                layout,
                decoded.frames,
                hero_name=hero_name or "yesterday-pig",
            )
            image_format = "gif"
            renderer_name = "pillow-yesterday-gif"
        else:
            composed = _YESTERDAY_CARD_RENDERER.compose_hero_image(
                base_image,
                layout,
                decoded.frames[0][0],
                width=YESTERDAY_CARD_WIDTH,
                name=hero_name or "yesterday-pig",
            )
            try:
                payload = _encode_png(composed)
                output_width, output_height = composed.size
            finally:
                composed.close()
            return YesterdayCardRenderResult(
                data=payload,
                image_format="png",
                renderer="pillow-yesterday",
                width=output_width,
                height=output_height,
                used_fallback_image=image_fallback,
            )

        return YesterdayCardRenderResult(
            data=payload,
            image_format=image_format,
            renderer=renderer_name,
            width=base_image.width,
            height=base_image.height,
            used_fallback_image=image_fallback,
        )
    finally:
        if base_image is not None:
            base_image.close()
        _YESTERDAY_CARD_RENDERER.release_external_asset(hero_name)


def _render_yesterday_card_sync(recap: YesterdayRecap) -> YesterdayCardRenderResult:
    """依次尝试快照立绘、基础立绘和无图卡；单次渲染期间串行使用原型缓存。"""

    candidates: list[Path] = []
    for candidate in (recap.image_path, recap.fallback_image_path):
        if candidate is None:
            continue
        resolved = candidate.resolve()
        if resolved not in candidates:
            candidates.append(resolved)

    expected_name = Path(recap.roll.resolved_image_name).name if recap.roll.resolved_image_name else ""
    with _YESTERDAY_CARD_RENDER_LOCK:
        for index, candidate in enumerate(candidates):
            decoded: _DecodedHeroFrames | None = None
            image_fallback = bool(
                index > 0
                or (expected_name and candidate.name != expected_name)
            )
            try:
                decoded = _decode_hero_frames(candidate)
                return _render_with_hero(
                    recap,
                    candidate,
                    decoded,
                    image_fallback=image_fallback,
                )
            except Exception as error:
                logger.warning(
                    "RollPig 昨日卡片角色读取或合成失败，继续尝试基础立绘: "
                    f"pig_id={recap.roll.pig_id} file={candidate} error={error}"
                )
            finally:
                if decoded is not None:
                    decoded.close()

        # 基础图同样失效时仍保留昨日事实卡，不让单个资源文件拖垮命令。
        return _render_with_hero(
            recap,
            None,
            None,
            image_fallback=bool(candidates or expected_name),
        )


# ================================ 缓存键与资源指纹 ================================ #
def _normalize_yesterday_cache_value(value: object) -> object:
    """把冻结数据模型转换为稳定 JSON 值，避免集合顺序造成伪缓存失效。"""

    if isinstance(value, Path):
        return str(value.expanduser().resolve())
    if isinstance(value, dict):
        return {
            str(key): _normalize_yesterday_cache_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_normalize_yesterday_cache_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [_normalize_yesterday_cache_value(item) for item in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, default=str),
        )
    return value


def _file_content_digest(file_path: Path) -> str:
    """流式计算缓存依赖文件摘要，避免只靠大小或时间戳误命中旧成品。"""

    hasher = hashlib.sha256()
    with file_path.open("rb") as file_handle:
        while chunk := file_handle.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def _yesterday_source_signature(file_path: Path | None) -> tuple[object, ...] | None:
    """记录候选立绘内容；资源在摘要期间切换时返回 None 并跳过本次缓存。"""

    if file_path is None:
        return ("image-none",)
    resolved = file_path.expanduser().resolve()
    try:
        before = resolved.stat()
        if not resolved.is_file():
            return ("image-not-file", str(resolved))
        digest = _file_content_digest(resolved)
        after = resolved.stat()
    except FileNotFoundError:
        # 缺图卡也允许在当天复用；资源补齐后签名会自动变化。
        return ("image-missing", str(resolved))
    except OSError:
        return None

    before_identity = (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    after_identity = (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    if before_identity != after_identity:
        return None
    return ("image", str(resolved), after.st_size, digest)


def _yesterday_renderer_asset_metadata() -> tuple[tuple[object, ...], ...]:
    """列出实际渲染资源元数据，供内容摘要缓存识别资源替换。"""

    paths: set[Path] = set()
    renderer_root = _YESTERDAY_CARD_RENDERER.root.expanduser().resolve()
    try:
        paths.update(path.resolve() for path in renderer_root.rglob("*") if path.is_file())
    except OSError:
        pass
    paths.update(path.expanduser().resolve() for path in _YESTERDAY_CARD_RENDERER._font_paths.values())
    paths.add((PACKAGE_RESOURCE_DIR / "emoji" / "google-emoji.zip").resolve())

    metadata: list[tuple[object, ...]] = []
    for path in sorted(paths, key=str):
        try:
            stat = path.stat()
            if path.is_file():
                metadata.append(
                    (str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
                )
                continue
        except OSError:
            pass
        metadata.append((str(path), "missing"))
    return tuple(metadata)


@lru_cache(maxsize=8)
def _yesterday_renderer_asset_signature(
    metadata: tuple[tuple[object, ...], ...],
) -> str:
    """计算布局资源、当前字体和 Emoji 的统一内容指纹。"""

    hasher = hashlib.sha256()
    for item in metadata:
        hasher.update(
            json.dumps(item, ensure_ascii=False, separators=(",", ":"), default=str).encode(
                "utf-8"
            )
        )
        path = Path(str(item[0]))
        try:
            if len(item) > 1 and item[1] != "missing":
                hasher.update(_file_content_digest(path).encode("ascii"))
        except OSError:
            # 渲染资源在摘要阶段变化时，元数据会在写入前复核并阻止旧键落盘。
            hasher.update(b"asset-unavailable")
    return hasher.hexdigest()


def _yesterday_card_cache_key(recap: YesterdayRecap) -> tuple[object, ...] | None:
    """汇总回顾事实、两级立绘与全部绘图资源，生成完整成品缓存键。"""

    source_signatures: list[tuple[object, ...]] = []
    signature_cache: dict[str, tuple[object, ...] | None] = {}
    for image_path in (recap.image_path, recap.fallback_image_path):
        identity = (
            str(image_path.expanduser().resolve())
            if image_path is not None
            else "<image-none>"
        )
        if identity not in signature_cache:
            signature_cache[identity] = _yesterday_source_signature(image_path)
        signature = signature_cache[identity]
        if signature is None:
            return None
        source_signatures.append(signature)

    recap_payload = json.dumps(
        _normalize_yesterday_cache_value(asdict(recap)),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    asset_metadata = _yesterday_renderer_asset_metadata()
    return (
        YESTERDAY_CARD_CACHE_VERSION,
        hashlib.sha256(recap_payload).hexdigest(),
        tuple(source_signatures),
        _yesterday_renderer_asset_signature(asset_metadata),
        YESTERDAY_CARD_WIDTH,
        YESTERDAY_CARD_SUPERSAMPLE,
    )


# ================================ 当日目录与磁盘容器 ================================ #
def _remove_stale_yesterday_cache_entry(cache_root: Path, entry: Path) -> None:
    """只删除缓存根目录的直接子项，避免异常软链接把清理范围带出插件目录。"""

    try:
        resolved_root = cache_root.resolve()
        resolved_entry = entry.resolve(strict=False)
        if resolved_entry.parent != resolved_root:
            logger.warning(f"RollPig 昨日卡片缓存路径越界，已跳过清理: {entry}")
            return
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink(missing_ok=True)
    except OSError as error:
        logger.warning(f"RollPig 昨日卡片过期缓存清理失败: path={entry}, error={error}")


def _prepare_yesterday_cache_day() -> tuple[str, Path]:
    """切换到上海业务当天目录，并在首次访问或跨日时删除其他日期。"""

    global _yesterday_card_cache_active_root, _yesterday_card_cache_active_date

    cache_date = rollpig_today().isoformat()
    cache_root = YESTERDAY_CARD_CACHE_DIR.expanduser().resolve()
    cache_dir = cache_root / cache_date
    with _yesterday_card_cache_lock:
        if (
            _yesterday_card_cache_active_root != cache_root
            or _yesterday_card_cache_active_date != cache_date
        ):
            prepared = False
            try:
                cache_root.mkdir(parents=True, exist_ok=True)
                for entry in cache_root.iterdir():
                    if entry.name != cache_date:
                        _remove_stale_yesterday_cache_entry(cache_root, entry)
                cache_dir.mkdir(parents=True, exist_ok=True)
                prepared = True
            except OSError as error:
                logger.warning(
                    "RollPig 昨日卡片当日缓存目录准备失败，本次继续即时渲染: "
                    f"dir={cache_dir}, error={error}"
                )
            _yesterday_card_cache_active_root = cache_root if prepared else None
            _yesterday_card_cache_active_date = cache_date if prepared else None
        else:
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
    return cache_date, cache_dir


def _yesterday_card_cache_path(cache_dir: Path, key: tuple[object, ...]) -> Path:
    """把完整输入摘要为安全文件名，中文与本地绝对路径不直接进入磁盘文件名。"""

    serialized = json.dumps(
        key,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    digest = hashlib.sha256(serialized).hexdigest()
    return cache_dir / f"v{YESTERDAY_CARD_CACHE_VERSION}-{digest}.cache"


def _serialize_yesterday_card_cache(result: YesterdayCardRenderResult) -> bytes:
    """把图片和回退元数据写入单一容器，避免 sidecar 与正文不同步。"""

    header = json.dumps(
        {
            "image_format": result.image_format,
            "image_sha256": hashlib.sha256(result.data).hexdigest(),
            "renderer": result.renderer,
            "width": result.width,
            "height": result.height,
            "used_fallback_image": result.used_fallback_image,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return YESTERDAY_CARD_CACHE_MAGIC + len(header).to_bytes(4, "big") + header + result.data


def _deserialize_yesterday_card_cache(payload: bytes) -> YesterdayCardRenderResult:
    """严格校验本插件生成的 PNG/GIF 缓存容器。"""

    prefix_size = len(YESTERDAY_CARD_CACHE_MAGIC)
    if not payload.startswith(YESTERDAY_CARD_CACHE_MAGIC) or len(payload) < prefix_size + 4:
        raise ValueError("缓存魔数不匹配")
    header_size = int.from_bytes(payload[prefix_size : prefix_size + 4], "big")
    if not 0 < header_size <= YESTERDAY_CARD_CACHE_HEADER_MAX_BYTES:
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

    expected_digest = metadata.get("image_sha256")
    if not isinstance(expected_digest, str) or len(expected_digest) != 64:
        raise ValueError("缓存正文摘要缺失或非法")
    actual_digest = hashlib.sha256(image_data).hexdigest()
    if actual_digest != expected_digest:
        raise ValueError("缓存正文摘要不匹配")

    metadata_format = str(metadata.get("image_format") or detected_format).lower()
    if metadata_format != detected_format:
        raise ValueError(
            f"缓存格式不一致: metadata={metadata_format}, data={detected_format}"
        )

    width = int(metadata.get("width") or 0)
    height = int(metadata.get("height") or 0)
    if width <= 0 or height <= 0:
        raise ValueError(f"缓存尺寸非法: {width}x{height}")
    return YesterdayCardRenderResult(
        data=image_data,
        image_format=detected_format,
        renderer=str(metadata.get("renderer") or f"pillow-yesterday-{detected_format}"),
        width=width,
        height=height,
        used_fallback_image=bool(metadata.get("used_fallback_image")),
    )


def _remove_invalid_yesterday_card_cache(cache_file: Path, error: Exception) -> None:
    """隔离单个损坏缓存；清理失败不阻断即时渲染。"""

    logger.warning(f"RollPig 昨日卡片磁盘缓存损坏，已忽略: file={cache_file}, error={error}")
    try:
        cache_file.unlink(missing_ok=True)
    except OSError:
        pass


def _get_yesterday_card_cache(
    cache_dir: Path,
    key: tuple[object, ...],
) -> YesterdayCardRenderResult | None:
    """读取当日成品，并用 mtime 维护近似 LRU。"""

    cache_file = _yesterday_card_cache_path(cache_dir, key)
    with _yesterday_card_cache_lock:
        try:
            file_size = cache_file.stat().st_size
            if file_size > YESTERDAY_CARD_DISK_CACHE_MAX_BYTES:
                raise ValueError(f"缓存文件超过总容量: {file_size}")
            result = _deserialize_yesterday_card_cache(cache_file.read_bytes())
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as error:
            _remove_invalid_yesterday_card_cache(cache_file, error)
            return None
        try:
            os.utime(cache_file, None)
        except OSError:
            pass
        return result


def _yesterday_card_disk_cache_files(cache_dir: Path) -> list[tuple[Path, int, int]]:
    """列出当日缓存占用；临时文件也计入 64 MiB 总预算。"""

    entries: list[tuple[Path, int, int]] = []
    try:
        candidates = cache_dir.iterdir()
        for path in candidates:
            try:
                stat = path.stat()
            except OSError:
                continue
            if path.is_file():
                entries.append((path, stat.st_size, stat.st_mtime_ns))
    except OSError:
        return []
    return entries


def _trim_yesterday_card_disk_cache(cache_dir: Path) -> None:
    """按最近使用时间把当天的昨日卡片成品清到 64 MiB 内。"""

    entries = _yesterday_card_disk_cache_files(cache_dir)
    total_bytes = sum(size for _, size, _ in entries)
    if total_bytes <= YESTERDAY_CARD_DISK_CACHE_MAX_BYTES:
        return
    for path, size, _ in sorted(entries, key=lambda item: item[2]):
        if total_bytes <= YESTERDAY_CARD_DISK_CACHE_MAX_BYTES:
            break
        try:
            path.unlink(missing_ok=True)
        except OSError:
            continue
        total_bytes -= size


def _store_yesterday_card_cache(
    cache_date: str,
    key: tuple[object, ...],
    result: YesterdayCardRenderResult,
) -> bool:
    """原子写入当日成品；跨日任务和超大结果都不会落盘。"""

    is_valid_gif = result.image_format == "gif" and result.data.startswith(
        (b"GIF87a", b"GIF89a")
    )
    is_valid_png = result.image_format == "png" and result.data.startswith(
        b"\x89PNG\r\n\x1a\n"
    )
    if not (is_valid_gif or is_valid_png):
        return False

    payload = _serialize_yesterday_card_cache(result)
    if len(payload) > YESTERDAY_CARD_DISK_CACHE_MAX_BYTES:
        logger.warning(
            "RollPig 昨日卡片成品超过磁盘缓存总上限，本次不缓存: "
            f"bytes={len(payload)}/{YESTERDAY_CARD_DISK_CACHE_MAX_BYTES}"
        )
        return False

    cache_root = YESTERDAY_CARD_CACHE_DIR.expanduser().resolve()
    cache_dir = cache_root / cache_date
    cache_file = _yesterday_card_cache_path(cache_dir, key)
    temporary_file = cache_file.with_name(
        f".{cache_file.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    with _yesterday_card_cache_lock:
        # 午夜前启动的慢渲染不能在新业务日清理后重新写回旧目录。
        if rollpig_today().isoformat() != cache_date:
            return False
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            temporary_file.write_bytes(payload)
            os.replace(temporary_file, cache_file)
            _trim_yesterday_card_disk_cache(cache_dir)
        except OSError as error:
            logger.warning(
                "RollPig 昨日卡片磁盘缓存写入失败，本次继续发送即时结果: "
                f"file={cache_file}, error={error}"
            )
            try:
                temporary_file.unlink(missing_ok=True)
            except OSError:
                pass
            return False
    return True


def _read_yesterday_card_cache_input(
    recap: YesterdayRecap,
) -> tuple[str, tuple[object, ...] | None, YesterdayCardRenderResult | None]:
    """在线程中准备日期、计算完整摘要并读取磁盘，不阻塞 NoneBot 事件循环。"""

    for _ in range(2):
        cache_date, cache_dir = _prepare_yesterday_cache_day()
        cache_key = _yesterday_card_cache_key(recap)
        cached = (
            _get_yesterday_card_cache(cache_dir, cache_key)
            if cache_key is not None
            else None
        )
        if rollpig_today().isoformat() == cache_date:
            return cache_date, cache_key, cached
    return cache_date, cache_key, None


def _render_and_store_yesterday_card(
    recap: YesterdayRecap,
    cache_date: str,
    cache_key: tuple[object, ...],
) -> YesterdayCardRenderResult:
    """生成一次昨日卡片，并仅在日期、立绘和渲染资源均未变化时落盘。"""

    result = _render_yesterday_card_sync(recap)
    current_key = _yesterday_card_cache_key(recap)
    if current_key == cache_key:
        _store_yesterday_card_cache(cache_date, cache_key, result)
    return result


# ================================ 异步入口与生命周期 ================================ #
async def render_yesterday_recap_card(recap: YesterdayRecap) -> YesterdayCardRenderResult:
    """读取当日磁盘成品；同键请求合流后在线程与共享 Pillow 预算内生成。"""

    cache_date, cache_key, cached = await asyncio.to_thread(
        _read_yesterday_card_cache_input,
        recap,
    )
    if cached is not None:
        return cached

    if cache_key is None:
        async with catalog_render_budget("yesterday-card"):
            return await asyncio.to_thread(_render_yesterday_card_sync, recap)

    task_key: tuple[object, ...] = ("yesterday-card", cache_date, *cache_key)
    async with _yesterday_card_render_tasks_lock:
        render_task = _yesterday_card_render_tasks.get(task_key)
        if render_task is None:
            render_task = asyncio.create_task(
                _render_yesterday_card_once(task_key, recap, cache_date, cache_key)
            )
            _yesterday_card_render_tasks[task_key] = render_task
    # 单个命令取消时不取消共享任务，其他同时查看同一回顾的人仍可收到成品。
    return await asyncio.shield(render_task)


async def _render_yesterday_card_once(
    task_key: tuple[object, ...],
    recap: YesterdayRecap,
    cache_date: str,
    cache_key: tuple[object, ...],
) -> YesterdayCardRenderResult:
    """执行一次共享昨日卡片渲染，并在成功或失败后释放任务键。"""

    try:
        async with catalog_render_budget("yesterday-card"):
            return await asyncio.to_thread(
                _render_and_store_yesterday_card,
                recap,
                cache_date,
                cache_key,
            )
    finally:
        current_task = asyncio.current_task()
        async with _yesterday_card_render_tasks_lock:
            if _yesterday_card_render_tasks.get(task_key) is current_task:
                _yesterday_card_render_tasks.pop(task_key, None)


def get_yesterday_card_renderer_cache_stats() -> dict[str, int | str]:
    """返回当天磁盘缓存占用，供运行诊断和回归测试。"""

    cache_date, cache_dir = _prepare_yesterday_cache_day()
    entries = _yesterday_card_disk_cache_files(cache_dir)
    return {
        "date": cache_date,
        "final_entries": len(entries),
        "final_bytes": sum(size for _, size, _ in entries),
    }


def clear_yesterday_card_renderer_caches() -> None:
    """释放进程内绘图资源并重置日期状态；当天磁盘成品继续跨重启复用。"""

    global _yesterday_card_cache_active_root, _yesterday_card_cache_active_date

    with _YESTERDAY_CARD_RENDER_LOCK:
        _YESTERDAY_CARD_RENDERER.clear_caches()
    _yesterday_renderer_asset_signature.cache_clear()
    with _yesterday_card_cache_lock:
        _yesterday_card_cache_active_root = None
        _yesterday_card_cache_active_date = None


async def shutdown_yesterday_card_renderer() -> None:
    """等待在途昨日卡片完成后释放 Pillow 资源；磁盘缓存保留到跨日清理。"""

    async with _yesterday_card_render_tasks_lock:
        render_tasks = list(_yesterday_card_render_tasks.values())
    if render_tasks:
        await asyncio.gather(*render_tasks, return_exceptions=True)
    clear_yesterday_card_renderer_caches()
