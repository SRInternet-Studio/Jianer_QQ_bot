import json
import typing

SUPPORTED_PROTOCOLS = ("OneBot", "Milky", "Kritor", "Feishu")
PROTOCOL_ALIASES = {
    "ONEBOT": "OneBot",
    "MILKY": "Milky",
    "KRITOR": "Kritor",
    "FEISHU": "Feishu",
    "LARK": "Feishu",
}


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
            protocol: typing.Union[str, list[str], tuple[str, ...]] = "OneBot",
            owner: list[int] = None,
            black_list: list[int] = None,
            silents: list[int] = None,
            connection: typing.Union[BotWSC, BotHTTPC, dict, None] = None,
            log_level: str = "INFO",
            others: dict = None,
            log_use_nf: bool = False,
            uin: int = 0,
            max_workers: int = 0,
            platform_switches: dict = None,
            protocols: typing.Optional[list[str]] = None,
            feishu: dict = None,
            connections: dict = None,
    ):
        self.inited = False
        self.file = file
        if file is None:
            requested_protocols = protocols if protocols is not None else protocol
            self.protocols = self._normalize_protocols(requested_protocols)
            self.protocol = self.protocols[0]
            self.owner = owner or []
            self.black_list = black_list or []
            self.silents = silents or []
            self.connection = connection
            self.log_level = log_level
            self.others = others or {}
            self.log_use_nf = bool(log_use_nf)
            self.uin = int(uin or 0)
            self.max_workers = int(max_workers or 0)
            if platform_switches is not None and protocols is None and isinstance(protocol, str):
                merged_protocols = self._protocols_from_platform_switches(platform_switches, self.protocol)
                self.protocols = merged_protocols
                self.protocol = self.protocols[0]
            self.feishu = self._validate_feishu_config(feishu, "Feishu" in self.protocols)
            self.connections = connections or {}
            self.inited = True

    @staticmethod
    def _validate_feishu_config(feishu: typing.Optional[dict], enabled: bool) -> dict:
        if feishu is None:
            feishu = {}
        if not isinstance(feishu, dict):
            raise ValueError("feishu must be an object")
        result = dict(feishu)
        result["app_id"] = str(result.get("app_id", "") or "")
        result["app_secret"] = str(result.get("app_secret", "") or "")
        result["encrypt_key"] = str(result.get("encrypt_key", "") or "")
        result["verification_token"] = str(result.get("verification_token", "") or "")
        result["event_mode"] = str(result.get("event_mode", "long_connection") or "long_connection").strip().lower()
        result["listener_host"] = str(result.get("listener_host", "127.0.0.1") or "127.0.0.1")
        result["listener_port"] = int(result.get("listener_port", 5003) or 5003)
        result["event_path"] = str(result.get("event_path", "/feishu/events") or "/feishu/events")
        result["default_receive_id_type"] = str(result.get("default_receive_id_type", "chat_id") or "chat_id")
        if result["event_mode"] not in ("long_connection", "webhook"):
            raise ValueError("feishu.event_mode must be 'long_connection' or 'webhook'")
        if enabled and (not result["app_id"] or not result["app_secret"]):
            raise ValueError("feishu.app_id and feishu.app_secret are required when Feishu is enabled")
        return result

    @staticmethod
    def _normalize_protocol(protocol: typing.Any) -> str:
        protocol_text = str(protocol or "").strip()
        if not protocol_text:
            return "OneBot"
        if protocol_text in SUPPORTED_PROTOCOLS:
            return protocol_text
        alias_key = protocol_text.upper()
        if alias_key in PROTOCOL_ALIASES:
            return PROTOCOL_ALIASES[alias_key]
        raise ValueError(f"Unsupported protocol: {protocol_text}")

    @staticmethod
    def _normalize_protocols(protocols: typing.Any) -> list[str]:
        if protocols is None or protocols == "":
            return ["OneBot"]
        if isinstance(protocols, (list, tuple)):
            source = list(protocols)
        else:
            source = [protocols]
        result = []
        for item in source:
            normalized = Config._normalize_protocol(item)
            if normalized not in result:
                result.append(normalized)
        if not result:
            raise ValueError("protocol must contain at least one supported protocol")
        return result

    @classmethod
    def _protocols_from_platform_switches(cls, platform_switches: typing.Optional[dict], fallback_protocol: str) -> list[str]:
        if not isinstance(platform_switches, dict):
            return [fallback_protocol]
        result = []
        for raw_name, enabled in platform_switches.items():
            normalized_name = cls._normalize_protocol(raw_name)
            if not isinstance(enabled, bool):
                raise ValueError(f"platform_switches.{normalized_name} must be a boolean")
            if enabled and normalized_name not in result:
                result.append(normalized_name)
        if fallback_protocol not in result:
            result.insert(0, fallback_protocol)
        return result

    @staticmethod
    def _build_connection_object(conn: typing.Any) -> typing.Union[BotWSC, BotHTTPC, dict]:
        if not isinstance(conn, dict):
            return conn if conn is not None else {}
        mode = str(conn.get("mode", "") or "").upper()
        if mode in ("FWS", "WS"):
            auth = conn.get("auth") or conn.get("token") or conn.get("satori_token") or ""
            return BotWSC(
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
        if mode in ("HTTP", "HTTPC"):
            return BotHTTPC(
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
        return dict(conn)

    def load_from_file(self):
        if not self.file:
            raise ValueError("Config file path is required")
        with open(self.file, "r", encoding="utf-8") as f:
            config_json = json.load(f)

        protocol_value = config_json.get("protocol", "OneBot")
        if isinstance(protocol_value, (list, tuple)):
            self.protocols = self._normalize_protocols(protocol_value)
        else:
            normalized_protocol = self._normalize_protocol(protocol_value)
            fallback_protocols = self._protocols_from_platform_switches(
                config_json.get("platform_switches", config_json.get("platforms")),
                normalized_protocol,
            )
            if config_json.get("platform_switches") is not None or config_json.get("platforms") is not None:
                self.protocols = self._normalize_protocols(fallback_protocols)
            else:
                self.protocols = [normalized_protocol]
        self.protocol = self.protocols[0]
        self.owner = config_json.get("owner") or []
        self.black_list = config_json.get("black_list") or []
        self.silents = config_json.get("silents") or []
        self.feishu = self._validate_feishu_config(
            config_json.get("feishu", config_json.get("Feishu")),
            "Feishu" in self.protocols,
        )
        raw_connections = config_json.get("connections", config_json.get("Connections")) or {}
        normalized_connections = {}
        if isinstance(raw_connections, dict):
            for raw_name, raw_conn in raw_connections.items():
                try:
                    name = self._normalize_protocol(raw_name)
                except ValueError:
                    continue
                if isinstance(raw_conn, dict):
                    normalized_connections[name] = dict(raw_conn)
        legacy_conn = config_json.get("Connection") or config_json.get("connection") or {}
        if not normalized_connections and isinstance(legacy_conn, dict) and legacy_conn:
            normalized_connections[self.protocol] = dict(legacy_conn)
        if self.protocol not in normalized_connections and isinstance(legacy_conn, dict) and legacy_conn:
            normalized_connections[self.protocol] = dict(legacy_conn)
        self.connections = normalized_connections
        selected_conn = self.connections.get(self.protocol, {})
        self.connection = self._build_connection_object(selected_conn)

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
            connections={k: (v.to_json() if hasattr(v, "to_json") else v) for k, v in (self.connections or {}).items()},
            Log_level=self.log_level,
            protocol=self.protocols,
            feishu=self.feishu,
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
