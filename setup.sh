#!/usr/bin/env bash
set -euo pipefail

SERVICE=telemerge.service
SRC="$(cd "$(dirname "$0")" && pwd)/$SERVICE"
LINK="/etc/systemd/system/$SERVICE"

sudo ln -sf "$SRC" "$LINK"
sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE"
sudo systemctl status "$SERVICE" --no-pager