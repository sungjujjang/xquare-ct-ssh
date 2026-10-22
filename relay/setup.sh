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
  --advertise-host HOST  public host for install URLs (default: auto)
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

web:
  web_enabled: true
  web_host: $WEB_HOST
  web_port: $WEB_PORT

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

print_summary() {
  local host
  host="$(hostname -I 2>/dev/null | awk '{print $1}')"
  [ -n "$host" ] || host="<this-host>"
  cat <<EOF

$(ok "relay server installation complete")

  SSH endpoint      : ssh <user>@$host -p $SSH_PORT
  Agent WebSocket   : ws://$host:$WS_PORT/agent
  Install web       : http://$host:$WEB_PORT
  config            : $CONFIG_FILE
  database          : $DATA_DIR/relay.db

Next steps
  1. open the firewall if needed:
       ufw allow $SSH_PORT/tcp && ufw allow $WS_PORT/tcp && ufw allow $WEB_PORT/tcp

  2. connect and create a server from the C2 CLI (id + password):
       ssh <user>@$host -p $SSH_PORT
       C2> add-server server-001
     It prints a one-line installer, e.g.
       curl -fsSL 'http://$host:$WEB_PORT/install/server-001?token=<token>' | sudo bash

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
  ensure_service_user
  init_database
  install_service
  print_summary
}

main "$@"
