"""PIL rendering for the Wordle board.

Colors and layout mirror ``nonebot-plugin-wordle`` (MIT).  Every entry point
returns PNG ``bytes`` or ``None`` when Pillow is unavailable, so callers can
fall back to the plain-text board.
"""

from __future__ import annotations

from functools import lru_cache
from io import BytesIO
from typing import TYPE_CHECKING, Any

from plugins.Wordle.game import ABSENT, GREEN, YELLOW

if TYPE_CHECKING:  # pragma: no cover - typing only
    from plugins.Wordle.game import Wordle


BLOCK_SIZE = (44, 44)
BLOCK_PADDING = (8, 8)
PADDING = (20, 20)
BORDER_WIDTH = 2
FONT_SIZE = 24

CORRECT_COLOR = (134, 163, 115)
EXIST_COLOR = (198, 182, 109)
WRONG_COLOR = (123, 123, 124)
BORDER_COLOR = (123, 123, 124)
BG_COLOR = (255, 255, 255)
FONT_COLOR = (255, 255, 255)

_COLOR_BY_CODE = {
    GREEN: CORRECT_COLOR,
    YELLOW: EXIST_COLOR,
    ABSENT: WRONG_COLOR,
}


def available() -> bool:
    try:
        import PIL  # noqa: F401
    except ImportError:
        return False
    return True


@lru_cache(maxsize=1)
def _font() -> Any:
    from PIL import ImageFont

    for loader in (
        lambda: ImageFont.load_default(size=FONT_SIZE),
        lambda: ImageFont.load_default(),
    ):
        try:
            return loader()
        except TypeError:
            continue
    return None


def _block(letter: str, color: tuple[int, int, int]) -> Any:
    from PIL import Image, ImageDraw

    block = Image.new("RGB", BLOCK_SIZE, BORDER_COLOR)
    inner_w = BLOCK_SIZE[0] - BORDER_WIDTH * 2
    inner_h = BLOCK_SIZE[1] - BORDER_WIDTH * 2
    inner = Image.new("RGB", (inner_w, inner_h), color)
    block.paste(inner, (BORDER_WIDTH, BORDER_WIDTH))
    if letter:
        draw = ImageDraw.Draw(block)
        font = _font()
        text = letter.upper()
        if font is not None:
            bbox = draw.textbbox((0, 0), text, font=font)
        else:
            bbox = draw.textbbox((0, 0), text)
        width = bbox[2] - bbox[0]
        height = bbox[3] - bbox[1]
        x = (BLOCK_SIZE[0] - width) / 2 - bbox[0]
        y = (BLOCK_SIZE[1] - height) / 2 - bbox[1]
        draw.text((x, y), text, font=font, fill=FONT_COLOR)
    return block


def _encode(image: Any) -> bytes:
    buffer = BytesIO()
    image.convert("RGBA").save(buffer, format="PNG")
    return buffer.getvalue()


def render_board(game: "Wordle") -> bytes | None:
    """Draw the full guess grid, including blank rows for unused attempts."""

    if not available():
        return None
    from PIL import Image

    length = game.length
    rows = game.rows
    board_w = length * BLOCK_SIZE[0] + (length - 1) * BLOCK_PADDING[0] + 2 * PADDING[0]
    board_h = rows * BLOCK_SIZE[1] + (rows - 1) * BLOCK_PADDING[1] + 2 * PADDING[1]
    board = Image.new("RGB", (board_w, board_h), BG_COLOR)

    for row in range(rows):
        if row < len(game.guessed_words):
            word = game.guessed_words[row]
            codes = game.codes_for(word)
            blocks = [
                _block(word[col], _COLOR_BY_CODE.get(codes[col], WRONG_COLOR))
                for col in range(length)
            ]
        else:
            blocks = [_block("", BG_COLOR) for _ in range(length)]
        for col, block in enumerate(blocks):
            x = PADDING[0] + (BLOCK_SIZE[0] + BLOCK_PADDING[0]) * col
            y = PADDING[1] + (BLOCK_SIZE[1] + BLOCK_PADDING[1]) * row
            board.paste(block, (x, y))
    return _encode(board)


def render_hint(hint: str) -> bytes | None:
    """Draw the discovered letters of ``hint`` as a single green row."""

    if not available() or not hint:
        return None
    from PIL import Image

    length = len(hint)
    board_w = length * BLOCK_SIZE[0] + (length - 1) * BLOCK_PADDING[0] + 2 * PADDING[0]
    board_h = BLOCK_SIZE[1] + 2 * PADDING[1]
    board = Image.new("RGB", (board_w, board_h), BG_COLOR)
    for index, letter in enumerate(hint):
        glyph = "" if letter == "*" else letter
        color = CORRECT_COLOR if glyph else BG_COLOR
        x = PADDING[0] + (BLOCK_SIZE[0] + BLOCK_PADDING[0]) * index
        board.paste(_block(glyph, color), (x, PADDING[1]))
    return _encode(board)
