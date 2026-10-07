#!/usr/bin/env bash
#
# setup-agent.sh - install the xquare Control Tower *internal agent* on Ubuntu.
#
# The agent dials OUT to the relay (no inbound firewall rule needed), runs the
# user's shell inside a real PTY and streams bytes bidirectionally.
# Safe to re-run (idempotent); existing credentials are preserved unless
# overridden with --relay/--id/--token.
#
#   sudo ./scripts/setup-agent.sh \
#       --relay ws://relay.example.com:8765/agent \
#       --id server-001 --token xq_xxxxxxxx
#
set -euo pipefail

# --------------------------------------------------------------------------- #
# defaults
# --------------------------------------------------------------------------- #
RELAY_URL=""
SERVER_ID=""
TOKEN=""
SHELL_PATH=""
INSTALL_DIR="/opt/xquare-ct-ssh-agent"
CONFIG_DIR="/etc/xquare-ct-ssh"
SERVICE_NAME="xq-agent"
SERVICE_USER="root"
INSTALL_SERVICE=1
SKIP_DEPS=0
ALLOW_NONROOT=0

# --------------------------------------------------------------------------- #
# pretty logging
# --------------------------------------------------------------------------- #
C_RESET=$'\033[0m'; C_INFO=$'\033[1;34m'; C_OK=$'\033[1;32m'; C_WARN=$'\033[1;33m'; C_ERR=$'\033[1;31m'
log()  { printf '%s[agent]%s %s\n' "$C_INFO" "$C_RESET" "$*"; }
ok()   { printf '%s[ ok ]%s %s\n'  "$C_OK"   "$C_RESET" "$*"; }
warn() { printf '%s[warn]%s %s\n'  "$C_WARN" "$C_RESET" "$*" >&2; }
die()  { printf '%s[fail]%s %s\n'  "$C_ERR"  "$C_RESET" "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage: sudo ./scripts/setup-agent.sh --relay URL --id ID --token TOKEN [options]

Options:
  --relay URL            relay WebSocket URL, e.g. ws://relay:8765/agent
  --id ID                server id registered on the relay (e.g. server-001)
  --token TOKEN          agent registration token (xq_...)
  --shell PATH           shell to run (default: platform default, e.g. /bin/bash)
  --install-dir DIR      application install dir   (default: /opt/xquare-ct-ssh-agent)
  --config-dir DIR       config dir                (default: /etc/xquare-ct-ssh)
  --service-name NAME    systemd unit name         (default: xq-agent)
  --service-user USER    run the agent as USER     (default: root)
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
    --relay)           RELAY_URL="${2:?}"; shift 2 ;;
    --id)              SERVER_ID="${2:?}"; shift 2 ;;
    --token)           TOKEN="${2:?}"; shift 2 ;;
    --shell)           SHELL_PATH="${2:?}"; shift 2 ;;
    --install-dir)     INSTALL_DIR="${2:?}"; shift 2 ;;
    --config-dir)      CONFIG_DIR="${2:?}"; shift 2 ;;
    --service-name)    SERVICE_NAME="${2:?}"; shift 2 ;;
    --service-user)    SERVICE_USER="${2:?}"; shift 2 ;;
    --no-service)      INSTALL_SERVICE=0; shift ;;
    --skip-deps)       SKIP_DEPS=1; shift ;;
    --allow-nonroot)   ALLOW_NONROOT=1; shift ;;
    -h|--help)         usage; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
done

VENV="$INSTALL_DIR/venv"
PY="$VENV/bin/python"
ENV_FILE="$CONFIG_DIR/agent.env"

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

detect_os() {
  if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    case "${ID:-}${ID_LIKE:-}" in
      *ubuntu*|*debian*|*linuxmint*) : ;;
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
  [ -d "$repo/agent" ] || die "cannot find 'agent/' next to $repo — run this script from the repository"
  log "installing application files into $INSTALL_DIR"
  mkdir -p "$INSTALL_DIR"
  rm -rf "$INSTALL_DIR/agent" "$INSTALL_DIR/common"
  cp -a "$repo/agent" "$INSTALL_DIR/"
  cp -a "$repo/common" "$INSTALL_DIR/"
  cp -a "$repo/requirements-agent.txt" "$INSTALL_DIR/"
  [ -f "$repo/README.md" ] && cp -a "$repo/README.md" "$INSTALL_DIR/" || true
}

create_venv() {
  log "creating virtualenv"
  if [ ! -x "$PY" ]; then
    python3 -m venv "$VENV"
  fi
  "$VENV/bin/pip" install --quiet --upgrade pip
  "$VENV/bin/pip" install --quiet -r "$INSTALL_DIR/requirements-agent.txt"
  ok "python dependencies installed"
}

read_existing_env() {
  [ -f "$ENV_FILE" ] || return 0
  local value
  value="$(sed -n 's/^XQ_RELAY_URL="\(.*\)"$/\1/p' "$ENV_FILE")"; [ -z "$RELAY_URL" ] && RELAY_URL="$value"
  value="$(sed -n 's/^XQ_SERVER_ID="\(.*\)"$/\1/p' "$ENV_FILE")"; [ -z "$SERVER_ID" ] && SERVER_ID="$value"
  value="$(sed -n 's/^XQ_AGENT_TOKEN="\(.*\)"$/\1/p' "$ENV_FILE")"; [ -z "$TOKEN" ] && TOKEN="$value"
  value="$(sed -n 's/^XQ_SHELL="\(.*\)"$/\1/p' "$ENV_FILE")"; [ -z "$SHELL_PATH" ] && SHELL_PATH="$value"
}

write_env_file() {
  [ -n "$RELAY_URL" ] || die "--relay is required (no existing $ENV_FILE to reuse)"
  [ -n "$SERVER_ID" ] || die "--id is required (no existing $ENV_FILE to reuse)"
  [ -n "$TOKEN" ]     || die "--token is required (no existing $ENV_FILE to reuse)"

  log "writing credentials $ENV_FILE"
  mkdir -p "$CONFIG_DIR"
  umask 077
  {
    printf 'XQ_RELAY_URL="%s"\n' "$RELAY_URL"
    printf 'XQ_SERVER_ID="%s"\n' "$SERVER_ID"
    printf 'XQ_AGENT_TOKEN="%s"\n' "$TOKEN"
    if [ -n "$SHELL_PATH" ]; then
      printf 'XQ_SHELL="%s"\n' "$SHELL_PATH"
    fi
  } > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  ok "credentials written"
}

ensure_service_user() {
  if [ "$(id -u)" -ne 0 ]; then
    warn "non-root install: skipping service user setup"
    return
  fi
  if [ "$SERVICE_USER" = "root" ]; then
    return
  fi
  if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    log "creating system user '$SERVICE_USER'"
    useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER" || true
  fi
  chown -R "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_DIR"
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
Description=xquare Control Tower internal agent ($SERVER_ID)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$PY -m agent
Restart=always
RestartSec=5
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
  cat <<EOF

$(ok "internal agent installation complete")

  server id     : $SERVER_ID
  relay         : $RELAY_URL
  credentials   : $ENV_FILE
  installed at  : $INSTALL_DIR

Useful commands
  systemctl status $SERVICE_NAME
  journalctl -u $SERVICE_NAME -f

Note: the agent runs as '$SERVICE_USER'. If that should be a specific login
      account (so the remote shell belongs to that user), re-run with
      --service-user <name>.

EOF
}

# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
main() {
  require_root "$@"
  detect_os
  read_existing_env
  local repo
  repo="$(repo_root)"

  install_deps
  copy_source "$repo"
  create_venv
  write_env_file
  ensure_service_user
  install_service
  print_summary
}

main "$@"
