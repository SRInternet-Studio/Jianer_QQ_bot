import asyncio
from typing import Any, Self
import sys
import threading
import time

from .. import common
from ..LecAdapters.OneBot import Actions as OneBotActions
from ..LecAdapters.KritorLib.Res import event_queue, to_protos, message_ids
from ..events import Event, HyperNotify, HyperListenerStartNotify, em
from ..service import FuncCall, IServiceStartUp, IServiceBase
from ..network import KritorConnection
from ..utils import errors
from ..utils.apiresponse import *
from .. import configurator, hyperogger

from ..LecAdapters.KritorLib.protos.common import Contact, Scene
from ..LecAdapters.KritorLib.protos.core import GetVersionRequest, CoreServiceStub
from ..LecAdapters.KritorLib.protos.message import (
    MessageServiceStub,
    SendMessageRequest,
    RecallMessageRequest,
    GetMessageRequest
)

config = configurator.BotConfig.get("hyper-bot")
logger = hyperogger.Logger()
logger.set_level(config.log_level)


def mid_res(msg_id: str) -> tuple[str, int, int]:
    ret = ["", 0, 0]
    ret[0] = "group" if msg_id.startswith("g") else "private"
    msg_id = msg_id.replace("g", "").replace("p", "")
    ret[1] = int(msg_id[:20])
    ret[2] = int(msg_id[20:])
    return ret[0], ret[1], ret[2]


class Actions(OneBotActions):
    def __init__(self, cnt: KritorConnection):
        self.connection = cnt

    async def send(
            self, message: common.Message, group_id: int = None, user_id: int = None
    ) -> common.Ret[MsgSendRsp]:
        msg = to_protos(message.get_sync())
        if group_id is not None:
            contact = Contact(
                scene=Scene.GROUP,
                peer=str(group_id),
            )
        else:
            contact = Contact(
                scene=Scene.FRIEND,
                peer=str(user_id),
            )
        req = SendMessageRequest(
            contact=contact,
            elements=msg,
            retry_count=3
        )
        res = await MessageServiceStub(self.connection.channel).send_message(req)
        message_ids[res.message_id] = len(message_ids)
        return common.Ret(
            {"data": {"message_id": message_ids[res.message_id]}},
            MsgSendRsp
        )

    async def get_version_info(self) -> common.Ret[GetVerInfoRsp]:
        res = await CoreServiceStub(self.connection.channel).get_version(GetVersionRequest())
        return common.Ret(
            {"data": {"app_name": res.app_name, "app_version": res.version, "protocol_version": None}},
            GetVerInfoRsp
        )

    async def get_msg(self, msg_id: int) -> common.Ret[GetMsgRsp]:
        try:
            mid = list(filter(lambda x: message_ids[x] == msg_id, message_ids))[1]
        except IndexError:
            mid = list(filter(lambda x: message_ids[x] == msg_id, message_ids))[0]
        if str(mid).startswith("g"):  # GROUP
            contact = Contact(
                scene=Scene.GROUP,
                peer=str(mid_res(mid)[1]),
            )
            res = await MessageServiceStub(self.connection.channel).get_message(GetMessageRequest(contact, mid))
            print(res)
            return common.Ret(
                {
                    "time": res.message.time,
                    "message_type": "group",
                    "message_id": msg_id,
                    "real_id": 0,
                    "sender": {
                        "user_id": res.message.group.uin,
                        "nickname": res.message.group.nick,
                        "sex": "unknown",
                        "age": 0,
                        "card": res.message.group.nick,
                        "area": "unknown",
                        "level": "0",
                        "role": "unknown",
                        "title": None
                    }
                },
                GetMsgRsp
            )
        else:
            contact = Contact(
                scene=Scene.FRIEND,
                peer=str(mid_res(mid)[1]),
            )
            res = await MessageServiceStub(self.connection.channel).get_message(GetMessageRequest(contact, mid))
            return common.Ret(
                {
                    "time": res.message.time,
                    "message_type": "private",
                    "message_id": msg_id,
                    "real_id": 0,
                    "sender": {
                        "user_id": res.message.private.uin,
                        "nickname": res.message.private.nick,
                        "sex": "unknown",
                        "age": 0
                    }
                },
                GetMsgRsp
            )


async def tester(message_data: Union[Event, HyperNotify], actions: Actions) -> None:
    ...


def _handler(data: Union[dict, HyperNotify], actions: Actions) -> None:
    if isinstance(data, dict):
        if data.get("post_type") == "meta_event" or data.get("user_id") == data.get("self_id"):
            pass
        else:
            asyncio.run(handler(em.new(data), actions))
    else:
        asyncio.run(handler(data, actions))


handler: callable = tester


def reg(func: callable):
    global handler
    handler = func


connection: KritorConnection
listener_ran = False


class KritorEventGettingService(IServiceBase):
    actions: Actions

    def handler(self, func: FuncCall) -> Any:
        pass

    def set_actions(self, act: Actions) -> Self:
        self.actions = act
        return self

    async def server(self) -> Any:
        while 1:
            threading.Thread(target=lambda: _handler(event_queue.get(), self.actions), daemon=True).start()


def run():
    global listener_ran
    listener_ran = True

    async def hy_i_runner():
        global connection
        if handler is tester:
            raise errors.ListenerNotRegisteredError("No handler registered")
        # connection = websocket.WebSocket()
        # if isinstance(config.connection, Configurator.WSConnectionC):
        #     connection = Network.WebsocketConnection(f"ws://{config.connection.host}:{config.connection.port}")
        # elif isinstance(config.connection, Configurator.HTTPConnectionC):
        #     connection = Network.HTTPConnection(
        #         url=f"http://{config.connection.host}:{config.connection.port}",
        #         listener_url=f"http://{config.connection.listener_host}:{config.connection.listener_port}"
        #     )
        connection = KritorConnection(
            host=config.connection.host,
            port=config.connection.port,
        )
        retried = 0
        while True:
            try:
                connection.connect()
            except ConnectionRefusedError or TimeoutError:
                if retried >= config.connection.retries:
                    logger.log(f"重试次数达到最大值({config.connection.retries})，退出",
                               level=hyperogger.levels.CRITICAL)
                    break

                logger.log(f"连接建立失败，3秒后重试({retried}/{config.connection.retries})",
                           level=hyperogger.levels.WARNING)
                retried += 1
                time.sleep(3)
                continue
            logger.log("成功建立连接", level=hyperogger.levels.INFO)
            retried = 0
            actions = Actions(connection)
            data = HyperListenerStartNotify(
                time_now=int(time.time()),
                notify_type="listener_start",
                connection=connection
            )
            threading.Thread(target=lambda: _handler(data, actions), daemon=True).start()
            task = None
            while True:
                try:
                    task = connection.recv()
                    KritorEventGettingService(IServiceStartUp.MANUAL).set_actions(actions).run_in_thread()
                    await task
                except ConnectionResetError:
                    task = None
                    logger.log("连接断开", level=hyperogger.levels.ERROR)
                    break
                # threading.Thread(target=lambda: asyncio.run(__handler(data, actions))).start()
                # threading.Thread(target=lambda: __handler(data, actions)).start()
                # asyncio.create_task(__handler(data, actions))

    try:
        asyncio.get_event_loop().run_until_complete(hy_i_runner())
    except KeyboardInterrupt:
        logger.log("正在退出(Ctrl+C)", level=hyperogger.levels.WARNING)
        sys.exit()


def stop() -> None:
    try:
        connection.close()
    except:
        pass
    logger.log("停止运行监听器", level=hyperogger.levels.WARNING)
