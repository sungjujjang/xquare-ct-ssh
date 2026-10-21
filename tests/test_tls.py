"""Tests for TLS/Caddy integration: config schemes, Caddyfile rendering, URLs."""

import yaml

from agent.agent import Agent, AgentConfig
from relay.caddy import patch_relay_config, render_caddyfile, tls_config_updates
from relay.config import RelayConfig
from relay.manage import agent_command, install_command


def test_scheme_defaults_are_plain() -> None:
    config = RelayConfig()
    assert config.tls is False
    assert config.ws_scheme == "ws"
    assert config.http_scheme == "http"


def test_scheme_switches_with_tls() -> None:
    config = RelayConfig(tls=True)
    assert config.ws_scheme == "wss"
    assert config.http_scheme == "https"


def test_bind_helpers_fall_back_to_public() -> None:
    config = RelayConfig(ws_host="0.0.0.0", ws_port=8765, web_host="0.0.0.0", web_port=1234)
    assert config.ws_listen_host == "0.0.0.0"
    assert config.ws_listen_port == 8765
    assert config.web_listen_port == 1234


def test_bind_helpers_prefer_bind_ports() -> None:
    config = RelayConfig(
        ws_host="0.0.0.0", ws_port=8765, ws_bind_host="127.0.0.1", ws_bind_port=18765,
        web_bind_host="127.0.0.1", web_bind_port=11234,
    )
    assert config.ws_listen_host == "127.0.0.1"
    assert config.ws_listen_port == 18765
    assert config.web_listen_host == "127.0.0.1"
    assert config.web_listen_port == 11234


def test_config_loads_tls_fields(tmp_path) -> None:
    path = tmp_path / "relay.yaml"
    path.write_text(
        "relay:\n"
        "  tls: true\n"
        "  tls_verify: false\n"
        "  tls_ca_file: /etc/xquare-ct-ssh/ca.crt\n"
        "  ws_bind_port: 18765\n"
        "web:\n"
        "  web_bind_port: 11234\n",
        encoding="utf-8",
    )
    config = RelayConfig.load(str(path))
    assert config.tls is True
    assert config.tls_verify is False
    assert config.tls_ca_file == "/etc/xquare-ct-ssh/ca.crt"
    assert config.ws_listen_port == 18765
    assert config.web_listen_port == 11234


def test_tls_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv("XQ_RELAY_TLS", "true")
    monkeypatch.setenv("XQ_RELAY_TLS_VERIFY", "0")
    config = RelayConfig.load(None)
    assert config.tls is True
    assert config.tls_verify is False


def test_install_and_agent_commands_use_tls() -> None:
    config = RelayConfig(
        tls=True, tls_verify=False, advertise_host="relay.example.com", web_port=1234
    )
    installer = install_command(config, "server-001", "xq_tok", None)
    assert installer is not None
    assert "https://relay.example.com:1234" in installer
    assert "curl -k " in installer

    agent = agent_command(config, "server-001", "xq_tok", None, None)
    assert "wss://relay.example.com:8765/agent" in agent
    assert "--no-verify-tls" in agent


def test_install_command_plain_when_not_tls() -> None:
    config = RelayConfig(advertise_host="relay.example.com", web_port=1234)
    installer = install_command(config, "server-001", "xq_tok", None)
    assert installer is not None
    assert installer.startswith("curl -fsSL 'http://")
    assert " -k " not in installer


def test_render_caddyfile_internal_avoids_privileged_ports() -> None:
    text = render_caddyfile(
        domain="relay.example.com",
        ws_port=8765,
        web_port=1234,
        ws_bind_port=18765,
        web_bind_port=11234,
        tls_mode="internal",
    )
    assert "http_port 18080" in text
    assert "https_port 18443" in text
    assert "relay.example.com:8765" in text
    assert "relay.example.com:1234" in text
    assert "tls internal" in text
    assert "reverse_proxy 127.0.0.1:18765" in text
    assert "reverse_proxy 127.0.0.1:11234" in text
    # never touch the privileged ports
    assert ":80" not in text
    assert ":443" not in text
    assert "0.0.0.0:80 " not in text


def test_render_caddyfile_files_requires_cert() -> None:
    import pytest

    with pytest.raises(ValueError):
        render_caddyfile(
            domain="d", ws_port=8765, web_port=1234, ws_bind_port=1, web_bind_port=2,
            tls_mode="files",
        )


def test_render_caddyfile_dns() -> None:
    text = render_caddyfile(
        domain="relay.example.com", ws_port=8765, web_port=1234,
        ws_bind_port=18765, web_bind_port=11234, tls_mode="dns",
        dns_provider="cloudflare", dns_token="secret", acme_email="a@b.c",
    )
    assert "email a@b.c" in text
    assert "dns cloudflare {env.XQ_CADDY_DNS_TOKEN}" in text


def test_patch_relay_config_merges_and_preserves(tmp_path) -> None:
    path = tmp_path / "relay.yaml"
    path.write_text("relay:\n  ssh_port: 2222\nweb:\n  web_port: 1234\n", encoding="utf-8")
    patch_relay_config(
        str(path),
        tls_config_updates(
            tls_mode="internal",
            ws_bind_port=18765,
            web_bind_port=11234,
            advertise_host="relay.example.com",
            ca_file="/etc/xquare-ct-ssh/caddy-root.crt",
        ),
    )
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["relay"]["ssh_port"] == 2222  # preserved
    assert data["relay"]["tls"] is True
    assert data["relay"]["tls_verify"] is False
    assert data["relay"]["advertise_host"] == "relay.example.com"
    assert data["relay"]["ws_bind_port"] == 18765
    assert data["web"]["web_port"] == 1234  # preserved
    assert data["web"]["web_bind_port"] == 11234

    # round-trips through RelayConfig
    config = RelayConfig.load(str(path))
    assert config.tls is True
    assert config.ws_listen_port == 18765
    assert config.http_scheme == "https"


def test_agent_ssl_context_for_wss(tmp_path) -> None:
    ca = tmp_path / "ca.crt"
    ca.write_text("dummy")
    agent = Agent(AgentConfig(relay_url="wss://relay/agent", server_id="s", token="t"))
    ctx = agent._ssl_context()
    assert ctx is not None
    assert ctx.check_hostname is True

    insecure = Agent(
        AgentConfig(relay_url="wss://relay/agent", server_id="s", token="t", verify_tls=False)
    )
    ictx = insecure._ssl_context()
    assert ictx is not None
    assert ictx.check_hostname is False
    assert ictx.verify_mode.name == "CERT_NONE"


def test_agent_ssl_context_none_for_ws() -> None:
    agent = Agent(AgentConfig(relay_url="ws://relay/agent", server_id="s", token="t"))
    assert agent._ssl_context() is None
