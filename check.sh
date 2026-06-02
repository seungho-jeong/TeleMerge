#!/usr/bin/env bash
set -euo pipefail
REPO="$(cd "$(dirname "$0")" && pwd)"
SERVICE=telemerge.service
LINK="/etc/systemd/system/$SERVICE"

if [ ! -f "$LINK" ]; then
    echo "서비스가 등록되어 있지 않습니다. setup.sh를 먼저 실행해주세요." >&2
    exit 1
fi

if ! systemctl is-active --quiet "$SERVICE"; then
    echo "서비스가 실행 중이 아닙니다. setup.sh를 먼저 실행해주세요." >&2
    exit 1
fi

echo "서비스가 정상적으로 실행 중입니다."

systemctl status "$SERVICE" --no-pager

journalctl -u "$SERVICE" --no-pager