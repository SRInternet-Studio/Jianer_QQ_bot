import asyncio
import html
import os
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, unquote, urlparse

from jianer import common, segments as Segments
from jianer.events import gen_message
from jianer.LecAdapters.FeishuLib.translator import feishu_message_to_segments

from Tools import tools as t
from Tools.site_catch import Catcher


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE_PATH = _PROJECT_ROOT / "assets" / "quote.html"
_TEMP_DIR = _PROJECT_ROOT / "temps"


def _field(value, name, default=None):
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def response_message(content) -> common.Message:
    data = getattr(content, "data", None)
    raw_message = _field(data, "message")
    if isinstance(raw_message, common.Message):
        return raw_message
    if isinstance(raw_message, list):
        return gen_message({"message": raw_message})
    return common.Message()


def response_sender(content):
    return _field(getattr(content, "data", None), "sender")


async def get_message_response(actions, message_id):
    if str(getattr(actions, "protocol", "")).casefold() == "feishu":
        client = getattr(actions, "client", None)
        getter = getattr(client, "get_message", None)
        if callable(getter):
            raw = await asyncio.to_thread(getter, str(message_id))
            if isinstance(raw, dict) and raw:
                body = raw.get("body") if isinstance(raw.get("body"), dict) else {}
                message_type = str(
                    raw.get("message_type") or raw.get("msg_type") or "text"
                )
                content = raw.get("content") or body.get("content") or ""
                translated = feishu_message_to_segments(
                    {
                        "message_type": message_type,
                        "content": content,
                    }
                )
                sender = raw.get("sender") if isinstance(raw.get("sender"), dict) else {}
                sender_identity = (
                    sender.get("sender_id")
                    if isinstance(sender.get("sender_id"), dict)
                    else {}
                )
                sender_id = str(
                    sender.get("id")
                    or sender_identity.get("open_id")
                    or sender_identity.get("user_id")
                    or sender_identity.get("union_id")
                    or ""
                )
                return SimpleNamespace(
                    data=SimpleNamespace(
                        message=gen_message({"message": translated}),
                        sender=SimpleNamespace(
                            user_id=sender_id,
                            nickname=sender_id or "未知用户",
                            card="",
                        ),
                    )
                )
    return await actions.get_msg(message_id)


def image_source(segment) -> str | None:
    source = str(getattr(segment, "file", "") or getattr(segment, "url", ""))
    if not source:
        return None
    if source.startswith(("http://", "https://", "data:", "file:")):
        return source
    path = Path(source)
    if path.is_file():
        return path.resolve().as_uri()
    return str(getattr(segment, "url", "") or "") or None


def _default_avatar(name: str) -> str:
    initial = (str(name or "?").strip() or "?")[0]
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="640">'
        '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
        '<stop stop-color="#24304a"/><stop offset="1" stop-color="#111827"/>'
        '</linearGradient></defs><rect width="640" height="640" fill="url(#g)"/>'
        '<text x="320" y="370" text-anchor="middle" font-size="260" '
        'font-family="Segoe UI, Microsoft YaHei, sans-serif" fill="#f8fafc">'
        f"{html.escape(initial)}</text></svg>"
    )
    return "data:image/svg+xml;charset=utf-8," + quote(svg)


async def _sender_display_name(actions, sender) -> tuple[str, str]:
    user_id = str(_field(sender, "user_id", "") or "")
    name = str(
        _field(sender, "card", "")
        or _field(sender, "nickname", "")
        or user_id
        or "未知用户"
    )
    if user_id and name in {user_id, "未知用户"}:
        try:
            profile = await actions.get_stranger_info(user_id)
            profile_data = getattr(profile, "data", None)
            name = str(
                _field(profile_data, "card", "")
                or _field(profile_data, "nickname", "")
                or name
            )
        except Exception:
            pass
    return name, user_id


async def get_image(quote_text: str, avatar: str, name: str) -> str:
    _TEMP_DIR.mkdir(parents=True, exist_ok=True)
    render_id = uuid.uuid4().hex
    catcher = None
    html_path = None
    try:
        template = _TEMPLATE_PATH.read_text(encoding="utf-8")
        rendered = (
            template.replace("{render_id}", render_id)
            .replace("{ava_url}", html.escape(avatar, quote=True))
            .replace("{quote}", html.escape(quote_text))
            .replace("{name}", html.escape(name))
        )
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            suffix=".html",
            prefix="advanced-quote-",
            dir=_TEMP_DIR,
            delete=False,
        ) as output:
            output.write(rendered)
            html_path = Path(output.name)
        catcher = await Catcher.init()
        screenshot = await catcher.catch(html_path.as_uri(), (1280, 640))
        return str(Path(screenshot).resolve())
    finally:
        if html_path is not None:
            html_path.unlink(missing_ok=True)
        if catcher is not None:
            await catcher.quit()


async def handle(
    message,
    actions,
    images=None,
    Manager=None,
    Segments=Segments,
    *,
    content=None,
) -> Segments.Image:
    if not message or not isinstance(message[0], Segments.Reply):
        raise ValueError("AdvancedQuote requires a reply segment")
    if content is None:
        content = await get_message_response(actions, message[0].id)

    quoted_message = response_message(content)
    sender = response_sender(content)
    name, user_id = await _sender_display_name(actions, sender)
    text = str(quoted_message)
    if hasattr(t, "replace_at_with_nickname"):
        text = await t.replace_at_with_nickname(
            quoted_message,
            Manager,
            Segments,
            actions,
        )
    text = str(text).replace("[图片]", "").strip()
    if not text:
        text = "（图片）"

    avatar = images
    if not avatar and user_id.isdigit():
        avatar = f"https://q2.qlogo.cn/headimg_dl?dst_uin={user_id}&spec=640"
    if not avatar:
        avatar = _default_avatar(name)

    screenshot = await get_image(text, avatar, name)
    return Segments.Image(screenshot)


def remove_rendered_image(image) -> None:
    source = str(getattr(image, "file", "") or "")
    if not source:
        return
    if source.startswith("file:"):
        parsed = urlparse(source)
        source = unquote(parsed.path)
        if os.name == "nt" and source.startswith("/"):
            source = source[1:]
    path = Path(source)
    try:
        resolved = path.resolve()
        resolved.relative_to(_TEMP_DIR.resolve())
    except (OSError, ValueError):
        return
    try:
        resolved.unlink(missing_ok=True)
    except OSError:
        pass
