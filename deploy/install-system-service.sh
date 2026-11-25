#!/usr/bin/env bash
# Install the Deep Research web UI as a system-level systemd service.
#
# Usage:
#   sudo /home/researcher/deep_research/deploy/install-system-service.sh
#
# The service starts after searxng.service and waits for SearXNG on
# 127.0.0.1:8888 before launching the web UI on 0.0.0.0:8080.
set -euo pipefail

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "Run as root: sudo $0" >&2
  exit 1
fi

RUN_USER="${SUDO_USER:-researcher}"
REPO="${1:-/home/researcher/deep_research}"
UNIT_SRC="$REPO/deploy/deep-research-web.service"
UNIT_DST="/etc/systemd/system/deep-research-web.service"
ENV_DIR="/home/${RUN_USER}/.config/deep-research"
ENV_FILE="$ENV_DIR/deep-research.env"

if [[ ! -f "$UNIT_SRC" ]]; then
  echo "Unit file not found: $UNIT_SRC" >&2
  exit 1
fi

# The unit ships with /home/researcher paths; retarget it to the invoking
# user so a non-researcher install (e.g. /home/xun) still starts.
sed "s#/home/researcher#/home/${RUN_USER}#g" "$UNIT_SRC" > "$UNIT_DST"
chmod 644 "$UNIT_DST"
install -d -m 700 "$ENV_DIR"
if [[ ! -f "$ENV_FILE" ]]; then
  install -o "$RUN_USER" -g "$RUN_USER" -m 600 \
    "$REPO/deploy/deep-research-web.env.example" "$ENV_FILE"
  echo "Created $ENV_FILE — edit it to add TAVILY/FIRECRAWL keys."
fi

systemctl daemon-reload
systemctl enable --now deep-research-web.service
systemctl status deep-research-web.service --no-pager || true

echo
echo "Web UI: http://127.0.0.1:8080/  (or http://<host-ip>:8080/)"
echo "Logs:   journalctl -u deep-research-web.service -f"
