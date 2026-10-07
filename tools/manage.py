"""Registry management CLI.

Examples::

    python -m tools.manage init
    python -m tools.manage add-user alice            # prompts for a password
    python -m tools.manage add-user alice --password s3cret
    python -m tools.manage add-server server-001 --password op-pass
    python -m tools.manage list
    python -m tools.manage disable server-001
    python -m tools.manage remove-server server-001
"""

from __future__ import annotations

import argparse
import getpass
import sys

from relay.config import RelayConfig
from relay.db import RegistryDB


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
    db = _open_db(args)
    password = args.password or _prompt_password(f"Login password for server {args.name}: ")
    token = db.add_server(
        args.name,
        password,
        description=args.description or "",
        token=args.token,
    )
    print(f"server '{args.name}' saved")
    print()
    print("Agent token (shown once - store it on the internal server):")
    print(f"  {token}")
    print()
    print("Run the agent with:")
    print(
        f"  python -m agent --relay ws://<relay-host>:8765/agent "
        f"--id {args.name} --token {token}"
    )
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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
