from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import ClassVar, Dict, Optional


_ADAPTER_LOADED: Optional[str] = None
_CM_FILE: Optional[str] = None


def _canon_protocol(protocol: str) -> str:
    mapping = {
        "onebot": "OneBot",
        "onebot11": "OneBot",
        "onebotv11": "OneBot",
        "onebot-v11": "OneBot",
        "milky": "Milky",
        "kritor": "Kritor",
        "feishu": "Feishu",
        "lark": "Feishu",
    }
    return mapping.get(str(protocol or "OneBot").strip().lower(), str(protocol or "OneBot").strip())


def _canon_mode(mode: str) -> str:
    mapping = {
        "fws": "FWS",
        "http": "HTTPC",
        "httpc": "HTTPC",
    }
    return mapping.get(str(mode or "FWS").strip().lower(), str(mode or "FWS").strip().upper())


@dataclass
class BotWSC:
    mode: str = "FWS"
    ob_auto_startup: bool = False
    ob_exec: Optional[str] = None
    ob_startup_path: Optional[str] = None
    ob_log_output: bool = False
    host: str = "127.0.0.1"
    port: int = 8080
    retries: int = 5
    token: Optional[str] = None
    auth: Optional[str] = None
    event_mode: Optional[str] = None
    app_id: Optional[str] = None
    app_secret: Optional[str] = None
    verification_token: Optional[str] = None
    encrypt_key: Optional[str] = None
    callback_path: Optional[str] = None
    base_url: Optional[str] = None
    token_refresh_skew_seconds: Optional[int] = None
    bot_open_id: Optional[str] = None


@dataclass
class BotHTTPC:
    mode: str = "HTTPC"
    ob_auto_startup: bool = False
    ob_exec: Optional[str] = None
    ob_startup_path: Optional[str] = None
    ob_log_output: bool = False
    host: str = "127.0.0.1"
    port: int = 8080
    listener_host: str = "127.0.0.1"
    listener_port: int = 8081
    retries: int = 5
    auth: Optional[str] = None
    event_mode: Optional[str] = None
    app_id: Optional[str] = None
    app_secret: Optional[str] = None
    verification_token: Optional[str] = None
    encrypt_key: Optional[str] = None
    callback_path: Optional[str] = None
    base_url: Optional[str] = None
    token_refresh_skew_seconds: Optional[int] = None
    bot_open_id: Optional[str] = None


@dataclass
class BotKritorC:
    mode: str = "KRITOR"
    host: str = "127.0.0.1"
    port: int = 8554
    retries: int = 5


@dataclass
class BotConfig:
    protocol: str = "OneBot"
    owner: list = field(default_factory=list)
    black_list: list = field(default_factory=list)
    silents: list = field(default_factory=list)
    connection: object = field(default_factory=BotWSC)
    connections: dict = field(default_factory=dict)
    log_level: str = "INFO"
    log_use_nf: bool = False
    uin: int = 0
    max_workers: int = 4
    others: dict = field(default_factory=dict)

    _cache: ClassVar[Dict[str, "BotConfig"]] = {}

    def get_connection(self, protocol: Optional[str] = None) -> object:
        proto = protocol or self.protocol
        proto = _canon_protocol(proto)
        if proto in self.connections:
            return self.connections[proto]
        return self.connection

    @classmethod
    def _from_dict(cls, data: dict) -> "BotConfig":
        protocol = _canon_protocol(data.get("protocol", "OneBot"))
        connections_raw = data.get("Connections") or {}

        connections = {}
        for proto_name, conn_raw in connections_raw.items():
            proto_canon = _canon_protocol(proto_name)
            mode = _canon_mode(conn_raw.get("mode", "FWS"))
            auth = conn_raw.get("auth") or conn_raw.get("token")

            if proto_canon == "Kritor":
                connections[proto_canon] = BotKritorC(
                    mode="KRITOR",
                    host=str(conn_raw.get("host", "127.0.0.1")),
                    port=int(conn_raw.get("port", 8554)),
                    retries=int(conn_raw.get("retries", 5)),
                )
            elif mode == "HTTPC":
                connections[proto_canon] = BotHTTPC(
                    mode=mode,
                    ob_auto_startup=bool(conn_raw.get("ob_auto_startup", False)),
                    ob_exec=conn_raw.get("ob_exec"),
                    ob_startup_path=conn_raw.get("ob_startup_path"),
                    ob_log_output=bool(conn_raw.get("ob_log_output", False)),
                    host=str(conn_raw.get("host", "127.0.0.1")),
                    port=int(conn_raw.get("port", 8080)),
                    listener_host=str(conn_raw.get("listener_host", "127.0.0.1")),
                    listener_port=int(conn_raw.get("listener_port", 8081)),
                    retries=int(conn_raw.get("retries", 5)),
                    auth=auth,
                    event_mode=conn_raw.get("event_mode"),
                    app_id=conn_raw.get("app_id"),
                    app_secret=conn_raw.get("app_secret"),
                    verification_token=conn_raw.get("verification_token"),
                    encrypt_key=conn_raw.get("encrypt_key"),
                    callback_path=conn_raw.get("callback_path"),
                    base_url=conn_raw.get("base_url"),
                    token_refresh_skew_seconds=conn_raw.get("token_refresh_skew_seconds"),
                    bot_open_id=conn_raw.get("bot_open_id"),
                )
            else:
                connections[proto_canon] = BotWSC(
                    mode="FWS",
                    ob_auto_startup=bool(conn_raw.get("ob_auto_startup", False)),
                    ob_exec=conn_raw.get("ob_exec"),
                    ob_startup_path=conn_raw.get("ob_startup_path"),
                    ob_log_output=bool(conn_raw.get("ob_log_output", False)),
                    host=str(conn_raw.get("host", "127.0.0.1")),
                    port=int(conn_raw.get("port", 8080)),
                    retries=int(conn_raw.get("retries", 5)),
                    token=auth,
                    auth=auth,
                    event_mode=conn_raw.get("event_mode"),
                    app_id=conn_raw.get("app_id"),
                    app_secret=conn_raw.get("app_secret"),
                    verification_token=conn_raw.get("verification_token"),
                    encrypt_key=conn_raw.get("encrypt_key"),
                    callback_path=conn_raw.get("callback_path"),
                    base_url=conn_raw.get("base_url"),
                    token_refresh_skew_seconds=conn_raw.get("token_refresh_skew_seconds"),
                    bot_open_id=conn_raw.get("bot_open_id"),
                )

        connection_raw = data.get("Connection") or data.get("connection") or {}
        mode = _canon_mode(connection_raw.get("mode", "FWS"))
        token = connection_raw.get("token") or connection_raw.get("satori_token")
        auth = connection_raw.get("auth") or token

        if mode == "HTTPC":
            default_connection = BotHTTPC(
                mode=mode,
                ob_auto_startup=bool(connection_raw.get("ob_auto_startup", False)),
                ob_exec=connection_raw.get("ob_exec"),
                ob_startup_path=connection_raw.get("ob_startup_path"),
                ob_log_output=bool(connection_raw.get("ob_log_output", False)),
                host=str(connection_raw.get("host", "127.0.0.1")),
                port=int(connection_raw.get("port", 8080)),
                listener_host=str(connection_raw.get("listener_host", "127.0.0.1")),
                listener_port=int(connection_raw.get("listener_port", 8081)),
                retries=int(connection_raw.get("retries", 5)),
                auth=auth,
                event_mode=connection_raw.get("event_mode"),
                app_id=connection_raw.get("app_id"),
                app_secret=connection_raw.get("app_secret"),
                verification_token=connection_raw.get("verification_token"),
                encrypt_key=connection_raw.get("encrypt_key"),
                callback_path=connection_raw.get("callback_path"),
                base_url=connection_raw.get("base_url"),
                token_refresh_skew_seconds=connection_raw.get("token_refresh_skew_seconds"),
                bot_open_id=connection_raw.get("bot_open_id"),
            )
        else:
            default_connection = BotWSC(
                mode="FWS",
                ob_auto_startup=bool(connection_raw.get("ob_auto_startup", False)),
                ob_exec=connection_raw.get("ob_exec"),
                ob_startup_path=connection_raw.get("ob_startup_path"),
                ob_log_output=bool(connection_raw.get("ob_log_output", False)),
                host=str(connection_raw.get("host", "127.0.0.1")),
                port=int(connection_raw.get("port", 8080)),
                retries=int(connection_raw.get("retries", 5)),
                token=token,
                auth=auth,
                event_mode=connection_raw.get("event_mode"),
                app_id=connection_raw.get("app_id"),
                app_secret=connection_raw.get("app_secret"),
                verification_token=connection_raw.get("verification_token"),
                encrypt_key=connection_raw.get("encrypt_key"),
                callback_path=connection_raw.get("callback_path"),
                base_url=connection_raw.get("base_url"),
                token_refresh_skew_seconds=connection_raw.get("token_refresh_skew_seconds"),
                bot_open_id=connection_raw.get("bot_open_id"),
            )

        if not connections and default_connection:
            connections[protocol] = default_connection

        return cls(
            protocol=protocol,
            owner=list(data.get("owner", [])),
            black_list=list(data.get("black_list", [])),
            silents=list(data.get("silents", [])),
            connection=default_connection,
            connections=connections,
            log_level=data.get("Log_level", data.get("log_level", "INFO")),
            log_use_nf=bool(data.get("log_use_nf", False)),
            uin=int(data.get("uin", 0) or 0),
            max_workers=int(data.get("max_workers", 4) or 4),
            others=dict(data.get("Others", data.get("others", {}))),
        )

    @classmethod
    def get(
        cls,
        name: str = "hyper-bot",
        file: str = "config.json",
        force_reload: bool = False,
    ) -> "BotConfig":
        if not force_reload and name in cls._cache:
            return cls._cache[name]
        with open(file, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        cfg = cls._from_dict(data)
        cls._cache[name] = cfg
        return cfg


def ensure_adapter_loaded(config: Optional[BotConfig] = None) -> None:
    global _ADAPTER_LOADED
    cfg = config or BotConfig.get("hyper-bot")
    protocol = _canon_protocol(cfg.protocol)
    if _ADAPTER_LOADED == protocol:
        return

    from .adapters import builtins

    if protocol == "Milky":
        builtins.load_milky()
    elif protocol == "Feishu":
        builtins.load_feishu()
    else:
        builtins.load_onebot()
    _ADAPTER_LOADED = protocol


class Config:
    def __init__(self, file: str = "config.json"):
        self.file = file

    def load_from_file(self) -> BotConfig:
        return BotConfig.get("hyper-bot", file=self.file, force_reload=True)


class ConfigManager:
    def __init__(self, config: BotConfig):
        self.config = config
        ensure_adapter_loaded(config)

    def get_cfg(self) -> BotConfig:
        return self.config


cm: Optional[ConfigManager] = None


def ensure_config_manager(file: str = "config.json", force_reload: bool = False) -> ConfigManager:
    global cm, _CM_FILE
    file_path = os.path.abspath(file)

    if cm is not None and not force_reload and _CM_FILE == file_path:
        ensure_adapter_loaded(cm.get_cfg())
        return cm

    cfg = Config(file=file).load_from_file()
    cm = ConfigManager(cfg)
    _CM_FILE = file_path
    return cm


try:
    ensure_config_manager()
except Exception:
    cm = None
