from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from jianer import segments as Segments
from jianer.adapters import (
    Capability,
    ConversationKey,
    ConversationKind,
)
from PIL import Image

from plugins.JianerAI.observability import sanitize_log_data
from plugins.JianerAI.tools import (
    ToolCall,
    ToolContext,
    ToolRegistry,
    ToolRisk,
)
from plugins.JianerAI.tools.html_card import RenderedCard, html_card_tool


class FakeRenderer:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.fragments: list[tuple[str, int]] = []
        self.paths: list[Path] = []
        self.closed = False

    async def render(self, fragment: str, *, width: int) -> RenderedCard:
        self.fragments.append((fragment, width))
        path = self.root / f"card-{len(self.fragments)}.png"
        Image.new("RGB", (width, 600), "white").save(path)
        self.paths.append(path)
        return RenderedCard(path=path, width=width, height=600, size=path.stat().st_size)

    async def shutdown(self) -> None:
        self.closed = True


class FakeActions:
    protocol = "milky"
    capabilities = frozenset({Capability.SEND_IMAGE})

    def __init__(self) -> None:
        self.sent = []

    async def send(self, message, **target):
        self.sent.append((target, message))
        return SimpleNamespace(data=SimpleNamespace(message_id="1"))


def _context(*, private: bool = False) -> tuple[ToolContext, FakeActions]:
    actions = FakeActions()
    event = SimpleNamespace(
        protocol="milky",
        self_id="bot",
        user_id="user-1",
        group_id=None if private else "group-1",
    )
    conversation = ConversationKey(
        protocol="milky",
        self_id="bot",
        kind=ConversationKind.PRIVATE if private else ConversationKind.GROUP,
        conversation_id="user-1" if private else "group-1",
        preset="Normal",
    )
    return (
        ToolContext(
            event=event,
            actions=actions,
            conversation=conversation,
            canonical_user_id="qq:user-1",
            runtime={},
            memory=None,
        ),
        actions,
    )


def _registry(renderer: FakeRenderer) -> ToolRegistry:
    registry = ToolRegistry(
        allowed_risks=frozenset({ToolRisk.PRESENTATION})
    )
    registry.register(html_card_tool(renderer))
    return registry


def test_weather_template_sends_image_with_fixed_attribution_and_sources(
    tmp_path: Path,
):
    async def scenario(private: bool):
        renderer = FakeRenderer(tmp_path)
        registry = _registry(renderer)
        context, actions = _context(private=private)
        result = await registry.execute(
            ToolCall(
                "card-1",
                "render_information_card",
                {
                    "operation": "render_weather",
                    "title": "杭州天气",
                    "subtitle": "2026-08-03 18:00 更新",
                    "highlights": [
                        {"label": "气温", "value": "31°C"},
                        {"label": "天气", "value": "多云"},
                    ],
                    "sections": [
                        {
                            "heading": "未来三小时",
                            "items": ["19:00｜30°C｜多云", "20:00｜29°C｜阵雨"],
                        }
                    ],
                    "sources": [
                        '{"name":"杭州市气象台",'
                        '"url":"https://example.test/source"}'
                    ],
                    "theme": "storm",
                    "alt_text": "杭州天气卡片",
                },
            ),
            context,
        )
        assert result.ok is True
        assert json.loads(result.content)["data"]["sent"] is True
        fragment, width = renderer.fragments[0]
        assert width == 1000
        assert "天气服务由和风天气驱动 · www.qweather.com" in fragment
        assert (
            '{&quot;name&quot;:&quot;杭州市气象台&quot;,'
            '&quot;url&quot;:&quot;https://example.test/source&quot;}'
            in fragment
        )
        assert "未来三小时" in fragment
        assert len(actions.sent) == 1
        target, message = actions.sent[0]
        assert target == ({"user_id": "user-1"} if private else {"group_id": "group-1"})
        assert isinstance(message[0], Segments.Image)
        assert message[0].summary == "杭州天气卡片"
        assert not renderer.paths[0].exists()
        await registry.shutdown()
        assert renderer.closed is True

    asyncio.run(scenario(False))
    asyncio.run(scenario(True))


def test_generic_html_mode_accepts_inline_css_and_svg_but_blocks_active_content(
    tmp_path: Path,
):
    async def scenario():
        renderer = FakeRenderer(tmp_path)
        registry = _registry(renderer)
        context, actions = _context()
        safe = await registry.execute(
            ToolCall(
                "safe",
                "render_information_card",
                {
                    "operation": "render_html",
                    "html": (
                        "<style>.bar{height:24px;background:#48f}</style>"
                        "<h1>项目进度</h1><svg viewBox='0 0 100 20'>"
                        "<rect width='75' height='20' fill='#48f'/></svg>"
                    ),
                    "width": 800,
                },
            ),
            context,
        )
        assert safe.ok is True
        assert renderer.fragments[0][1] == 800
        assert len(actions.sent) == 1

        for index, unsafe_html in enumerate(
            (
                "<script>alert(1)</script>",
                "<img src='https://example.test/a.png'>",
                "<div onclick='alert(1)'>x</div>",
                "<style>.x{background:url(file:///secret)}</style>",
            ),
            start=1,
        ):
            result = await registry.execute(
                ToolCall(
                    f"unsafe-{index}",
                    "render_information_card",
                    {"operation": "render_html", "html": unsafe_html},
                ),
                context,
            )
            assert result.ok is False
            assert result.error_code == "unsafe_card_html"
        assert len(actions.sent) == 1
        await registry.shutdown()

    asyncio.run(scenario())


def test_card_tool_is_presentation_scoped_and_requires_image_capability(tmp_path: Path):
    renderer = FakeRenderer(tmp_path)
    spec = html_card_tool(renderer)
    assert spec.risk is ToolRisk.PRESENTATION
    assert spec.required_capabilities == frozenset({Capability.SEND_IMAGE})

    registry = _registry(renderer)
    context, _ = _context()
    assert [item.name for item in registry.available(context)] == [
        "render_information_card"
    ]
    context.actions.capabilities = frozenset()
    assert registry.available(context) == ()


def test_html_card_logs_store_only_length_and_digest():
    raw = "<h1>private card content</h1>"
    sanitized = sanitize_log_data(
        {"operation": "render_html", "html": raw},
        tool_name="render_information_card",
    )
    assert raw not in str(sanitized)
    assert sanitized["html"].startswith("<html:29 chars sha256:")
