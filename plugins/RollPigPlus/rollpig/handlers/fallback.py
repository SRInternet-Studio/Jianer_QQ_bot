"""黏连写法的补充入口。

Alconna 只按「命令 + 分隔符 + 参数」匹配，``随机小猪3`` / ``找猪玩偶`` /
``小猪图鉴2`` 这类上游允许的黏连写法不会命中命令，因此在 ``on_message``
钩子里补一层：标准写法仍由 Alconna 处理，只有黏连写法才会走到这里。
"""

from __future__ import annotations

from bot import plugin_state

from ..helpers import event_message

from . import collection, roll

_GLUED_COMMANDS = (
    ("随机小猪", roll.cmd_roll, "count"),
    ("找猪", roll.cmd_find, "keyword"),
    ("搜猪", roll.cmd_find, "keyword"),
    ("小猪图鉴", collection.cmd_catalog, "page"),
    ("猪猪图鉴", collection.cmd_catalog, "page"),
    ("完整图鉴", collection.cmd_catalog, "page"),
)


def _plain_text(event) -> str:
    from jianer import segments

    text = "".join(
        str(content.text)
        for content in event_message(event).contents
        if isinstance(content, segments.Text)
    ).strip()
    reminder = str(plugin_state.get_runtime().get("reminder") or "")
    if reminder and text.startswith(reminder):
        text = text[len(reminder) :].lstrip()
    return text


async def handle_glued_command(event, actions) -> bool:
    """处理黏连写法的命令；不是黏连写法时返回 False。"""

    text = _plain_text(event)
    if not text:
        return False
    for name, handler, parameter in _GLUED_COMMANDS:
        if not text.startswith(name):
            continue
        tail = text[len(name) :].strip()
        if not tail:
            continue
        if parameter in {"count", "page"} and not tail.split()[0].isdigit():
            continue
        return bool(
            await handler(event, actions, user_message=None, **{parameter: tail})
        )
    return False


__all__ = ["handle_glued_command"]
