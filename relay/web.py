"""Install web server (default port 1234).

Small, dependency-free HTTP server that makes provisioning an internal server a
single command.  It serves three things:

``GET /install/<server-id>?token=<agent-token>``
    A short bash bootstrap that downloads the agent package and runs its
    installer.  The token is validated against the registry first, so a link
    only works for the server it was minted for.

``GET /agent.tar.gz``
    The agent package (the ``agent/`` folder).  Not secret - the token in the
    install URL is what authorises registration.

``GET /health``
    Liveness probe.

The server runs in its own thread so it never interferes with the asyncio
relay loop.  Put it behind TLS (nginx/caddy) in production.
"""

from __future__ import annotations

import logging
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from relay.config import RelayConfig
from relay.db import RegistryDB

log = logging.getLogger("relay.web")

INSTALL_PATH = "/install/"
PACKAGE_PATH = "/agent.tar.gz"

_LANDING = b"""<!doctype html>
<html><head><meta charset="utf-8"><title>xquare Control Tower</title></head>
<body style="font-family:monospace;padding:2rem">
<h1>xquare Control Tower</h1>
<p>This host is an SSH relay. Internal agents install themselves with the
one-line command printed by <code>add-server</code> in the C2 CLI.</p>
<p>Endpoints: <code>/health</code>, <code>/agent.tar.gz</code>,
<code>/install/&lt;server-id&gt;?token=&lt;token&gt;</code></p>
</body></html>
"""

# The bootstrap is generated per request.  Placeholders use an unusual marker
# so they never collide with bash syntax.
_BOOTSTRAP = """#!/usr/bin/env bash
#
# xquare Control Tower agent installer (generated).
#   curl -fsSL '@@URL@@' | sudo bash
#
set -euo pipefail

XQ_RELAY_URL=@@RELAY_URL@@
XQ_SERVER_ID=@@SERVER_ID@@
XQ_AGENT_TOKEN=@@TOKEN@@
XQ_SHELL=''
XQ_PACKAGE_URL=@@PACKAGE_URL@@
XQ_INSTALL_DIR=@@INSTALL_DIR@@
export XQ_RELAY_URL XQ_SERVER_ID XQ_AGENT_TOKEN XQ_SHELL XQ_PACKAGE_URL XQ_INSTALL_DIR

if [ "$(id -u)" -ne 0 ] && [ "${XQ_ALLOW_NONROOT:-0}" != "1" ]; then
  echo "[xq] must run as root (use: curl ... | sudo bash)" >&2
  exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y >/dev/null
    apt-get install -y --no-install-recommends curl ca-certificates >/dev/null
  else
    echo "[xq] curl is required" >&2
    exit 1
  fi
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "[xq] downloading agent package..."
curl -fsSL "$XQ_PACKAGE_URL" -o "$TMP/agent.tar.gz"
tar -xzf "$TMP/agent.tar.gz" -C "$TMP"

if [ ! -f "$TMP/agent/setup.sh" ]; then
  echo "[xq] agent package is missing agent/setup.sh" >&2
  exit 1
fi

exec bash "$TMP/agent/setup.sh"
"""

DEFAULT_AGENT_DIR = "/opt/xquare-ct-ssh-agent"


def _sh_single(value: str) -> str:
    """Quote *value* for a single-quoted bash word."""
    return "'" + value.replace("'", "'\\''") + "'"


def _dq_escape(value: str) -> str:
    """Escape *value* for use inside a double-quoted bash string."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")


def _host_from_header(host_header: str) -> str:
    host_header = (host_header or "").strip()
    if host_header.startswith("["):
        return host_header.split("]", 1)[0] + "]"
    return host_header.split(":", 1)[0]


def _format_host(host: str) -> str:
    host = (host or "").strip()
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


class _InstallHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, db: RegistryDB, config: RelayConfig):
        super().__init__(address, handler)
        self.db = db
        self.config = config


class _InstallRequestHandler(BaseHTTPRequestHandler):
    server_version = "xq-install/1.0"
    protocol_version = "HTTP/1.1"

    # -- routing -----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/health":
            self._send_bytes(200, b"ok\n", "text/plain; charset=utf-8")
        elif path in ("/", "/index.html"):
            self._send_bytes(200, _LANDING, "text/html; charset=utf-8")
        elif path == PACKAGE_PATH:
            self._send_package()
        elif path.startswith(INSTALL_PATH):
            self._send_installer(path[len(INSTALL_PATH):], parsed.query)
        else:
            self._send_bytes(404, b"not found\n", "text/plain; charset=utf-8")

    def do_HEAD(self) -> None:  # noqa: N802 - http.server API
        self._send_bytes(200, b"", "text/plain; charset=utf-8")

    # -- handlers ----------------------------------------------------------
    def _send_package(self) -> None:
        config: RelayConfig = self.server.config
        try:
            with open(config.agent_package, "rb") as handle:
                data = handle.read()
        except OSError as exc:
            log.error("cannot read agent package %s: %s", config.agent_package, exc)
            self._send_bytes(404, b"agent package not available\n", "text/plain; charset=utf-8")
            return
        self._send_bytes(200, data, "application/gzip", filename="agent.tar.gz")

    def _send_installer(self, raw_name: str, query: str) -> None:
        db: RegistryDB = self.server.db
        config: RelayConfig = self.server.config
        name = urllib.parse.unquote(raw_name)
        params = urllib.parse.parse_qs(query)
        token = (params.get("token") or [""])[0]

        if not name or not token or not db.verify_agent_token(name, token):
            log.warning("rejected installer request for %r", name)
            self._send_bytes(403, b"invalid server id or token\n", "text/plain; charset=utf-8")
            return

        host = config.advertise_host or _host_from_header(self.headers.get("Host", ""))
        if not host:
            host = self.connection.getsockname()[0]
        display = _format_host(host)
        relay_url = f"ws://{display}:{config.ws_port}{config.ws_path}"
        package_url = f"http://{display}:{config.web_port}{PACKAGE_PATH}"
        url = f"http://{display}:{config.web_port}{INSTALL_PATH}{name}?token={token}"

        script = (
            _BOOTSTRAP
            .replace("@@RELAY_URL@@", _sh_single(relay_url))
            .replace("@@SERVER_ID@@", _sh_single(name))
            .replace("@@TOKEN@@", _sh_single(token))
            .replace("@@PACKAGE_URL@@", _sh_single(package_url))
            .replace(
                "@@INSTALL_DIR@@",
                '"${XQ_INSTALL_DIR:-' + _dq_escape(DEFAULT_AGENT_DIR) + '}"',
            )
            .replace("@@URL@@", url)
        )
        self._send_bytes(
            200,
            script.encode("utf-8"),
            "text/x-shellscript; charset=utf-8",
        )

    # -- helpers -----------------------------------------------------------
    def _send_bytes(self, status: int, body: bytes, content_type: str, filename: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - http.server API
        log.debug("web %s %s", self.address_string(), fmt % args)


class WebServer:
    def __init__(self, httpd: _InstallHTTPServer, thread: threading.Thread):
        self._httpd = httpd
        self._thread = thread

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    def close(self) -> None:
        try:
            self._httpd.shutdown()
        except Exception:  # pragma: no cover - best effort
            pass
        try:
            self._httpd.server_close()
        except Exception:  # pragma: no cover - best effort
            pass
        if self._thread.is_alive():
            self._thread.join(timeout=5)


def start_web_server(db: RegistryDB, config: RelayConfig) -> WebServer:
    httpd = _InstallHTTPServer(
        (config.web_host, config.web_port), _InstallRequestHandler, db, config
    )
    thread = threading.Thread(target=httpd.serve_forever, name="xq-install-web", daemon=True)
    thread.start()
    return WebServer(httpd, thread)
