#!/usr/bin/env python3
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml이 설치되어 있지 않습니다. venv가 정상적으로 생성되었는지 확인하세요.", file=sys.stderr)
    sys.exit(1)

REPO = Path(__file__).parent
REQUIRED_KEYS = ["api_id", "api_hash", "sources", "destinations"]

errors = []

# config.yaml 존재 확인
config_path = REPO / "config.yaml"
if not config_path.exists():
    print(f"ERROR: config.yaml이 없습니다.", file=sys.stderr)
    print(f"  cp {REPO}/config.yaml.example {REPO}/config.yaml", file=sys.stderr)
    sys.exit(1)

# config.yaml 파싱
try:
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
except yaml.YAMLError as e:
    print(f"ERROR: config.yaml 파싱 실패: {e}", file=sys.stderr)
    sys.exit(1)

# 필수 키 체크
for key in REQUIRED_KEYS:
    if not config.get(key):
        errors.append(f"config.yaml에 '{key}'가 없거나 비어 있습니다.")

# 세션 파일 체크
if not (REPO / "telemerge_session.session").exists():
    errors.append(
        "telemerge_session.session이 없습니다. 먼저 인증을 완료하세요.\n"
        f"  cd {REPO} && python telemerge.py"
    )

if config.get("bot_token"):
    if not (REPO / "bot_session.session").exists():
        errors.append(
            "bot_token이 설정되어 있지만 bot_session.session이 없습니다. 먼저 인증을 완료하세요.\n"
            f"  cd {REPO} && python telemerge.py"
        )

if errors:
    for err in errors:
        print(f"ERROR: {err}", file=sys.stderr)
    sys.exit(1)

print("preflight 통과.")