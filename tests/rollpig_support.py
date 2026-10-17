"""Shared helpers for the RollPigPlus plugin tests.

RollPigPlus is a native JianerCore directory plugin: Alconna commands are
registered by ``plugins/RollPigPlus/rollpig/handlers/`` and dispatched by the
host ``PluginManager``.  These helpers load the plugin against a temporary data
directory and provide a fake OneBot ``actions`` object.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# One data directory per test session: the plugin resolves its data file paths
# at import time, so every rollpig test module must share them.
TEST_DATA_DIR = Path(
    os.environ.get("ROLLPIG_TEST_DATA_DIR") or tempfile.mkdtemp(prefix="rollpig-tests-")
)


def bootstrap() -> None:
    """Point the plugin at the test data directory and configure the host."""

    os.environ["ROLLPIG_PATH"] = str(TEST_DATA_DIR)

    import jianer.listener  # noqa: F401  (initialises jianer.events globals)

    from bot import plugin_state

    plugin_state.configure(
        config=None,
        logger=logging.getLogger("rollpig_test"),
        reminder="~",
        bot_name="简儿",
        bot_name_en="Jianer",
        one_slogan="测试",
        confused_word="?",
        root_users=["22222"],
        cooldowns={},
        cooldowns1={},
    )
    plugin_state.set_auth_snapshot(
        admins=["22222"],
        supers=["22222"],
        root_users=["22222"],
        super_users=["22222"],
        manage_users=[],
    )


def install_plugin_folder(tmp_path, monkeypatch) -> None:
    """Expose only RollPigPlus so unrelated plugins cannot fail the reload."""

    plugins_dir = Path(tmp_path) / "plugins"
    plugins_dir.mkdir()
    os.symlink(ROOT / "plugins" / "RollPigPlus", plugins_dir / "RollPigPlus")
    monkeypatch.chdir(tmp_path)
    bootstrap()


class Actions:
    """Fake JianerCore adapter actions recording everything the plugin sends."""

    protocol = "onebot"

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.custom = _Custom(self)
        self._next_id = 1000

    async def send(self, message=None, group_id=None, user_id=None, **kwargs):
        self.sent.append({"group_id": group_id, "user_id": user_id, "message": message})
        self._next_id += 1
        return SimpleNamespace(data=SimpleNamespace(message_id=str(self._next_id)))

    async def del_message(self, message_id) -> None:
        self.calls.append(("del_message", {"message_id": message_id}))

    async def send_group_forward_msg(self, group_id, message):
        self.sent.append({"group_id": group_id, "user_id": None, "message": message})
        return SimpleNamespace(data=SimpleNamespace(message_id="999"))

    async def get_group_member_info(self, group_id, user_id):
        return SimpleNamespace(
            raw={
                "status": "ok",
                "retcode": 0,
                "data": {
                    "group_id": str(group_id),
                    "user_id": str(user_id),
                    "nickname": f"昵称{user_id}",
                    "card": f"名片{user_id}",
                    "role": "member",
                },
            }
        )

    async def get_msg(self, message_id):
        return SimpleNamespace(
            raw={
                "status": "ok",
                "retcode": 0,
                "data": {
                    "message_id": message_id,
                    "message": [{"type": "text", "data": {"text": "被引用的消息"}}],
                    "sender": {
                        "user_id": "33333",
                        "nickname": "被引用的人",
                        "card": "",
                    },
                },
            }
        )

    def texts(self) -> list[str]:
        return [str(item["message"]) for item in self.sent]


class _Custom:
    def __init__(self, actions: Actions) -> None:
        self._actions = actions

    def __getattr__(self, name: str):
        async def caller(**params: Any) -> str:
            self._actions.calls.append((name, params))
            return f"{name}_echo"

        return caller


def group_event(
    text: str,
    *,
    segments_payload: list[dict[str, Any]] | None = None,
    user_id: int | str = 22222,
    group_id: int | str | None = 33333,
    self_id: int | str = 10000,
    role: str = "owner",
):
    """Build a JianerCore group message event the way the adapter does."""

    from jianer import events

    message = segments_payload
    if message is None:
        message = [{"type": "text", "data": {"text": text}}]
    payload: dict[str, Any] = {
        "post_type": "message",
        "message_type": "group" if group_id is not None else "private",
        "sub_type": "normal",
        "time": 1_700_000_000,
        "self_id": self_id,
        "user_id": user_id,
        "message_id": 444,
        "message": message,
        "sender": {
            "user_id": user_id,
            "nickname": "tester",
            "card": "",
            "role": role,
        },
    }
    if group_id is not None:
        payload["group_id"] = group_id
        return events.GroupMessageEvent(payload)
    return events.PrivateMessageEvent(payload)


async def dispatch(text: str, actions: Actions | None = None, **event_kwargs):
    """Dispatch one message through the host plugin pipeline."""

    from bot import plugin_state

    actions = actions or Actions()
    event = group_event(text, **event_kwargs)
    handled = await plugin_state.dispatch_normal(
        event, actions, message_text=text
    )
    return handled, actions


def run(coro: Any) -> Any:
    return asyncio.run(coro)
