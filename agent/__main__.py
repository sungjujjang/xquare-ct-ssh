"""Agent entry point: ``python -m agent --relay ... --id ... --token ...``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal

import yaml

from agent.agent import Agent, AgentConfig

log = logging.getLogger("agent")


def _load_config_file(path: str | None) -> dict:
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return data.get("agent", data) if isinstance(data, dict) else {}


def build_config(args: argparse.Namespace) -> AgentConfig:
    file_cfg = _load_config_file(args.config)

    def pick(cli_value, env_name, file_key, default=None):
        if cli_value is not None:
            return cli_value
        if env_name and env_name in os.environ:
            return os.environ[env_name]
        if file_key in file_cfg:
            return file_cfg[file_key]
        return default

    def pick_bool(cli_value, env_name, file_key, default):
        raw = pick(cli_value, env_name, file_key, None)
        if raw is None:
            return default
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on")

    relay_url = pick(args.relay, "XQ_RELAY_URL", "relay_url")
    server_id = pick(args.id, "XQ_SERVER_ID", "server_id")
    token = pick(args.token, "XQ_AGENT_TOKEN", "token")
    if not relay_url or not server_id or not token:
        raise SystemExit(
            "relay url, server id and token are required "
            "(use --relay/--id/--token or XQ_RELAY_URL/XQ_SERVER_ID/XQ_AGENT_TOKEN)"
        )

    env = dict(file_cfg.get("env") or {})
    return AgentConfig(
        relay_url=relay_url,
        server_id=server_id,
        token=token,
        shell=pick(args.shell, "XQ_SHELL", "shell"),
        cwd=pick(args.cwd, "XQ_CWD", "cwd"),
        verify_tls=pick_bool(args.verify_tls, "XQ_VERIFY_TLS", "verify_tls", True),
        ca_file=pick(args.ca_file, "XQ_CA_FILE", "ca_file"),
        env=env,
    )


async def amain(config: AgentConfig) -> None:
    agent = Agent(config)
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def _request_stop() -> None:
        agent.stop()
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:  # pragma: no cover - windows
            pass

    runner = asyncio.ensure_future(agent.run())
    stopper = asyncio.ensure_future(stop.wait())
    done, pending = await asyncio.wait(
        {runner, stopper}, return_when=asyncio.FIRST_COMPLETED
    )
    for task in pending:
        task.cancel()
    if runner in done:
        runner.result()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="xquare Control Tower internal agent")
    parser.add_argument("-c", "--config", help="path to a YAML config file")
    parser.add_argument("--relay", help="relay WebSocket URL, e.g. wss://relay:8765/agent")
    parser.add_argument("--id", help="server id (name) registered on the relay")
    parser.add_argument("--token", help="agent registration token")
    parser.add_argument("--shell", help="shell to run (default: platform shell)")
    parser.add_argument("--cwd", help="working directory for the shell")
    parser.add_argument(
        "--verify-tls",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="verify the relay TLS certificate for wss:// (default: verify)",
    )
    parser.add_argument("--ca-file", help="PEM bundle used to verify the relay TLS certificate")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    config = build_config(args)
    try:
        asyncio.run(amain(config))
    except KeyboardInterrupt:  # pragma: no cover
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
