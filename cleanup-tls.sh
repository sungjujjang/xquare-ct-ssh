#!/usr/bin/env bash
#
# Undo the Caddy/TLS setup on a relay host and return to the plain ws/http
# relay (SSH 2222, agent ws 8765, install web http 1234).
#
# It is safe to run more than once.  Run as root:
#
#   sudo ./cleanup-tls.sh
#
set -euo pipefail

CONFIG_DIR="/etc/xquare-ct-ssh"
CONFIG_FILE="$CONFIG_DIR/relay.yaml"
CADDYFILE="$CONFIG_DIR/Caddyfile"
CADDY_UNIT="/etc/systemd/system/xq-caddy.service"
CADDY_DATA_DIR="/var/lib/xquare-ct-ssh/caddy-data"

log() { printf '[cleanup] %s\n' "$*"; }
ok()  { printf '[ ok ] %s\n' "$*"; }
warn(){ printf '[warn] %s\n' "$*" >&2; }

[ "$(id -u)" -eq 0 ] || { echo "must run as root (try: sudo $0)" >&2; exit 1; }

# --- 1. stop and remove the caddy service ---------------------------------- #
for svc in xq-caddy; do
  if systemctl list-unit-files "$svc.service" >/dev/null 2>&1; then
    if systemctl is-active "$svc.service" >/dev/null 2>&1 \
       || systemctl is-enabled "$svc.service" >/dev/null 2>&1; then
      log "stopping and disabling $svc.service"
      systemctl disable --now "$svc.service" >/dev/null 2>&1 || true
    fi
  fi
done
if [ -f "$CADDY_UNIT" ]; then
  rm -f "$CADDY_UNIT"
  log "removed $CADDY_UNIT"
fi
systemctl daemon-reload
ok "caddy service removed"

# --- 2. remove the caddy Caddyfile and data -------------------------------- #
[ -f "$CADDYFILE" ] && { rm -f "$CADDYFILE"; log "removed $CADDYFILE"; }
if [ -d "$CADDY_DATA_DIR" ]; then
  rm -rf "$CADDY_DATA_DIR"
  log "removed $CADDY_DATA_DIR (certificates/ACME account)"
fi

# --- 3. strip TLS keys from relay.yaml ------------------------------------- #
# Uses a tiny pure-python editor so we never touch unrelated settings.
if [ -f "$CONFIG_FILE" ]; then
  python3 - "$CONFIG_FILE" <<'PY'
import sys
import re

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as fh:
    lines = fh.readlines()

# keys that only exist because of the Caddy/TLS setup
drop = {
    "tls", "tls_verify", "tls_ca_file",
    "ws_bind_host", "ws_bind_port",
    "web_bind_host", "web_bind_port", "web_advertise_host",
}

out = []
removed = []
for line in lines:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        out.append(line)
        continue
    m = re.match(r"^(\s*)([A-Za-z0-9_]+)\s*:", line)
    if m and m.group(2) in drop:
        removed.append(m.group(2))
        continue
    out.append(line)

with open(path, "w", encoding="utf-8") as fh:
    fh.writelines(out)

if removed:
    print("[cleanup] stripped relay.yaml keys: " + ", ".join(sorted(set(removed))))
else:
    print("[cleanup] relay.yaml had no TLS keys")
PY
  ok "relay.yaml cleaned ($CONFIG_FILE)"
else
  warn "$CONFIG_FILE not found; nothing to clean"
fi

# --- 4. restore the default public ports in relay.yaml --------------------- #
# The TLS run rewrote ws_port/web_port to 443; put them back to the plain
# defaults unless the operator had customised them (we cannot know, so we set
# the documented defaults and tell the user).
if [ -f "$CONFIG_FILE" ]; then
  python3 - "$CONFIG_FILE" <<'PY'
import re
import sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as fh:
    text = fh.read()

def set_key(section, key, value, text):
    # replace key within the given top-level section, or insert it there
    lines = text.splitlines()
    out = []
    in_section = False
    section_re = re.compile(r"^(\S.*):\s*$")
    key_re = re.compile(r"^(\s+)%s\s*:" % re.escape(key))
    replaced = False
    for i, line in enumerate(lines):
        m = section_re.match(line)
        if m:
            in_section = (m.group(1) == section)
        if in_section and key_re.match(line):
            indent = key_re.match(line).group(1)
            out.append(f"{indent}{key}: {value}")
            replaced = True
            continue
        out.append(line)
    if not replaced:
        # insert as the first line of the section
        result = []
        inserted = False
        for line in out:
            result.append(line)
            m = section_re.match(line)
            if m and m.group(1) == section and not inserted:
                result.append(f"  {key}: {value}")
                inserted = True
        out = result
    return "\n".join(out) + "\n"

text = set_key("relay", "ws_port", "8765", text)
text = set_key("web", "web_port", "1234", text)
with open(path, "w", encoding="utf-8") as fh:
    fh.write(text)
print("[cleanup] relay.yaml ports reset: ws_port=8765 web_port=1234")
PY
  ok "relay.yaml ports reset to plain defaults"
fi

# --- 5. verify ------------------------------------------------------------- #
echo
log "verifying relay config"
if [ -f "$CONFIG_FILE" ]; then
  grep -nE 'tls|_bind_|advertise_host' "$CONFIG_FILE" || echo "  (no TLS keys remain)"
fi

echo
ok "cleanup complete"
cat <<'EOF'

Next steps
  1. pull the reverted code and re-run the installer (config is preserved):
       cd ~/xquare-ct-ssh && git pull
       sudo ./relay/setup.sh --admin-user <user>

  2. restart the relay:
       sudo systemctl restart xq-relay

  3. confirm the plain ports are listening:
       sudo ss -ltnp | grep -E ':2222|:8765|:1234'
EOF
