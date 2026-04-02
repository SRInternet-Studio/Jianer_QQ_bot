from typing import Optional, TypeVar, TypedDict, Union, Any, Protocol, runtime_checkable, NoReturn, Callable, Literal


class OneBotJsonPacket(TypedDict):
    action: str
    params: dict
    echo: Optional[str]


class OneBotSegReg(TypedDict):
    type: type
    args: list[str]
