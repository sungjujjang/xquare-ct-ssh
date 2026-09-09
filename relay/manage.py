"""Registry management CLI.

Examples::

    python -m relay.manage init
    python -m relay.manage add-user alice            # prompts for a password
    python -m relay.manage add-user alice --password s3cret
    python -m relay.manage add-server server-001 --password op-pass --advertise-host relay.example.com
    python -m relay.manage list
    python -m relay.manage disable server-001
    python -m relay.manage remove-server server-001
    python -m relay.manage users
    python -m relay.manage remove-user alice
    python -m relay.manage reset-token server-001
    python -m relay.manage set-password server-001       # change a server login password
    python -m relay.manage set-user-password alice       # change an operator password
"""

from __future__ import annotations

import argparse
import getpass
import sys

from relay.config import RelayConfig
from relay.db import RegistryDB


def _format_host(host: str) -> str:
    host = (host or "").strip()
    if not host:
        return ""
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def install_command(config: RelayConfig, name: str, token: str, advertise_host: str | None) -> str | None:
    """Return the one-line ``curl ... | sudo bash`` installer, or None if web is off."""
    if not config.web_enabled:
        return None
    host = _format_host(advertise_host or config.advertise_host)
    if not host:
        return None
    return (
        f"curl -fsSL 'http://{host}:{config.web_port}/install/{name}"
        f"?token={token}' | sudo bash"
    )


def agent_command(config: RelayConfig, name: str, token: str, advertise_host: str | None, shell: str | None) -> str:
    host = _format_host(advertise_host or config.advertise_host) or "<relay-host>"
    command = (
        f"python -m agent --relay ws://{host}:{config.ws_port}{config.ws_path} "
        f"--id {name} --token {token}"
    )
    if shell:
        command += f" --shell {shell}"
    return command


def _prompt_password(prompt: str) -> str:
    first = getpass.getpass(prompt)
    second = getpass.getpass("Repeat: ")
    if first != second:
        raise SystemExit("passwords do not match")
    if not first:
        raise SystemExit("password must not be empty")
    return first


def _open_db(args: argparse.Namespace) -> RegistryDB:
    config = RelayConfig.load(args.config)
    db = RegistryDB(config.db_path)
    db.init_schema()
    return db


def cmd_init(args: argparse.Namespace) -> int:
    db = _open_db(args)
    print(f"initialized database at {db.path}")
    return 0


def cmd_add_user(args: argparse.Namespace) -> int:
    db = _open_db(args)
    password = args.password or _prompt_password(f"Password for SSH user {args.username}: ")
    db.add_relay_user(args.username, password)
    print(f"relay user '{args.username}' saved")
    return 0


def cmd_add_server(args: argparse.Namespace) -> int:
    config = RelayConfig.load(args.config)
    if args.advertise_host:
        config.advertise_host = args.advertise_host
    if args.web_port:
        config.web_port = args.web_port
    if args.ws_port:
        config.ws_port = args.ws_port
    db = RegistryDB(config.db_path)
    db.init_schema()

    password = args.password or _prompt_password(f"Login password for server {args.name}: ")
    token = db.add_server(
        args.name,
        password,
        description=args.description or "",
        token=args.token,
    )
    print(f"server '{args.name}' saved")
    print()
    print("Agent token (shown once - keep it secret):")
    print(f"  {token}")
    print()

    installer = install_command(config, args.name, token, args.advertise_host)
    if installer:
        print("On the internal server, run this one-liner (as root):")
        print()
        print(f"  {installer}")
        print()
        print("It installs the agent, starts it now, and enables it on boot.")
    else:
        if config.web_enabled and not (args.advertise_host or config.advertise_host):
            print("(pass --advertise-host to also print a one-line installer URL)")
        print("Run the agent on the internal server with:")
        print(f"  {agent_command(config, args.name, token, args.advertise_host, args.shell)}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    db = _open_db(args)
    servers = db.list_servers()
    if not servers:
        print("no servers registered")
        return 0
    width = max(len(s.name) for s in servers) + 2
    print(f"{'SERVER':<{width}} {'ENABLED':<8} DESCRIPTION")
    for server in servers:
        print(f"{server.name:<{width}} {str(server.enabled):<8} {server.description}")
    return 0


def cmd_set_enabled(args: argparse.Namespace, enabled: bool) -> int:
    db = _open_db(args)
    if not db.set_server_enabled(args.name, enabled):
        print(f"server '{args.name}' not found", file=sys.stderr)
        return 1
    print(f"server '{args.name}' {'enabled' if enabled else 'disabled'}")
    return 0


def cmd_remove_server(args: argparse.Namespace) -> int:
    db = _open_db(args)
    if not db.remove_server(args.name):
        print(f"server '{args.name}' not found", file=sys.stderr)
        return 1
    print(f"server '{args.name}' removed")
    return 0


def cmd_list_users(args: argparse.Namespace) -> int:
    db = _open_db(args)
    users = db.list_relay_users()
    if not users:
        print("no relay users registered")
        return 0
    for user in users:
        print(user)
    return 0


def cmd_remove_user(args: argparse.Namespace) -> int:
    db = _open_db(args)
    if not db.remove_relay_user(args.username):
        print(f"relay user '{args.username}' not found", file=sys.stderr)
        return 1
    print(f"relay user '{args.username}' removed")
    return 0


def cmd_set_server_password(args: argparse.Namespace) -> int:
    db = _open_db(args)
def cmd_reset_token(args: argparse.Namespace) -> int:
    config = RelayConfig.load(args.config)
    if args.advertise_host:
        config.advertise_host = args.advertise_host
    if args.web_port:
        config.web_port = args.web_port
    if args.ws_port:
        config.ws_port = args.ws_port
    db = RegistryDB(config.db_path)
    db.init_schema()
    token = db.rotate_server_token(args.name, token=args.token)
    if token is None:
        print(f"server '{args.name}' not found", file=sys.stderr)
        return 1
    print(f"new agent token for '{args.name}' (shown once):")
    print(f"  {token}")
    installer = install_command(config, args.name, token, args.advertise_host)
    if installer:
        print()
        print("Reinstall the agent on the server with:")
        print(f"  {installer}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="xquare Control Tower registry manager")
    parser.add_argument("-c", "--config", help="path to a YAML config file")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the database schema").set_defaults(func=cmd_init)

    p_user = sub.add_parser("add-user", help="create/update a relay SSH user")
    p_user.add_argument("username")
    p_user.add_argument("--password")
    p_user.set_defaults(func=cmd_add_user)

    p_server = sub.add_parser("add-server", help="create/update an internal server")
    p_server.add_argument("name")
    p_server.add_argument("--password", help="C2 login password (prompted if omitted)")
    p_server.add_argument("--description", default="")
    p_server.add_argument("--token", help="reuse an existing agent token")
    p_server.add_argument("--advertise-host", help="public host for generated install URLs")
    p_server.add_argument("--web-port", type=int, help="install web port (default: from config/1234)")
    p_server.add_argument("--ws-port", type=int, help="agent WebSocket port (default: from config)")
    p_server.add_argument("--shell", help="shell the agent should run")
    p_server.set_defaults(func=cmd_add_server)

    sub.add_parser("list", help="list internal servers").set_defaults(func=cmd_list)

    p_enable = sub.add_parser("enable", help="enable a server")
    p_enable.add_argument("name")
    p_enable.set_defaults(func=lambda a: cmd_set_enabled(a, True))

    p_disable = sub.add_parser("disable", help="disable a server")
    p_disable.add_argument("name")
    p_disable.set_defaults(func=lambda a: cmd_set_enabled(a, False))

    p_remove = sub.add_parser("remove-server", help="delete a server")
    p_remove.add_argument("name")
    p_remove.set_defaults(func=cmd_remove_server)

    sub.add_parser("users", help="list relay SSH users").set_defaults(func=cmd_list_users)

    p_rmuser = sub.add_parser("remove-user", help="delete a relay SSH user")
    p_rmuser.add_argument("username")
    p_rmuser.set_defaults(func=cmd_remove_user)

    p_token = sub.add_parser("reset-token", help="rotate a server's agent token")
    p_token.add_argument("name")
    p_token.add_argument("--token", help="explicit new token (default: random)")
    p_token.add_argument("--advertise-host", help="public host for the installer URL")
    p_token.add_argument("--web-port", type=int)
    p_token.add_argument("--ws-port", type=int)
    p_token.set_defaults(func=cmd_reset_token)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
