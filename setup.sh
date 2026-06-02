#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
SERVICE=telemerge.service
LINK="/etc/systemd/system/$SERVICE"

# python3-venv 없으면 설치
if ! python3 -m venv --help &>/dev/null; then
    if command -v apt-get &>/dev/null; then
        sudo apt-get install -y python3-venv
    elif command -v dnf &>/dev/null; then
        sudo dnf install -y python3-venv
    else
        echo "지원하지 않는 패키지 매니저입니다." >&2
        exit 1
    fi
fi

# venv 없으면 생성 + 패키지 설치
if [ ! -d "$REPO/.venv" ]; then
    echo "가상환경 생성 중..."
    python3 -m venv "$REPO/.venv"
    "$REPO/.venv/bin/pip" install -r "$REPO/requirements.txt"
fi

# WorkingDirectory, ExecStart를 실제 경로로 치환해서 /tmp에 렌더링
sed \
    -e "s|WorkingDirectory=.*|WorkingDirectory=$REPO|" \
    -e "s|ExecStart=.*|ExecStart=$REPO/.venv/bin/python telemerge.py|" \
    "$REPO/$SERVICE" > "/tmp/$SERVICE"

# preflight 체크
"$REPO/.venv/bin/python" "$REPO/preflight.py" || exit 1

# systemd 등록
sudo cp "/tmp/$SERVICE" "$LINK"
sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE"
sudo systemctl try-restart "$SERVICE" || sudo systemctl start "$SERVICE"
sudo systemctl status "$SERVICE" --no-pager