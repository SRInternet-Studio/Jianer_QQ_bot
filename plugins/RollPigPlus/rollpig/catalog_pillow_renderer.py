from __future__ import annotations

import sys
from dataclasses import dataclass, field
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Iterable, Sequence

from ..host import logger
from pilmoji import Pilmoji
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

from .card_renderer import (
    EMOJI_SCALE_FACTOR,
    _has_emoji_candidate,
    _measure_text,
    _normalize_extra_emoji_symbols,
    _truncate_to_width,
    get_noto_emoji_source,
)


# ================================ 图鉴数据模型 ================================ #


@dataclass(frozen=True)
class CatalogCard:
    """单张小猪图鉴卡片；image_path 允许为空以处理资源缺失。"""

    pig_id: str
    name: str
    image_path: Path | None
    level: int = 0
    badge: str = ""
    fallback_image_path: Path | None = None


@dataclass(frozen=True)
class CatalogFavorite:
    """底栏本命猪摘要。"""

    name: str = "暂无"
    image_path: Path | None = None
    level: int = 0
    copies: int = 0
    fallback_image_path: Path | None = None


@dataclass(frozen=True)
class CatalogStats:
    """图鉴顶部和底栏使用的统计数据。"""

    unlocked: int
    total: int
    progress_percent: float
    max_level: int
    maxed_count: int
    recent_new_count: int = 0
    checkin_streak: int = 0
    roasted_7d: int = 0
    next_milestone: int = 0
    page: int = 1
    pages: int = 1


@dataclass(frozen=True)
class CatalogData:
    """一次完整图鉴渲染所需的只读输入。"""

    user_name: str
    stats: CatalogStats
    cards: tuple[CatalogCard, ...] = field(default_factory=tuple)
    favorite: CatalogFavorite = field(default_factory=CatalogFavorite)


CANVAS_SIZE = (1536, 1024)
CATALOG_PAGE_SIZE = 38


class CatalogRenderer:
    """使用固定底图和纯 Pillow 绘制 1536×1024 小猪图鉴。

    默认以 2 倍分辨率绘制后缩小，换取接近浏览器截图的文字和圆角抗锯齿。
    实例本身不保存用户数据，可跨请求复用。
    """

    def __init__(
        self,
        base_image: Path,
        font_path: Path,
        *,
        scale_factor: float = 2.0,
    ) -> None:
        safe_scale = float(scale_factor)
        if not 1.0 <= safe_scale <= 3.0:
            raise ValueError("scale_factor 必须在 1.0～3.0 之间")
        if not base_image.is_file():
            raise FileNotFoundError(f"图鉴底图不存在: {base_image}")
        if not font_path.is_file():
            raise FileNotFoundError(f"图鉴字体不存在: {font_path}")

        self.base_image = base_image
        self.font_path = font_path
        self.scale = safe_scale

    # ================================ 对外渲染入口 ================================ #

    def render(self, data: CatalogData, *, output_format: str = "PNG") -> bytes:
        """渲染图鉴并返回 PNG/JPEG 字节；输入卡片超过 38 张时只绘制当前页内容。"""

        normalized_format = "JPEG" if output_format.strip().lower() in {"jpg", "jpeg"} else "PNG"
        canvas = self._create_canvas()
        # Pillow 在 RGBA 画布上直接绘制半透明 fill 时会替换目标 alpha，
        # 不会像浏览器一样自动与已有底图合成。保留一份干净底图，末尾统一补合成，
        # 既保住背景纹理，也保证最终 PNG 没有被聊天客户端显示成黑块的透明区域。
        underlay = canvas.copy()

        self._draw_title(canvas, data)
        self._draw_top_stats(canvas, data)
        self._draw_progress(canvas, data)
        self._draw_cards(canvas, data.cards[:CATALOG_PAGE_SIZE])
        self._draw_footer(canvas, data)
        self._draw_page_box(canvas, data)

        canvas = Image.alpha_composite(underlay, canvas)

        if self.scale != 1:
            canvas = canvas.resize(CANVAS_SIZE, Image.Resampling.LANCZOS)

        output = BytesIO()
        if normalized_format == "JPEG":
            rgb = Image.new("RGB", canvas.size, (255, 255, 255))
            rgb.paste(canvas, mask=canvas.getchannel("A"))
            rgb.save(output, format="JPEG", quality=94, optimize=True)
        else:
            # 成图没有透明内容；RGB PNG 可减少不同聊天客户端/预览器对 alpha
            # 解释不一致造成的黑块，也比保留恒为 255 的 alpha 通道更直接。
            # level 4 比默认使用的 6 少约 40% 编码时间，而当前成图仅增大约
            # 2%~3%；对机器人即时回图比继续压缩几十 KiB 更划算。
            canvas.convert("RGB").save(output, format="PNG", compress_level=4)
        return output.getvalue()

    def render_to_file(self, data: CatalogData, output_path: Path) -> Path:
        """按输出文件扩展名渲染并写入磁盘，主要供独立演示和视觉回归使用。"""

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_format = "JPEG" if output_path.suffix.lower() in {".jpg", ".jpeg"} else "PNG"
        output_path.write_bytes(self.render(data, output_format=output_format))
        return output_path

    # ================================ 基础绘图工具 ================================ #

    def _create_canvas(self) -> Image.Image:
        stat = self.base_image.stat()
        # 底图及其超采样尺寸在请求之间不变。缓存只读母版、每次返回副本，
        # 避免重复 PNG 解码和 1536→3072 的大图缩放，同时不让绘制污染缓存。
        return _load_base_canvas_cached(
            str(self.base_image),
            stat.st_mtime_ns,
            stat.st_size,
            self.scale,
        ).copy()

    def _font(self, size: int) -> ImageFont.FreeTypeFont:
        return _load_font(str(self.font_path), max(1, self._v(size)))

    def _v(self, value: float | int) -> int:
        return int(round(float(value) * self.scale))

    def _xy(self, x: float, y: float) -> tuple[int, int]:
        return self._v(x), self._v(y)

    def _size(self, size: tuple[int, int]) -> tuple[int, int]:
        return self._v(size[0]), self._v(size[1])

    def _box(self, box: tuple[float, float, float, float]) -> tuple[int, int, int, int]:
        return tuple(self._v(value) for value in box)  # type: ignore[return-value]

    # ================================ Emoji 文本绘制 ================================ #

    def _draw_text(
        self,
        canvas: Image.Image,
        position: tuple[int, int],
        text: str,
        *,
        font: ImageFont.ImageFont,
        fill: tuple[int, int, int, int],
        anchor: str = "la",
        stroke_width: int = 0,
        stroke_fill: tuple[int, int, int, int] | None = None,
    ) -> None:
        """绘制单行文字；含 Emoji 时复用普通卡片的内置 Noto 贴图源。"""

        if _has_emoji_candidate(text):
            source = get_noto_emoji_source()
            if source is not None:
                try:
                    render_text = _normalize_extra_emoji_symbols(text)
                    # 图鉴会先超采样再缩回 1×，偏移量也必须同步放大，
                    # 否则 Emoji 在 2×/3× 模式下会相对中文基线偏低。
                    emoji_offset = (0, self._v(-1))
                    with Pilmoji(
                        canvas,
                        source=source,
                        render_discord_emoji=False,
                        emoji_scale_factor=EMOJI_SCALE_FACTOR,
                        emoji_position_offset=emoji_offset,
                    ) as pilmoji:
                        pilmoji.text(
                            position,
                            render_text,
                            fill=fill,
                            font=font,
                            anchor=anchor,
                            spacing=0,
                            stroke_width=stroke_width,
                            stroke_fill=stroke_fill,
                            emoji_scale_factor=EMOJI_SCALE_FACTOR,
                            emoji_position_offset=emoji_offset,
                        )
                    return
                except Exception as error:
                    logger.debug(
                        f"RollPig 图鉴 pilmoji 绘制失败，回退普通字体: "
                        f"text={text!r}, error={error}"
                    )

        ImageDraw.Draw(canvas).text(
            position,
            text,
            font=font,
            fill=fill,
            anchor=anchor,
            stroke_width=stroke_width,
            stroke_fill=stroke_fill,
        )

    def _fit_text(self, draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> str:
        """按真实贴图宽度截断，且不从 ZWJ/变体选择符中间切碎 Emoji。"""

        return _truncate_to_width(draw, text, font, max_width)

    def _paste_rounded_gradient(
        self,
        canvas: Image.Image,
        box: tuple[float, float, float, float],
        colors: Sequence[tuple[int, int, int, int]],
        *,
        radius: float,
        horizontal: bool = True,
    ) -> None:
        scaled_box = self._box(box)
        width = scaled_box[2] - scaled_box[0]
        height = scaled_box[3] - scaled_box[1]
        if width <= 0 or height <= 0:
            return
        gradient = _make_gradient((width, height), tuple(colors), horizontal=horizontal)
        mask = _rounded_mask(width, height, self._v(radius))
        canvas.paste(gradient, scaled_box[:2], mask)

    def _draw_centered_runs(
        self,
        draw: ImageDraw.ImageDraw,
        center: tuple[float, float],
        runs: Iterable[tuple[str, ImageFont.ImageFont, tuple[int, int, int, int]]],
    ) -> None:
        """将不同字号/颜色的文本片段作为一行整体居中。"""

        materialized = list(runs)
        widths = [_measure_text(draw, text, font) for text, font, _ in materialized]
        x = self._v(center[0]) - sum(widths) / 2
        y = self._v(center[1])
        for (text, font, color), width in zip(materialized, widths):
            draw.text((int(x), y), text, font=font, fill=color, anchor="lm")
            x += width

    # ================================ 顶部标题与统计 ================================ #

    def _draw_title(self, canvas: Image.Image, data: CatalogData) -> None:
        draw = ImageDraw.Draw(canvas)
        center_x = 408
        prefix_font = self._font(23)
        title_font = self._font(45)

        prefix = self._fit_text(
            draw,
            f"{data.user_name} 的",
            prefix_font,
            self._v(390),
        )
        self._draw_text(
            canvas,
            self._xy(center_x, 94),
            prefix,
            font=prefix_font,
            fill=(113, 87, 164, 220),
            anchor="mm",
            stroke_width=self._v(1),
            stroke_fill=(255, 255, 255, 210),
        )

        # 标题只保留干净渐变，不再叠加投影；底图本身已有足够装饰层次。
        title = "小猪图鉴"
        self._draw_gradient_text(
            canvas,
            title,
            title_font,
            center=(center_x, 138),
            top_color=(101, 80, 164, 255),
            bottom_color=(213, 111, 168, 255),
        )

    def _draw_gradient_text(
        self,
        canvas: Image.Image,
        text: str,
        font: ImageFont.ImageFont,
        *,
        center: tuple[float, float],
        top_color: tuple[int, int, int, int],
        bottom_color: tuple[int, int, int, int],
    ) -> None:
        """绘制纵向渐变文字；局部蒙版避免为整张大图分配额外渐变层。"""

        probe = ImageDraw.Draw(canvas)
        bbox = probe.textbbox((0, 0), text, font=font, stroke_width=self._v(1))
        width = bbox[2] - bbox[0] + self._v(8)
        height = bbox[3] - bbox[1] + self._v(8)
        mask = Image.new("L", (width, height), 0)
        mask_draw = ImageDraw.Draw(mask)
        mask_draw.text(
            (self._v(4) - bbox[0], self._v(4) - bbox[1]),
            text,
            font=font,
            fill=255,
            stroke_width=self._v(1),
            stroke_fill=255,
        )
        gradient = _make_gradient((width, height), (top_color, bottom_color), horizontal=False)
        left = self._v(center[0]) - width // 2
        top = self._v(center[1]) - height // 2
        canvas.paste(gradient, (left, top), mask)

    def _draw_top_stats(self, canvas: Image.Image, data: CatalogData) -> None:
        draw = ImageDraw.Draw(canvas)
        label_font = self._font(17)
        value_font = self._font(29)
        stats = data.stats
        items = (
            ("已解锁", f"{stats.unlocked}/{stats.total}", (166, 90, 153, 255)),
            ("完成度", f"{stats.progress_percent:.1f}%", (116, 96, 189, 255)),
            ("最高 EX", f"Lv.{max(0, stats.max_level)}", (79, 133, 180, 255)),
            ("满级", f"{max(0, stats.maxed_count)} 只", (87, 155, 154, 255)),
        )
        # 与原 CSS grid 完全一致：592px 宽、4 列、13px gap，再应用各卡片微调。
        column_width = (592 - 13 * 3) / 4
        offsets = (-2, 5, 5, 6)
        for index, (label, value, label_color) in enumerate(items):
            center_x = 681 + column_width / 2 + index * (column_width + 13) + offsets[index]
            draw.text(
                self._xy(center_x, 55),
                label,
                font=label_font,
                fill=label_color,
                anchor="mm",
                stroke_width=max(1, self._v(0.5)),
                stroke_fill=label_color,
            )
            draw.text(
                self._xy(center_x, 84),
                value,
                font=value_font,
                fill=(85, 69, 142, 255),
                anchor="mm",
                stroke_width=max(1, self._v(0.5)),
                stroke_fill=(85, 69, 142, 255),
            )

    def _draw_progress(self, canvas: Image.Image, data: CatalogData) -> None:
        percent = max(0.0, min(100.0, float(data.stats.progress_percent)))
        left, top, width, height = 714, 137, 533, 38
        fill_width = width * percent / 100
        if fill_width > 1:
            self._paste_rounded_gradient(
                canvas,
                (left, top, left + fill_width, top + height),
                (
                    (255, 158, 210, 158),
                    (255, 207, 129, 148),
                    (154, 226, 255, 138),
                ),
                radius=19,
            )
            shine_width = self._v(max(1, fill_width))
            shine_height = self._v(height)
            shine = Image.new("RGBA", (shine_width, shine_height), (0, 0, 0, 0))
            shine_draw = ImageDraw.Draw(shine)
            shine_draw.rounded_rectangle(
                (
                    self._v(5),
                    self._v(4),
                    max(self._v(6), shine_width - self._v(5)),
                    self._v(14),
                ),
                radius=self._v(8),
                fill=(255, 255, 255, 62),
            )
            canvas.alpha_composite(shine, self._xy(left, top))

        draw = ImageDraw.Draw(canvas)
        draw.text(
            self._xy(left + width / 2, top + height / 2),
            f"图鉴完成度 {percent:.1f}%",
            font=self._font(16),
            fill=(255, 255, 255, 255),
            anchor="mm",
            stroke_width=self._v(2),
            stroke_fill=(104, 59, 128, 180),
        )

    # ================================ 图鉴卡片网格 ================================ #

    def _card_box(self, index: int) -> tuple[float, float, float, float]:
        row, column = divmod(index, 8)
        if row == 4:
            column += 1
        left = 104 + column * 161
        top = 233 + row * 126
        return left, top, left + 148, top + 106

    def _draw_cards(self, canvas: Image.Image, cards: Sequence[CatalogCard]) -> None:
        if not cards:
            return

        # 卡片尺寸固定，复用一张局部阴影贴片；避免对整张 2× 画布做高斯模糊。
        shadow_patch, shadow_padding = _rounded_shadow_patch(
            self._v(148),
            self._v(106),
            self._v(18),
            self._v(8),
            self._v(8),
            (91, 55, 130, 46),
        )
        for index, _card in enumerate(cards):
            left, top, _right, _bottom = self._card_box(index)
            canvas.alpha_composite(
                shadow_patch,
                (
                    self._v(left + 1) - shadow_padding,
                    self._v(top) - shadow_padding,
                ),
            )

        for index, card in enumerate(cards):
            self._draw_card(canvas, card, self._card_box(index))

    def _draw_card(
        self,
        canvas: Image.Image,
        card: CatalogCard,
        box: tuple[float, float, float, float],
    ) -> None:
        left, top, right, bottom = box
        is_max = card.badge.upper() == "MAX" or card.level >= 5
        self._draw_glass_card_background(canvas, box, is_max=is_max)
        draw = ImageDraw.Draw(canvas)

        # 内容统一向内收 7~8px；图片、等级胶囊和角标都不能借用外框边界。
        self._draw_pig_image(
            canvas,
            card.image_path,
            fallback_image_path=card.fallback_image_path,
            center=(left + 74, top + 30),
            max_size=(54, 48),
        )

        name_font = self._font(15)
        name = self._fit_text(draw, card.name, name_font, self._v(132))
        # 内置字体只有 Medium 字重，不再附加描边模拟粗体；白色下层只保留
        # 原模板很轻的文字分离感，避免猪名显得发黑、过重。
        # Emoji 是彩色贴图，重复绘制白色分离层只会造成一像素重影；
        # 纯文字猪名继续保留原模板的轻微白色下层。
        if not _has_emoji_candidate(name):
            self._draw_text(
                canvas,
                self._xy(left + 74, top + 69),
                name,
                font=name_font,
                fill=(255, 255, 255, 190),
                anchor="mm",
            )
        self._draw_text(
            canvas,
            self._xy(left + 74, top + 68),
            name,
            font=name_font,
            fill=(85, 73, 127, 255),
            anchor="mm",
        )

        level = max(0, min(5, int(card.level)))
        self._draw_level_pill(
            canvas,
            level,
            box=(left + 44, top + 81, left + 104, top + 99),
        )

        badge = card.badge.upper().strip()
        if badge:
            self._draw_badge(canvas, badge, right=right - 8, top=top + 8)

    def _draw_glass_card_background(
        self,
        canvas: Image.Image,
        box: tuple[float, float, float, float],
        *,
        is_max: bool,
    ) -> None:
        """模拟原模板的毛玻璃卡片，不额外制造会形成波浪的椭圆反光。"""

        left, top, right, bottom = self._box(box)
        width = right - left
        height = bottom - top
        radius = self._v(18)
        stat = self.base_image.stat()
        # 卡片位置与底图均固定，毛玻璃结果不依赖用户数据。缓存透明圆角贴片后，
        # 热渲染无需再对 38 个区域逐个模糊、增强和着色。
        glass = _load_glass_card_patch_cached(
            str(self.base_image),
            stat.st_mtime_ns,
            stat.st_size,
            self.scale,
            (left, top, right, bottom),
        )
        canvas.alpha_composite(glass, (left, top))

        border = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        border_draw = ImageDraw.Draw(border)
        outer_color = (255, 218, 99, 225) if is_max else (255, 255, 255, 184)
        border_draw.rounded_rectangle(
            (1, 1, width - 2, height - 2),
            radius=radius,
            outline=outer_color,
            width=self._v(2),
        )
        border_draw.rounded_rectangle(
            (self._v(3), self._v(3), width - self._v(4), height - self._v(4)),
            radius=self._v(15),
            outline=(207, 180, 255, 117),
            width=max(1, self._v(1)),
        )
        _draw_dashed_rounded_border(
            border_draw,
            (
                self._v(5),
                self._v(5),
                width - self._v(6),
                height - self._v(6),
            ),
            radius=self._v(12),
            fill=(255, 153, 211, 56),
            width=max(1, self._v(1)),
            dash=self._v(3),
            gap=self._v(3),
        )
        canvas.alpha_composite(border, (left, top))

    def _draw_level_pill(
        self,
        canvas: Image.Image,
        level: int,
        *,
        box: tuple[float, float, float, float],
    ) -> None:
        """绘制克制的三段渐变 EX 胶囊；上下内高光塑形，不使用整圈弧线。"""

        palettes = {
            0: (
                (255, 255, 255, 255), (237, 248, 252, 255), (207, 228, 239, 255),
                (142, 177, 198, 190), (91, 115, 137, 255),
            ),
            1: (
                (255, 255, 255, 255), (255, 241, 250, 255), (255, 213, 237, 255),
                (239, 143, 196, 190), (142, 84, 145, 255),
            ),
            2: (
                (255, 255, 255, 255), (246, 240, 255, 255), (224, 207, 255, 255),
                (167, 137, 225, 190), (105, 84, 157, 255),
            ),
            3: (
                (255, 255, 255, 255), (234, 249, 255, 255), (190, 231, 251, 255),
                (104, 187, 222, 190), (70, 119, 154, 255),
            ),
            4: (
                (255, 255, 255, 255), (255, 244, 224, 255), (255, 211, 156, 255),
                (231, 149, 66, 190), (137, 88, 28, 255),
            ),
            5: (
                (255, 255, 250, 255), (255, 242, 190, 255), (255, 207, 83, 255),
                (235, 157, 38, 210), (126, 86, 10, 255),
            ),
        }
        top_color, middle_color, bottom_color, outline_color, text_color = palettes[level]
        left, top, right, bottom = self._box(box)
        width = right - left
        height = bottom - top
        radius = height // 2

        shadow_padding = self._v(4)
        shadow = Image.new(
            "RGBA",
            (width + shadow_padding * 2, height + shadow_padding * 2),
            (0, 0, 0, 0),
        )
        shadow_draw = ImageDraw.Draw(shadow)
        shadow_draw.rounded_rectangle(
            (
                shadow_padding,
                shadow_padding + self._v(2),
                shadow_padding + width,
                shadow_padding + height + self._v(2),
            ),
            radius=radius,
            fill=(94, 60, 128, 38),
        )
        canvas.alpha_composite(
            shadow.filter(ImageFilter.GaussianBlur(self._v(2))),
            (left - shadow_padding, top - shadow_padding),
        )

        gradient = _make_gradient(
            (width, height),
            (top_color, middle_color, bottom_color),
            horizontal=False,
        )
        mask = _rounded_mask(width, height, radius)
        canvas.paste(gradient, (left, top), mask)

        detail = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        detail_draw = ImageDraw.Draw(detail)
        detail_draw.rounded_rectangle(
            (0, 0, width - 1, height - 1),
            radius=radius,
            outline=outline_color,
            width=max(1, self._v(1)),
        )
        detail_draw.line(
            (radius, self._v(2), width - radius, self._v(2)),
            fill=(255, 255, 255, 220),
            width=max(1, self._v(1)),
        )
        detail_draw.line(
            (radius, height - self._v(3), width - radius, height - self._v(3)),
            fill=(*text_color[:3], 42),
            width=max(1, self._v(1)),
        )
        canvas.alpha_composite(detail, (left, top))
        ImageDraw.Draw(canvas).text(
            ((left + right) // 2, (top + bottom) // 2),
            f"EX Lv.{level}",
            font=self._font(11),
            fill=text_color,
            anchor="mm",
        )

    def _draw_badge(self, canvas: Image.Image, badge: str, *, right: float, top: float) -> None:
        draw = ImageDraw.Draw(canvas)
        font = self._font(11)
        text_width = draw.textlength(badge, font=font)
        width = max(self._v(37), int(text_width + self._v(12)))
        left = self._v(right) - width
        top_px = self._v(top)
        height = self._v(18)
        # 原版色相保持不变，但增加一个柔和中间色标，让 18px 高度内仍能看出
        # 顶亮、中央主色和底部收暗；立体层次来自底色，不再依赖文字描边。
        colors = (
            ((255, 245, 184, 255), (255, 211, 79, 255), (255, 157, 52, 255))
            if badge == "MAX"
            else ((211, 248, 255, 255), (101, 224, 255, 255), (75, 157, 255, 255))
        )

        shadow_padding = self._v(4)
        shadow = Image.new(
            "RGBA",
            (width + shadow_padding * 2, height + shadow_padding * 2),
            (0, 0, 0, 0),
        )
        shadow_draw = ImageDraw.Draw(shadow)
        shadow_draw.rounded_rectangle(
            (
                shadow_padding,
                shadow_padding + self._v(2),
                shadow_padding + width,
                shadow_padding + height + self._v(2),
            ),
            radius=height // 2,
            fill=(86, 70, 172, 34),
        )
        canvas.alpha_composite(
            shadow.filter(ImageFilter.GaussianBlur(self._v(2))),
            (left - shadow_padding, top_px - shadow_padding),
        )

        gradient = _make_gradient((width, height), colors, horizontal=False)
        mask = _rounded_mask(width, height, height // 2)
        canvas.paste(gradient, (left, top_px), mask)

        detail = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        detail_draw = ImageDraw.Draw(detail)
        detail_draw.rounded_rectangle(
            (0, 0, width - 1, height - 1),
            radius=height // 2,
            outline=(255, 255, 255, 105),
            width=max(1, self._v(0.5)),
        )
        detail_draw.line(
            (height // 2, self._v(2), width - height // 2, self._v(2)),
            fill=(255, 255, 255, 175),
            width=max(1, self._v(0.5)),
        )
        bottom_color = (164, 82, 25, 58) if badge == "MAX" else (39, 92, 177, 52)
        detail_draw.line(
            (height // 2, height - self._v(2), width - height // 2, height - self._v(2)),
            fill=bottom_color,
            width=max(1, self._v(0.5)),
        )
        canvas.alpha_composite(detail, (left, top_px))

        # 文字只绘制一次纯白前景：没有 stroke，也没有模糊文字阴影，因此不会
        # 再出现包着字或胶囊边缘的脏紫色光晕。
        text_position = (width // 2, height // 2)
        ImageDraw.Draw(canvas).text(
            (left + text_position[0], top_px + text_position[1]),
            badge,
            font=font,
            fill=(255, 255, 255, 255),
            anchor="mm",
        )

    def _draw_pig_image(
        self,
        canvas: Image.Image,
        image_path: Path | None,
        *,
        fallback_image_path: Path | None = None,
        center: tuple[float, float],
        max_size: tuple[int, int] = (58, 52),
    ) -> None:
        target_size = self._size(max_size)
        pig_patch = _load_pig_patch(
            image_path,
            target_size=target_size,
            shadow_blur=self._v(2),
            shadow_offset=self._v(3),
        )
        # EX 立绘是可选增强；文件虽然存在但无法解码时，优先回退仍可用的基础立绘，
        # 只有两者都失败才显示通用占位猪。
        if pig_patch is None and fallback_image_path != image_path:
            pig_patch = _load_pig_patch(
                fallback_image_path,
                target_size=target_size,
                shadow_blur=self._v(2),
                shadow_offset=self._v(3),
            )
        if pig_patch is None:
            draw = ImageDraw.Draw(canvas)
            radius = self._v(22)
            cx, cy = self._xy(*center)
            draw.ellipse(
                (cx - radius, cy - radius, cx + radius, cy + radius),
                fill=(255, 225, 239, 255),
            )
            draw.text(
                (cx, cy),
                "猪",
                font=self._font(18),
                fill=(142, 108, 170, 255),
                anchor="mm",
            )
            return

        patch, target_width, target_height = pig_patch
        cx, cy = self._xy(*center)
        x = cx - target_width // 2
        y = cy - target_height // 2
        canvas.alpha_composite(patch, (x, y))

    # ================================ 底部摘要与页码 ================================ #

    def _draw_footer(self, canvas: Image.Image, data: CatalogData) -> None:
        draw = ImageDraw.Draw(canvas, "RGBA")
        favorite = data.favorite

        draw.rounded_rectangle(
            self._box((346, 903, 542, 961)),
            radius=self._v(22),
            fill=(255, 248, 253, 148),
            outline=(198, 155, 229, 88),
            width=max(1, self._v(1)),
        )
        draw.rounded_rectangle(
            self._box((356, 909, 402, 955)),
            radius=self._v(16),
            fill=(255, 255, 255, 164),
            outline=(255, 180, 224, 112),
            width=max(1, self._v(1)),
        )
        self._draw_pig_image(
            canvas,
            favorite.image_path,
            fallback_image_path=favorite.fallback_image_path,
            center=(379, 932),
            max_size=(39, 39),
        )

        draw = ImageDraw.Draw(canvas)
        draw.text(
            self._xy(414, 916),
            "本命猪",
            font=self._font(13),
            fill=(141, 94, 158, 200),
            anchor="lm",
        )
        favorite_name = self._fit_text(draw, favorite.name, self._font(19), self._v(116))
        self._draw_text(
            canvas,
            self._xy(414, 935),
            favorite_name,
            font=self._font(19),
            fill=(103, 80, 156, 255),
            anchor="lm",
            stroke_width=max(1, self._v(0.5)),
            stroke_fill=(103, 80, 156, 255),
        )
        draw.text(
            self._xy(414, 952),
            f"EX Lv.{max(0, min(5, favorite.level))} · {max(0, favorite.copies)} 次",
            font=self._font(13),
            fill=(108, 84, 159, 205),
            anchor="lm",
        )

        stats = data.stats
        chips = (
            (
                580, 903, 817, 931, "近 7 天新猪", str(stats.recent_new_count), "只",
                (255, 226, 246, 158), (222, 106, 169, 255),
            ),
            (827, 903, 1064, 931, "连续打卡", str(stats.checkin_streak), "天", (255, 255, 255, 132), (127, 104, 207, 255)),
            (580, 937, 817, 965, "近 7 天被烤", str(stats.roasted_7d), "次", (255, 255, 255, 132), (63, 155, 207, 255)),
            (827, 937, 1064, 965, "下个里程碑", str(stats.next_milestone), "只", (255, 255, 255, 132), (211, 138, 56, 255)),
        )
        for left, top, right, bottom, label, number, suffix, fill, accent in chips:
            draw.rounded_rectangle(
                self._box((left, top, right, bottom)),
                radius=self._v(14),
                fill=fill,
                outline=(197, 154, 229, 82),
                width=max(1, self._v(1)),
            )
            self._draw_centered_runs(
                draw,
                ((left + right) / 2, (top + bottom) / 2),
                (
                    (label + " ", self._font(14), (104, 80, 155, 225)),
                    (number, self._font(16), accent),
                    (" " + suffix, self._font(14), (104, 80, 155, 225)),
                ),
            )

    def _draw_page_box(self, canvas: Image.Image, data: CatalogData) -> None:
        draw = ImageDraw.Draw(canvas)
        page = max(1, data.stats.page)
        pages = max(page, data.stats.pages, 1)
        center_y = 943
        draw.text(
            self._xy(1152, center_y),
            "‹",
            font=self._font(30),
            fill=(177, 138, 222, 255),
            anchor="mm",
        )
        draw.text(
            self._xy(1260, center_y),
            f"第 {page} / {pages} 页",
            font=self._font(23),
            fill=(109, 88, 165, 255),
            anchor="mm",
            stroke_width=max(1, self._v(0.5)),
            stroke_fill=(109, 88, 165, 255),
        )
        draw.text(
            self._xy(1368, center_y),
            "›",
            font=self._font(30),
            fill=(177, 138, 222, 255),
            anchor="mm",
        )


# ================================ 模块级图像缓存 ================================ #


@lru_cache(maxsize=4)
def _load_font_bytes(font_path: str) -> bytes:
    """仅供 Windows 非 ASCII 路径回退；不同字号共享同一份源字体字节。"""

    return Path(font_path).read_bytes()


@lru_cache(maxsize=2)
def _load_font(font_path: str, size: int) -> ImageFont.FreeTypeFont:
    """按平台选择直接文件或内存回退，并只缓存最常用的两个字号。"""

    # Linux、Docker、WSL2 以及 Windows ASCII 路径都应直接交给 FreeType：
    # 它可利用文件映射/系统页缓存，不必把完整 CJK 字体复制到 Python 内存。
    # Windows FreeType 无法可靠打开非 ASCII 路径，才使用 Pillow 同类的内存回退。
    if sys.platform == "win32" and not font_path.isascii():
        source: str | BytesIO = BytesIO(_load_font_bytes(font_path))
    else:
        source = font_path
    return ImageFont.truetype(source, size=size)


@lru_cache(maxsize=3)
def _load_base_canvas_cached(
    path: str,
    mtime_ns: int,
    file_size: int,
    scale: float,
) -> Image.Image:
    """解码并缓存指定倍率的只读底图母版；文件元数据变化时自动失效。"""

    del mtime_ns, file_size
    with Image.open(path) as opened:
        base = ImageOps.exif_transpose(opened).convert("RGBA")
    if base.size != CANVAS_SIZE:
        base = base.resize(CANVAS_SIZE, Image.Resampling.LANCZOS)
    if scale != 1:
        scaled_size = round(CANVAS_SIZE[0] * scale), round(CANVAS_SIZE[1] * scale)
        base = base.resize(scaled_size, Image.Resampling.LANCZOS)
    return base


@lru_cache(maxsize=64)
def _load_glass_card_patch_cached(
    path: str,
    mtime_ns: int,
    file_size: int,
    scale: float,
    box: tuple[int, int, int, int],
) -> Image.Image:
    """从固定底图生成可复用的透明毛玻璃卡片底，不包含动态边框。"""

    left, top, right, bottom = box
    width = right - left
    height = bottom - top
    radius = round(18 * scale)
    background = _load_base_canvas_cached(path, mtime_ns, file_size, scale).crop(box)
    background = background.filter(ImageFilter.GaussianBlur(round(6 * scale)))

    # 只做中等模糊并降低白粉覆盖，让底图纹理和颜色能透过卡片；这是“半透
    # 半毛”的平衡，不再把每张卡片处理成近乎不透明的乳白色块。
    background = ImageEnhance.Color(background).enhance(1.10)
    background = ImageEnhance.Brightness(background).enhance(1.01)
    tint = _make_gradient(
        (width, height),
        (
            (255, 255, 255, 148),
            (255, 252, 254, 112),
            (255, 246, 253, 88),
        ),
        horizontal=False,
    )
    glass = Image.alpha_composite(background, tint)
    patch = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    patch.paste(glass, (0, 0), _rounded_mask(width, height, radius))
    return patch


@lru_cache(maxsize=192)
def _load_rgba_image_cached(path: str, mtime_ns: int, file_size: int) -> Image.Image | None:
    """读取静态首帧并缓存；mtime 与文件大小用于资源更新后的自动失效。"""

    del mtime_ns, file_size
    try:
        with Image.open(path) as opened:
            if getattr(opened, "is_animated", False):
                opened.seek(0)
            image = ImageOps.exif_transpose(opened).convert("RGBA")
            # 图鉴最大只显示约 174×156（3×超采样）；缓存原始大图会让几十张
            # 800px/更大素材常驻数百 MiB。统一收敛到 256px 后再进入 LRU。
            image.thumbnail((256, 256), Image.Resampling.LANCZOS)
            return image
    except (OSError, ValueError):
        return None


def _load_pig_patch(
    path: Path | None,
    *,
    target_size: tuple[int, int],
    shadow_blur: int,
    shadow_offset: int,
) -> tuple[Image.Image, int, int] | None:
    """读取精确显示尺寸的小猪与投影贴片；同一资源热渲染时直接复用。"""

    if path is None:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    return _load_pig_patch_cached(
        str(path),
        stat.st_mtime_ns,
        stat.st_size,
        target_size,
        shadow_blur,
        shadow_offset,
    )


@lru_cache(maxsize=192)
def _load_pig_patch_cached(
    path: str,
    mtime_ns: int,
    file_size: int,
    target_size: tuple[int, int],
    shadow_blur: int,
    shadow_offset: int,
) -> tuple[Image.Image, int, int] | None:
    """缓存已缩放的小猪和 alpha 投影，避免每次请求重复缩放与模糊。"""

    source = _load_rgba_image_cached(path, mtime_ns, file_size)
    if source is None:
        return None
    target = source.copy()
    target.thumbnail(target_size, Image.Resampling.LANCZOS)

    # 投影沿用源图 alpha，贴片额外增加底部高度，避免下移部分被裁掉。
    alpha = target.getchannel("A")
    shadow_alpha = alpha.filter(ImageFilter.GaussianBlur(shadow_blur))
    shadow = Image.new("RGBA", target.size, (66, 45, 92, 0))
    shadow.putalpha(shadow_alpha.point(lambda value: int(value * 0.24)))
    patch = Image.new(
        "RGBA",
        (target.width, target.height + max(0, shadow_offset)),
        (0, 0, 0, 0),
    )
    patch.alpha_composite(shadow, (0, shadow_offset))
    patch.alpha_composite(target, (0, 0))
    return patch, target.width, target.height


@lru_cache(maxsize=128)
def _rounded_mask(width: int, height: int, radius: int) -> Image.Image:
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, width - 1, height - 1), radius=radius, fill=255)
    return mask


@lru_cache(maxsize=96)
def _make_gradient(
    size: tuple[int, int],
    colors: tuple[tuple[int, int, int, int], ...],
    *,
    horizontal: bool,
) -> Image.Image:
    """生成支持多个色标的线性 RGBA 渐变；色标按等间距分布。"""

    if not colors:
        raise ValueError("渐变至少需要一个颜色")
    if len(colors) == 1:
        return Image.new("RGBA", size, colors[0])

    width, height = size
    length = width if horizontal else height
    strip_size = (length, 1) if horizontal else (1, length)
    strip = Image.new("RGBA", strip_size)
    pixels = strip.load()
    segments = len(colors) - 1
    for position in range(length):
        ratio = position / max(1, length - 1)
        segment_position = ratio * segments
        segment = min(segments - 1, int(segment_position))
        local_ratio = segment_position - segment
        start = colors[segment]
        end = colors[segment + 1]
        color = tuple(
            int(round(start[channel] + (end[channel] - start[channel]) * local_ratio))
            for channel in range(4)
        )
        if horizontal:
            pixels[position, 0] = color
        else:
            pixels[0, position] = color
    return strip.resize(size)


def _draw_dashed_rounded_border(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    *,
    radius: int,
    fill: tuple[int, int, int, int],
    width: int,
    dash: int,
    gap: int,
) -> None:
    """绘制低存在感的圆角虚线内框，复现原模板卡片的细节层次。"""

    left, top, right, bottom = box
    radius = max(width, min(radius, (right - left) // 2, (bottom - top) // 2))
    dash = max(1, dash)
    gap = max(1, gap)

    # 圆角本身保持连续，直边使用短虚线；低透明度下比把整圈拆成碎弧更干净。
    draw.arc((left, top, left + radius * 2, top + radius * 2), 180, 270, fill=fill, width=width)
    draw.arc((right - radius * 2, top, right, top + radius * 2), 270, 360, fill=fill, width=width)
    draw.arc((right - radius * 2, bottom - radius * 2, right, bottom), 0, 90, fill=fill, width=width)
    draw.arc((left, bottom - radius * 2, left + radius * 2, bottom), 90, 180, fill=fill, width=width)

    for start in range(left + radius, right - radius, dash + gap):
        end = min(start + dash, right - radius)
        draw.line((start, top, end, top), fill=fill, width=width)
        draw.line((start, bottom, end, bottom), fill=fill, width=width)
    for start in range(top + radius, bottom - radius, dash + gap):
        end = min(start + dash, bottom - radius)
        draw.line((left, start, left, end), fill=fill, width=width)
        draw.line((right, start, right, end), fill=fill, width=width)


@lru_cache(maxsize=16)
def _rounded_shadow_patch(
    width: int,
    height: int,
    radius: int,
    blur_radius: int,
    offset_y: int,
    fill: tuple[int, int, int, int],
) -> tuple[Image.Image, int]:
    """生成可重复贴到多张同尺寸卡片下方的局部模糊阴影。"""

    padding = blur_radius * 3 + abs(offset_y)
    patch = Image.new(
        "RGBA",
        (width + padding * 2, height + padding * 2),
        (0, 0, 0, 0),
    )
    ImageDraw.Draw(patch).rounded_rectangle(
        (
            padding,
            padding + offset_y,
            padding + width,
            padding + offset_y + height,
        ),
        radius=radius,
        fill=fill,
    )
    return patch.filter(ImageFilter.GaussianBlur(blur_radius)), padding


def clear_catalog_pillow_caches() -> None:
    """释放字体、底图和贴片 LRU；供 NoneBot 关闭或插件重载时主动回收常驻内存。"""

    for cached_function in (
        _load_font_bytes,
        _load_font,
        _load_base_canvas_cached,
        _load_glass_card_patch_cached,
        _load_rgba_image_cached,
        _load_pig_patch_cached,
        _rounded_mask,
        _make_gradient,
        _rounded_shadow_patch,
    ):
        cached_function.cache_clear()
