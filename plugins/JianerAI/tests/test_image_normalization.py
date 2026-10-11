"""OpenAI image attachments must reach the endpoint as static bitmaps.

Animated GIF and multi-megabyte base64 bodies make OpenAI-compatible gateways
fail the whole request with an upstream 400 or a 504 timeout. Those failures
surface as "暂时无法连接AI服务", so the image path is normalized before use.
"""

from __future__ import annotations

import asyncio
import base64
import json
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from plugins.JianerAI import providers
from plugins.JianerAI.media_input import (
    MAX_IMAGE_BYTES,
    MAX_IMAGE_EDGE,
    MediaProcessingError,
    normalize_image,
)


def _png_bytes(size=(64, 48)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, (20, 90, 200)).save(buffer, format="PNG")
    return buffer.getvalue()


def _animated_gif_bytes(size=(600, 400)) -> bytes:
    frames = [
        Image.new("RGB", size, (10, 20, 200)),
        Image.new("RGB", size, (200, 30, 10)),
    ]
    buffer = BytesIO()
    frames[0].save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=200,
        loop=0,
    )
    return buffer.getvalue()


def _oversized_png_bytes(size=(2600, 1400)) -> bytes:
    image = Image.effect_noise(size, 100).convert("RGB")
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _write_config(config_dir: Path, key: str, response_type: str) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / f"{key}.ai.json").write_text(
        json.dumps(
            {
                "FriendlyName": key,
                "Model": "vision-model",
                "ResponseType": response_type,
                "ApiKey": "test-key",
            }
        ),
        encoding="utf-8",
    )


def test_normalize_image_turns_animated_gif_into_one_static_frame() -> None:
    result = asyncio.run(normalize_image(_animated_gif_bytes(), "image/gif"))

    assert result.mime in {"image/png", "image/jpeg"}
    assert not result.data.startswith(b"GIF")
    with Image.open(BytesIO(result.data)) as image:
        assert not getattr(image, "is_animated", False)


def test_normalize_image_keeps_small_static_images_untouched() -> None:
    payload = _png_bytes()

    result = asyncio.run(normalize_image(payload, "image/png"))

    assert result.data == payload
    assert result.mime == "image/png"


def test_normalize_image_downscales_oversized_photos() -> None:
    payload = _oversized_png_bytes()
    assert max(Image.open(BytesIO(payload)).size) > MAX_IMAGE_EDGE

    result = asyncio.run(normalize_image(payload, "image/png"))

    assert result.mime in {"image/png", "image/jpeg"}
    assert len(result.data) <= MAX_IMAGE_BYTES
    with Image.open(BytesIO(result.data)) as image:
        assert max(image.size) <= MAX_IMAGE_EDGE


def test_normalize_image_reports_undecodable_bytes() -> None:
    with pytest.raises(MediaProcessingError):
        asyncio.run(normalize_image(b"\x89PNG\r\n\x1a\nnot-an-image", "image/png"))


def test_openai_chat_sends_gif_as_static_image(tmp_path: Path) -> None:
    payloads: list[dict] = []

    async def transport(provider, config, payload):
        payloads.append(dict(payload))
        return {"choices": [{"message": {"content": "看到了"}}]}

    config_dir = tmp_path / "chat"
    _write_config(config_dir, "chat", "OpenAI Chat Completions")
    registry = providers.ProviderRegistry(config_dir, transport=transport)

    async def scenario() -> str:
        return await registry.chat(
            "chat",
            "看看这个",
            attachments=(
                providers.MediaAttachment(
                    data=_animated_gif_bytes(),
                    mime="image/gif",
                ),
            ),
        )

    assert asyncio.run(scenario()) == "看到了"
    image_part = payloads[0]["messages"][-1]["content"][1]
    assert image_part["type"] == "image_url"
    url = image_part["image_url"]["url"]
    assert url.startswith(("data:image/png;base64,", "data:image/jpeg;base64,"))


def test_openai_chat_downscales_oversized_image_before_sending(
    tmp_path: Path,
) -> None:
    payloads: list[dict] = []

    async def transport(provider, config, payload):
        payloads.append(dict(payload))
        return {"choices": [{"message": {"content": "ok"}}]}

    config_dir = tmp_path / "chat"
    _write_config(config_dir, "chat", "OpenAI Chat Completions")
    registry = providers.ProviderRegistry(config_dir, transport=transport)
    oversized = _oversized_png_bytes()

    async def scenario() -> None:
        await registry.chat(
            "chat",
            "看看这个",
            attachments=(
                providers.MediaAttachment(data=oversized, mime="image/png"),
            ),
        )

    asyncio.run(scenario())
    url = payloads[0]["messages"][-1]["content"][1]["image_url"]["url"]
    payload = url.split(",", 1)[1]
    encoded = base64.b64decode(payload)
    assert len(encoded) <= MAX_IMAGE_BYTES
    with Image.open(BytesIO(encoded)) as image:
        assert max(image.size) <= MAX_IMAGE_EDGE


def test_openai_chat_reports_undecodable_image_as_capability_error(
    tmp_path: Path,
) -> None:
    async def transport(provider, config, payload):
        raise AssertionError("the request must not reach the endpoint")

    config_dir = tmp_path / "chat"
    _write_config(config_dir, "chat", "OpenAI Chat Completions")
    registry = providers.ProviderRegistry(config_dir, transport=transport)

    async def scenario() -> None:
        await registry.chat(
            "chat",
            "看看这个",
            attachments=(
                providers.MediaAttachment(
                    data=b"\x89PNG\r\n\x1a\nnot-an-image",
                    mime="image/png",
                ),
            ),
        )

    with pytest.raises(providers.MediaCapabilityError):
        asyncio.run(scenario())


def test_openai_responses_sends_gif_as_static_image(tmp_path: Path) -> None:
    payloads: list[dict] = []

    async def transport(provider, config, payload):
        payloads.append(dict(payload))
        return {"output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}]}

    config_dir = tmp_path / "responses"
    _write_config(config_dir, "responses", "OpenAI Responses")
    registry = providers.ProviderRegistry(config_dir, transport=transport)

    async def scenario() -> str:
        return await registry.chat(
            "responses",
            "看看这个",
            attachments=(
                providers.MediaAttachment(
                    data=_animated_gif_bytes(),
                    mime="image/gif",
                ),
            ),
        )

    assert asyncio.run(scenario()) == "ok"
    image_part = payloads[0]["input"][-1]["content"][1]
    assert image_part["type"] == "input_image"
    assert image_part["image_url"].startswith(
        ("data:image/png;base64,", "data:image/jpeg;base64,")
    )
