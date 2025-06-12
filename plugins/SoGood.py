import asyncio
import random
import time
import httpx
from random import randint
import dataclasses
import json
from Hyper import Configurator, Events
Configurator.cm = Configurator.ConfigManager(Configurator.Config(file="config.json").load_from_file())

TRIGGHT_KEYWORD = "Any"
HELP_MESSAGE = f'''{Configurator.cm.get_cfg().others["reminder"]}发电 (名字) —> 对某个人表达内心深处的诉求
       我今天棒不棒 —> 让{Configurator.cm.get_cfg().others["bot_name"]}来评评你今天表现怎么样'''

@dataclasses.dataclass
class UserInfo:
    goodness: int
    time: int

    @property
    def level(self) -> str:
        if 0 <= self.goodness <= 20:
            return "嗯~今天表现不乖，下次一定要听话哦"
        elif 20 < self.goodness <= 40:
            return "看着顺眼"
        elif 40 < self.goodness <= 60:
            return "亲爱的太棒啦！"
        elif 60 < self.goodness <= 80:
            return "来，抱一个~嗯~"
        else:
            return "👍_ _ _👍"

    @classmethod
    def build(cls) -> "UserInfo":
        return cls(randint(0, 100), int(time.time()))

users: dict[str, UserInfo] = {}
with open("./assets/quick.json", "r", encoding="utf-8") as f:
    words = json.load(f)["ele"]


async def on_message(event, actions, Manager, Events: Events, Segments, reminder):
        if not isinstance(event, Events.GroupMessageEvent):
            return None
        
        if "今天棒不棒" in str(event.message):
            if "我" in str(event.message):
                name = "\n你"
                uin = str(event.user_id)
            elif "@" in str(event.message):
                name = ""
                uin = event.message[0].qq
            else:
                return

            if str(uin) not in users.keys():
                users[str(uin)] = UserInfo.build()

            msg = Manager.Message(
                Segments.At(uin),
                Segments.Text(
                    f" {name}今天的分数: {users[str(uin)].goodness}\n评级: {users[str(uin)].level}")
            )

            await actions.send(
                group_id=event.group_id,
                user_id=event.user_id,
                message=msg
            )
            return True

        elif str(event.message).startswith(f"{reminder}发电"):
            uin = 0
            for i in event.message:
                if isinstance(i, Segments.At):
                    uin = i.qq
                    break
            if uin == 0:
                tag = str(event.message).replace(f"{reminder}发电", "", 1)
            else:
                tag = f"@{(await actions.get_stranger_info(uin)).data.raw["nickname"]}"

            word = random.choice(words).replace("{target_name}", tag)
            await actions.send(
                group_id=event.group_id,
                user_id=event.user_id,
                message=Manager.Message(
                    Segments.Reply(event.message_id), Segments.Text(word)
                )
            )
            return True