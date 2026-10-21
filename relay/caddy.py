"""Caddy reverse-proxy integration for the relay.

Caddy terminates TLS for the two public relay ports (WebSocket and install web)
and forwards to the Python servers, which bind only on loopback.  We deliberately
avoid ports 80/443 so Caddy can coexist with other services (e.g. a WireGuard
dashboard); ACME HTTP-01/TLS-ALPN-01 are therefore unavailable, so certificates
come from one of:

* ``internal`` - Caddy's local CA (self-signed; works offline, needs no ports)
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

TLS_MODES = ("internal", "files", "dns")

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
    domain: str,
    ws_port: int,
    web_port: int,
    ws_bind_port: int,
    web_bind_port: int,
    tls_mode: str = "internal",
    cert_file: str | None = None,
    key_file: str | None = None,
    acme_email: str | None = None,
    dns_provider: str | None = None,
    dns_token: str | None = None,
) -> str:
    if not domain:
        raise ValueError("a domain is required for the TLS proxy")

    tls = _tls_directive(
        tls_mode=tls_mode,
        cert_file=cert_file,
        key_file=key_file,
        dns_provider=dns_provider,
        dns_token=dns_token,
    )

    global_lines = [
        f"\thttp_port {_HTTP_PORT}",
        f"\thttps_port {_HTTPS_PORT}",
        "\tauto_https disable_redirects",
    ]
    if acme_email:
        global_lines.append(f"\temail {acme_email}")

    return (
        "{\n"
        + "\n".join(global_lines)
        + "\n}\n\n"
        # Agent WebSocket (wss)
        f"{domain}:{ws_port} {{\n"
        f"\t{tls}\n"
        f"\treverse_proxy 127.0.0.1:{ws_bind_port}\n"
        "}\n\n"
        # Install web (https)
        f"{domain}:{web_port} {{\n"
        f"\t{tls}\n"
        f"\treverse_proxy 127.0.0.1:{web_bind_port}\n"
        "}\n"
    )


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
    web_bind_port: int,
    advertise_host: str,
    ca_file: str = "",
) -> dict[str, dict]:
    return {
        "relay": {
            "advertise_host": advertise_host,
            "ws_bind_host": "127.0.0.1",
            "ws_bind_port": ws_bind_port,
            "tls": True,
            "tls_verify": tls_mode != "internal",
            "tls_ca_file": ca_file,
        },
        "web": {
            "web_bind_host": "127.0.0.1",
            "web_bind_port": web_bind_port,
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="render a Caddyfile for the xquare relay (TLS on the public relay ports)"
    )
    parser.add_argument("--domain", required=True)
    parser.add_argument("--ws-port", type=int, required=True)
    parser.add_argument("--web-port", type=int, required=True)
    parser.add_argument("--ws-bind-port", type=int, required=True)
    parser.add_argument("--web-bind-port", type=int, required=True)
    parser.add_argument("--tls-mode", choices=TLS_MODES, default="internal")
    parser.add_argument("--tls-cert")
    parser.add_argument("--tls-key")
    parser.add_argument("--tls-ca-file", default="")
    parser.add_argument("--advertise-host")
    parser.add_argument("--acme-email")
    parser.add_argument("--dns-provider")
    parser.add_argument("--dns-token")
    parser.add_argument("--patch-config", help="merge TLS settings into this relay YAML file")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.patch_config:
        patch_relay_config(
            args.patch_config,
            tls_config_updates(
                tls_mode=args.tls_mode,
                ws_bind_port=args.ws_bind_port,
                web_bind_port=args.web_bind_port,
                advertise_host=args.advertise_host or args.domain,
                ca_file=args.tls_ca_file,
            ),
        )
        return 0

    try:
        text = render_caddyfile(
            domain=args.domain,
            ws_port=args.ws_port,
            web_port=args.web_port,
            ws_bind_port=args.ws_bind_port,
            web_bind_port=args.web_bind_port,
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
