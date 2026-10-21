#!/usr/bin/env bash
#
# setup-relay.sh - install the xquare Control Tower SSH *relay server* on Ubuntu.
#
# Creates a Python virtualenv, writes a config file, initialises the SQLite
# registry and installs a systemd service. Safe to re-run (idempotent).
#
#   sudo ./relay/setup.sh --admin-user alice
#   sudo ./relay/setup.sh --ssh-port 2222 --ws-port 8765 --admin-user alice --admin-password s3cret
#
set -euo pipefail

# --------------------------------------------------------------------------- #
# defaults
# --------------------------------------------------------------------------- #
SSH_HOST="0.0.0.0"
SSH_PORT="2222"
WS_HOST="0.0.0.0"
WS_PORT="8765"
WEB_HOST="0.0.0.0"
WEB_PORT="1234"
ADVERTISE_HOST=""
INSTALL_DIR="/opt/xquare-ct-ssh-relay"
CONFIG_DIR="/etc/xquare-ct-ssh"
DATA_DIR="/var/lib/xquare-ct-ssh"
SERVICE_NAME="xq-relay"
SERVICE_USER="root"
ADMIN_USER=""
ADMIN_PASSWORD=""
INSTALL_SERVICE=1
SKIP_DEPS=0
ALLOW_NONROOT=0

# --- TLS (Caddy) ----------------------------------------------------------- #
# Set --domain (shared port) or --ws-domain/--web-domain (per-service host) to
# put Caddy in front of the relay and terminate TLS.  With per-service hosts on
# port 443, ACME TLS-ALPN-01 issues a public certificate automatically (the
# Python servers move to loopback-only bind ports so nothing else is exposed).
DOMAIN=""
WS_DOMAIN=""
WEB_DOMAIN=""
TLS_MODE="internal"          # acme | internal | files | dns
TLS_CERT=""
TLS_KEY=""
TLS_CA_FILE=""
ACME_EMAIL=""
DNS_PROVIDER=""
DNS_TOKEN=""
WS_BIND_HOST="127.0.0.1"
WS_BIND_PORT=""
WEB_BIND_HOST="127.0.0.1"
WEB_BIND_PORT=""
# Public TLS listen ports (only used with --domain/--ws-domain/--web-domain).
WS_PUBLIC_PORT=""
WEB_PUBLIC_PORT=""
CADDY_VERSION="${XQ_CADDY_VERSION:-2.9.1}"
CADDYFILE="$CONFIG_DIR/Caddyfile"
CADDY_ENV_FILE="$CONFIG_DIR/caddy.env"
CADDY_DATA_DIR="/var/lib/xquare-ct-ssh/caddy-data"
CADDY_SERVICE="xq-caddy"
CADDY_CERT=""
CADDY_KEY=""
TLS_ENABLED=0

# --------------------------------------------------------------------------- #
# pretty logging
# --------------------------------------------------------------------------- #
C_RESET=$'\033[0m'; C_INFO=$'\033[1;34m'; C_OK=$'\033[1;32m'; C_WARN=$'\033[1;33m'; C_ERR=$'\033[1;31m'
log()  { printf '%s[relay]%s %s\n' "$C_INFO" "$C_RESET" "$*"; }
ok()   { printf '%s[ ok ]%s %s\n'  "$C_OK"   "$C_RESET" "$*"; }
warn() { printf '%s[warn]%s %s\n'  "$C_WARN" "$C_RESET" "$*" >&2; }
die()  { printf '%s[fail]%s %s\n'  "$C_ERR"  "$C_RESET" "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage: sudo ./relay/setup.sh [options]

Options:
  --ssh-host HOST        SSH bind address          (default: 0.0.0.0)
  --ssh-port PORT        SSH port                  (default: 2222)
  --ws-host HOST         Agent WebSocket bind      (default: 0.0.0.0)
  --ws-port PORT         Agent WebSocket port      (default: 8765)
  --web-host HOST        installer web bind        (default: 0.0.0.0)
  --web-port PORT        installer web port        (default: 1234)
  --advertise-host HOST  public host for install URLs (default: auto / --domain)
  --domain DOMAIN        Caddy TLS, both services on this host (shared port)
  --ws-domain DOMAIN     Caddy TLS host for the agent WebSocket (wss)
  --web-domain DOMAIN    Caddy TLS host for the install web server (https)
  --tls-mode MODE        acme | internal | files | dns   (default: internal)
  --acme-email EMAIL     ACME account email (acme/dns)
  --tls-cert PATH        certificate (PEM) for --tls-mode files
  --tls-key PATH         private key (PEM) for --tls-mode files
  --dns-provider NAME    DNS-01 provider module for --tls-mode dns (e.g. cloudflare)
  --dns-token TOKEN      DNS provider API token for --tls-mode dns
  --ws-tls-port PORT     public wss port when using --ws-domain (default: ws+10000)
  --web-tls-port PORT    public https port when using --web-domain (default: 443)
  --ws-bind-port PORT    loopback port for the agent WS backend (default: ws+10000)
  --web-bind-port PORT   loopback port for the install web backend (default: web+10000)
  --install-dir DIR      application install dir   (default: /opt/xquare-ct-ssh-relay)
  --config-dir DIR       config dir                (default: /etc/xquare-ct-ssh)
  --data-dir DIR         database / host key dir   (default: /var/lib/xquare-ct-ssh)
  --service-name NAME    systemd unit name         (default: xq-relay)
  --service-user USER    run the service as USER   (default: root)
  --admin-user NAME      create this relay SSH user
  --admin-password PASS  password for --admin-user (omit to be prompted)
  --no-service           do not install/start systemd service
  --skip-deps            skip apt-get package installation
  --allow-nonroot        install into a user-writable dir without root
  -h, --help             show this help
EOF
}

# --------------------------------------------------------------------------- #
# argument parsing
# --------------------------------------------------------------------------- #
while [ $# -gt 0 ]; do
  case "$1" in
    --ssh-host)        SSH_HOST="${2:?}"; shift 2 ;;
    --ssh-port)        SSH_PORT="${2:?}"; shift 2 ;;
    --ws-host)         WS_HOST="${2:?}"; shift 2 ;;
    --ws-port)         WS_PORT="${2:?}"; shift 2 ;;
    --web-host)        WEB_HOST="${2:?}"; shift 2 ;;
    --web-port)        WEB_PORT="${2:?}"; shift 2 ;;
    --advertise-host)  ADVERTISE_HOST="${2:?}"; shift 2 ;;
    --domain)          DOMAIN="${2:?}"; shift 2 ;;
    --ws-domain)       WS_DOMAIN="${2:?}"; shift 2 ;;
    --web-domain)      WEB_DOMAIN="${2:?}"; shift 2 ;;
    --tls-mode)        TLS_MODE="${2:?}"; shift 2 ;;
    --tls-cert)        TLS_CERT="${2:?}"; shift 2 ;;
    --tls-key)         TLS_KEY="${2:?}"; shift 2 ;;
    --acme-email)      ACME_EMAIL="${2:?}"; shift 2 ;;
    --dns-provider)    DNS_PROVIDER="${2:?}"; shift 2 ;;
    --dns-token)       DNS_TOKEN="${2:?}"; shift 2 ;;
    --ws-tls-port)     WS_PUBLIC_PORT="${2:?}"; shift 2 ;;
    --web-tls-port)    WEB_PUBLIC_PORT="${2:?}"; shift 2 ;;
    --ws-bind-port)    WS_BIND_PORT="${2:?}"; shift 2 ;;
    --web-bind-port)   WEB_BIND_PORT="${2:?}"; shift 2 ;;
    --install-dir)     INSTALL_DIR="${2:?}"; shift 2 ;;
    --config-dir)      CONFIG_DIR="${2:?}"; shift 2 ;;
    --data-dir)        DATA_DIR="${2:?}"; shift 2 ;;
    --service-name)    SERVICE_NAME="${2:?}"; shift 2 ;;
    --service-user)    SERVICE_USER="${2:?}"; shift 2 ;;
    --admin-user)      ADMIN_USER="${2:?}"; shift 2 ;;
    --admin-password)  ADMIN_PASSWORD="${2:?}"; shift 2 ;;
    --no-service)      INSTALL_SERVICE=0; shift ;;
    --skip-deps)       SKIP_DEPS=1; shift ;;
    --allow-nonroot)   ALLOW_NONROOT=1; shift ;;
    -h|--help)         usage; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
done

CONFIG_FILE="$CONFIG_DIR/relay.yaml"
VENV="$INSTALL_DIR/venv"
PY="$VENV/bin/python"

# --------------------------------------------------------------------------- #
# TLS option validation
# --------------------------------------------------------------------------- #
if [ -n "$DOMAIN" ] || [ -n "$WS_DOMAIN" ] || [ -n "$WEB_DOMAIN" ]; then
  TLS_ENABLED=1
  # default the per-service hosts from --domain
  [ -n "$WS_DOMAIN" ] || WS_DOMAIN="$DOMAIN"
  [ -n "$WEB_DOMAIN" ] || WEB_DOMAIN="$DOMAIN"
  case "$TLS_MODE" in
    acme|internal|files|dns) : ;;
    *) die "invalid --tls-mode '$TLS_MODE' (expected acme, internal, files or dns)" ;;
  esac

  if [ "$TLS_MODE" = "acme" ]; then
    # TLS-ALPN-01 needs each host on port 443.
    [ -n "$WS_PUBLIC_PORT" ] || WS_PUBLIC_PORT=443
    [ -n "$WEB_PUBLIC_PORT" ] || WEB_PUBLIC_PORT=443
    [ "$WS_PUBLIC_PORT" = "443" ] || die "--tls-mode acme requires the wss host on port 443 (got $WS_PUBLIC_PORT)"
    [ "$WEB_PUBLIC_PORT" = "443" ] || die "--tls-mode acme requires the web host on port 443 (got $WEB_PUBLIC_PORT)"
  else
    # keep the current public ports unless overridden
    [ -n "$WS_PUBLIC_PORT" ] || WS_PUBLIC_PORT="$WS_PORT"
    [ -n "$WEB_PUBLIC_PORT" ] || WEB_PUBLIC_PORT="$WEB_PORT"
  fi

  [ -n "$WS_BIND_PORT" ] || WS_BIND_PORT=$((WS_PORT + 10000))
  [ -n "$WEB_BIND_PORT" ] || WEB_BIND_PORT=$((WEB_PORT + 10000))
  [ -n "$ADVERTISE_HOST" ] || ADVERTISE_HOST="$WS_DOMAIN"

  # the Python servers (and the wss URL) use the public ports so Caddy, which
  # receives them on its own advertised ports, can be reached at the same port.
  WS_PORT="$WS_PUBLIC_PORT"
  WEB_PORT="$WEB_PUBLIC_PORT"

  if [ "$TLS_MODE" = "files" ]; then
    [ -n "$TLS_CERT" ] && [ -n "$TLS_KEY" ] || die "--tls-mode files requires --tls-cert and --tls-key"
    [ -f "$TLS_CERT" ] || die "certificate not found: $TLS_CERT"
    [ -f "$TLS_KEY" ] || die "key not found: $TLS_KEY"
  fi
  if [ "$TLS_MODE" = "dns" ]; then
    [ -n "$DNS_PROVIDER" ] && [ -n "$DNS_TOKEN" ] || die "--tls-mode dns requires --dns-provider and --dns-token"
  fi
  if [ "$TLS_MODE" = "internal" ] && [ -z "$TLS_CA_FILE" ]; then
    TLS_CA_FILE="$CONFIG_DIR/caddy-root.crt"
  fi
fi

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    if [ "$ALLOW_NONROOT" = "1" ]; then
      warn "running as non-root (--allow-nonroot): system packages and services are disabled"
      INSTALL_SERVICE=0
      SKIP_DEPS=1
      return
    fi
    die "must run as root (try: sudo $0 $*) or pass --allow-nonroot for a user-local install"
  fi
}

detect_ubuntu() {
  if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    case "${ID:-}${ID_LIKE:-}" in
      *ubuntu*|*debian*) : ;;
      *) warn "this script targets Ubuntu/Debian but ID='${ID:-unknown}' was detected; continuing" ;;
    esac
  fi
}

install_deps() {
  if [ "$SKIP_DEPS" = "1" ]; then
    log "skipping apt-get (--skip-deps)"
    return
  fi
  log "installing system packages (python3, venv, pip)"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip ca-certificates
}

repo_root() {
  local here
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  echo "$(cd "$here/.." && pwd)"
}

copy_source() {
  local repo="$1"
  [ -d "$repo/relay" ] || die "cannot find 'relay/' next to $repo - run this script from the repository (relay/setup.sh)"
  log "installing application files into $INSTALL_DIR"
  mkdir -p "$INSTALL_DIR"
  rm -rf "$INSTALL_DIR/relay"
  cp -a "$repo/relay" "$INSTALL_DIR/"
  [ -f "$repo/config.example.yaml" ] && cp -a "$repo/config.example.yaml" "$INSTALL_DIR/" || true
  [ -f "$repo/README.md" ] && cp -a "$repo/README.md" "$INSTALL_DIR/" || true

  # Package the agent so the install web server can hand it out as a tarball.
  if [ -d "$repo/agent" ]; then
    log "packaging agent -> $INSTALL_DIR/agent-dist.tar.gz"
    rm -f "$INSTALL_DIR/agent-dist.tar.gz"
    tar -czf "$INSTALL_DIR/agent-dist.tar.gz" -C "$repo" agent
  else
    warn "no 'agent/' directory found - /agent.tar.gz will be unavailable"
  fi
}

create_venv() {
  log "creating virtualenv"
  if [ ! -x "$PY" ]; then
    python3 -m venv "$VENV"
  fi
  "$VENV/bin/pip" install --quiet --upgrade pip
  "$VENV/bin/pip" install --quiet -r "$INSTALL_DIR/relay/requirements.txt"
  ok "python dependencies installed"
}

write_config() {
  log "writing config $CONFIG_FILE"
  mkdir -p "$CONFIG_DIR" "$DATA_DIR"
  if [ -f "$CONFIG_FILE" ]; then
    warn "$CONFIG_FILE already exists - leaving it untouched"
    return
  fi
  cat > "$CONFIG_FILE" <<EOF
relay:
  ssh_host: $SSH_HOST
  ssh_port: $SSH_PORT
  ws_host: $WS_HOST
  ws_port: $WS_PORT
  ws_path: /agent
  host_key: $DATA_DIR/relay_host_key
  authorized_keys: $DATA_DIR/authorized_keys
  allow_anonymous: false
  default_term: xterm-256color
  open_timeout: 15
  auth_timeout: 30
  advertise_host: $ADVERTISE_HOST
  agent_package: $INSTALL_DIR/agent-dist.tar.gz
EOF
  if [ "$TLS_ENABLED" = "1" ]; then
    local verify=true
    [ "$TLS_MODE" = "internal" ] && verify=false
    cat >> "$CONFIG_FILE" <<EOF
  ws_bind_host: $WS_BIND_HOST
  ws_bind_port: $WS_BIND_PORT
  tls: true
  tls_verify: $verify
  tls_ca_file: $TLS_CA_FILE
  web_advertise_host: $WEB_DOMAIN
EOF
  fi
  cat >> "$CONFIG_FILE" <<EOF

web:
  web_enabled: true
  web_host: $WEB_HOST
  web_port: $WEB_PORT
EOF
  if [ "$TLS_ENABLED" = "1" ]; then
    cat >> "$CONFIG_FILE" <<EOF
  web_bind_host: $WEB_BIND_HOST
  web_bind_port: $WEB_BIND_PORT
EOF
  fi
  cat >> "$CONFIG_FILE" <<EOF

database:
  path: $DATA_DIR/relay.db

logging:
  level: INFO
EOF
  touch "$DATA_DIR/authorized_keys"
  chmod 600 "$DATA_DIR/authorized_keys"
  chmod 750 "$CONFIG_DIR"
  ok "config written"
}

ensure_service_user() {
  if [ "$(id -u)" -ne 0 ]; then
    warn "non-root install: skipping service user setup"
    return
  fi
  if [ "$SERVICE_USER" = "root" ]; then
    chmod 700 "$DATA_DIR"
    return
  fi
  if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    log "creating system user '$SERVICE_USER'"
    useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
  fi
  chown -R "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_DIR" "$DATA_DIR"
  chmod 700 "$DATA_DIR"
}

init_database() {
  log "initialising registry database"
  ( cd "$INSTALL_DIR" && "$PY" -m relay.manage -c "$CONFIG_FILE" init )
  if [ -n "$ADMIN_USER" ]; then
    if [ -n "$ADMIN_PASSWORD" ]; then
      ( cd "$INSTALL_DIR" && "$PY" -m relay.manage -c "$CONFIG_FILE" \
          add-user "$ADMIN_USER" --password "$ADMIN_PASSWORD" )
    else
      ( cd "$INSTALL_DIR" && "$PY" -m relay.manage -c "$CONFIG_FILE" add-user "$ADMIN_USER" )
    fi
    ok "relay SSH user '$ADMIN_USER' ready"
  fi
}

install_service() {
  if [ "$INSTALL_SERVICE" = "0" ]; then
    warn "skipping systemd install (--no-service)"
    return
  fi
  command -v systemctl >/dev/null 2>&1 || die "systemctl not found; re-run with --no-service"

  local unit="/etc/systemd/system/$SERVICE_NAME.service"
  log "installing systemd unit $unit"
  cat > "$unit" <<EOF
[Unit]
Description=xquare Control Tower SSH relay
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$PY -m relay -c $CONFIG_FILE
Restart=on-failure
RestartSec=3
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable "$SERVICE_NAME" >/dev/null
  systemctl restart "$SERVICE_NAME"
  ok "service '$SERVICE_NAME' enabled and started"
}

install_caddy() {
  [ "$TLS_ENABLED" = "1" ] || return 0
  if command -v caddy >/dev/null 2>&1; then
    ok "caddy already installed: $(caddy version 2>/dev/null | head -n1)"
    return 0
  fi
  if [ "$SKIP_DEPS" = "1" ]; then
    warn "skipping caddy install (--skip-deps): install caddy before starting $CADDY_SERVICE"
    return 0
  fi
  log "installing caddy"
  if apt-get install -y --no-install-recommends caddy >/dev/null 2>&1; then
    ok "caddy installed via apt"
    return 0
  fi
  local arch
  case "$(uname -m)" in
    x86_64|amd64) arch=amd64 ;;
    aarch64|arm64) arch=arm64 ;;
    *) warn "unsupported architecture $(uname -m); install caddy manually"; return 0 ;;
  esac
  warn "apt could not install caddy; downloading the static binary v$CADDY_VERSION"
  local tmp url
  tmp="$(mktemp -d)"
  url="https://github.com/caddyserver/caddy/releases/download/v${CADDY_VERSION}/caddy_${CADDY_VERSION}_linux_${arch}.tar.gz"
  if curl -fsSL "$url" -o "$tmp/caddy.tar.gz" && tar -xzf "$tmp/caddy.tar.gz" -C "$tmp" caddy; then
    install -m 0755 "$tmp/caddy" /usr/local/bin/caddy
    ok "caddy installed to /usr/local/bin/caddy"
  else
    warn "could not download caddy; install it manually, then: systemctl restart $CADDY_SERVICE"
  fi
  rm -rf "$tmp"
}

prepare_tls_certs() {
  [ "$TLS_MODE" = "files" ] || return 0
  log "installing provided certificate into $CONFIG_DIR/tls"
  mkdir -p "$CONFIG_DIR/tls"
  cp -f "$TLS_CERT" "$CONFIG_DIR/tls/fullchain.pem"
  cp -f "$TLS_KEY" "$CONFIG_DIR/tls/privkey.pem"
  chmod 600 "$CONFIG_DIR/tls/privkey.pem"
  CADDY_CERT="$CONFIG_DIR/tls/fullchain.pem"
  CADDY_KEY="$CONFIG_DIR/tls/privkey.pem"
}

write_caddy_env() {
  if [ "$TLS_MODE" = "dns" ] && [ -n "$DNS_TOKEN" ]; then
    umask 077
    printf 'XQ_CADDY_DNS_TOKEN=%s\n' "$DNS_TOKEN" > "$CADDY_ENV_FILE"
    chmod 600 "$CADDY_ENV_FILE"
    ok "wrote $CADDY_ENV_FILE"
  fi
}

render_caddyfile() {
  log "rendering $CADDYFILE"
  mkdir -p "$CONFIG_DIR"
  local args=(
    --ws-host "$WS_DOMAIN"
    --web-host "$WEB_DOMAIN"
    --ws-port "$WS_PORT"
    --web-port "$WEB_PORT"
    --ws-bind-port "$WS_BIND_PORT"
    --web-bind-port "$WEB_BIND_PORT"
    --tls-mode "$TLS_MODE"
  )
  [ -n "$CADDY_CERT" ] && args+=(--tls-cert "$CADDY_CERT")
  [ -n "$CADDY_KEY" ] && args+=(--tls-key "$CADDY_KEY")
  [ -n "$ACME_EMAIL" ] && args+=(--acme-email "$ACME_EMAIL")
  [ -n "$DNS_PROVIDER" ] && args+=(--dns-provider "$DNS_PROVIDER")
  [ -n "$DNS_TOKEN" ] && args+=(--dns-token "$DNS_TOKEN")
  ( cd "$INSTALL_DIR" && "$PY" -m relay.caddy "${args[@]}" ) > "$CADDYFILE"
  ok "caddyfile written"
}

patch_relay_config() {
  log "applying TLS settings to $CONFIG_FILE"
  ( cd "$INSTALL_DIR" && "$PY" -m relay.caddy --patch-config "$CONFIG_FILE" \
      --ws-host "$WS_DOMAIN" --web-host "$WEB_DOMAIN" \
      --ws-port "$WS_PORT" --web-port "$WEB_PORT" \
      --ws-bind-port "$WS_BIND_PORT" --web-bind-port "$WEB_BIND_PORT" \
      --tls-mode "$TLS_MODE" --advertise-host "$ADVERTISE_HOST" \
      --tls-ca-file "$TLS_CA_FILE" )
  ok "config updated"
}

install_caddy_service() {
  [ "$TLS_ENABLED" = "1" ] || return 0
  if [ "$INSTALL_SERVICE" = "0" ]; then
    warn "skipping caddy systemd install (--no-service); run manually:"
    warn "  XDG_DATA_HOME=$CADDY_DATA_DIR caddy run --config $CADDYFILE --adapter caddyfile"
    return 0
  fi
  local caddy_bin
  caddy_bin="$(command -v caddy || true)"
  if [ -z "$caddy_bin" ]; then
    warn "caddy not found; skipping $CADDY_SERVICE (install caddy, then re-run this script)"
    return 0
  fi
  if [ "$TLS_MODE" = "dns" ] && ! "$caddy_bin" list-modules 2>/dev/null | grep -q "dns.providers.$DNS_PROVIDER"; then
    warn "this caddy build lacks the '$DNS_PROVIDER' module; DNS-01 will fail until you install one that bundles it"
  fi

  # The distro caddy package ships its own caddy.service bound to :80/:443.
  # It would fight our unit for the ports, so stop and disable it.
  if systemctl list-unit-files caddy.service >/dev/null 2>&1; then
    if systemctl is-enabled caddy.service >/dev/null 2>&1 \
       || systemctl is-active caddy.service >/dev/null 2>&1; then
      log "disabling conflicting system 'caddy.service' (we use $CADDY_SERVICE)"
      systemctl disable --now caddy.service >/dev/null 2>&1 || true
    fi
  fi

  local unit="/etc/systemd/system/$CADDY_SERVICE.service"
  log "installing systemd unit $unit"
  mkdir -p "$CADDY_DATA_DIR"
  cat > "$unit" <<EOF
[Unit]
Description=xquare Control Tower TLS proxy (caddy)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=XDG_DATA_HOME=$CADDY_DATA_DIR
EnvironmentFile=-$CADDY_ENV_FILE
ExecStart=$caddy_bin run --config $CADDYFILE --adapter caddyfile
Restart=on-failure
RestartSec=3
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable "$CADDY_SERVICE" >/dev/null
  systemctl restart "$CADDY_SERVICE"
  ok "service '$CADDY_SERVICE' enabled and started"
}

export_internal_ca() {
  [ "$TLS_MODE" = "internal" ] || return 0
  local src="$CADDY_DATA_DIR/caddy/pki/authorities/local/root.crt" i
  log "waiting for caddy's internal root CA"
  for i in $(seq 1 30); do
    [ -f "$src" ] && break
    sleep 0.5
  done
  if [ -f "$src" ]; then
    cp -f "$src" "$TLS_CA_FILE"
    chmod 644 "$TLS_CA_FILE"
    ok "internal CA exported to $TLS_CA_FILE"
  else
    warn "caddy root CA not found at $src yet; it will be served at /ca.crt once caddy starts"
  fi
}

enable_tls() {
  install_caddy
  prepare_tls_certs
  write_caddy_env
  render_caddyfile
  patch_relay_config
  install_caddy_service
  export_internal_ca
}

print_summary() {
  local host ws_scheme http_scheme ws_addr web_addr tls_block
  ws_scheme="ws"; http_scheme="http"
  tls_block=""
  host="$(hostname -I 2>/dev/null | awk '{print $1}')"
  [ -n "$host" ] || host="<this-host>"
  ws_addr="$host:$WS_PORT"
  web_addr="$host:$WEB_PORT"
  if [ "$TLS_ENABLED" = "1" ]; then
    ws_scheme="wss"; http_scheme="https"
    ws_addr="$WS_DOMAIN"
    web_addr="$WEB_DOMAIN"
    [ "$WS_PORT" != "443" ] && ws_addr="$WS_DOMAIN:$WS_PORT"
    [ "$WEB_PORT" != "443" ] && web_addr="$WEB_DOMAIN:$WEB_PORT"
    tls_block=$(cat <<EOF

  TLS proxy         : caddy ($TLS_MODE), wss on $WS_DOMAIN:$WS_PORT, web on $WEB_DOMAIN:$WEB_PORT
  CA certificate    : ${TLS_CA_FILE:-(system trust store)}
EOF
)
  fi
  cat <<EOF

$(ok "relay server installation complete")

  SSH endpoint      : ssh <user>@$host -p $SSH_PORT
  Agent WebSocket   : $ws_scheme://$ws_addr/agent
  Install web       : $http_scheme://$web_addr$tls_block
  config            : $CONFIG_FILE
  database          : $DATA_DIR/relay.db

Next steps
  1. open the firewall if needed:
       ufw allow $SSH_PORT/tcp && ufw allow $WS_PORT/tcp && ufw allow $WEB_PORT/tcp

  2. connect and create a server from the C2 CLI (id + password):
       ssh <user>@$host -p $SSH_PORT
       C2> add-server server-001
     It prints a one-line installer, e.g.
       curl -fsSL '$http_scheme://$web_addr/install/server-001?token=<token>' | sudo bash

  3. run that one-liner on the internal server: it installs the agent, starts
     it now, and enables it on boot (systemd Restart=always).

EOF
}

# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
main() {
  require_root "$@"
  detect_ubuntu
  local repo
  repo="$(repo_root)"

  install_deps
  copy_source "$repo"
  create_venv
  write_config
  enable_tls
  ensure_service_user
  init_database
  install_service
  print_summary
}

main "$@"
