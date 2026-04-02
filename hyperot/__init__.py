from . import configurator
from .utils import screens

from typing import Union
import asyncio
import sys
import os

HYPER_BOT_VERSION = "0.81.2"

# listener = None

screens.play_startup()
screens.play_info(HYPER_BOT_VERSION)


class Client:
    def __init__(self):
        self.records = {}
        self.lis = None

    def subscribe(
            self,
            func: callable,
            event: Union[
                "events.GroupMessageEvent",
                "events.PrivateMessageEvent",
                "events.GroupFileUploadEvent",
                "events.GroupAdminEvent",
                "events.GroupMemberDecreaseEvent",
                "events.GroupMemberIncreaseEvent",
                "events.GroupMuteEvent",
                "events.FriendAddEvent",
                "events.GroupRecallEvent",
                "events.FriendRecallEvent",
                "events.NotifyEvent",
                "events.GroupEssenceEvent",
                "events.MessageReactionEvent",
                "events.GroupAddInviteEvent",
                "events.HyperListenerStartNotify",
                "events.HyperListenerStopNotify"
            ]
    ) -> None:
        if not self.records.get(event):
            self.records[event] = [func]
        else:
            self.records[event].append(func)

    async def distributor(
            self, message_data: Union["events.Event", "events.HyperNotify"], actions: "Listener.Actions"
    ) -> None:
        if type(message_data) in list(self.records.keys()):
            tasks = []
            for i in self.records[type(message_data)]:
                tasks.append(asyncio.create_task(i(message_data, actions)))
            await asyncio.gather(*tasks)
        else:
            return

    def run(self):
        from . import listener
        self.lis = listener
        self.lis.reg(self.distributor)
        if self.records:
            self.lis.run()

    def restart(self) -> None:
        self.lis.stop()
        os.execv(sys.executable, [sys.executable] + sys.argv)
        # os._exit(1)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass
