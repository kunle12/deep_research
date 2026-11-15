#!/usr/bin/env bash
# Install the Deep Research web UI as a systemd *user* service.
#
# Usage:
#   ./deploy/install-user-service.sh [repo-path]
#
# This is the no-sudo variant. It starts at boot via logind linger; if
# `loginctl enable-linger` requires root on your distro, run that one command
# with sudo afterwards.
set -euo pipefail

REPO="${1:-$HOME/deep_research}"
UNIT_SRC="$REPO/deploy/deep-research-web.user.service"
UNIT_DST="$HOME/.config/systemd/user/deep-research-web.service"
ENV_DIR="$HOME/.config/deep-research"
ENV_FILE="$ENV_DIR/deep-research.env"

if [[ ! -f "$UNIT_SRC" ]]; then
  echo "Unit file not found: $UNIT_SRC" >&2
  exit 1
fi

install -d -m 755 "$HOME/.config/systemd/user"
install -m 644 "$UNIT_SRC" "$UNIT_DST"
install -d -m 700 "$ENV_DIR"
if [[ ! -f "$ENV_FILE" ]]; then
  install -m 600 "$REPO/deploy/deep-research-web.env.example" "$ENV_FILE"
  echo "Created $ENV_FILE — edit it to add TAVILY/FIRECRAWL keys."
fi

systemctl --user daemon-reload
systemctl --user enable --now deep-research-web.service

if ! loginctl enable-linger "$USER" 2>/dev/null; then
  echo "Run 'sudo loginctl enable-linger $USER' to start at boot without login." >&2
fi

systemctl --user status deep-research-web.service --no-pager || true
echo
echo "Web UI: http://127.0.0.1:8080/  (or http://<host-ip>:8080/)"
echo "Logs:   journalctl --user-unit deep-research-web.service -f"
