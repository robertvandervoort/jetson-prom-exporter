#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Re-running with sudo..."
  exec sudo -E bash "$0" "$@"
fi

TARGET_USER="${SUDO_USER:-${USER:-}}"

echo "Installing Jetson exporter host prerequisites..."
apt-get update
apt-get install -y python3-pip python3-prometheus-client

# LAN hosts: use NAS devpi (never pull pypi.org directly). Override via env if needed.
PIP_INDEX_URL="${PIP_INDEX_URL:-http://nas:3141/root/pypi/+simple/}"
PIP_TRUSTED_HOST="${PIP_TRUSTED_HOST:-nas}"
export PIP_INDEX_URL PIP_TRUSTED_HOST

python3 -m pip install -U jetson-stats --break-system-packages

# jetson-stats >=5 no longer drops the unit from pip alone; install + enable explicitly.
# Always restart so a pip upgrade cannot leave an old daemon version on the socket.
jtop --install-service
install -d -o root -g root -m 0755 /etc/systemd/system/jtop.service.d
install -o root -g root -m 0644 "$(dirname "$(readlink -f "$0")")"/systemd/jtop.service.d/*.conf \
  /etc/systemd/system/jtop.service.d/
systemctl daemon-reload
systemctl enable jtop.service
# Returns once socket-dir.conf's ExecStartPost has linked /run/jtop/jtop.sock.
systemctl restart jtop.service

if [[ -n "${TARGET_USER}" && "${TARGET_USER}" != "root" ]] && getent group jtop >/dev/null; then
  usermod -aG jtop "${TARGET_USER}"
fi

echo "Verifying jtop.service..."
systemctl is-active --quiet jtop.service
test -S /run/jtop.sock
test -S /run/jtop/jtop.sock

echo "Verifying jtop Python API as root..."
python3 - <<'PY'
from jtop import jtop

with jtop(interval=0.5) as jetson:
    if not jetson.ok():
        raise SystemExit("jtop did not produce a sample")
    print(f"jtop ok: {len(jetson.cpu.get('cpu', []))} CPU cores")
    print(f"libraries: {jetson.board.get('libraries', {})}")
PY

cat <<EOF

Host setup complete.

If ${TARGET_USER:-your user} was just added to the jtop group, log out and back in
before running jtop clients as that user. Docker containers that run as root can
use the socket immediately when /run/jtop is mounted into the container.

Next step from /opt/chip, with the compose file for this host
(docker-compose.yml on chip, docker-compose.nano.yml on chip-nano,
docker-compose.nano-two.yml on chip-nano-two):
  docker compose -f <compose file> up -d --force-recreate jetson-exporter
EOF
