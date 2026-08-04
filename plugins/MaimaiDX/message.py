"""Small compatibility surface used by the source-faithful rendering core."""

import base64
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image
from jianer import common, segments


def _image_source(value: Any) -> str:
    if isinstance(value, Path):
        return value.resolve().as_uri()
    if isinstance(value, BytesIO):
        value.seek(0)
        return "base64://" + base64.b64encode(value.read()).decode("ascii")
    if isinstance(value, (bytes, bytearray)):
        return "base64://" + base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, Image.Image):
        output = BytesIO()
        value.save(output, format="PNG")
        return "base64://" + base64.b64encode(output.getvalue()).decode("ascii")
    source = str(value)
    if source.startswith(("http://", "https://", "file://", "base64://")):
        return source
    path = Path(source)
    if path.exists():
        return path.resolve().as_uri()
    return source


class MessageSegment:
    """Return JianerCore Message chains with a NoneBot-like constructor API."""

    @staticmethod
    def text(text: Any) -> common.Message:
        return common.Message(segments.Text(str(text)))

    @staticmethod
    def image(file: Any) -> common.Message:
        return common.Message(segments.Image(_image_source(file)))

    @staticmethod
    def at(user_id: int | str) -> common.Message:
        return common.Message(segments.At(str(user_id)))
