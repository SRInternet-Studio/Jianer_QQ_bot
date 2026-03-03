import json
import typing


class BotWSC:
    def __init__(
            self,
            host: str,
            port: int,
            retries: int = 5,
            token: str = "",
            auth: str = "",
            mode: str = "FWS",
            ob_auto_startup: bool = False,
            ob_exec: str = None,
            ob_startup_path: str = None,
            ob_log_output: bool = False,
            **kwargs
    ):
        self.mode = mode
        self.ob_auto_startup = ob_auto_startup
        self.ob_exec = ob_exec
        self.ob_startup_path = ob_startup_path
        self.ob_log_output = ob_log_output
        self.host = host
        self.port = int(port)
        self.retries = int(retries)
        self.token = token or ""
        self.auth = auth or ""

    def to_json(self) -> dict:
        return dict(
            mode=self.mode,
            host=self.host,
            port=self.port,
            retries=self.retries,
            token=self.token,
            auth=self.auth,
            ob_auto_startup=self.ob_auto_startup,
            ob_exec=self.ob_exec,
            ob_startup_path=self.ob_startup_path,
            ob_log_output=self.ob_log_output,
        )


class BotHTTPC:
    def __init__(
            self,
            host: str,
            port: int,
            listener_host: str,
            listener_port: int,
            retries: int = 5,
            auth: str = "",
            mode: str = "HTTPC",
            ob_auto_startup: bool = False,
            ob_exec: str = None,
            ob_startup_path: str = None,
            ob_log_output: bool = False,
            **kwargs
    ):
        self.mode = mode
        self.ob_auto_startup = ob_auto_startup
        self.ob_exec = ob_exec
        self.ob_startup_path = ob_startup_path
        self.ob_log_output = ob_log_output
        self.host = host
        self.port = int(port)
        self.listener_host = listener_host
        self.listener_port = int(listener_port)
        self.retries = int(retries)
        self.auth = auth or ""

    def to_json(self) -> dict:
        return dict(
            mode=self.mode,
            host=self.host,
            port=self.port,
            listener_host=self.listener_host,
            listener_port=self.listener_port,
            retries=self.retries,
            auth=self.auth,
            ob_auto_startup=self.ob_auto_startup,
            ob_exec=self.ob_exec,
            ob_startup_path=self.ob_startup_path,
            ob_log_output=self.ob_log_output,
        )


class Config:
    def __init__(
            self,
            file: str = None,
            protocol: str = "OneBot",
            owner: list[int] = None,
            black_list: list[int] = None,
            silents: list[int] = None,
            connection: typing.Union[BotWSC, BotHTTPC, dict, None] = None,
            log_level: str = "INFO",
            others: dict = None,
            log_use_nf: bool = False,
            uin: int = 0,
            max_workers: int = 0,
    ):
        self.inited = False
        self.file = file
        if file is None:
            self.protocol = protocol
            self.owner = owner or []
            self.black_list = black_list or []
            self.silents = silents or []
            self.connection = connection
            self.log_level = log_level
            self.others = others or {}
            self.log_use_nf = bool(log_use_nf)
            self.uin = int(uin or 0)
            self.max_workers = int(max_workers or 0)
            self.inited = True

    def load_from_file(self):
        if not self.file:
            raise ValueError("Config file path is required")
        with open(self.file, "r", encoding="utf-8") as f:
            config_json = json.load(f)

        self.protocol = config_json.get("protocol", "OneBot")
        self.owner = config_json.get("owner") or []
        self.black_list = config_json.get("black_list") or []
        self.silents = config_json.get("silents") or []

        conn = config_json.get("Connection") or config_json.get("connection") or {}
        mode = (conn.get("mode") or "").upper()
        if mode in ("FWS", "WS"):
            auth = conn.get("auth") or conn.get("token") or conn.get("satori_token") or ""
            self.connection = BotWSC(
                host=conn.get("host", "127.0.0.1"),
                port=conn.get("port", 0),
                retries=conn.get("retries", 5),
                token=conn.get("token", conn.get("satori_token", "")),
                auth=auth,
                mode=mode,
                ob_auto_startup=bool(conn.get("ob_auto_startup", False)),
                ob_exec=conn.get("ob_exec"),
                ob_startup_path=conn.get("ob_startup_path"),
                ob_log_output=bool(conn.get("ob_log_output", False)),
            )
        elif mode in ("HTTP", "HTTPC"):
            self.connection = BotHTTPC(
                host=conn.get("host", "127.0.0.1"),
                port=conn.get("port", 0),
                listener_host=conn.get("listener_host", "127.0.0.1"),
                listener_port=conn.get("listener_port", 0),
                retries=conn.get("retries", 5),
                auth=conn.get("auth", ""),
                mode="HTTPC",
                ob_auto_startup=bool(conn.get("ob_auto_startup", False)),
                ob_exec=conn.get("ob_exec"),
                ob_startup_path=conn.get("ob_startup_path"),
                ob_log_output=bool(conn.get("ob_log_output", False)),
            )
        else:
            self.connection = conn

        self.log_level = config_json.get("log_level", config_json.get("Log_level", "INFO"))
        self.others = config_json.get("others", config_json.get("Others", {})) or {}

        self.log_use_nf = bool(self.others.get("log_use_nf", False))
        self.uin = int(self.others.get("uin", 0) or 0)
        self.max_workers = int(self.others.get("max_workers", 0) or 0)

        self.inited = True
        return self

    def dump(self, file: str = None) -> None:
        target = file or self.file
        if not target:
            return
        cfg = dict(
            owner=self.owner,
            black_list=self.black_list,
            silents=self.silents,
            Connection=self.connection.to_json() if hasattr(self.connection, "to_json") else self.connection,
            Log_level=self.log_level,
            protocol=self.protocol,
            Others=self.others,
        )
        with open(target, "w", encoding="utf-8") as f:
            f.write(json.dumps(cfg, ensure_ascii=False, indent=2))


class ConfigManager:
    def __init__(self, config: Config):
        self.config = config

    def get_cfg(self) -> Config:
        return self.config


cm: typing.Optional[ConfigManager] = None


class BotConfig(Config):
    @classmethod
    def get(cls, name: str = "hyper-bot") -> Config:
        global cm
        if cm is not None:
            return cm.get_cfg()
        cfg = cls(file="config.json").load_from_file()
        cm = ConfigManager(cfg)
        return cfg
