from typing import Union, Any, NoReturn
from abc import ABC, abstractmethod
from enum import Enum
from dataclasses import dataclass
import threading
import asyncio


class IServiceStartUp(Enum):
    AUTO = 0
    MANUAL = 1


@dataclass
class FuncCall:
    func: callable
    args: tuple
    kwargs: dict


class IServiceBase(ABC):
    def __init__(
            self,
            start_up: Union[IServiceStartUp.AUTO, IServiceStartUp.MANUAL] = IServiceStartUp.MANUAL
    ):
        if start_up == IServiceStartUp.AUTO:
            asyncio.run(self.server())

    @abstractmethod
    async def server(self, *args, **kwargs) -> Any: ...

    @abstractmethod
    def handler(self, func: FuncCall) -> Any: ...

    def call_on(self, func: callable) -> callable:
        def wrapper(*args, **kwargs) -> Any:
            return self.handler(
                FuncCall(
                    func=func,
                    args=args,
                    kwargs=kwargs,
                )
            )

        return wrapper

    def run_in_thread(self, *args, **kwargs) -> None:
        threading.Thread(target=lambda: asyncio.run(self.server(*args, **kwargs)), daemon=True).start()

    async def run_forever(self, *args, **kwargs) -> None:
        await self.server(*args, **kwargs)
