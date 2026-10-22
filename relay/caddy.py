"""Caddy reverse-proxy integration for the relay (agent WebSocket only).

Caddy terminates TLS in front of the *agent* WebSocket and forwards to the
Python relay bound on loopback.  The install web server is intentionally left
alone (plain http on its own port), so only a single site block is rendered.

Deployment shape:

* the agent endpoint (``wss://<domain>/agent``) is served on port 443, so ACME
  **TLS-ALPN-01** can issue a publicly trusted certificate with no port 80.
* ``ws_host``/``ws_port`` describe the public endpoint; the Python server binds
  ``ws_bind_host``/``ws_bind_port`` instead.

TLS modes:

* ``acme``     - automatic public certificate via TLS-ALPN-01 (needs the site on
                 port 443 and the host's A record pointing at this server)
* ``internal`` - Caddy's local CA (self-signed; works offline)
* ``files``    - a certificate/key pair provided by the operator
* ``dns``      - an ACME DNS-01 challenge (requires a Caddy build that bundles
                 the selected DNS provider module)

The renderer is pure (no I/O) so it can be unit tested; :func:`main` is a small
CLI used by ``relay/setup.sh``.
"""

from __future__ import annotations

import argparse
import os
import sys

import yaml

TLS_MODES = ("acme", "internal", "files", "dns")

# Caddy would otherwise grab these for ACME / HTTP->HTTPS redirects.
_HTTP_PORT = 18080
_HTTPS_PORT = 18443


def _tls_directive(
    *,
    tls_mode: str,
    cert_file: str | None,
    key_file: str | None,
    dns_provider: str | None,
    dns_token: str | None,
) -> str:
    if tls_mode == "acme":
        return ""  # automatic HTTPS (TLS-ALPN-01); nothing to emit
    if tls_mode == "internal":
        return "tls internal"
    if tls_mode == "files":
        if not cert_file or not key_file:
            raise ValueError("tls-mode 'files' requires cert_file and key_file")
        return f"tls {cert_file} {key_file}"
    if tls_mode == "dns":
        if not dns_provider or not dns_token:
            raise ValueError("tls-mode 'dns' requires dns_provider and dns_token")
        token_env = "{env.XQ_CADDY_DNS_TOKEN}"
        return (
            "tls {\n"
            f"\t\tdns {dns_provider} {token_env}\n"
            "\t\tresolvers 1.1.1.1 8.8.8.8\n"
            "\t}"
        )
    raise ValueError(f"unknown tls mode: {tls_mode!r} (expected one of {', '.join(TLS_MODES)})")


def render_caddyfile(
    *,
    ws_host: str | None = None,
    domain: str | None = None,
    ws_port: int = 443,
    ws_bind_port: int = 18765,
    tls_mode: str = "acme",
    cert_file: str | None = None,
    key_file: str | None = None,
    acme_email: str | None = None,
    dns_provider: str | None = None,
    dns_token: str | None = None,
) -> str:
    ws_host = ws_host or domain
    if not ws_host:
        raise ValueError("a domain (or ws_host) is required for the TLS proxy")

    if tls_mode == "acme" and ws_port != 443:
        raise ValueError(
            f"tls-mode 'acme' (TLS-ALPN-01) requires the agent endpoint on port 443, got {ws_port}"
        )

    tls = _tls_directive(
        tls_mode=tls_mode,
        cert_file=cert_file,
        key_file=key_file,
        dns_provider=dns_provider,
        dns_token=dns_token,
    )

    if tls_mode == "acme":
        # ACME must answer on the standard 443, so do NOT relocate the ports;
        # only stop the automatic HTTP->HTTPS redirect from grabbing port 80.
        global_lines = ["\tauto_https disable_redirects"]
    else:
        global_lines = [f"\thttp_port {_HTTP_PORT}", f"\thttps_port {_HTTPS_PORT}"]
    if acme_email:
        global_lines.append(f"\temail {acme_email}")

    address = ws_host if ws_port == 443 else f"{ws_host}:{ws_port}"
    body = [address + " {"]
    if tls:
        for line in tls.splitlines():
            body.append("\t" + line)
    body.append(f"\treverse_proxy 127.0.0.1:{ws_bind_port}")
    body.append("}")

    return "{\n" + "\n".join(global_lines) + "\n}\n\n" + "\n".join(body) + "\n"


def patch_relay_config(path: str, updates: dict[str, dict]) -> None:
    """Deep-merge *updates* (``{section: {key: value}}``) into a relay YAML file."""
    data: dict = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        data = {}
    for section, values in updates.items():
        target = data.get(section)
        if not isinstance(target, dict):
            target = {}
            data[section] = target
        target.update(values)
    with open(path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(data, handle, default_flow_style=False, sort_keys=False)


def tls_config_updates(
    *,
    tls_mode: str,
    ws_bind_port: int,
    advertise_host: str,
    ws_port: int,
    ca_file: str = "",
) -> dict[str, dict]:
    return {
        "relay": {
            "advertise_host": advertise_host,
            "ws_port": ws_port,
            "ws_bind_host": "127.0.0.1",
            "ws_bind_port": ws_bind_port,
            "tls": True,
            "tls_verify": tls_mode != "internal",
            "tls_ca_file": ca_file,
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="relay.caddy",
        description="render a Caddyfile for the relay agent WebSocket and/or patch relay.yaml",
    )
    parser.add_argument("--domain", help="hostname for the agent WebSocket (wss)")
    parser.add_argument("--ws-host", help="alias of --domain")
    parser.add_argument("--ws-port", type=int, default=443, help="public wss port (default 443)")
    parser.add_argument(
        "--ws-bind-port", type=int, default=18765,
        help="loopback port the relay binds for caddy to proxy (default 18765)",
    )
    parser.add_argument("--tls-mode", default="acme", choices=TLS_MODES)
    parser.add_argument("--tls-cert", help="certificate file (tls-mode files)")
    parser.add_argument("--tls-key", help="key file (tls-mode files)")
    parser.add_argument("--tls-ca-file", default="", help="PEM bundle exposed to agents")
    parser.add_argument("--advertise-host", help="host placed in relay.yaml advertise_host")
    parser.add_argument("--acme-email", help="contact email for ACME")
    parser.add_argument("--dns-provider", help="DNS-01 provider name (tls-mode dns)")
    parser.add_argument("--dns-token", help="DNS-01 API token (tls-mode dns)")
    parser.add_argument(
        "--patch-config",
        metavar="PATH",
        help="merge the TLS settings into this relay.yaml instead of printing a Caddyfile",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ws_host = args.ws_host or args.domain

    if args.patch_config:
        if not ws_host:
            print("error: --patch-config requires --domain/--ws-host", file=sys.stderr)
            return 2
        patch_relay_config(
            args.patch_config,
            tls_config_updates(
                tls_mode=args.tls_mode,
                ws_bind_port=args.ws_bind_port,
                advertise_host=args.advertise_host or ws_host,
                ws_port=args.ws_port,
                ca_file=args.tls_ca_file,
            ),
        )
        return 0

    try:
        text = render_caddyfile(
            ws_host=ws_host,
            ws_port=args.ws_port,
            ws_bind_port=args.ws_bind_port,
            tls_mode=args.tls_mode,
            cert_file=args.tls_cert,
            key_file=args.tls_key,
            acme_email=args.acme_email,
            dns_provider=args.dns_provider,
            dns_token=args.dns_token,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
