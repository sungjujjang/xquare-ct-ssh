"""Relay configuration loading."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import yaml

_ART = (
    "██╗  ██╗   ██████╗   ██╗   ██╗   █████╗   ██████╗   ███████╗\r\n"
    "╚██╗██╔╝  ██╔═══██╗  ██║   ██║  ██╔══██╗  ██╔══██╗  ██╔════╝\r\n"
    " ╚███╔╝   ██║   ██║  ██║   ██║  ███████║  ██████╔╝  █████╗  \r\n"
    " ██╔██╗   ██║▄▄ ██║  ██║   ██║  ██╔══██║  ██╔══██╗  ██╔══╝  \r\n"
    "██╔╝ ██╗  ╚██████╔╝  ╚██████╔╝  ██║  ██║  ██║  ██║  ███████╗\r\n"
    "╚═╝  ╚═╝   ╚══▀▀═╝    ╚═════╝   ╚═╝  ╚═╝  ╚═╝  ╚═╝  ╚══════╝"
)

DEFAULT_BANNER = (
    "\r\n"
    "\x1b[1;36m" + _ART + "\x1b[0m\r\n"
    "\r\n"
    "\x1b[1mxquare Control Tower\x1b[0m \x1b[2m·\x1b[0m \x1b[2mSecure SSH Relay\x1b[0m\r\n"
    "\x1b[2mAuthorized operators only.\x1b[0m "
    "\x1b[2mType\x1b[0m \x1b[1mhelp\x1b[0m \x1b[2mfor commands.\x1b[0m\r\n"
    "\r\n"
)


@dataclass
class RelayConfig:
    ssh_host: str = "0.0.0.0"
    ssh_port: int = 2222
    ws_host: str = "0.0.0.0"
    ws_port: int = 8765
    ws_path: str = "/agent"
    host_key: str = "data/relay_host_key"
    authorized_keys: str | None = "data/authorized_keys"
    db_path: str = "data/relay.db"
    allow_anonymous: bool = False
    default_term: str = "xterm-256color"
    auth_timeout: float = 30.0
    ping_interval: float = 20.0
    ping_timeout: float = 20.0
    open_timeout: float = 15.0
    # install web server: serves the agent package and one-line installers
    web_enabled: bool = True
    web_host: str = "0.0.0.0"
    web_port: int = 1234
    # tarball served at /agent.tar.gz (contains the agent/ package)
    agent_package: str = "agent-dist.tar.gz"
    # host/IP placed in generated install URLs; falls back to the address the
    # operator connected to when empty
    advertise_host: str = ""
    log_level: str = "INFO"
    banner: str = DEFAULT_BANNER
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | None) -> "RelayConfig":
        data: dict[str, Any] = {}
        if path:
            with open(path, "r", encoding="utf-8") as handle:
                data = yaml.safe_load(handle) or {}
        relay = data.get("relay", {}) if isinstance(data, dict) else {}
        database = data.get("database", {}) if isinstance(data, dict) else {}
        logging = data.get("logging", {}) if isinstance(data, dict) else {}
        web = data.get("web", {}) if isinstance(data, dict) else {}

        config = cls()
        simple_fields = (
            "ssh_host",
            "ssh_port",
            "ws_host",
            "ws_port",
            "ws_path",
            "host_key",
            "authorized_keys",
            "allow_anonymous",
            "default_term",
            "auth_timeout",
            "ping_interval",
            "ping_timeout",
            "open_timeout",
            "advertise_host",
            "banner",
        )
        for name in simple_fields:
            if name in relay:
                setattr(config, name, relay[name])
        web_fields = ("web_enabled", "web_host", "web_port", "agent_package")
        for name in web_fields:
            if name in web:
                setattr(config, name, web[name])
        if "agent_package" in relay:  # allow either section
            config.agent_package = relay["agent_package"]
        if "path" in database:
            config.db_path = database["path"]
        if "level" in logging:
            config.log_level = logging["level"]

        # environment overrides (handy for containers)
        env_map = {
            "XQ_RELAY_SSH_HOST": "ssh_host",
            "XQ_RELAY_SSH_PORT": "ssh_port",
            "XQ_RELAY_WS_HOST": "ws_host",
            "XQ_RELAY_WS_PORT": "ws_port",
            "XQ_RELAY_HOST_KEY": "host_key",
            "XQ_RELAY_DB": "db_path",
            "XQ_RELAY_ALLOW_ANONYMOUS": "allow_anonymous",
            "XQ_RELAY_WEB_HOST": "web_host",
            "XQ_RELAY_WEB_PORT": "web_port",
            "XQ_RELAY_ADVERTISE_HOST": "advertise_host",
            "XQ_RELAY_AGENT_PACKAGE": "agent_package",
        }
        for env_name, field_name in env_map.items():
            if env_name in os.environ:
                raw = os.environ[env_name]
                current = getattr(config, field_name)
                if isinstance(current, bool):
                    setattr(config, field_name, raw.strip().lower() in ("1", "true", "yes", "on"))
                elif isinstance(current, int):
                    setattr(config, field_name, int(raw))
                else:
                    setattr(config, field_name, raw)
        return config
