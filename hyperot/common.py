from . import configurator
from .utils.typextensions import ObjectedJson
from . import segments
from .utils.hypetyping import TypeVar

from typing import Generic, Type, Self

config = configurator.BotConfig.get("hyper-bot")
T = TypeVar("T")

from .adapters.common import *

init()


class MessageBuilder:
    def __init__(self):
        self.sgs = []

    def __getattr__(self, item):
        if item == "build":
            def build() -> Message:
                return Message(*self.sgs)

            return build

        elif item in segments.message_types.keys():
            def wrapper(*args, **kwargs):
                self.sgs.append(segments.message_types[item]["type"](*args, **kwargs))
                return self

            return wrapper
        else:
            return None


class Message:
    builder = MessageBuilder()

    def __init__(self, *args):
        if len(args) == 1 and isinstance(args[0], list):
            contents = args[0]
        elif len(args) >= 1:
            contents = list(args)
        else:
            contents = []
        self.contents = contents

    def add(self, content) -> None:
        self.contents.append(content)

    async def get(self) -> list:
        return self.get_sync()

    def get_sync(self) -> list:
        ret = []
        for i in self.contents:
            ret.append(i.to_json())
        return ret

    def __len__(self) -> int:
        return len(self.contents)

    def __getitem__(self, index: int):
        return self.contents[index]

    def __setitem__(self, index: int, content) -> None:
        self.contents[index] = content

    def __delitem__(self, index: int) -> None:
        del self.contents[index]

    def __iter__(self):
        for i in self.contents:
            yield i

    def __str__(self) -> str:
        return "".join([str(content) for content in self.contents])

    def __repr__(self) -> str:
        return str(self.contents)

    def __add__(self, new):
        self.contents += new.contents
        return self

    def __iadd__(self, new):
        self.contents += new.contents
        return self


class Ret(Generic[T]):
    def __init__(self, json_data: dict, serializer: Type[T]):
        self.raw = json_data.copy()
        self.status = json_data.get("status")
        self.ret_code = json_data.get("retcode")
        self.data: T = serializer(json_data.get("data"))
        self.echo = json_data.get("echo")

    @classmethod
    def fetch(cls, echo: str, serializer=ObjectedJson) -> Self:
        return cls(reports.get(echo), serializer)
