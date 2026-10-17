from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import math
import re
from dataclasses import asdict, dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Iterable, Literal, Mapping, Sequence

from ..host import logger
from pilmoji import Pilmoji
from pilmoji.helpers import getsize as pilmoji_getsize
from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps

from .card_renderer import (
    EMOJI_SCALE_FACTOR,
    _has_emoji_candidate,
    _normalize_extra_emoji_symbols,
    _tokenize_text,
    get_noto_emoji_source,
)
from .config import plugin_config
from .daily_report import (
    DailyReport,
    HeadlineKind,
    NormalizedDailyEvent,
    ObservationKind,
    RankingKind,
    TimelineKind,
    TimelineSelection,
    reservation_party_size,
)
from .resource_manager import get_pig_by_id, pig_resource_manager
from .texts import (
    DAILY_REPORT_HEADLINE_TEXTS,
    DAILY_REPORT_OBSERVATION_TEXTS,
    DAILY_REPORT_TIMELINE_DETAIL_TEXTS,
    DAILY_REPORT_TIMELINE_INTRO_TEXTS,
)


# ================================ 数据模型 ================================ #

RGB = tuple[int, int, int]
FontRole = Literal["display", "body", "regular", "black", "fallback"]


@dataclass(frozen=True)
class RichSpan:
    """一段动态正文；Pillow 会保持颜色并参与自动换行。"""

    text: str
    color: RGB
    keep_together: bool = False


@dataclass(frozen=True)
class StatItem:
    """数据速览项目；asset 可传内置资源名或绝对图片路径。"""

    label: str
    value: str
    unit: str
    asset: str


@dataclass(frozen=True)
class Observation:
    """今日观察；正文与插画可独立替换。"""

    spans: tuple[RichSpan, ...]
    asset: str = "observation_variety"
    paw_asset: str | None = "observation_paw"


@dataclass(frozen=True)
class Headline:
    """今日头条；标签数量与正文长度都可变化。"""

    spans: tuple[RichSpan, ...]
    tags: tuple[str, ...]
    asset: str = "headline_roast_success"


@dataclass(frozen=True)
class EventItem:
    """事件追踪的一条记录。"""

    title: str
    detail: str
    asset: str
    color: RGB


@dataclass(frozen=True)
class RankingEntry:
    """排行中的一行；单栏最多渲染三行。"""

    name: str
    detail: str
    avatar: str
    rank: int = 0


@dataclass(frozen=True)
class RankingColumn:
    """一个排行榜栏目。"""

    title: str
    subtitle: str
    entries: tuple[RankingEntry, ...]


@dataclass(frozen=True)
class Coupon:
    """次日保护券；name 与 benefit 使用动态正文字体，避免子集缺字。"""

    name: str
    benefit: str
    expires: str
    roast_count: int = 0
    shield_asset: str = "coupon_shield"
    barcode_asset: str = "coupon_barcode"


@dataclass(frozen=True)
class DailyReportCardData:
    """日报完整数据；值为 None 或空数组的区域会自动收起。"""

    volume: str
    date_year: str
    date_month_day: str
    weekday: str
    stats: tuple[StatItem, ...]
    observation: Observation | None
    headline: Headline | None
    event_intro: str
    events: tuple[EventItem, ...]
    rankings: tuple[RankingColumn, ...]
    coupon: Coupon | None
    footer: str = "数据截止 23:45 · 仅统计本群当日记录"
    background_asset: str = "clean-paper-original-tone"
    header_asset: str = "header_pig_news"


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
class DailyReportCardLayout:
    """渲染前计算出的逻辑布局，便于测试及未来接入 RollPig Plus。"""

    width: float
    height: float
    boxes: dict[str, Box]
    stat_cells: list[Box] = field(default_factory=list)
    ranking_visible_counts: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "width": self.width,
            "height": self.height,
            "boxes": {name: asdict(box) for name, box in self.boxes.items()},
            "stat_cells": [asdict(box) for box in self.stat_cells],
            "ranking_visible_counts": self.ranking_visible_counts,
        }


# ================================ 设计常量 ================================ #

BASE_WIDTH = 1024.0
CONTENT_X = 45.0
CONTENT_WIDTH = 934.0
TOP_PADDING = 28.0
BOTTOM_PADDING = 14.0
EVENT_ROW_HEIGHT = 104.0

PAPER = (232, 214, 178)
INK = (18, 16, 13)
RED = (140, 31, 19)
RIBBON_RED = (157, 44, 29)
CREAM = (245, 235, 209)
MUTED = (122, 110, 92)
GOLD = (178, 110, 28)
RANK_TWO = (99, 112, 110)
RANK_THREE = (148, 71, 31)
BLUE = (26, 82, 87)

ASSET_BOXES: dict[str, tuple[float, float]] = {
    "header_pig_news": (178, 154),
    "stats_people": (112, 88),
    "stats_flame": (91, 98),
    "stats_calendar": (96, 100),
    "observation_variety": (162, 177),
    "observation_paw": (77, 78),
    "headline_roast_success": (264, 247),
    "event_fire": (38, 50),
    "event_run": (38, 49),
    "event_angry": (38, 50),
    "coupon_shield": (96, 96),
    "coupon_barcode": (97, 143),
}

# 绝大多数插画需要透明通道，继续使用 PNG；只有确认不透明且适合有损压缩的
# 大面积底图使用 JPG。显式登记文件名，避免同名旧文件残留时加载到错误格式。
_BUILTIN_ASSET_FILES: dict[str, str] = {
    "clean-paper-original-tone": "clean-paper-original-tone.jpg",
    "coupon_surface": "coupon_surface.jpg",
}

_CLOSING_PUNCTUATION = set("，。！？；：、）》】」』…,.!?;:")


# ================================ 字体与资源 ================================ #


class _FontBook:
    """按文字角色解析字体；固定报头使用子集，动态正文复用插件现有完整字体。"""

    def __init__(self, root: Path, body_font: Path | str | None = None) -> None:
        self.root = root
        # 字体属于整个插件的渲染基础设施，不跟随某张卡片的图片素材目录。
        self.font_root = root.parent / "fonts"
        self.display_path = self.font_root / "RollPigDailySerif-Bold.subset.otf"
        self.regular_path = self.font_root / "RollPigDailySerif-Regular.subset.otf"
        self.black_path = self.font_root / "RollPigDailySerif-Black.subset.otf"
        # 固定标题继续使用轻量子集；用户昵称、猪名与事件正文整段使用同一
        # 完整字体。不能按单字切换字体，否则同一句会混排且测量结果失真。
        self.body_path = self._resolve_body_font(body_font)
        self.fallback_path = self.body_path
        charset_path = self.font_root / "RollPigDailySerif.charset.txt"
        if not charset_path.is_file():
            raise FileNotFoundError(f"找不到宋体子集字符表: {charset_path}")
        self.serif_charset = set(charset_path.read_text(encoding="utf-8"))
        self._cache: dict[tuple[FontRole, int], ImageFont.FreeTypeFont] = {}

    def _resolve_body_font(self, configured: Path | str | None) -> Path:
        candidates: list[Path] = []
        if configured:
            candidates.append(Path(configured).expanduser())
        candidates.extend(
            [
                self.font_root / "NotoSerifSC-Bold.otf",
                self.font_root / "SourceHanSansSC-Medium.otf",
                self.display_path,
            ]
        )
        for path in candidates:
            if path.is_file():
                return path.resolve()
        raise FileNotFoundError(
            "找不到日报正文字体；请传入 body_font，或保留插件现有思源正文字体。"
        )

    def get(self, role: FontRole, size: float, unit: float) -> ImageFont.FreeTypeFont:
        pixel_size = max(1, int(round(size * unit)))
        key = (role, pixel_size)
        if key not in self._cache:
            path = {
                "display": self.display_path,
                "body": self.body_path,
                "regular": self.regular_path,
                "black": self.black_path,
                "fallback": self.fallback_path,
            }[role]
            if not path.is_file():
                raise FileNotFoundError(f"找不到字体文件: {path}")
            self._cache[key] = ImageFont.truetype(str(path), pixel_size)
        return self._cache[key]

    def text_role(self, text: str) -> FontRole:
        """动态内容整段使用完整正文字体，禁止在同一行内按字符切换字体。"""

        return "body"

    def subset_supports(self, text: str) -> bool:
        """判断固定标题子集能否完整绘制一段短文字。"""

        return all(character in self.serif_charset for character in text)

    def resolve_role(self, text: str, role: FontRole) -> FontRole:
        """固定子集缺任一字符时整段回退完整字体，避免静默漏字和局部混排。"""

        if role == "body":
            return self.text_role(text)
        if role in {"display", "regular", "black"} and not self.subset_supports(text):
            return "body"
        return role


@dataclass(frozen=True)
class _StyledToken:
    text: str
    color: RGB
    role: FontRole


@dataclass(frozen=True)
class _RichLine:
    tokens: tuple[_StyledToken, ...]
    width_px: float


class DailyReportCardRenderer:
    """以 Figma 逻辑尺寸测量并绘制无分页、动态高度的日报。"""

    def __init__(self, root: Path | str | None = None, body_font: Path | str | None = None) -> None:
        self.root = Path(root or Path(__file__).resolve().parent)
        self.image_dir = self.root
        self.fonts = _FontBook(self.root, body_font)
        self._image_cache: dict[str, Image.Image] = {}
        self._texture_cache: dict[tuple[str, int, int], Image.Image] = {}

    @staticmethod
    def _display_units(text: str) -> list[str]:
        """把 ZWJ/变体 Emoji 保持为完整显示单元，其余文本仍按字符截断。"""

        units: list[str] = []
        for token in _tokenize_text(text):
            if _has_emoji_candidate(token):
                units.append(token)
            else:
                units.extend(token)
        return units

    @staticmethod
    def _measure_text(text: str, font: ImageFont.ImageFont) -> float:
        """按 Pilmoji 实际贴图宽度测量 Emoji；失败时退回 Pillow 字宽。"""

        if text and _has_emoji_candidate(text) and get_noto_emoji_source() is not None:
            try:
                width, _ = pilmoji_getsize(
                    _normalize_extra_emoji_symbols(text),
                    font=font,
                    spacing=0,
                    emoji_scale_factor=EMOJI_SCALE_FACTOR,
                )
                return float(width)
            except Exception as error:
                logger.debug(f"RollPig 日报 Emoji 测量失败，回退普通字体: text={text!r} error={error}")
        return float(font.getlength(text))

    @staticmethod
    def _draw_text_with_emoji(
        draw: ImageDraw.ImageDraw,
        position: tuple[int | float, int | float],
        text: str,
        *,
        font: ImageFont.ImageFont,
        fill: tuple[int, int, int, int],
        unit: float,
        anchor: str,
        stroke_width: int = 0,
        stroke_fill: tuple[int, int, int, int] | None = None,
        emoji_position_offset_y: float = -2,
    ) -> None:
        """复用现有 Noto Emoji ZIP 混排；资源异常时保持普通文字降级。"""

        if text and _has_emoji_candidate(text):
            source = get_noto_emoji_source()
            canvas = getattr(draw, "_image", None)
            if source is not None and isinstance(canvas, Image.Image):
                try:
                    emoji_offset = (0, int(round(emoji_position_offset_y * unit)))
                    with Pilmoji(
                        canvas,
                        source=source,
                        draw=draw,
                        render_discord_emoji=False,
                        emoji_scale_factor=EMOJI_SCALE_FACTOR,
                        emoji_position_offset=emoji_offset,
                    ) as emoji_drawer:
                        emoji_drawer.text(
                            position,
                            _normalize_extra_emoji_symbols(text),
                            font=font,
                            fill=fill,
                            anchor=anchor,
                            spacing=0,
                            stroke_width=stroke_width,
                            stroke_fill=stroke_fill,
                            emoji_scale_factor=EMOJI_SCALE_FACTOR,
                            emoji_position_offset=emoji_offset,
                        )
                    return
                except Exception as error:
                    logger.debug(f"RollPig 日报 Emoji 绘制失败，回退普通字体: text={text!r} error={error}")
        draw.text(
            position,
            text,
            font=font,
            fill=fill,
            anchor=anchor,
            stroke_width=stroke_width,
            stroke_fill=stroke_fill,
        )

    def _asset(self, name: str) -> Image.Image:
        requested = Path(name)
        if requested.is_absolute():
            # 排行会不断出现新的小猪图片，外部资源不能进入永久缓存，否则长期运行
            # 会按每天出现过的猪持续占用解码后的 RGBA 内存。
            try:
                with Image.open(requested) as opened:
                    opened.seek(0)
                    return opened.convert("RGBA")
            except (OSError, ValueError) as error:
                logger.warning(
                    f"RollPig 日报排行图片读取失败，改用报纸猪章: file={requested} error={error}"
                )
                return self._asset("header_pig_news").copy()

        if name not in self._image_cache:
            path = self.image_dir / _BUILTIN_ASSET_FILES.get(name, f"{name}.png")
            if not path.is_file():
                raise FileNotFoundError(f"找不到图片资源: {path}")
            with Image.open(path) as opened:
                self._image_cache[name] = opened.convert("RGBA")
        return self._image_cache[name]

    def close(self) -> None:
        """释放内置图片和纹理缓存，供插件关闭或热重载时收束资源。"""

        for image in self._image_cache.values():
            image.close()
        for image in self._texture_cache.values():
            image.close()
        self._image_cache.clear()
        self._texture_cache.clear()

    def _tokens(self, spans: Sequence[RichSpan]) -> list[_StyledToken]:
        result: list[_StyledToken] = []
        for span in spans:
            if span.keep_together:
                # 用户名、猪名和结果短语需要作为一个排版单元；只在它自身已经
                # 宽于整栏时才由 _wrap_rich 继续逐字拆分。
                for part in re.split(r"(\n)", span.text):
                    if part:
                        result.append(_StyledToken(part, span.color, "body"))
                continue
            for token in _tokenize_text(span.text):
                if token == "\n":
                    result.append(_StyledToken(token, span.color, "body"))
                    continue
                result.append(_StyledToken(token, span.color, "body"))
        return result

    def _wrap_rich(
        self,
        spans: Sequence[RichSpan],
        size: float,
        max_width: float,
        unit: float,
    ) -> tuple[_RichLine, ...]:
        max_width_px = max_width * unit
        lines: list[_RichLine] = []
        current: list[_StyledToken] = []
        current_width = 0.0

        for token in self._tokens(spans):
            if token.text == "\n":
                lines.append(_RichLine(tuple(current), current_width))
                current = []
                current_width = 0.0
                continue
            font = self.fonts.get(token.role, size, unit)
            pieces = [token]
            if self._measure_text(token.text, font) > max_width_px:
                pieces = [
                    _StyledToken(unit_text, token.color, token.role)
                    for unit_text in self._display_units(token.text)
                ]
            for piece in pieces:
                font = self.fonts.get(piece.role, size, unit)
                piece_width = self._measure_text(piece.text, font)
                is_space = piece.text.isspace()
                if not current and is_space:
                    continue
                should_wrap = bool(current) and current_width + piece_width > max_width_px
                if should_wrap and piece.text[0] in _CLOSING_PUNCTUATION:
                    should_wrap = False
                if should_wrap:
                    lines.append(_RichLine(tuple(current), current_width))
                    current = []
                    current_width = 0.0
                    if is_space:
                        continue
                current.append(piece)
                current_width += piece_width
        if current or not lines:
            lines.append(_RichLine(tuple(current), current_width))
        return tuple(lines)

    def _measure_rich(
        self,
        spans: Sequence[RichSpan],
        size: float,
        max_width: float,
        line_height: float,
        unit: float,
    ) -> float:
        return len(self._wrap_rich(spans, size, max_width, unit)) * line_height

    # ================================ 动态文本约束 ================================ #

    def _fit_single_line(
        self,
        text: str,
        max_width: float,
        size: float,
        unit: float,
        role: FontRole = "body",
    ) -> str:
        """把不可换行字段限制在给定像素宽度内，必要时在末尾加省略号。"""

        normalized = re.sub(r"\s+", " ", text).strip()
        if not normalized:
            return ""
        resolved_role = self.fonts.text_role(normalized) if role == "body" else role
        font = self.fonts.get(resolved_role, size, unit)
        max_width_px = max(0.0, max_width * unit)
        if self._measure_text(normalized, font) <= max_width_px:
            return normalized

        ellipsis = "…"
        if self._measure_text(ellipsis, font) > max_width_px:
            return ""
        units = self._display_units(normalized)
        low = 0
        high = len(units)
        while low < high:
            middle = (low + high + 1) // 2
            candidate = "".join(units[:middle]).rstrip() + ellipsis
            if self._measure_text(candidate, font) <= max_width_px:
                low = middle
            else:
                high = middle - 1
        return "".join(units[:low]).rstrip() + ellipsis

    def _fit_text_size(
        self,
        text: str,
        max_width: float,
        max_size: int,
        min_size: int,
        unit: float,
        role: FontRole = "body",
    ) -> tuple[str, float]:
        """优先缩小短字段，达到最小字号后再省略，避免保护券姓名越界。"""

        normalized = re.sub(r"\s+", " ", text).strip()
        resolved_role = self.fonts.text_role(normalized) if role == "body" else role
        for candidate_size in range(max_size, min_size - 1, -1):
            font = self.fonts.get(resolved_role, candidate_size, unit)
            if self._measure_text(normalized, font) <= max_width * unit:
                return normalized, float(candidate_size)
        return (
            self._fit_single_line(normalized, max_width, min_size, unit, role),
            float(min_size),
        )

    def _fit_wrapped_text_size(
        self,
        text: str,
        max_width: float,
        max_size: int,
        min_size: int,
        max_lines: int,
        unit: float,
        role: FontRole = "body",
    ) -> tuple[tuple[str, ...], float]:
        """优先缩字并允许有限换行，避免长昵称破坏保护券的横向结构。"""

        normalized = re.sub(r"\s+", " ", text).strip()
        if not normalized or max_lines <= 0:
            return (), float(min_size)
        resolved_role = self.fonts.text_role(normalized) if role == "body" else role

        def wrap(candidate_size: int) -> tuple[str, ...]:
            font = self.fonts.get(resolved_role, candidate_size, unit)
            max_width_px = max_width * unit
            lines: list[str] = []
            current = ""
            for text_unit in self._display_units(normalized):
                candidate = current + text_unit
                if current and self._measure_text(candidate, font) > max_width_px:
                    lines.append(current.rstrip())
                    current = text_unit.lstrip()
                else:
                    current = candidate
            if current or not lines:
                lines.append(current.rstrip())
            return tuple(lines)

        for candidate_size in range(max_size, min_size - 1, -1):
            lines = wrap(candidate_size)
            if len(lines) <= max_lines:
                return lines, float(candidate_size)

        lines = wrap(min_size)
        if len(lines) <= max_lines:
            return lines, float(min_size)
        visible = list(lines[: max_lines - 1])
        hidden_tail = "".join(lines[max_lines - 1 :])
        visible.append(
            self._fit_single_line(hidden_tail, max_width, min_size, unit, resolved_role)
        )
        return tuple(visible), float(min_size)

    def _fit_event_intro(
        self,
        text: str,
        max_width: float,
        unit: float,
    ) -> tuple[tuple[str, ...], float, float]:
        """让事件追踪开场优先缩字，最小字号仍超高时才省略。"""

        lines, size = self._fit_wrapped_text_size(
            text,
            max_width,
            22,
            16,
            3,
            unit,
        )
        line_height = max(24.0, round(size * 34 / 22, 1))
        return lines, size, line_height

    def _fit_wrapped_lines(
        self,
        text: str,
        max_width: float,
        size: float,
        max_lines: int,
        unit: float,
        role: FontRole = "body",
    ) -> tuple[str, ...]:
        """把窄栏正文换成有限行数，超出的内容只在最后一行省略。"""

        normalized = re.sub(r"\s+", " ", text).strip()
        if not normalized or max_lines <= 0:
            return ()
        spans = (RichSpan(normalized, INK),)
        wrapped = self._wrap_rich(spans, size, max_width, unit)
        lines = ["".join(token.text for token in line.tokens) for line in wrapped]
        if len(lines) <= max_lines:
            return tuple(lines)

        visible = lines[:max_lines]
        hidden_tail = "".join(lines[max_lines - 1 :]) + "…"
        visible[-1] = self._fit_single_line(
            hidden_tail,
            max_width,
            size,
            unit,
            role,
        )
        return tuple(visible)

    def _fit_rich_block(
        self,
        spans: Sequence[RichSpan],
        max_width: float,
        max_height: float,
        max_size: int,
        min_size: int,
        unit: float,
    ) -> tuple[float, float, float]:
        """在固定栏目内缩放富文本，并返回字号、行高和实际文字高度。"""

        for candidate_size in range(max_size, min_size - 1, -1):
            line_height = round(candidate_size * 42 / 29, 1)
            text_height = self._measure_rich(
                spans,
                candidate_size,
                max_width,
                line_height,
                unit,
            )
            if text_height <= max_height:
                return float(candidate_size), line_height, text_height

        line_height = round(min_size * 42 / 29, 1)
        text_height = self._measure_rich(
            spans,
            min_size,
            max_width,
            line_height,
            unit,
        )
        max_lines = max(1, int(max_height // line_height))
        return float(min_size), line_height, min(text_height, max_lines * line_height)

    def _limit_rich_lines(
        self,
        lines: Sequence[_RichLine],
        max_lines: int,
        size: float,
        max_width: float,
        unit: float,
    ) -> tuple[_RichLine, ...]:
        """把超高富文本收口到固定行数，并保持末尾 Emoji 簇与颜色完整。"""

        if len(lines) <= max_lines:
            return tuple(lines)
        visible = list(lines[:max_lines])
        tokens = list(visible[-1].tokens)
        ellipsis_color = tokens[-1].color if tokens else INK
        ellipsis = _StyledToken("…", ellipsis_color, "body")
        max_width_px = max_width * unit

        def measured(items: Sequence[_StyledToken]) -> float:
            return sum(
                self._measure_text(
                    item.text,
                    self.fonts.get(item.role, size, unit),
                )
                for item in items
            )

        while tokens and measured((*tokens, ellipsis)) > max_width_px:
            last = tokens[-1]
            units = self._display_units(last.text)
            if len(units) <= 1:
                tokens.pop()
                continue
            tokens[-1] = _StyledToken(
                "".join(units[:-1]),
                last.color,
                last.role,
            )
        tokens.append(ellipsis)
        visible[-1] = _RichLine(tuple(tokens), measured(tokens))
        return tuple(visible)

    def _headline_tag_rows(
        self,
        tags: Sequence[str],
        max_width: float,
        unit: float,
    ) -> int:
        """按实际字宽计算头条标签行数，不能再按固定三枚一行估算。"""

        if not tags:
            return 0
        font = self.fonts.get("body", 19, unit)
        rows = 1
        occupied = 0.0
        for tag in tags:
            tag_width = min(max_width, max(108.0, font.getlength(tag) / unit + 30.0))
            required = tag_width if occupied == 0 else tag_width + 12.0
            if occupied and occupied + required > max_width:
                rows += 1
                occupied = tag_width
            else:
                occupied += required
        return rows


# ================================ 动态布局 ================================ #

    def build_layout(self, data: DailyReportCardData, measure_unit: float = 2.0) -> DailyReportCardLayout:
        """先测量动态文字和列表，再按模块流计算最终画布高度。"""

        boxes: dict[str, Box] = {}
        stat_cells: list[Box] = []
        y = TOP_PADDING

        boxes["header"] = Box(CONTENT_X, y, CONTENT_WIDTH, 238)
        y += 242

        if data.stats:
            columns = min(3, len(data.stats))
            rows = math.ceil(len(data.stats) / columns)
            stats_height = 48 + rows * 122
            boxes["stats"] = Box(CONTENT_X, y, CONTENT_WIDTH, stats_height)
            cell_width = CONTENT_WIDTH / columns
            for index in range(len(data.stats)):
                row = index // columns
                column = index % columns
                stat_cells.append(
                    Box(CONTENT_X + column * cell_width, y + 38 + row * 122, cell_width, 122)
                )
            y += stats_height + 3

        has_left = data.observation is not None or data.headline is not None
        has_events = bool(data.events or data.event_intro)
        if has_left or has_events:
            event_width = 266.0 if has_left and has_events else 0.0
            left_width = CONTENT_WIDTH - event_width
            left_height = 0.0
            left_y = y

            if data.observation is not None:
                # 观察栏保持 Figma 的固定高度；正文在绘制阶段按底板内部空间
                # 缩放和垂直居中，不能再靠向下拉长底色掩盖越界。
                observation_height = 218.0
                boxes["observation"] = Box(CONTENT_X, left_y, left_width, observation_height)
                left_y += observation_height
                left_height += observation_height

            if data.headline is not None:
                asset_width = min(264.0, left_width * 0.40)
                text_width = max(1.0, left_width - asset_width - 42.0)
                text_height = self._measure_rich(data.headline.spans, 29, text_width, 43, measure_unit)
                tag_width = max(1.0, left_width - asset_width - 30.0)
                tag_rows = self._headline_tag_rows(
                    data.headline.tags,
                    tag_width,
                    measure_unit,
                )
                # 标签跟在正文后向下排布；额外留出正文/标签间距和底部安全区，
                # 避免长头条把最后一行文字挤进标签框。
                headline_height = max(
                    270.0,
                    87.0 + text_height + 48.0 * max(1, tag_rows),
                )
                boxes["headline"] = Box(CONTENT_X, left_y, left_width, headline_height)
                left_height += headline_height

            event_height = 0.0
            if has_events:
                event_box_width = event_width if event_width else CONTENT_WIDTH
                intro_width = event_box_width - 36.0
                intro_lines, _, intro_line_height = self._fit_event_intro(
                    data.event_intro,
                    intro_width,
                    measure_unit,
                )
                intro_height = len(intro_lines) * intro_line_height
                event_height = (
                    86.0
                    + intro_height
                    + 15.0
                    + len(data.events) * EVENT_ROW_HEIGHT
                    + 12.0
                )
                event_x = CONTENT_X + left_width if event_width else CONTENT_X
                boxes["events"] = Box(event_x, y, event_box_width, event_height)

            main_height = max(left_height, event_height)
            boxes["main"] = Box(CONTENT_X, y, CONTENT_WIDTH, main_height)
            y += main_height + 7

        if data.rankings:
            visible_counts = [min(3, len(column.entries)) for column in data.rankings]
            max_rows = max(visible_counts, default=0)
            # Figma 正式动态版：48.6px 标题区、66.6px 栏头、79px 行高和 28px 注记。
            ranking_height = 48.6 + 66.6 + max_rows * 79.0 + 28.4
            boxes["ranking"] = Box(CONTENT_X, y, CONTENT_WIDTH, ranking_height)
            y += ranking_height + 7
        else:
            visible_counts = []

        if data.coupon is not None:
            boxes["coupon"] = Box(CONTENT_X, y, CONTENT_WIDTH, 145)
            y += 154

        boxes["footer"] = Box(CONTENT_X, y, CONTENT_WIDTH, 43)
        y += 43 + BOTTOM_PADDING
        return DailyReportCardLayout(BASE_WIDTH, y, boxes, stat_cells, visible_counts)


# ================================ 基础绘图工具 ================================ #

    @staticmethod
    def _px(value: float, unit: float) -> int:
        return int(round(value * unit))

    def _scaled_box(self, box: Box, unit: float) -> tuple[int, int, int, int]:
        return (
            self._px(box.x, unit),
            self._px(box.y, unit),
            self._px(box.right, unit),
            self._px(box.bottom, unit),
        )

    def _crown(self, draw: ImageDraw.ImageDraw, center_x: float, center_y: float, unit: float) -> None:
        """绘制小型报纸皇冠，避免依赖正文字体是否包含符号字形。"""

        points = [
            (center_x - 9, center_y - 5),
            (center_x - 5, center_y + 3),
            (center_x, center_y - 7),
            (center_x + 5, center_y + 3),
            (center_x + 9, center_y - 5),
            (center_x + 7, center_y + 6),
            (center_x - 7, center_y + 6),
            (center_x - 9, center_y - 5),
        ]
        self._line(draw, points, GOLD, 1.6, unit)
        self._line(draw, [(center_x - 7, center_y + 9), (center_x + 7, center_y + 9)], GOLD, 1.6, unit)

    def _sparkle(self, draw: ImageDraw.ImageDraw, center_x: float, center_y: float, unit: float) -> None:
        """绘制排行标题两侧四角星，尺寸对应 Figma 的 27px 文字符号。"""

        points = (
            (center_x, center_y - 11),
            (center_x + 5, center_y),
            (center_x, center_y + 11),
            (center_x - 5, center_y),
        )
        draw.polygon(
            [(self._px(x, unit), self._px(y, unit)) for x, y in points],
            fill=(*INK, 255),
        )

    def _line(
        self,
        draw: ImageDraw.ImageDraw,
        points: Sequence[tuple[float, float]],
        fill: RGB,
        width: float,
        unit: float,
    ) -> None:
        draw.line(
            [(self._px(x, unit), self._px(y, unit)) for x, y in points],
            fill=(*fill, 255),
            width=max(1, self._px(width, unit)),
        )

    def _dashed_line(
        self,
        draw: ImageDraw.ImageDraw,
        start: tuple[float, float],
        end: tuple[float, float],
        fill: RGB,
        width: float,
        dash: float,
        gap: float,
        unit: float,
    ) -> None:
        x1, y1 = start
        x2, y2 = end
        length = math.hypot(x2 - x1, y2 - y1)
        if length <= 0:
            return
        dx = (x2 - x1) / length
        dy = (y2 - y1) / length
        cursor = 0.0
        while cursor < length:
            segment_end = min(length, cursor + dash)
            self._line(
                draw,
                [(x1 + dx * cursor, y1 + dy * cursor), (x1 + dx * segment_end, y1 + dy * segment_end)],
                fill,
                width,
                unit,
            )
            cursor += dash + gap

    def _paste_asset(
        self,
        canvas: Image.Image,
        name: str,
        box: Box,
        unit: float,
        mode: Literal["contain", "cover"] = "contain",
    ) -> None:
        image = self._asset(name)
        target = (max(1, self._px(box.width, unit)), max(1, self._px(box.height, unit)))
        if mode == "cover":
            resized = ImageOps.fit(image, target, method=Image.Resampling.LANCZOS)
        else:
            # 超采样画布的目标框通常大于 1× 素材；thumbnail 只会缩小，
            # 会让所有插画在 3× 渲染后变成原设计的三分之一。
            scale = min(target[0] / image.width, target[1] / image.height)
            resized = image.resize(
                (
                    max(1, int(round(image.width * scale))),
                    max(1, int(round(image.height * scale))),
                ),
                Image.Resampling.LANCZOS,
            )
        x = self._px(box.x, unit) + (target[0] - resized.width) // 2
        y = self._px(box.y, unit) + (target[1] - resized.height) // 2
        canvas.alpha_composite(resized, (x, y))

    def _text(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        x: float,
        y: float,
        size: float,
        color: RGB,
        unit: float,
        role: FontRole = "body",
        anchor: str = "lt",
        stroke_width: float = 0,
        stroke_fill: RGB | None = None,
    ) -> None:
        role = self.fonts.resolve_role(text, role)
        font = self.fonts.get(role, size, unit)
        self._draw_text_with_emoji(
            draw,
            (self._px(x, unit), self._px(y, unit)),
            text,
            font=font,
            fill=(*color, 255),
            unit=unit,
            anchor=anchor,
            stroke_width=self._px(stroke_width, unit),
            stroke_fill=(*(stroke_fill or color), 255),
        )

    # ================================ 固定基线文字 ================================ #

    def _baseline_text(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        x: float,
        baseline_y: float,
        size: float,
        color: RGB,
        unit: float,
        role: FontRole = "display",
        horizontal: Literal["left", "center", "right"] = "left",
        tracking: float = 0,
    ) -> None:
        """按固定基线绘制短标题，避免不同字形高度造成报头上下漂移。"""

        if not text:
            return
        role = self.fonts.resolve_role(text, role)
        font = self.fonts.get(role, size, unit)
        target_x = x * unit
        baseline_px = baseline_y * unit

        if tracking:
            # 字距只用于固定英文报头；按字符 advance 计算整行宽度，但所有字符
            # 必须共享同一条基线，不能再按各自字形 bbox 单独居中。
            advances = [font.getlength(character) for character in text]
            text_width = sum(advances) + tracking * unit * max(0, len(text) - 1)
            if horizontal == "center":
                cursor_x = target_x - text_width / 2
            elif horizontal == "right":
                cursor_x = target_x - text_width
            else:
                cursor_x = target_x
            for index, (character, advance) in enumerate(zip(text, advances)):
                draw.text(
                    (cursor_x, baseline_px),
                    character,
                    font=font,
                    fill=(*color, 255),
                    anchor="ls",
                )
                cursor_x += advance
                if index + 1 < len(text):
                    cursor_x += tracking * unit
            return

        text_width = self._measure_text(text, font)
        if horizontal == "center":
            origin_x = target_x - text_width / 2
        elif horizontal == "right":
            origin_x = target_x - text_width
        else:
            origin_x = target_x
        self._draw_text_with_emoji(
            draw,
            (origin_x, baseline_px),
            text,
            font=font,
            fill=(*color, 255),
            unit=unit,
            anchor="ls",
        )

    def _ink_centered_text(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        x: float,
        center_y: float,
        size: float,
        color: RGB,
        unit: float,
        role: FontRole = "display",
        horizontal: Literal["left", "center", "right"] = "left",
        tracking: float = 0,
    ) -> None:
        """让整行文字共用基线，再按实际油墨边界对齐容器中心。"""

        role = self.fonts.resolve_role(text, role)
        font = self.fonts.get(role, size, unit)
        if not text:
            return

        # ================================ Emoji 居中绘制 ================================ #
        # Pilmoji 不能参与逐字 tracking；动态昵称本身不使用 tracking，因此先在
        # 整行分支按 Emoji 实际宽度定位，再复用统一 Emoji 绘制入口。若继续落到
        # Pillow 的普通 draw.text，ZWJ 家庭 Emoji 会被正文中文字体画成一串方框。
        if not tracking and _has_emoji_candidate(text):
            text_width = self._measure_text(text, font)
            target_x = x * unit
            if horizontal == "left":
                origin_x = target_x
            elif horizontal == "right":
                origin_x = target_x - text_width
            else:
                origin_x = target_x - text_width / 2
            _sample_left, ink_top, _sample_right, ink_bottom = font.getbbox("国", anchor="ls")
            baseline_y = center_y * unit - (ink_top + ink_bottom) / 2
            self._draw_text_with_emoji(
                draw,
                (origin_x, baseline_y),
                text,
                font=font,
                fill=(*color, 255),
                unit=unit,
                anchor="ls",
            )
            return

        # 非零字距必须逐字绘制，但所有字符共享同一基线；旧实现按字形顶部逐字
        # 对齐，数字、冒号和汉字会各自上下漂移，尤其容易让页脚时间看起来歪斜。
        if tracking:
            cursor = 0.0
            glyphs: list[tuple[str, float]] = []
            ink_left = math.inf
            ink_top = math.inf
            ink_right = -math.inf
            ink_bottom = -math.inf
            for character in text:
                left, top, right, bottom = font.getbbox(character, anchor="ls")
                glyphs.append((character, cursor))
                ink_left = min(ink_left, cursor + left)
                ink_top = min(ink_top, top)
                ink_right = max(ink_right, cursor + right)
                ink_bottom = max(ink_bottom, bottom)
                cursor += font.getlength(character) + tracking * unit
        else:
            left, top, right, bottom = font.getbbox(text, anchor="ls")
            ink_left, ink_top, ink_right, ink_bottom = left, top, right, bottom
            glyphs = [(text, 0.0)]

        target_x = x * unit
        if horizontal == "left":
            origin_x = target_x - ink_left
        elif horizontal == "right":
            origin_x = target_x - ink_right
        else:
            origin_x = target_x - (ink_left + ink_right) / 2
        baseline_y = center_y * unit - (ink_top + ink_bottom) / 2

        for content, offset_x in glyphs:
            draw.text(
                (origin_x + offset_x, baseline_y),
                content,
                font=font,
                fill=(*color, 255),
                anchor="ls",
            )

    def _draw_rich(
        self,
        draw: ImageDraw.ImageDraw,
        spans: Sequence[RichSpan],
        box: Box,
        size: float,
        line_height: float,
        unit: float,
        max_lines: int | None = None,
        emoji_position_offset_y: float = -6,
    ) -> None:
        lines = self._wrap_rich(spans, size, box.width, unit)
        if max_lines is not None:
            lines = self._limit_rich_lines(
                lines,
                max(1, max_lines),
                size,
                box.width,
                unit,
            )
        for line_index, line in enumerate(lines):
            cursor_x = box.x * unit
            line_top = (box.y + line_index * line_height) * unit
            for token in line.tokens:
                font = self.fonts.get(token.role, size, unit)
                ascent, descent = font.getmetrics()
                baseline = line_top + (line_height * unit - ascent - descent) / 2 + ascent
                # Pilmoji 对“文字+Emoji”混合字符串和纯 Emoji 的基线算法不同。
                # 绘制时统一拆成独立单元，既保留 token 的整体换行约束，也避免
                # 同一昵称因模板切分方式不同而在不同卡片中上下漂移。
                for segment in _tokenize_text(token.text):
                    self._draw_text_with_emoji(
                        draw,
                        (cursor_x, baseline),
                        segment,
                        font=font,
                        fill=(*token.color, 255),
                        unit=unit,
                        anchor="ls",
                        emoji_position_offset_y=emoji_position_offset_y,
                    )
                    cursor_x += self._measure_text(segment, font)

    def _distressed_text(
        self,
        canvas: Image.Image,
        text: str,
        x: float,
        y: float,
        size: float,
        unit: float,
        texture_asset: str,
        anchor: str = "lt",
        role: FontRole = "display",
        tracking: float = 0,
        scale_y: float = 1,
        texture_box: Box | None = None,
    ) -> None:
        """用独立油墨纹理填充文字蒙版，保留可编辑数据与旧报纸掉墨感。"""

        mask = Image.new("L", canvas.size, 0)
        mask_draw = ImageDraw.Draw(mask)
        role = self.fonts.resolve_role(text, role)
        font = self.fonts.get(role, size, unit)
        if tracking:
            cursor = x * unit
            for character in text:
                mask_draw.text((cursor, y * unit), character, font=font, fill=255, anchor=anchor)
                cursor += font.getlength(character) + tracking * unit
        else:
            mask_draw.text((x * unit, y * unit), text, font=font, fill=255, anchor=anchor)
        bbox = mask.getbbox()
        if bbox is None:
            return
        glyph_mask = mask.crop(bbox)
        if scale_y != 1:
            glyph_mask = glyph_mask.resize(
                (glyph_mask.width, max(1, int(round(glyph_mask.height * scale_y)))),
                Image.Resampling.LANCZOS,
            )
            bbox = (bbox[0], bbox[1], bbox[2], bbox[1] + glyph_mask.height)
        texture = self._asset(texture_asset)
        if texture_box is None:
            tile = ImageOps.fit(
                texture,
                (bbox[2] - bbox[0], bbox[3] - bbox[1]),
                method=Image.Resampling.LANCZOS,
            )
        else:
            # Figma 的 IMAGE/FILL 先铺满整个文本节点，再由字形裁切；不能按字形
            # bbox 重新拉伸纹理，否则掉墨斑点的位置会整体漂移。
            texture_rect = self._scaled_box(texture_box, unit)
            fitted = ImageOps.fit(
                texture,
                (texture_rect[2] - texture_rect[0], texture_rect[3] - texture_rect[1]),
                method=Image.Resampling.LANCZOS,
            )
            tile = fitted.crop(
                (
                    bbox[0] - texture_rect[0],
                    bbox[1] - texture_rect[1],
                    bbox[2] - texture_rect[0],
                    bbox[3] - texture_rect[1],
                )
            )
        tile_alpha = ImageChops.multiply(tile.getchannel("A"), glyph_mask)
        tile.putalpha(tile_alpha)
        canvas.alpha_composite(tile, (bbox[0], bbox[1]))

    def _distress_alpha(self, texture_asset: str, size: tuple[int, int], strength: float = 1.0) -> Image.Image:
        """生成确定性的掉墨 Alpha；相同尺寸每次渲染都得到同一磨损位置。"""

        cache_key = (texture_asset, size[0], size[1])
        if cache_key not in self._texture_cache:
            alpha = ImageOps.fit(
                self._asset(texture_asset).getchannel("A"),
                size,
                method=Image.Resampling.LANCZOS,
            )
            self._texture_cache[cache_key] = alpha
        alpha = self._texture_cache[cache_key]
        if strength == 1:
            return alpha
        return alpha.point(lambda value: max(0, min(255, round(255 - (255 - value) * strength))))

    def _composite_distressed_mask(
        self,
        canvas: Image.Image,
        mask: Image.Image,
        origin: tuple[int, int],
        color: RGB,
        texture_asset: str,
        strength: float = 1.0,
    ) -> None:
        """将几何蒙版与油墨纹理相乘，几何仍可动态伸缩，磨损不会成为整块截图。"""

        texture_alpha = self._distress_alpha(texture_asset, mask.size, strength)
        layer = Image.new("RGBA", mask.size, (*color, 255))
        layer.putalpha(ImageChops.multiply(mask, texture_alpha))
        canvas.alpha_composite(layer, origin)

    def _distressed_outline(
        self,
        canvas: Image.Image,
        box: Box,
        color: RGB,
        width: float,
        unit: float,
        radius: float = 0,
        texture_asset: str = "ink-texture-black",
        strength: float = 0.9,
    ) -> None:
        """绘制可伸缩的掉墨边框，用于主区域、排行和保护券。"""

        pixel_width = max(1, self._px(box.width, unit))
        pixel_height = max(1, self._px(box.height, unit))
        mask = Image.new("L", (pixel_width, pixel_height), 0)
        mask_draw = ImageDraw.Draw(mask)
        stroke = max(1, self._px(width, unit))
        bounds = (stroke // 2, stroke // 2, pixel_width - 1 - stroke // 2, pixel_height - 1 - stroke // 2)
        if radius:
            mask_draw.rounded_rectangle(bounds, radius=self._px(radius, unit), outline=255, width=stroke)
        else:
            mask_draw.rectangle(bounds, outline=255, width=stroke)
        self._composite_distressed_mask(
            canvas,
            mask,
            (self._px(box.x, unit), self._px(box.y, unit)),
            color,
            texture_asset,
            strength,
        )

    def _distressed_line(
        self,
        canvas: Image.Image,
        points: Sequence[tuple[float, float]],
        color: RGB,
        width: float,
        unit: float,
        strength: float = 0.9,
    ) -> None:
        """绘制轻度断墨线条；只改变油墨覆盖率，不改变布局几何。"""

        if len(points) < 2:
            return
        padding = max(2, self._px(width + 1, unit))
        scaled_points = [(x * unit, y * unit) for x, y in points]
        left = math.floor(min(point[0] for point in scaled_points)) - padding
        top = math.floor(min(point[1] for point in scaled_points)) - padding
        right = math.ceil(max(point[0] for point in scaled_points)) + padding + 1
        bottom = math.ceil(max(point[1] for point in scaled_points)) + padding + 1
        mask = Image.new("L", (right - left, bottom - top), 0)
        ImageDraw.Draw(mask).line(
            [(round(x - left), round(y - top)) for x, y in scaled_points],
            fill=255,
            width=max(1, self._px(width, unit)),
        )
        self._composite_distressed_mask(
            canvas,
            mask,
            (left, top),
            color,
            "ink-texture-black",
            strength,
        )

    def _ribbon(
        self,
        canvas: Image.Image,
        x: float,
        y: float,
        width: float,
        height: float,
        color: RGB,
        unit: float,
    ) -> None:
        pixel_width = max(1, self._px(width, unit))
        pixel_height = max(1, self._px(height, unit))
        notch = self._px(14, unit)
        mask = Image.new("L", (pixel_width, pixel_height), 0)
        ImageDraw.Draw(mask).polygon(
            [(0, 0), (pixel_width - notch, 0), (pixel_width - 1, pixel_height // 2), (pixel_width - notch, pixel_height - 1), (0, pixel_height - 1)],
            fill=255,
        )
        texture_asset = "ink-texture-red" if color == RIBBON_RED else "ink-texture-black"
        self._composite_distressed_mask(
            canvas,
            mask,
            (self._px(x, unit), self._px(y, unit)),
            color,
            texture_asset,
            1.08,
        )


# ================================ 区域绘制 ================================ #

    def _draw_header(self, canvas: Image.Image, draw: ImageDraw.ImageDraw, data: DailyReportCardData, box: Box, unit: float) -> None:
        x, y = box.x, box.y
        self._line(draw, [(x, y + 3), (x + box.width, y + 3)], INK, 2, unit)
        self._line(draw, [(x, y + 37), (x + box.width, y + 37)], INK, 1, unit)
        # 三段报头必须共享同一条基线。按各自字形 bbox 居中会让数字、英文和中文
        # 的可见顶边上下漂移；横向继续使用原设计的左 15px、右 38px 光学锚点。
        top_row_baseline = y + 27
        self._baseline_text(draw, data.volume, x + 15, top_row_baseline, 16, INK, unit, "display")
        self._baseline_text(
            draw,
            "ROLLPIG DAILY",
            x + box.width / 2,
            top_row_baseline,
            17,
            INK,
            unit,
            "display",
            "center",
            1.5,
        )
        self._baseline_text(
            draw,
            "★ 今日猪圈记录",
            x + box.width - 38,
            top_row_baseline,
            16,
            INK,
            unit,
            "display",
            "right",
        )

        self._paste_asset(canvas, data.header_asset, Box(x + 10, y + 51, 178, 154), unit)
        self._line(draw, [(x + 197, y + 57), (x + 197, y + 200)], MUTED, 1, unit)
        # Figma 的文本框保留约 41px 字体上沿并应用 -3px 字距；Pillow 的 lt 锚点
        # 不包含这段行框留白，因此显式补偿并把字形纵向拉伸到同一油墨边界。
        self._distressed_text(
            canvas,
            "猪圈日报",
            x + 225,
            y + 66,
            139,
            unit,
            "ink-texture-black",
            role="black",
            tracking=-3,
            scale_y=1,
            texture_box=Box(x + 225, y + 25, 547, 200),
        )
        # Figma 的日期文本框在字形上方保留约 16px 行框；纹理文字直接按字形蒙版落位，
        # 因此这里补回行框上沿，避免年份与月日整体贴到报头顶部。
        self._distressed_text(canvas, data.date_year, x + 872.5, y + 79, 40, unit, "ink-texture-red", "mt")
        self._distressed_text(canvas, data.date_month_day, x + 872, y + 121, 38, unit, "ink-texture-red", "mt")
        self._dashed_line(draw, (x + 799, y + 61), (x + 799, y + 207), INK, 2, 5, 5, unit)

        weekday_box = Box(x + 822, y + 171, 104, 28)
        draw.rectangle(self._scaled_box(weekday_box, unit), fill=(*MUTED, 255))
        self._text(draw, data.weekday, weekday_box.x + weekday_box.width / 2, weekday_box.y + weekday_box.height / 2, 18, CREAM, unit, "display", "mm")
        self._line(draw, [(x, y + 221), (x + box.width, y + 221)], INK, 7, unit)
        self._line(draw, [(x, y + 234), (x + box.width, y + 234)], INK, 2, unit)

    def _draw_stats(self, canvas: Image.Image, draw: ImageDraw.ImageDraw, data: DailyReportCardData, layout: DailyReportCardLayout, box: Box, unit: float) -> None:
        self._ribbon(canvas, box.x, box.y, 132, 38, RIBBON_RED, unit)
        self._ink_centered_text(draw, "今日猪圈", box.x + 18, box.y + 19, 24, CREAM, unit, "display")
        self._line(draw, [(box.x + 135, box.y + 22), (box.right, box.y + 22)], MUTED, 1, unit)

        columns = min(3, len(data.stats))
        for index, (item, cell) in enumerate(zip(data.stats, layout.stat_cells)):
            if index % columns:
                self._dashed_line(draw, (cell.x, cell.y + 4), (cell.x, cell.bottom - 7), INK, 1.5, 5, 5, unit)
            icon_width, icon_height = ASSET_BOXES.get(item.asset, (96, 96))
            # 同一速览行的标签必须使用同一完整字体；不能因某个标题恰好在子集中
            # 就与“数量/种类/意外翻车”等新标题形成宋体、黑体混排。
            title_role: FontRole = "body"
            value_role = self.fonts.resolve_role(item.value, "display")
            unit_role = self.fonts.resolve_role(item.unit, "display")
            title_font = self.fonts.get(title_role, 22, unit)
            value_font = self.fonts.get(value_role, 74, unit)
            unit_font = self.fonts.get(unit_role, 23, unit)
            title_width = title_font.getlength(item.label) / unit
            value_width = value_font.getlength(item.value) / unit
            unit_width = unit_font.getlength(item.unit) / unit
            text_width = max(title_width, value_width + 24 + unit_width)
            group_width = icon_width + 15 + text_width
            group_x = cell.x + (cell.width - group_width) / 2
            if columns == 3:
                # 三栏默认版严格对应 Figma 的非对称视觉中心；两栏及更多行仍自动等分。
                group_x = (
                    cell.x + 7.17,
                    cell.x + 26.67,
                    cell.x + 34.66,
                )[index]
            icon_box = Box(group_x, cell.y + 15, icon_width, icon_height)
            self._paste_asset(canvas, item.asset, icon_box, unit)
            text_x = group_x + icon_width + 15
            self._text(draw, item.label, text_x, cell.y + 1, 22, INK, unit, title_role)
            # 大数字的 Figma 行框上沿比可见字形高 18px；按字形蒙版绘制时需要显式补偿。
            self._distressed_text(canvas, item.value, text_x, cell.y + 45, 74, unit, "ink-texture-red")
            self._text(draw, item.unit, text_x + value_width + 24, cell.y + 88, 23, INK, unit, unit_role, "ls")

    def _draw_main(self, canvas: Image.Image, draw: ImageDraw.ImageDraw, data: DailyReportCardData, layout: DailyReportCardLayout, box: Box, unit: float) -> None:
        event_box = layout.boxes.get("events")
        observation_box = layout.boxes.get("observation")

        # 原图的观察内容位于一块独立半透明纸板上；底板跟随动态区域增高，
        # 只叠加暖色与细描边，不携带任何文字或插画像素。
        if observation_box and data.observation:
            panel_box = Box(
                observation_box.x,
                observation_box.y + 28,
                observation_box.width - 22,
                max(42, observation_box.height - 40),
            )
            draw.rounded_rectangle(
                self._scaled_box(panel_box, unit),
                radius=self._px(7, unit),
                fill=(222, 199, 160, 48),
                outline=(139, 113, 78, 52),
                width=max(1, self._px(0.8, unit)),
            )

        self._distressed_outline(canvas, box, INK, 1.2, unit, texture_asset="ink-texture-black", strength=0.82)
        if event_box and event_box.x > box.x:
            self._distressed_line(canvas, [(event_box.x, box.y), (event_box.x, box.bottom)], INK, 1, unit, 0.88)

        if observation_box and data.observation:
            self._ribbon(canvas, observation_box.x, observation_box.y, 132, 38, RIBBON_RED, unit)
            self._ink_centered_text(
                draw,
                "猪圈见闻",
                observation_box.x + 18,
                observation_box.y + 19,
                24,
                CREAM,
                unit,
                "display",
            )
            self._paste_asset(canvas, data.observation.asset, Box(observation_box.x + 55, observation_box.y + 38, 162, 177), unit)
            text_width = max(1.0, observation_box.width - 238.0 - 30.0)
            text_area_top = observation_box.y + 42
            text_area_height = 158.0
            text_size, line_height, text_height = self._fit_rich_block(
                data.observation.spans,
                text_width,
                text_area_height,
                29,
                20,
                unit,
            )
            text_y = text_area_top + max(0.0, (text_area_height - text_height) / 2)
            self._draw_rich(
                draw,
                data.observation.spans,
                Box(
                    observation_box.x + 238,
                    text_y,
                    text_width,
                    text_height,
                ),
                text_size,
                line_height,
                unit,
                max_lines=max(1, int(text_area_height // line_height)),
            )
            # 四行以上正文会进入右下角装饰区，猪爪此时主动让位。
            if data.observation.paw_asset and text_height <= line_height * 3:
                self._paste_asset(canvas, data.observation.paw_asset, Box(observation_box.right - 108, observation_box.bottom - 97, 77, 78), unit)

        headline_box = layout.boxes.get("headline")
        if headline_box and data.headline:
            if headline_box.y > box.y:
                self._distressed_line(
                    canvas,
                    [(headline_box.x, headline_box.y), (headline_box.right, headline_box.y)],
                    INK,
                    1,
                    unit,
                    0.88,
                )
            self._ribbon(canvas, headline_box.x, headline_box.y, 132, 38, RIBBON_RED, unit)
            self._ink_centered_text(
                draw,
                "今日头条",
                headline_box.x + 18,
                headline_box.y + 19,
                24,
                CREAM,
                unit,
                "display",
            )
            asset_width = min(264.0, headline_box.width * 0.40)
            self._paste_asset(canvas, data.headline.asset, Box(headline_box.right - asset_width - 8, headline_box.bottom - 247, asset_width, 247), unit)
            text_width = max(330.0, headline_box.width - asset_width - 42.0)
            text_height = self._measure_rich(data.headline.spans, 29, text_width, 43, unit)
            self._draw_rich(
                draw,
                data.headline.spans,
                Box(headline_box.x + 18, headline_box.y + 56, text_width, text_height),
                29,
                43,
                unit,
                emoji_position_offset_y=-24,
            )

            tag_y = headline_box.y + 56 + text_height + 19
            cursor_x = headline_box.x + 18
            max_right = headline_box.right - asset_width - 12
            for tag in data.headline.tags:
                font = self.fonts.get("body", 19, unit)
                available_width = max_right - headline_box.x - 18
                fitted_tag = self._fit_single_line(tag, available_width - 30, 19, unit)
                tag_width = min(
                    available_width,
                    max(108.0, font.getlength(fitted_tag) / unit + 30.0),
                )
                if cursor_x + tag_width > max_right:
                    cursor_x = headline_box.x + 18
                    tag_y += 48
                tag_box = Box(cursor_x, tag_y, tag_width, 38)
                self._distressed_outline(
                    canvas,
                    tag_box,
                    RED,
                    1.2,
                    unit,
                    radius=3,
                    texture_asset="ink-texture-red",
                    strength=0.9,
                )
                self._text(draw, fitted_tag, tag_box.x + tag_box.width / 2, tag_box.y + tag_box.height / 2, 19, RED, unit, "body", "mm")
                cursor_x += tag_width + 12

        if event_box:
            self._ribbon(canvas, event_box.x + 18, event_box.y + 18, min(176, event_box.width - 36), 44, INK, unit)
            self._ink_centered_text(
                draw,
                "事件追踪",
                event_box.x + 41,
                event_box.y + 40,
                27,
                CREAM,
                unit,
                "display",
            )
            intro_lines, intro_size, intro_line_height = self._fit_event_intro(
                data.event_intro,
                event_box.width - 36,
                unit,
            )
            intro_height = len(intro_lines) * intro_line_height
            for line_index, line in enumerate(intro_lines):
                self._text(
                    draw,
                    line,
                    event_box.x + 22,
                    event_box.y + 86 + line_index * intro_line_height,
                    intro_size,
                    INK,
                    unit,
                    "body",
                )
            row_y = event_box.y + 86 + intro_height + 15
            timeline_x = event_box.x + 40
            if data.events:
                self._dashed_line(
                    draw,
                    (timeline_x, row_y + 24),
                    (
                        timeline_x,
                        row_y + len(data.events) * EVENT_ROW_HEIGHT - 27,
                    ),
                    MUTED,
                    1,
                    5,
                    5,
                    unit,
                )
            for item in data.events:
                self._paste_asset(canvas, item.asset, Box(event_box.x + 9, row_y + 5, 55, 73), unit)
                text_width = max(1.0, event_box.right - (event_box.x + 66) - 14.0)
                title = self._fit_single_line(item.title, text_width, 20, unit)
                detail_lines, detail_size = self._fit_wrapped_text_size(
                    item.detail,
                    text_width,
                    19,
                    15,
                    2,
                    unit,
                )
                detail_line_gap = max(22.0, round(detail_size * 27 / 19, 1))
                self._text(draw, title, event_box.x + 66, row_y + 10, 20, INK, unit, "body")
                for line_index, detail in enumerate(detail_lines):
                    self._text(
                        draw,
                        detail,
                        event_box.x + 66,
                        row_y + 43 + line_index * detail_line_gap,
                        detail_size,
                        item.color,
                        unit,
                        "body",
                    )
                row_y += EVENT_ROW_HEIGHT

    def _draw_ranking(self, canvas: Image.Image, draw: ImageDraw.ImageDraw, data: DailyReportCardData, box: Box, unit: float) -> None:
        self._distressed_outline(canvas, box, INK, 1.2, unit, texture_asset="ink-texture-black", strength=0.82)
        title_center = box.x + box.width / 2
        self._text(draw, "猪圈名人堂", title_center, box.y + 24, 27, INK, unit, "display", "mm")
        self._sparkle(draw, title_center - 99, box.y + 24, unit)
        self._sparkle(draw, title_center + 99, box.y + 24, unit)
        self._distressed_line(canvas, [(box.x, box.y + 48.6), (box.right, box.y + 48.6)], MUTED, 1, unit, 0.95)
        count = len(data.rankings)
        column_widths = [box.width / count] * count
        if count == 3:
            column_widths = [320.0, 311.0, 303.0]
        visible_rows = max((min(3, len(column.entries)) for column in data.rankings), default=0)
        row_start = box.y + 115.2

        for column_index, column in enumerate(data.rankings):
            column_x = box.x + sum(column_widths[:column_index])
            column_width = column_widths[column_index]
            if column_index:
                self._distressed_line(
                    canvas,
                    [(column_x, box.y + 48.6), (column_x, box.bottom - 28.4)],
                    CREAM,
                    1,
                    unit,
                    0.95,
                )
            # 副标题已移除，皇冠和栏目名在标题区剩余高度内重新居中，避免留下
            # 原副标题占用的下半截空白。
            column_header_center = box.y + 82
            self._crown(draw, column_x + 89, column_header_center, unit)
            # 排行标题允许后续继续调整；固定宋体子集缺字时必须回退完整正文字体，
            # 不能让 Pillow 静默绘制成零散残字。
            title_role = self.fonts.resolve_role(column.title, "display")
            self._ink_centered_text(
                draw,
                column.title,
                column_x + 108,
                column_header_center,
                23,
                INK,
                unit,
                title_role,
            )

            for row_index, entry in enumerate(column.entries[:3]):
                row_y = row_start + row_index * 79
                self._distressed_line(
                    canvas,
                    [(column_x, row_y), (column_x + column_width, row_y)],
                    MUTED,
                    0.8,
                    unit,
                    0.95,
                )
                displayed_rank = entry.rank if entry.rank > 0 else row_index + 1
                badge_color = {1: GOLD, 2: RANK_TWO, 3: RANK_THREE}.get(
                    displayed_rank,
                    RANK_THREE,
                )
                badge = Box(column_x + 17, row_y + 18, 36, 42)
                draw.rounded_rectangle(self._scaled_box(badge, unit), radius=self._px(3, unit), fill=(*badge_color, 255))
                self._text(draw, str(displayed_rank), badge.x + badge.width / 2, badge.y + badge.height / 2, 24, CREAM, unit, "display", "mm")
                self._paste_asset(canvas, entry.avatar, Box(column_x + 58, row_y + 2, 74, 74), unit)
                text_width = max(1.0, column_width - 160.0)
                # 排行昵称和小猪信息先逐级缩字，只有达到可读下限后仍超宽才省略。
                # 数据层不能提前截断，否则两三个汉字就可能丢失完整昵称。
                name, name_size = self._fit_text_size(
                    entry.name,
                    text_width,
                    22,
                    16,
                    unit,
                )
                detail, detail_size = self._fit_text_size(
                    entry.detail,
                    text_width,
                    19,
                    15,
                    unit,
                )
                self._text(draw, name, column_x + 145, row_y + 14, name_size, INK, unit, "body")
                self._text(draw, detail, column_x + 145, row_y + 45, detail_size, INK, unit, "body")

            # 列表不足最高行数时保留网格高度，但不伪造空排名内容。
            for row_index in range(len(column.entries[:3]), visible_rows):
                row_y = row_start + row_index * 79
                self._distressed_line(
                    canvas,
                    [(column_x, row_y), (column_x + column_width, row_y)],
                    MUTED,
                    0.8,
                    unit,
                    0.95,
                )

        note_y = box.bottom - 28.4
        self._distressed_line(canvas, [(box.x, note_y), (box.right, note_y)], MUTED, 1, unit, 0.95)
        self._text(draw, "-  并列排名按首次达成时间排序", box.x + box.width / 2, note_y + 14, 16, INK, unit, "regular", "mm")

    def _draw_coupon(self, canvas: Image.Image, draw: ImageDraw.ImageDraw, data: Coupon, box: Box, unit: float) -> None:
        # 券面颜色来自独立纸张纤维着色层；边框与孔洞仍由代码绘制，猪盾保持
        # 最上层可替换素材，不与券面背景合并。
        self._paste_asset(canvas, "coupon_surface", box, unit, "cover")
        self._distressed_outline(
            canvas,
            box,
            BLUE,
            2,
            unit,
            texture_asset="ink-texture-black",
            strength=1.05,
        )
        inner = Box(box.x + 15, box.y + 10, box.width - 34, box.height - 24)
        self._distressed_outline(
            canvas,
            inner,
            (228, 220, 194),
            1.2,
            unit,
            radius=4,
            texture_asset="ink-texture-black",
            strength=1.18,
        )

        for divider_x in (box.x + 446, box.x + 823):
            self._dashed_line(draw, (divider_x, box.y + 12), (divider_x, box.bottom - 13), BLUE, 1.5, 5, 5, unit)
        for offset in range(16, 145, 22):
            for center_x in (box.x, box.right):
                radius = 7
                draw.ellipse(
                    (
                        self._px(center_x - radius, unit),
                        self._px(box.y + offset - radius, unit),
                        self._px(center_x + radius, unit),
                        self._px(box.y + offset + radius, unit),
                    ),
                    # 券孔必须真正透明，才能露出当前可替换纸张，而不是固定米色圆。
                    fill=(0, 0, 0, 0),
                )

        self._paste_asset(canvas, data.shield_asset, Box(box.x + 48, box.y + 24, 96, 96), unit)
        # 保留原券从“券种 → 持券人 → 权益”的横向阅读关系。有效期删除后，
        # 昵称区可使用两行，但不能挤占右侧权益和条形码。
        coupon_center = box.y + 72
        self._ink_centered_text(draw, "次日保护券", box.x + 170, coupon_center, 42, BLUE, unit, "black")
        self._ink_centered_text(draw, "▶", box.x + 414, coupon_center, 28, BLUE, unit, "display")
        name_role: FontRole = "black" if self.fonts.subset_supports(data.name) else "body"
        name_lines, name_size = self._fit_wrapped_text_size(
            data.name,
            178,
            28,
            24,
            2,
            unit,
            name_role,
        )
        name_center_x = box.x + 551
        line_gap = max(27.0, name_size * 1.15)
        first_line_y = coupon_center - line_gap * (len(name_lines) - 1) / 2
        for line_index, line in enumerate(name_lines):
            self._ink_centered_text(
                draw,
                line,
                name_center_x,
                first_line_y + line_index * line_gap,
                name_size,
                BLUE,
                unit,
                name_role,
                "center",
            )
        self._dashed_line(
            draw,
            (box.x + 652, box.y + 27),
            (box.x + 652, box.bottom - 27),
            BLUE,
            1,
            6,
            4,
            unit,
        )
        self._ink_centered_text(
            draw,
            self._fit_single_line(f"遭到 {data.roast_count} 次烧烤", 142, 18, unit),
            box.x + 730,
            coupon_center - 17,
            18,
            BLUE,
            unit,
            "body",
            "center",
        )
        self._ink_centered_text(
            draw,
            self._fit_single_line(data.benefit, 142, 21, unit),
            box.x + 730,
            coupon_center + 18,
            21,
            BLUE,
            unit,
            "body",
            "center",
        )
        self._paste_asset(canvas, data.barcode_asset, Box(box.x + 827, box.y + 1, 97, 143), unit, "cover")

    def _draw_footer(self, draw: ImageDraw.ImageDraw, footer: str, box: Box, unit: float) -> None:
        self._line(draw, [(box.x, box.y), (box.right, box.y)], INK, 1, unit)
        self._line(draw, [(box.x, box.bottom - 3), (box.right, box.bottom - 3)], INK, 1, unit)
        # 中英文、数字与标点共用基线，并按整行油墨边界在上下横线之间居中。
        self._ink_centered_text(
            draw,
            footer,
            box.x + box.width / 2,
            box.y + (box.height - 3) / 2,
            20,
            INK,
            unit,
            "regular",
            "center",
            2,
        )

    def _crisp_header_rules(
        self,
        canvas: Image.Image,
        paper: Image.Image,
        header: Box,
        scale: float,
    ) -> None:
        """在最终分辨率恢复 Figma 的整数像素报头横线，消除缩采样振铃。"""

        rules = (
            (3.0, 2.0, INK),
            (37.0, 1.0, (39, 36, 30)),
            (221.0, 7.0, INK),
            (234.0, 2.0, INK),
        )
        draw = ImageDraw.Draw(canvas)
        x1 = int(round(header.x * scale))
        x2 = int(round(header.right * scale))
        for offset, height, color in rules:
            y1 = int(round((header.y + offset) * scale))
            y2 = max(y1 + 1, int(round((header.y + offset + height) * scale)))
            padding = max(2, int(round(3 * scale)))
            restore = (x1, max(0, y1 - padding), x2, min(canvas.height, y2 + padding))
            canvas.paste(paper.crop(restore), restore[:2])
            draw.rectangle((x1, y1, x2 - 1, y2 - 1), fill=(*color, 255))

    def _paper_background(self, asset_name: str, width: int, height: int) -> Image.Image:
        """保持纸张纤维密度；动态增高时只扩展中段，不纵向拉伸整张纸。"""

        source = self._asset(asset_name).convert("RGB")
        scaled_height = max(1, round(source.height * width / source.width))
        scaled = source.resize((width, scaled_height), Image.Resampling.LANCZOS)
        if abs(height - scaled_height) <= 2:
            return scaled.resize((width, height), Image.Resampling.LANCZOS)
        if height < scaled_height:
            return scaled.crop((0, 0, width, height))

        top_height = min(round(384 * width / BASE_WIDTH), scaled_height // 3)
        bottom_height = min(round(256 * width / BASE_WIDTH), scaled_height // 4)
        middle = scaled.crop((0, top_height, width, scaled_height - bottom_height))
        middle_height = height - top_height - bottom_height
        result = Image.new("RGB", (width, height))
        result.paste(scaled.crop((0, 0, width, top_height)), (0, 0))

        cursor_y = top_height
        flip = False
        while cursor_y < top_height + middle_height:
            tile = ImageOps.flip(middle) if flip else middle
            remaining = top_height + middle_height - cursor_y
            if tile.height > remaining:
                tile = tile.crop((0, 0, width, remaining))
            result.paste(tile, (0, cursor_y))
            cursor_y += tile.height
            flip = not flip
        result.paste(scaled.crop((0, scaled_height - bottom_height, width, scaled_height)), (0, height - bottom_height))
        return result


# ================================ 合成与命令行 ================================ #

    def render(
        self,
        data: DailyReportCardData,
        width: int = 1024,
        supersample: int = 3,
    ) -> tuple[Image.Image, DailyReportCardLayout]:
        """渲染无损 RGB PNG；高度由内容决定，不分页、不截断。"""

        if width < 480:
            raise ValueError("width 不能小于 480")
        if supersample < 1:
            raise ValueError("supersample 必须大于等于 1")

        layout = self.build_layout(data, measure_unit=float(supersample))
        scale = width / BASE_WIDTH
        unit = scale * supersample
        canvas_size = (
            int(round(BASE_WIDTH * unit)),
            int(round(layout.height * unit)),
        )
        # 纸张保持独立资源并只在最终分辨率缩放一次。若随前景先放大到 3×再缩回，
        # 细小纤维会被二次采样抹平，整张图都会产生不必要的纹理误差。
        canvas = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(canvas)

        self._draw_header(canvas, draw, data, layout.boxes["header"], unit)
        if "stats" in layout.boxes:
            self._draw_stats(canvas, draw, data, layout, layout.boxes["stats"], unit)
        if "main" in layout.boxes:
            self._draw_main(canvas, draw, data, layout, layout.boxes["main"], unit)
        if "ranking" in layout.boxes:
            self._draw_ranking(canvas, draw, data, layout.boxes["ranking"], unit)
        if "coupon" in layout.boxes and data.coupon is not None:
            self._draw_coupon(canvas, draw, data.coupon, layout.boxes["coupon"], unit)
        self._draw_footer(draw, data.footer, layout.boxes["footer"], unit)

        final_height = int(round(layout.height * scale))
        if supersample > 1:
            canvas = canvas.resize((width, final_height), Image.Resampling.LANCZOS)
        background = self._paper_background(data.background_asset, width, final_height).convert("RGBA")
        paper = background.copy()
        background.alpha_composite(canvas)
        self._crisp_header_rules(background, paper, layout.boxes["header"], scale)
        return background.convert("RGB"), layout


# ================================ 日报业务展示适配 ================================ #

DAILY_REPORT_CARD_RESOURCE_DIR = (
    Path(__file__).resolve().parent / "resource" / "daily_report_card"
)

_STAT_ASSETS: dict[str, str] = {
    "roll_count": "stats_people",
    "ordinary_roast": "stats_flame",
    "reservation": "stats_calendar",
    "escape": "stats_escape",
    "backfire": "stats_backfire",
    "pig_variety": "stats_pig_variety",
}
_OBSERVATION_ASSETS: dict[ObservationKind, str] = {
    "reservation": "observation_reservation",
    "backfire": "observation_backfire",
    "escape": "observation_escape",
    "success": "observation_roast_success",
    "human": "observation_human",
    "collision": "observation_collision",
    "variety": "observation_variety",
}
_HEADLINE_ASSETS: dict[HeadlineKind, str] = {
    "normal_success": "headline_roast_success",
    "normal_escape": "headline_escape",
    "normal_backfire": "headline_backfire",
    "self_roast": "headline_self_roast",
    "bot_backfire": "headline_bot_backfire",
    "reservation_success": "headline_reservation_success",
    "reservation_escape": "headline_reservation_escape",
    "reservation_backfire": "headline_reservation_backfire",
    "reservation_human": "headline_reservation_human",
    "reservation_food": "headline_reservation_food",
    "reservation_eaten": "headline_reservation_eaten",
    "reservation_sold": "headline_reservation_sold",
}
_RANKING_TITLES: dict[RankingKind, str] = {
    "expert_level": "严选好猪",
    "roast_success": "烧烤狂人",
    "catalog": "养猪大户",
}
_WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


# ================================ 日报稳定文案池 ================================ #

# 日报可能在失败后由其他 Bot 重领。模板选择必须由业务快照稳定决定，不能使用随机数。
_TEMPLATE_FIELD_PATTERN = re.compile(r"\{([a-z][a-z0-9_]*)\}")


def _stable_template(
    report: DailyReport,
    section: str,
    kind: str,
    identity: str,
    templates: Sequence[str],
) -> str:
    """按日报业务身份稳定选择模板；相同快照在重试和跨 Bot 渲染时保持一致。"""

    if not templates:
        raise ValueError(f"日报文案池不能为空: section={section} kind={kind}")
    seed = "\x1f".join((report.date_str, report.group_id, section, kind, identity))
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return templates[int.from_bytes(digest[:8], "big") % len(templates)]


def _rich_template(
    template: str,
    fields: Mapping[str, RichSpan],
) -> tuple[RichSpan, ...]:
    """把受控占位符转换为富文本；模板缺字段时立即失败，避免静默发出半截文案。"""

    spans: list[RichSpan] = []
    cursor = 0
    for match in _TEMPLATE_FIELD_PATTERN.finditer(template):
        if match.start() > cursor:
            spans.append(RichSpan(template[cursor : match.start()], INK))
        field_name = match.group(1)
        if field_name not in fields:
            raise KeyError(f"日报文案缺少字段: {field_name}")
        spans.append(fields[field_name])
        cursor = match.end()
    if cursor < len(template):
        spans.append(RichSpan(template[cursor:], INK))
    return tuple(span for span in spans if span.text)


def _observation_breakdown(selection: object) -> str:
    """生成预约结果明细；特殊目标数量由预约总场次减去三种常规结算得到。"""

    parts: list[str] = []
    for label, count in (
        ("成功", int(getattr(selection, "success_count", 0) or 0)),
        ("逃脱", int(getattr(selection, "escape_count", 0) or 0)),
        ("反噬", int(getattr(selection, "backfire_count", 0) or 0)),
    ):
        if count:
            parts.append(f"{label} {count} 场")
    regular_count = sum(
        int(getattr(selection, field, 0) or 0)
        for field in ("success_count", "escape_count", "backfire_count")
    )
    special_count = max(0, int(getattr(selection, "matched", 0) or 0) - regular_count)
    if special_count:
        parts.append(f"特殊目标 {special_count} 场")
    return "、".join(parts) if parts else "各场结果没有形成明显倾向"


@dataclass(frozen=True)
class DailyReportCardRenderResult:
    """日报卡 PNG 成品及实际输出尺寸。"""

    data: bytes
    width: int
    height: int


def _report_name(
    report: DailyReport,
    user_id: str,
    explicit_name: str = "",
) -> str:
    """优先使用事件快照姓名，再回退聚合昵称和稳定用户标识。"""

    value = explicit_name or report.display_names.get(user_id, "") or user_id or "群友"
    # 姓名解析只负责归一化，不能提前丢失用户信息；头条和追踪栏分别在
    # 自己的绘制约束内缩字、换行，并仅在最小字号仍放不下时省略。
    return re.sub(r"\s+", " ", value).strip() or "群友"


def _resource_pig_name(pig_id: str) -> str:
    pig = get_pig_by_id(pig_id) if pig_id else None
    return str(pig.get("name") or pig_id) if pig else (pig_id or "小猪")


def _report_date_parts(date_str: str) -> tuple[str, str, str, str]:
    """将固定业务日期转换为报头字段；非法旧值仍生成稳定占位日期。"""

    try:
        report_date = dt.date.fromisoformat(date_str)
    except ValueError:
        return f"VOL.{date_str.replace('-', '')}   ★", "----", "--.--", "日期未知"
    return (
        f"VOL.{report_date:%Y-%m%d}   ★",
        f"{report_date:%Y}",
        f"{report_date:%m.%d}",
        _WEEKDAYS[report_date.weekday()],
    )


def _observation_card(report: DailyReport) -> Observation | None:
    """把唯一观察结果转换为稳定池化正文和对应插画。"""

    selection = report.observation
    if selection is None:
        return None
    overview = report.overview
    result_text = {
        "success": "烤成了",
        "escape": "跑了",
        "backfire": "把自己烤了",
    }.get(selection.dominant_result, "结果各不相同")
    identity = ":".join(
        str(value)
        for value in (
            selection.total,
            selection.matched,
            selection.success_count,
            selection.escape_count,
            selection.backfire_count,
            overview.top_pig_id,
            overview.top_pig_count,
        )
    )
    template = _stable_template(
        report,
        "observation",
        selection.kind,
        identity,
        DAILY_REPORT_OBSERVATION_TEXTS[selection.kind],
    )
    spans = _rich_template(
        template,
        {
            "total": RichSpan(str(selection.total), INK, True),
            "matched": RichSpan(str(selection.matched), RED, True),
            "result": RichSpan(result_text, RED, True),
            "breakdown": RichSpan(_observation_breakdown(selection), RED),
            "roll_count": RichSpan(str(overview.roll_count), INK, True),
            "variety_count": RichSpan(str(overview.pig_variety_count), RED, True),
            "top_pig": RichSpan(_resource_pig_name(overview.top_pig_id), RED, True),
            "top_count": RichSpan(str(overview.top_pig_count), RED, True),
        },
    )
    return Observation(spans=spans, asset=_OBSERVATION_ASSETS[selection.kind])


def _reservation_size(event: NormalizedDailyEvent) -> tuple[int, int]:
    """返回 (加入人数, 总人数)。"""

    total_count = reservation_party_size(event)
    return max(0, total_count - 1), total_count


def _headline_card(report: DailyReport) -> Headline | None:
    """生成今日头条卡片渲染数据。"""

    selection = report.headline
    if selection is None:
        return None
    event = selection.event
    attacker = _report_name(report, event.attacker_id, event.attacker_name)
    target = _report_name(report, event.target_id, event.target_name)
    victim = _report_name(
        report,
        event.backfire_victim_id or event.attacker_id,
        event.backfire_victim_name or event.attacker_name,
    )
    kind = selection.kind
    food = re.sub(r"\s+", " ", event.food or "今日出餐").strip()
    food = food if len(food) <= 10 else f"{food[:9]}…"
    joined_count, total_count = _reservation_size(event)
    if joined_count:
        opening = f"{attacker}召集 {joined_count} 名群友围住{target}"
        team_subject = f"{attacker}和另外 {joined_count} 名群友"
        team = f"由{attacker}带队的 {total_count} 人预约队伍"
    else:
        opening = f"{attacker}独自守着预约烤架等到{target}"
        team_subject = f"{attacker}独自"
        team = f"{attacker}的单人预约"

    if kind == "normal_success":
        tags = ("普通烤猪", "成功出餐")
    elif kind == "normal_escape":
        tags = ("普通烤猪", "目标逃脱")
    elif kind == "normal_backfire":
        tags = ("普通烤猪", "当场翻车")
    elif kind == "self_roast":
        tags = ("主动自烤", "自觉上桌")
    elif kind == "bot_backfire":
        tags = ("挑战 Bot", "当场伏诛")
    else:
        size_tag = "大型预约" if total_count >= 6 else "预约烤猪"
        count_tag = f"{total_count} 人参与"
        result_tag = {
            "reservation_success": "成功出餐",
            "reservation_escape": "集体扑空",
            "reservation_backfire": "意外走火",
            "reservation_human": "人类形态",
            "reservation_food": "早已熟透",
            "reservation_eaten": "已经吃掉",
            "reservation_sold": "已经售出",
        }[kind]
        if kind == "reservation_food":
            size_tag = "预约烤猪"
            count_tag = f"{total_count} 人围观"
        tags = (size_tag, count_tag, result_tag)

    template = _stable_template(
        report,
        "headline",
        kind,
        f"{event.event_id}:{total_count}:{food}:{victim}",
        DAILY_REPORT_HEADLINE_TEXTS[kind],
    )
    spans = _rich_template(
        template,
        {
            "attacker": RichSpan(attacker, INK, True),
            "target": RichSpan(target, INK, True),
            "victim": RichSpan(victim, INK, True),
            "food": RichSpan(food, RED, True),
            "success": RichSpan("被烤了", RED, True),
            "escape": RichSpan("溜了", RED, True),
            "backfire": RichSpan("被烤了", RED, True),
            "self_result": RichSpan("自烤", RED, True),
            "human_state": RichSpan("人类形态", RED, True),
            "food_state": RichSpan("熟食", RED, True),
            "eaten_state": RichSpan("被吃掉", RED, True),
            "sold_state": RichSpan("售出", RED, True),
            "opening": RichSpan(opening, INK),
            "team_subject": RichSpan(team_subject, INK),
            "team": RichSpan(team, INK),
            "forks": RichSpan(f"{total_count} 把烤叉", INK, True),
        },
    )
    return Headline(spans=spans, tags=tags, asset=_HEADLINE_ASSETS[kind])


def _timeline_event_detail(report: DailyReport, event: NormalizedDailyEvent) -> str:
    """将事件结果转换为稳定短句；同一事件重试时不得切换措辞。"""

    attacker = _report_name(report, event.attacker_id, event.attacker_name)
    target = _report_name(report, event.target_id, event.target_name)
    victim = _report_name(
        report,
        event.backfire_victim_id or event.attacker_id,
        event.backfire_victim_name or event.attacker_name,
    )
    if event.event_type == "self_roast":
        kind = "self_roast"
    elif event.event_type == "success":
        kind = "reservation_success" if event.is_reservation else "normal_success"
    elif event.event_type == "escape":
        kind = "escape"
    elif event.event_type == "bot_backfire":
        kind = "bot_backfire"
    elif event.event_type == "backfire":
        kind = "backfire"
    else:
        kind = {
            "human": "special_human",
            "food": "special_food",
            "eaten": "special_eaten",
            "sold": "special_sold",
        }.get(event.special_reason, "special_other")

    template = _stable_template(
        report,
        "timeline_detail",
        kind,
        event.event_id,
        DAILY_REPORT_TIMELINE_DETAIL_TEXTS[kind],
    )
    return template.format(attacker=attacker, target=target, victim=victim)


def _timeline_asset(event: NormalizedDailyEvent) -> tuple[str, RGB]:
    if event.event_type == "reserved_special":
        return "event_special_target", GOLD
    if event.is_reservation:
        return "event_reservation", GOLD
    if event.event_type in {"backfire", "bot_backfire"}:
        return "event_angry", (174, 56, 36)
    if event.event_type == "escape":
        return "event_run", (38, 111, 142)
    return "event_fire", (172, 55, 35)


def _timeline_intro(report: DailyReport, selection: TimelineSelection) -> str:
    """按事件链身份稳定选择开场，排版收口统一留给渲染层处理。"""

    events = selection.events
    first = selection.anchor_event or events[0]
    attacker = _report_name(report, first.attacker_id, first.attacker_name)
    target = _report_name(report, first.target_id, first.target_name)
    kind = selection.kind
    identity = ":".join(
        (first.event_id, *(event.event_id for event in events))
    )
    template = _stable_template(
        report,
        "timeline_intro",
        kind,
        identity,
        DAILY_REPORT_TIMELINE_INTRO_TEXTS[kind],
    )
    return template.format(
        attacker=attacker,
        target=target,
        event_count=len(events),
    )


def _timeline_card(report: DailyReport) -> tuple[str, tuple[EventItem, ...]]:
    selection = report.timeline
    if selection is None:
        return "", ()
    ordinal = ("第一次", "第二次", "第三次")
    items: list[EventItem] = []
    for index, event in enumerate(selection.events[:3]):
        asset, color = _timeline_asset(event)
        items.append(
            EventItem(
                ordinal[index],
                _timeline_event_detail(report, event),
                asset,
                color,
            )
        )
    return _timeline_intro(report, selection), tuple(items)


def _ranking_avatar(kind: RankingKind, entry: object) -> str:
    image_name = str(getattr(entry, "image_name", "") or "")
    if image_name:
        image_path = Path(image_name).expanduser()
        if image_path.is_file():
            return str(image_path.resolve())

    pig_id = str(getattr(entry, "pig_id", "") or "")
    pig = get_pig_by_id(pig_id) if pig_id else None
    if pig:
        requested_level = int(getattr(entry, "score", 0) or 0) if kind == "expert_level" else 0
        appearance = pig_resource_manager.resolve_pig_appearance(pig, requested_level)
        for candidate in (appearance.image_path, appearance.base_image_path):
            if candidate is not None and candidate.is_file():
                return str(candidate.resolve())
    return "header_pig_news"


def _compact_ranking_name(name: str) -> str:
    # 完整昵称交给排行栏的像素级缩字逻辑处理，不能在数据转换阶段固定截断。
    return re.sub(r"\s+", " ", name).strip() or "群友"


def _ranking_cards(report: DailyReport) -> tuple[RankingColumn, ...]:
    columns: list[RankingColumn] = []
    for ranking in report.rankings:
        rows: list[RankingEntry] = []
        for entry in ranking.entries[:3]:
            pig_name = entry.pig_name or _resource_pig_name(entry.pig_id)
            detail = {
                "expert_level": f"{pig_name} Lv.{entry.score}",
                "roast_success": f"{entry.score} 次",
                "catalog": f"{entry.score} 种",
            }[ranking.kind]
            rows.append(
                RankingEntry(
                    name=_compact_ranking_name(entry.display_name),
                    detail=detail,
                    avatar=_ranking_avatar(ranking.kind, entry),
                    rank=entry.rank,
                )
            )
        if rows:
            columns.append(
                RankingColumn(
                    title=_RANKING_TITLES[ranking.kind],
                    subtitle="",
                    entries=tuple(rows),
                )
            )
    return tuple(columns)


def _coupon_card(report: DailyReport) -> Coupon | None:
    if not report.protections:
        return None
    protection = report.protections[0]
    name = protection.display_name or report.display_names.get(protection.user_id, "") or protection.user_id
    benefit = protection.scope or "本群免烤"
    if "一天" not in benefit:
        benefit = f"{benefit}一天"
    expires = protection.expires_at
    try:
        parsed = dt.datetime.fromisoformat(expires.replace("Z", "+00:00"))
        expires = f"有效至 {parsed:%m.%d %H:%M}"
    except ValueError:
        expires = f"有效至 {expires}" if expires else "次日有效"
    roast_count = sum(
        1
        for event in report.events
        if event.event_type == "success"
        and event.target_id == protection.user_id
        and event.target_id != event.attacker_id
    )
    return Coupon(name=name, benefit=benefit, expires=expires, roast_count=roast_count)


def build_daily_report_card_data(
    report: DailyReport,
    *,
    cutoff_time: str = "23:45",
) -> DailyReportCardData:
    """把纯业务日报快照转换为不再执行规则判断的绘图数据。"""

    volume, year, month_day, weekday = _report_date_parts(report.date_str)
    event_intro, events = _timeline_card(report)
    return DailyReportCardData(
        volume=volume,
        date_year=year,
        date_month_day=month_day,
        weekday=weekday,
        stats=tuple(
            StatItem(metric.label, str(metric.value), metric.unit, _STAT_ASSETS[metric.kind])
            for metric in report.overview_metrics
        ),
        observation=_observation_card(report),
        headline=_headline_card(report),
        event_intro=event_intro,
        events=events,
        rankings=_ranking_cards(report),
        coupon=_coupon_card(report),
        footer=f"数据截止 {cutoff_time} · 仅统计本群当日记录",
    )


# ================================ Plus 异步渲染入口 ================================ #

_daily_report_card_renderer: DailyReportCardRenderer | None = None
_daily_report_card_render_lock = asyncio.Lock()


def _configured_body_font() -> Path:
    configured = str(plugin_config.rollpig_card_font_path or "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        if path.is_file():
            return path.resolve()
        logger.warning(f"RollPig 日报自定义正文字体不存在，改用插件内置思源字体: file={path}")
    return DAILY_REPORT_CARD_RESOURCE_DIR.parent / "fonts" / "SourceHanSansSC-Medium.otf"


def create_daily_report_card_renderer() -> DailyReportCardRenderer:
    """创建绑定插件内置日报资源和现有正文字体配置的 renderer。"""

    return DailyReportCardRenderer(
        root=DAILY_REPORT_CARD_RESOURCE_DIR,
        body_font=_configured_body_font(),
    )


def _get_daily_report_card_renderer() -> DailyReportCardRenderer:
    global _daily_report_card_renderer
    if _daily_report_card_renderer is None:
        _daily_report_card_renderer = create_daily_report_card_renderer()
    return _daily_report_card_renderer


def _render_daily_report_card_sync(
    report: DailyReport,
    *,
    cutoff_time: str,
    width: int,
    supersample: int,
) -> DailyReportCardRenderResult:
    renderer = _get_daily_report_card_renderer()
    card_data = build_daily_report_card_data(report, cutoff_time=cutoff_time)
    image, _layout = renderer.render(card_data, width=width, supersample=supersample)
    try:
        output = BytesIO()
        image.save(output, format="PNG", optimize=True)
        return DailyReportCardRenderResult(output.getvalue(), image.width, image.height)
    finally:
        image.close()


async def render_daily_report_card(
    report: DailyReport,
    *,
    cutoff_time: str = "23:45",
    width: int = 1024,
    supersample: int = 3,
) -> DailyReportCardRenderResult:
    """在线程中渲染日报；同进程串行使用共享字体和内置素材缓存。"""

    async with _daily_report_card_render_lock:
        return await asyncio.to_thread(
            _render_daily_report_card_sync,
            report,
            cutoff_time=cutoff_time,
            width=width,
            supersample=supersample,
        )


async def shutdown_daily_report_card_renderer() -> None:
    """关闭日报 renderer 的常驻素材缓存。"""

    global _daily_report_card_renderer
    async with _daily_report_card_render_lock:
        if _daily_report_card_renderer is not None:
            _daily_report_card_renderer.close()
            _daily_report_card_renderer = None
