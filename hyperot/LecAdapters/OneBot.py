import json
import threading
import time
import asyncio
import sys
import subprocess

from .. import network, events, common, segments
from ..service import FuncCall, IServiceBase, IServiceStartUp
from ..utils import errors, logic
from ..utils.apiresponse import *
from ..LecAdapters.OneBotLib.Manager import reports, Packet
from ..events import *
from ..utils.hypetyping import Any, Union, NoReturn

config = configurator.BotConfig.get("hyper-bot")
logger = hyperogger.Logger()
logger.set_level(config.log_level)
listener_ran = False


class Actions:
    def __init__(self, cnt: Union[network.WebsocketConnection, network.HTTPConnection]):
        self.connection = cnt

        class CustomAction:
            def __init__(self, cnt_i: Union[network.WebsocketConnection, network.HTTPConnection]):
                self.connection = cnt_i

            def __getattr__(self, item) -> callable:
                async def wrapper(**kwargs) -> str:
                    packet = Packet(
                        str(item),
                        **kwargs
                    )
                    packet.send_to(self.connection)
                    return packet.echo

                return wrapper

        self.custom = CustomAction(self.connection)

    async def send(
            self, message: Union[common.Message, str], group_id: int = None, user_id: int = None
    ) -> common.Ret[MsgSendRsp]:
        if isinstance(message, str):
            message = common.Message(segments.Text(message))
        if group_id is not None:
            packet = Packet(
                "send_msg",
                group_id=group_id,
                message=await message.get()
            )
        elif user_id is not None:
            packet = Packet(
                "send_msg",
                user_id=user_id,
                message=await message.get()
            )
        else:
            raise errors.ArgsInvalidError("'send' API requires 'group_id' or 'user_id' but none of them are provided.")
        packet.send_to(self.connection)
        logger.info(f"向{(('群 ' + str(group_id)) if group_id else ('用户' + str(user_id))) + ' '}发送：{str(message)}")
        return common.Ret.fetch(packet.echo, MsgSendRsp)

    async def del_message(self, message_id: int) -> None:
        Packet(
            "delete_msg",
            message_id=message_id,
        ).send_to(self.connection)
        logger.info(f"撤回 {message_id}")

    async def set_group_kick(self, group_id: int, user_id: int) -> None:
        Packet(
            "set_group_kick",
            group_id=group_id,
            user_id=user_id,
        ).send_to(self.connection)
        logger.info(f"将用户 {user_id} 移出群 {group_id}")

    async def set_group_ban(self, group_id: int, user_id: int, duration: int = 60) -> None:
        Packet(
            "set_group_ban",
            group_id=group_id,
            user_id=user_id,
            duration=duration,
        ).send_to(self.connection)
        logger.info(f"在群 {group_id} 将用户 {user_id} 禁言 {duration}s")

    async def get_login_info(self) -> common.Ret[GetLoginInfoRsp]:
        packet = Packet("get_login_info")
        packet.send_to(self.connection)
        return common.Ret.fetch(packet.echo, GetLoginInfoRsp)

    async def get_version_info(self) -> common.Ret[GetVerInfoRsp]:
        packet = Packet("get_version_info")
        packet.send_to(self.connection)
        return common.Ret.fetch(packet.echo, GetVerInfoRsp)

    async def send_forward_msg(self, message: common.Message) -> common.Ret[SendForwardRsp]:
        packet = Packet(
            "send_forward_msg",
            messages=await message.get()
        )
        packet.send_to(self.connection)
        return common.Ret.fetch(packet.echo, SendForwardRsp)

    async def get_forward_msg(self, sid: str) -> common.Ret[common.Message]:
        packet = Packet(
            "get_forward_msg",
            id=sid,
        )
        packet.send_to(self.connection)
        ret = common.Ret.fetch(packet.echo, events.gen_message)
        for i in ret.data:
            if isinstance(i, segments.Node):
                i.content = gen_message({"message": i.content})

        return ret

    async def forward_solve(self, message: common.Message) -> common.Message:
        for i in message:
            if isinstance(i, segments.Forward):
                data = await self.get_forward_msg(i.id)
                return data.data
        raise ValueError("Incorrect message type")

    async def send_group_forward_msg(self, group_id: int, message: common.Message) -> common.Ret[SendGrpForwardRsp]:
        packet = Packet(
            "send_group_forward_msg",
            group_id=group_id,
            messages=await message.get()
        )
        packet.send_to(self.connection)
        return common.Ret.fetch(packet.echo, SendForwardRsp)

    async def set_group_add_request(self, flag: str, sub_type: str, approve: bool,
                                    reason: str = "Not Mentioned") -> None:
        Packet(
            "set_group_add_request",
            flag=flag,
            sub_type=sub_type,
            approve=approve,
            reason=reason
        ).send_to(self.connection)
        logger.info(f"由于 {reason}，{'通过' if approve else '拒绝'} {flag} 请求")

    async def get_stranger_info(self, user_id: int) -> common.Ret[GetStrInfoRsp]:
        packet = Packet(
            "get_stranger_info",
            user_id=user_id,
            no_cache=True,
        )
        packet.send_to(self.connection)
        return common.Ret.fetch(packet.echo, GetStrInfoRsp)

    async def get_group_member_info(self, group_id: int, user_id: int) -> common.Ret[GetGrpMemInfoRsp]:
        packet = Packet(
            "get_group_member_info",
            group_id=group_id,
            user_id=user_id,
            no_cache=True
        )
        packet.send_to(self.connection)
        return common.Ret.fetch(packet.echo, GetGrpMemInfoRsp)

    async def get_group_info(self, group_id: int) -> common.Ret[GetGrpInfoRsp]:
        packet = Packet(
            "get_group_info",
            group_id=group_id,
            no_cache=True
        )
        packet.send_to(self.connection)
        return common.Ret.fetch(packet.echo, GetGrpInfoRsp)

    async def get_status(self) -> common.Ret:
        packet = Packet("get_status")
        packet.send_to(self.connection)
        return common.Ret.fetch(packet.echo)

    async def set_essence_msg(self, message_id: int) -> None:
        Packet(
            "set_essence_msg",
            message_id=message_id
        ).send_to(self.connection)

    async def set_group_special_title(self, group_id: int, user_id: int, title: str) -> None:
        Packet(
            "set_group_special_title",
            group_id=group_id,
            user_id=user_id,
            special_title=title,
        ).send_to(self.connection)

    async def get_msg(self, msg_id: int) -> common.Ret[GetMsgRsp]:
        packet = Packet(
            "get_msg",
            message_id=msg_id
        )
        packet.send_to(self.connection)
        return common.Ret.fetch(packet.echo, GetMsgRsp)

    async def send_callback(self, group_id: int, bot_id: int, data: dict) -> None:
        Packet(
            "send_group_bot_callback",
            group_id=group_id,
            bot_id=bot_id,
            **data
        ).send_to(self.connection)


async def tester(
        message_data: Union[Event, HyperNotify], actions: Actions
) -> None:
    ...


def __handler(data: Union[dict, HyperNotify], actions: Actions) -> None:
    if isinstance(data, dict):
        if data.get("echo") is not None:
            reports.put(data.get("echo"), data)
        elif data.get("post_type") == "meta_event" or data.get("user_id") == data.get("self_id"):
            pass
        else:
            asyncio.run(handler(events.em.new(data), actions))
    else:
        asyncio.run(handler(data, actions))


handler: callable = tester


def reg(func: callable) -> None:
    global handler
    handler = func


connection: Union[network.WebsocketConnection, network.HTTPConnection]


class LagrangeOneBotService(IServiceBase):

    def handler(self, func: FuncCall) -> Any:
        pass

    async def server(self, bot_config: configurator.BotConfig) -> None:
        proc = subprocess.Popen(
            args=config.connection.ob_exec,
            cwd=config.connection.ob_startup_path,
            stdout=subprocess.PIPE
        )
        if bot_config.connection.ob_log_output:
            for i in proc.stdout:
                print(i.decode(), end="")


def run() -> NoReturn:
    global connection, listener_ran
    listener_ran = True
    try:
        if handler is tester:
            raise errors.ListenerNotRegisteredError("No handler registered")
        if isinstance(config.connection, configurator.BotWSC):
            connection = network.WebsocketConnection(f"ws://{config.connection.host}:{config.connection.port}")
        elif isinstance(config.connection, configurator.BotHTTPC):
            connection = network.HTTPConnection(
                url=f"http://{config.connection.host}:{config.connection.port}",
                listener_url=f"http://{config.connection.listener_host}:{config.connection.listener_port}"
            )
        retried = 0
        if config.connection.ob_auto_startup:
            LagrangeOneBotService(IServiceStartUp.MANUAL).run_in_thread(config)

        while True:
            try:
                connection.connect()
            except ConnectionRefusedError or TimeoutError:
                if retried >= config.connection.retries:
                    logger.critical(f"重试次数达到最大值({config.connection.retries})，退出")
                    break

                logger.warning(f"连接建立失败，3秒后重试({retried}/{config.connection.retries})")
                retried += 1
                time.sleep(3)
                continue
            retried = 0
            logger.info(f"成功在 {connection.url} 建立连接")
            actions = Actions(connection)
            data = HyperListenerStartNotify(
                time_now=int(time.time()),
                notify_type="listener_start",
                connection=connection
            )
            threading.Thread(target=lambda: __handler(data, actions), daemon=True).start()
            while True:
                try:
                    data = connection.recv()
                except ConnectionResetError:
                    logger.error("连接断开")
                    break
                except json.decoder.JSONDecodeError:
                    logger.error("收到错误的JSON内容")
                    continue
                threading.Thread(target=lambda: __handler(data, actions), daemon=True).start()
    except KeyboardInterrupt:
        logger.warning("正在退出(Ctrl+C)")
        try:
            connection.close()
        except:
            pass
        sys.exit()


def stop() -> None:
    try:
        connection.close()
    except:
        pass
    logger.log("停止运行监听器", level=hyperogger.levels.WARNING)
