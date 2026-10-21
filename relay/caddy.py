"""Caddy reverse-proxy integration for the relay.

Caddy terminates TLS in front of the agent WebSocket and the install web server,
forwarding to the Python servers which bind only on loopback.

Two deployment shapes are supported:

* **Shared port** (default): one domain, two public ports (e.g. ``:8765`` for
  wss and ``:1234`` for https).  Because neither 80 nor 443 is used, automatic
  ACME issuance is impossible and certificates come from ``internal``/``files``/
  ``dns``.
* **Per-service host** (``--ws-host``/``--web-host``): each service gets its own
  host; if that host is served on port 443, ACME **TLS-ALPN-01** works and a
  publicly trusted certificate is issued automatically (no port 80 needed).
  Typically ``web_host=xquare.xyz`` and ``ws_host=tunnel.xquare.xyz``, both on 443.

TLS modes:

* ``acme``     - automatic public certificate via TLS-ALPN-01 (needs the service
                 on port 443 and the host's A record pointing at this server)
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


def _site(
    *,
    host: str,
    port: int,
    bind_port: int,
    tls: str,
) -> str:
    """Render one Caddy site block (``host:port`` -> ``127.0.0.1:bind_port``)."""
    address = host if port == 443 else f"{host}:{port}"
    body = [address + " {"]
    if tls:
        for line in tls.splitlines():
            body.append("\t" + line)
    body.append(f"\treverse_proxy 127.0.0.1:{bind_port}")
    body.append("}")
    return "\n".join(body)


def render_caddyfile(
    *,
    ws_host: str | None = None,
    web_host: str | None = None,
    domain: str | None = None,
    ws_port: int = 8765,
    web_port: int = 1234,
    ws_bind_port: int = 18765,
    web_bind_port: int = 11234,
    tls_mode: str = "internal",
    cert_file: str | None = None,
    key_file: str | None = None,
    acme_email: str | None = None,
    dns_provider: str | None = None,
    dns_token: str | None = None,
) -> str:
    web_host = web_host or domain
    ws_host = ws_host or domain
    if not ws_host or not web_host:
        raise ValueError("a domain (or ws_host/web_host) is required for the TLS proxy")

    if tls_mode == "acme":
        for host, port, kind in ((ws_host, ws_port, "ws"), (web_host, web_port, "web")):
            if port != 443:
                raise ValueError(
                    f"tls-mode 'acme' (TLS-ALPN-01) requires {kind} on port 443, got {port}"
                )

    tls = _tls_directive(
        tls_mode=tls_mode,
        cert_file=cert_file,
        key_file=key_file,
        dns_provider=dns_provider,
        dns_token=dns_token,
    )

    global_lines = [f"\thttp_port {_HTTP_PORT}", f"\thttps_port {_HTTPS_PORT}"]
    if tls_mode == "acme":
        # ACME (TLS-ALPN-01) must answer on the standard 443 (and http-01 on 80),
        # so do NOT relocate the ports; only stop the automatic HTTP->HTTPS
        # redirect from grabbing port 80.
        global_lines = ["\tauto_https disable_redirects"]
    if acme_email:
        global_lines.append(f"\temail {acme_email}")

    sites = [
        _site(host=ws_host, port=ws_port, bind_port=ws_bind_port, tls=tls),
        _site(host=web_host, port=web_port, bind_port=web_bind_port, tls=tls),
    ]
    return (
        "{\n"
        + "\n".join(global_lines)
        + "\n}\n\n"
        + "\n\n".join(sites)
        + "\n"
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
    ws_port: int,
    web_port: int,
    web_advertise_host: str = "",
    ca_file: str = "",
) -> dict[str, dict]:
    relay_updates = {
        "advertise_host": advertise_host,
        "ws_port": ws_port,
        "ws_bind_host": "127.0.0.1",
        "ws_bind_port": ws_bind_port,
        "tls": True,
        "tls_verify": tls_mode != "internal",
        "tls_ca_file": ca_file,
    }
    if web_advertise_host:
        relay_updates["web_advertise_host"] = web_advertise_host
    return {
        "relay": relay_updates,
        "web": {
            "web_port": web_port,
            "web_bind_host": "127.0.0.1",
            "web_bind_port": web_bind_port,
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="render a Caddyfile for the xquare relay (TLS in front of wss/https)"
    )
    parser.add_argument("--domain", help="host for both services (shared-port mode)")
    parser.add_argument("--ws-host", help="host for the agent WebSocket (wss)")
    parser.add_argument("--web-host", help="host for the install web server (https)")
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
    ws_host = args.ws_host or args.domain
    web_host = args.web_host or args.domain

    if args.patch_config:
        patch_relay_config(
            args.patch_config,
            tls_config_updates(
                tls_mode=args.tls_mode,
                ws_bind_port=args.ws_bind_port,
                web_bind_port=args.web_bind_port,
                ws_port=args.ws_port,
                web_port=args.web_port,
                advertise_host=args.advertise_host or ws_host or "",
                web_advertise_host=web_host or "",
                ca_file=args.tls_ca_file,
            ),
        )
        return 0

    try:
        text = render_caddyfile(
            ws_host=ws_host,
            web_host=web_host,
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
