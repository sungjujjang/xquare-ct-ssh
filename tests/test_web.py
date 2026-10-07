"""Tests for the install web server (relay/web.py)."""

import os
import tempfile
import urllib.error
import urllib.request

from relay.config import RelayConfig
from relay.db import RegistryDB
from relay.web import start_web_server


def _get(url: str):
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_install_endpoints():
    tmp = tempfile.mkdtemp(prefix="xq-web-")
    package = os.path.join(tmp, "agent-dist.tar.gz")
    with open(package, "wb") as handle:
        handle.write(b"FAKE_TARBALL")

    config = RelayConfig()
    config.db_path = os.path.join(tmp, "relay.db")
    config.web_host = "127.0.0.1"
    config.web_port = 0
    config.ws_port = 8765
    config.ws_path = "/agent"
    config.advertise_host = "relay.example.com"
    config.agent_package = package

    db = RegistryDB(config.db_path)
    db.init_schema()
    db.add_server("server-001", "opsecret", token="testtoken")

    server = start_web_server(db, config)
    base = f"http://127.0.0.1:{server.port}"
    try:
        status, body = _get(f"{base}/health")
        assert status == 200 and body == b"ok\n"

        status, body = _get(f"{base}/agent.tar.gz")
        assert status == 200 and body == b"FAKE_TARBALL"

        status, body = _get(f"{base}/install/server-001?token=testtoken")
        assert status == 200
        script = body.decode()
        assert "XQ_SERVER_ID='server-001'" in script
        assert "XQ_AGENT_TOKEN='testtoken'" in script
        assert "ws://relay.example.com:8765/agent" in script
        assert "http://relay.example.com:" in script

        status, _ = _get(f"{base}/install/server-001?token=wrong")
        assert status == 403

        status, _ = _get(f"{base}/install/unknown?token=testtoken")
        assert status == 403

        status, _ = _get(f"{base}/nope")
        assert status == 404
    finally:
        server.close()
