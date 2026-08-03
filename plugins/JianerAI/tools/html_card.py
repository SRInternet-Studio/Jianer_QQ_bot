from __future__ import annotations

import asyncio
import contextlib
import html
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from jianer import common as Manager, segments as Segments
from jianer.adapters import Capability
from PIL import Image

from plugins.JianerAI.tools.contracts import (
    ToolContext,
    ToolExecutionError,
    ToolRisk,
    ToolSpec,
)


_MIN_WIDTH = 480
_MAX_WIDTH = 1400
_MAX_HTML_CHARS = 50_000
_MAX_IMAGE_HEIGHT = 4096
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_QWEATHER_NAME = "天气服务由和风天气驱动"
_QWEATHER_URL = "www.qweather.com"
_FORBIDDEN_TAGS = frozenset(
    {
        "applet",
        "base",
        "embed",
        "frame",
        "frameset",
        "iframe",
        "input",
        "link",
        "meta",
        "object",
        "script",
    }
)
_RESOURCE_ATTRIBUTES = frozenset(
    {
        "action",
        "formaction",
        "href",
        "poster",
        "src",
        "srcdoc",
        "srcset",
        "xlink:href",
    }
)
_UNSAFE_CSS_PATTERN = re.compile(
    r"(?is)(?:@import\b|\burl\s*\(|\bexpression\s*\(|\bjavascript\s*:|"
    r"-moz-binding\b|\bbehavior\s*:|\bdata\s*:\s*text/html)"
)


@dataclass(frozen=True, slots=True)
class RenderedCard:
    path: Path
    width: int
    height: int
    size: int


class _SafeHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._style_depth = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        normalized_tag = str(tag).casefold()
        if normalized_tag in _FORBIDDEN_TAGS:
            raise ValueError(f"不允许使用 <{normalized_tag}> 标签。")
        if normalized_tag == "style":
            self._style_depth += 1
        for name, value in attrs:
            normalized_name = str(name).casefold()
            if normalized_name.startswith("on"):
                raise ValueError("不允许使用 HTML 事件处理属性。")
            if normalized_name in _RESOURCE_ATTRIBUTES:
                raise ValueError("不允许在制图 HTML 中引用外部或本地资源。")
            if normalized_name == "style":
                _validate_css(str(value or ""))

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.handle_starttag(tag, attrs)
        if str(tag).casefold() == "style":
            self._style_depth = max(0, self._style_depth - 1)

    def handle_endtag(self, tag: str) -> None:
        if str(tag).casefold() == "style":
            self._style_depth = max(0, self._style_depth - 1)

    def handle_data(self, data: str) -> None:
        if self._style_depth:
            _validate_css(data)


class HtmlCardRenderer:
    """Render isolated HTML fragments with one shared headless Chromium."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._playwright: Any = None
        self._browser: Any = None
        self._closed = False

    async def render(self, fragment: str, *, width: int) -> RenderedCard:
        async with self._lock:
            if self._closed:
                raise ToolExecutionError(
                    "card_renderer_closed", "制图工具正在关闭。"
                )
            await self._ensure_started()
            return await self._render_locked(fragment, width=width)

    async def shutdown(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            browser, self._browser = self._browser, None
            playwright, self._playwright = self._playwright, None
            if browser is not None:
                with contextlib.suppress(Exception):
                    await browser.close()
            if playwright is not None:
                with contextlib.suppress(Exception):
                    await playwright.stop()

    async def _ensure_started(self) -> None:
        if self._browser is not None:
            return
        try:
            from playwright.async_api import async_playwright

            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=("--disable-dev-shm-usage",),
            )
        except Exception as exc:
            playwright, self._playwright = self._playwright, None
            if playwright is not None:
                with contextlib.suppress(Exception):
                    await playwright.stop()
            raise ToolExecutionError(
                "card_renderer_unavailable",
                "制图所需的 Chromium 不可用，请安装 Playwright Chromium。",
            ) from exc

    async def _render_locked(self, fragment: str, *, width: int) -> RenderedCard:
        fd, raw_path = tempfile.mkstemp(prefix="jianer-card-", suffix=".png")
        os.close(fd)
        output_path = Path(raw_path)
        context = None
        try:
            context = await self._browser.new_context(
                accept_downloads=False,
                java_script_enabled=False,
                service_workers="block",
                viewport={"width": width, "height": 900},
            )

            async def block_request(route: Any) -> None:
                await route.abort("blockedbyclient")

            await context.route("**/*", block_request)
            page = await context.new_page()
            page.set_default_timeout(10_000)
            await page.set_content(
                _document(fragment),
                wait_until="domcontentloaded",
                timeout=10_000,
            )
            layout = await page.evaluate(
                """() => ({
                    width: Math.ceil(Math.max(
                        document.documentElement.scrollWidth,
                        document.body ? document.body.scrollWidth : 0
                    )),
                    height: Math.ceil(Math.max(
                        document.documentElement.scrollHeight,
                        document.body ? document.body.scrollHeight : 0
                    ))
                })"""
            )
            if (
                int(layout.get("width", 0)) > width
                or int(layout.get("height", 0)) > _MAX_IMAGE_HEIGHT
            ):
                raise ToolExecutionError(
                    "card_too_large",
                    "制图内容过多，请精简内容或拆成多张图片。",
                )
            await page.screenshot(
                path=str(output_path),
                type="png",
                full_page=True,
                animations="disabled",
            )
            with Image.open(output_path) as image:
                rendered_width, rendered_height = image.size
            size = output_path.stat().st_size
            if rendered_height > _MAX_IMAGE_HEIGHT or size > _MAX_IMAGE_BYTES:
                raise ToolExecutionError(
                    "card_too_large",
                    "制图内容过多，请精简内容或拆成多张图片。",
                )
            return RenderedCard(
                path=output_path,
                width=rendered_width,
                height=rendered_height,
                size=size,
            )
        except ToolExecutionError:
            with contextlib.suppress(OSError):
                output_path.unlink()
            raise
        except asyncio.CancelledError:
            with contextlib.suppress(OSError):
                output_path.unlink()
            raise
        except Exception as exc:
            with contextlib.suppress(OSError):
                output_path.unlink()
            raise ToolExecutionError(
                "card_render_failed", "HTML 卡片渲染失败。"
            ) from exc
        finally:
            if context is not None:
                with contextlib.suppress(Exception):
                    await context.close()


def html_card_tool(renderer: HtmlCardRenderer | None = None) -> ToolSpec:
    card_renderer = renderer or HtmlCardRenderer()

    async def execute(
        context: ToolContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        operation = str(arguments.get("operation") or "").strip().casefold()
        if operation == "render_html":
            _reject_unexpected(
                arguments,
                {"operation", "html", "width", "alt_text"},
            )
            fragment = _validated_html(arguments.get("html"))
        elif operation == "render_weather":
            _reject_unexpected(
                arguments,
                {
                    "operation",
                    "title",
                    "subtitle",
                    "highlights",
                    "sections",
                    "sources",
                    "theme",
                    "width",
                    "alt_text",
                },
            )
            fragment = _weather_fragment(arguments)
        else:
            raise ToolExecutionError(
                "invalid_card_operation", "不支持的制图操作。"
            )

        width = int(arguments.get("width", 1000))
        alt_text = str(arguments.get("alt_text") or "信息卡片").strip()
        card = await card_renderer.render(fragment, width=width)
        image_segment = Segments.Image(
            str(card.path.resolve()),
            summary=alt_text,
        )
        target = _target(context)
        try:
            await context.actions.send(
                message=Manager.Message(image_segment),
                **target,
            )
        except Exception as exc:
            raise ToolExecutionError(
                "card_send_failed", "图片已生成，但发送到当前会话失败。"
            ) from exc
        finally:
            with contextlib.suppress(OSError):
                card.path.unlink()
        return {
            "operation": operation,
            "sent": True,
            "width": card.width,
            "height": card.height,
            "size": card.size,
            "alt_text": alt_text,
            "instruction": "图片已发送；最终回答只需使用纯文本概括，不要重复图片内容。",
        }

    return ToolSpec(
        name="render_information_card",
        description=(
            "生成并发送信息卡片图片。天气数据使用 render_weather 固定模板；其他图表、"
            "表格、时间线、流程和信息卡使用 render_html，由你编写安全的 HTML/CSS/内联 SVG。"
            "HTML 不能包含脚本、事件、外部资源或本地资源。图片发送成功后，最终回答只用纯文本概括。"
        ),
        input_schema=_input_schema(),
        handler=execute,
        risk=ToolRisk.PRESENTATION,
        timeout_seconds=30.0,
        max_output_chars=2048,
        required_capabilities=frozenset({Capability.SEND_IMAGE}),
        shutdown=card_renderer.shutdown,
    )


def _input_schema() -> dict[str, Any]:
    text_item = {"type": "string", "minLength": 1, "maxLength": 240}
    return {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["render_html", "render_weather"],
                "description": "通用 HTML 制图或固定天气卡片。",
            },
            "html": {
                "type": "string",
                "minLength": 1,
                "maxLength": _MAX_HTML_CHARS,
                "description": (
                    "render_html 使用的 HTML 片段，可含 style 和内联 SVG；"
                    "不得含脚本、事件属性、链接或任何外部/本地资源。"
                ),
            },
            "title": {
                "type": "string",
                "minLength": 1,
                "maxLength": 100,
                "description": "render_weather 的地点或数据标题。",
            },
            "subtitle": {
                "type": "string",
                "maxLength": 180,
                "description": "时间范围、更新时间或简短副标题。",
            },
            "highlights": {
                "type": "array",
                "minItems": 1,
                "maxItems": 8,
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "minLength": 1, "maxLength": 40},
                        "value": {"type": "string", "minLength": 1, "maxLength": 80},
                    },
                    "required": ["label", "value"],
                    "additionalProperties": False,
                },
            },
            "sections": {
                "type": "array",
                "minItems": 1,
                "maxItems": 10,
                "items": {
                    "type": "object",
                    "properties": {
                        "heading": {"type": "string", "minLength": 1, "maxLength": 60},
                        "items": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 20,
                            "items": text_item,
                        },
                    },
                    "required": ["heading", "items"],
                    "additionalProperties": False,
                },
            },
            "sources": {
                "type": "array",
                "maxItems": 12,
                "items": {"type": "string", "minLength": 1, "maxLength": 1000},
                "description": (
                    "天气预警和空气质量返回的上游归因；对象必须完整序列化为 JSON 文本。"
                ),
            },
            "theme": {
                "type": "string",
                "enum": ["day", "night", "storm", "ocean", "air"],
                "default": "day",
            },
            "width": {
                "type": "integer",
                "minimum": _MIN_WIDTH,
                "maximum": _MAX_WIDTH,
                "default": 1000,
            },
            "alt_text": {
                "type": "string",
                "maxLength": 160,
                "default": "信息卡片",
            },
        },
        "required": ["operation"],
        "additionalProperties": False,
    }


def _validated_html(value: Any) -> str:
    fragment = str(value or "").strip()
    if not fragment:
        raise ToolExecutionError("invalid_card_html", "render_html 必须提供 HTML。")
    if len(fragment) > _MAX_HTML_CHARS:
        raise ToolExecutionError("invalid_card_html", "HTML 内容过长。")
    parser = _SafeHtmlParser()
    try:
        parser.feed(fragment)
        parser.close()
    except (ValueError, TypeError) as exc:
        raise ToolExecutionError("unsafe_card_html", str(exc)) from exc
    return fragment


def _validate_css(value: str) -> None:
    if _UNSAFE_CSS_PATTERN.search(str(value or "")):
        raise ValueError("CSS 不允许加载资源或执行表达式。")


def _weather_fragment(arguments: Mapping[str, Any]) -> str:
    title = str(arguments.get("title") or "").strip()
    if not title:
        raise ToolExecutionError(
            "invalid_weather_card", "render_weather 必须提供 title。"
        )
    highlights = _mapping_sequence(arguments.get("highlights"))
    sections = _mapping_sequence(arguments.get("sections"))
    if not highlights and not sections:
        raise ToolExecutionError(
            "invalid_weather_card", "天气卡片至少需要 highlights 或 sections。"
        )
    subtitle = str(arguments.get("subtitle") or "").strip()
    theme = str(arguments.get("theme") or "day").casefold()
    palette = {
        "day": ("#0c67d8", "#65c7f7", "#eff8ff"),
        "night": ("#191f48", "#4a55a2", "#f0f2ff"),
        "storm": ("#263238", "#59656b", "#eef2f3"),
        "ocean": ("#006064", "#2ebf91", "#ebfffb"),
        "air": ("#3f4c6b", "#7d8aa8", "#f2f5fb"),
    }.get(theme, ("#0c67d8", "#65c7f7", "#eff8ff"))
    primary, secondary, surface = palette
    highlight_html = "".join(
        (
            '<div class="metric"><div class="metric-label">'
            + html.escape(str(item.get("label") or ""))
            + '</div><div class="metric-value">'
            + html.escape(str(item.get("value") or ""))
            + "</div></div>"
        )
        for item in highlights
    )
    section_html = "".join(
        (
            '<section><h2>'
            + html.escape(str(item.get("heading") or ""))
            + "</h2><div class=\"rows\">"
            + "".join(
                '<div class="row">' + html.escape(str(line)) + "</div>"
                for line in _string_sequence(item.get("items"))
            )
            + "</div></section>"
        )
        for item in sections
    )
    sources = _string_sequence(arguments.get("sources"))
    source_html = ""
    if sources:
        source_html = (
            '<div class="sources"><strong>上游来源</strong>'
            + "".join("<div>" + html.escape(item) + "</div>" for item in sources)
            + "</div>"
        )
    return f"""
<style>
  .weather-card {{
    box-sizing: border-box; width: 100%; padding: 64px;
    color: #172033; background: linear-gradient(145deg, {surface}, #ffffff 64%);
    border: 1px solid rgba(255,255,255,.75); border-radius: 36px;
    box-shadow: 0 28px 70px rgba(20,40,80,.18); overflow: hidden;
  }}
  .weather-card::before {{
    content: ""; display: block; width: 120px; height: 8px; border-radius: 99px;
    background: linear-gradient(90deg, {primary}, {secondary}); margin-bottom: 34px;
  }}
  h1 {{ margin: 0; font-size: 52px; line-height: 1.2; letter-spacing: -.5px; }}
  .subtitle {{ margin-top: 12px; color: #5d6a80; font-size: 24px; line-height: 1.55; }}
  .metrics {{ display: grid; grid-template-columns: repeat(4, minmax(0,1fr)); gap: 16px; margin-top: 38px; }}
  .metric {{ padding: 24px 22px; border-radius: 22px; background: rgba(255,255,255,.82); border: 1px solid rgba(40,80,140,.10); }}
  .metric-label {{ color: #637088; font-size: 19px; margin-bottom: 8px; }}
  .metric-value {{ color: {primary}; font-weight: 760; font-size: 30px; line-height: 1.25; overflow-wrap: anywhere; }}
  section {{ margin-top: 28px; padding: 30px 32px; border-radius: 24px; background: rgba(255,255,255,.78); border: 1px solid rgba(40,80,140,.10); }}
  h2 {{ margin: 0 0 16px; font-size: 28px; color: #25314a; }}
  .rows {{ display: grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap: 12px 28px; }}
  .row {{ padding: 11px 0; font-size: 22px; line-height: 1.5; color: #3e4c65; border-bottom: 1px solid rgba(40,80,140,.09); white-space: pre-wrap; overflow-wrap: anywhere; }}
  .sources {{ margin-top: 30px; padding-top: 22px; border-top: 1px solid rgba(40,80,140,.14); color: #6b7588; font-size: 17px; line-height: 1.6; overflow-wrap: anywhere; }}
  footer {{ margin-top: 28px; color: #56647a; font-size: 18px; line-height: 1.5; text-align: right; }}
  @media (max-width: 720px) {{ .metrics, .rows {{ grid-template-columns: 1fr 1fr; }} .weather-card {{ padding: 42px; }} }}
</style>
<article class="weather-card">
  <header><h1>{html.escape(title)}</h1>{'<div class="subtitle">' + html.escape(subtitle) + '</div>' if subtitle else ''}</header>
  {'<div class="metrics">' + highlight_html + '</div>' if highlight_html else ''}
  {section_html}
  {source_html}
  <footer>{_QWEATHER_NAME} · {_QWEATHER_URL}</footer>
</article>
"""


def _document(fragment: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src 'none'; connect-src 'none'; media-src 'none'; object-src 'none'; frame-src 'none'; base-uri 'none'; form-action 'none'">
  <style>
    * {{ box-sizing: border-box; }}
    html, body {{ margin: 0; padding: 0; background: transparent; overflow-x: hidden; }}
    body {{ padding: 34px; font-family: "Microsoft YaHei", "Noto Sans CJK SC", "PingFang SC", "Segoe UI", sans-serif; text-rendering: optimizeLegibility; }}
    .jianer-card-root {{ width: 100%; }}
  </style>
</head>
<body><main class="jianer-card-root">{fragment}</main></body>
</html>"""


def _mapping_sequence(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _string_sequence(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [str(item) for item in value]


def _reject_unexpected(arguments: Mapping[str, Any], allowed: set[str]) -> None:
    unexpected = set(arguments) - allowed
    if unexpected:
        raise ToolExecutionError(
            "invalid_card_arguments",
            "当前制图操作不接受这些参数：" + ", ".join(sorted(unexpected)),
        )


def _target(context: ToolContext) -> dict[str, str]:
    group_id = getattr(context.event, "group_id", None)
    if group_id is not None:
        return {"group_id": str(group_id)}
    user_id = str(getattr(context.event, "user_id", "") or "")
    if not user_id:
        raise ToolExecutionError("card_target_missing", "无法确定图片发送目标。")
    return {"user_id": user_id}
