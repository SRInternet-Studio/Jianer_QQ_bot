from ... import configurator, hyperogger
from ...utils import logic
from .translator import MilkyHttpConnection

import random

reports = logic.KeyQueue()

config: configurator.BotConfig
logger: hyperogger.Logger


def init() -> None:
    global config, logger
    config = configurator.BotConfig.get("hyper-bot")
    logger = hyperogger.Logger()
    logger.set_level(config.log_level)


class Packet:
    def __init__(self, endpoint: str, **kwargs):
        self.endpoint = endpoint
        self.paras = kwargs
        self.echo = f"{endpoint}_{random.randint(1000, 9999)}"

    def send_to(self, connection: MilkyHttpConnection) -> dict:
        if isinstance(connection, MilkyHttpConnection):
            res = connection.http_send(self.endpoint, self.paras)
            if isinstance(res, dict):
                res["echo"] = self.echo
                reports.put(self.echo, res)
            return res
        else:
            raise ValueError(f"Invalid connection: {connection}")
