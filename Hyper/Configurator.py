import hyperot.configurator as _c

BotWSC = _c.BotWSC
BotHTTPC = _c.BotHTTPC
BotConfig = _c.BotConfig


class Config(_c.Config):
    pass


class ConfigManager(_c.ConfigManager):
    def __init__(self, config: _c.Config):
        super().__init__(config)
        _c.cm = self


cm = _c.cm

