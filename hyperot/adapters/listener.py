from ..utils.hypetyping import Any, NoReturn, TypeVar, Callable
from ..utils.apiresponse import *
from ..events import *

config = configurator.BotConfig.get("hyper-bot")
logger = hyperogger.Logger()
logger.set_level(config.log_level)
listener_ran = False


class Actions:
    def __init__(self):
        self.custom: type

    async def send(
            self, message: Union[common.Message, str], group_id: int = None, user_id: int = None
    ) -> common.Ret[MsgSendRsp]:
        ...

    async def del_message(self, message_id: int) -> None:
        ...

    async def set_group_kick(self, group_id: int, user_id: int) -> None:
        ...

    async def set_group_ban(self, group_id: int, user_id: int, duration: int = 60) -> None:
        ...

    async def get_login_info(self) -> common.Ret[GetLoginInfoRsp]:
        ...

    async def get_version_info(self) -> common.Ret[GetVerInfoRsp]:
        ...

    async def send_forward_msg(self, message: common.Message) -> common.Ret[SendForwardRsp]:
        ...

    async def get_forward_msg(self, sid: str) -> common.Ret[common.Message]:
        ...

    async def forward_solve(self, message: common.Message) -> common.Message:
        ...

    async def send_group_forward_msg(self, group_id: int, message: common.Message) -> common.Ret[SendGrpForwardRsp]:
        ...

    async def set_group_add_request(self, flag: str, sub_type: str, approve: bool,
                                    reason: str = "Not Mentioned") -> None:
        ...

    async def get_stranger_info(self, user_id: int) -> common.Ret[GetStrInfoRsp]:
        ...

    async def get_group_member_info(self, group_id: int, user_id: int) -> common.Ret[GetGrpMemInfoRsp]:
        ...

    async def get_group_info(self, group_id: int) -> common.Ret[GetGrpInfoRsp]:
        ...

    async def get_status(self) -> common.Ret:
        ...

    async def set_essence_msg(self, message_id: int) -> None:
        ...

    async def set_group_special_title(self, group_id: int, user_id: int, title: str) -> None:
        ...

    async def get_msg(self, msg_id: int) -> common.Ret[GetMsgRsp]:
        ...

    async def send_callback(self, group_id: int, bot_id: int, data: dict) -> None:
        ...


def reg(func: Callable) -> None:
    ...


def run() -> NoReturn:
    logger.error("你没有设置适配器！")


def stop() -> None:
    ...
