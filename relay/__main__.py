"""Relay entry point: ``python -m relay --config config.yaml``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal

from relay.config import RelayConfig
from relay.db import RegistryDB
from relay.logs import install as install_log_buffer
from relay.registry import Registry
from relay.ssh_server import start_ssh_server
from relay.web import start_web_server
from relay.ws_server import start_ws_server

log = logging.getLogger("relay")


async def amain(config: RelayConfig) -> None:
    db = RegistryDB(config.db_path)
    db.init_schema()

    registry = Registry()
    ssh_listener = await start_ssh_server(db, registry, config)
    ws_server = await start_ws_server(db, registry, config)

    web_server = None
    if config.web_enabled:
        web_server = start_web_server(db, config)

    log.info("SSH server listening on %s:%s", config.ssh_host, config.ssh_port)
    log.info(
        "Agent WebSocket listening on ws://%s:%s%s",
        config.ws_host,
        config.ws_port,
        config.ws_path,
    )
    if web_server is not None:
        log.info(
            "install server listening on http://%s:%s (advertise_host=%r)",
            config.web_host,
            config.web_port,
            config.advertise_host or "<auto>",
        )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - windows
            pass
    await stop.wait()

    log.info("shutting down")
    await registry.shutdown()
    ssh_listener.close()
    await ssh_listener.wait_closed()
    ws_server.close()
    await ws_server.wait_closed()
    if web_server is not None:
        web_server.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="xquare Control Tower relay server")
    parser.add_argument("-c", "--config", help="path to a YAML config file")
    parser.add_argument("--ssh-port", type=int, help="override SSH port")
    parser.add_argument("--ws-port", type=int, help="override agent WebSocket port")
    parser.add_argument("--web-port", type=int, help="override install web port (default 1234)")
    parser.add_argument("--advertise-host", help="public host used in generated install URLs")
    parser.add_argument("--no-web", action="store_true", help="disable the install web server")
    parser.add_argument("--log-level", default=None)
    args = parser.parse_args(argv)

    config = RelayConfig.load(args.config)
    if args.ssh_port:
        config.ssh_port = args.ssh_port
    if args.ws_port:
        config.ws_port = args.ws_port
    if args.web_port:
        config.web_port = args.web_port
    if args.advertise_host:
        config.advertise_host = args.advertise_host
    if args.no_web:
        config.web_enabled = False
    if args.log_level:
        config.log_level = args.log_level

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    install_log_buffer(logging.DEBUG if config.log_level.lower() == "debug" else logging.INFO)
    try:
        asyncio.run(amain(config))
    except KeyboardInterrupt:  # pragma: no cover
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
