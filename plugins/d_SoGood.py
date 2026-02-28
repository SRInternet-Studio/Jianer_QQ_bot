import asyncio
import random
import time
import httpx
import os
import sqlite3
from random import randint
import dataclasses
import json
from Hyper import Configurator, Events
Configurator.cm = Configurator.ConfigManager(Configurator.Config(file="config.json").load_from_file())

TRIGGHT_KEYWORD = "Any"
HELP_MESSAGE = f'''{Configurator.cm.get_cfg().others['reminder']}发电 (名字) —> 对某个人表达内心深处的诉求
       我今天棒不棒 —> 让{Configurator.cm.get_cfg().others['bot_name']}来评评你今天表现怎么样'''

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

with open("./assets/quick.json", "r", encoding="utf-8") as f:
    words = json.load(f)["ele"]

DB_PATH = os.path.join(".", "data", "sogood.db")

def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn

def _ensure_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sogood_users (
                uin TEXT PRIMARY KEY,
                goodness INTEGER NOT NULL,
                ts INTEGER NOT NULL
            );
            """
        )

def _load_user(uin: str):
    with _connect() as conn:
        row = conn.execute(
            "SELECT goodness, ts FROM sogood_users WHERE uin = ?;",
            (str(uin),),
        ).fetchone()
        if not row:
            return None
        return UserInfo(int(row[0]), int(row[1]))

def _save_user(uin: str, info: UserInfo):
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sogood_users (uin, goodness, ts) VALUES (?, ?, ?);",
            (str(uin), int(info.goodness), int(info.time)),
        )

_ensure_db()


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

            info = _load_user(str(uin))
            if info is None:
                info = UserInfo.build()
                _save_user(str(uin), info)

            msg = Manager.Message(
                Segments.At(uin),
                Segments.Text(
                    f" {name}今天的分数: {info.goodness}\n评级: {info.level}")
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
