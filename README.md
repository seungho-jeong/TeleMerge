# TeleMerge

여러 텔레그램 채널/그룹의 메시지를 하나의 목적지로 모아주는 aggregator.

## Features

- **다중 소스 → 단일 목적지**: 여러 채널/그룹의 메시지를 하나의 채널로 집약
- **봇 기반 전달**: Bot API를 통해 소스명 뱃지(`📢 [채널명]`)와 원본 링크를 포함하여 전달
- **키워드 필터**: include/exclude 정규식 패턴으로 원하는 메시지만 선별
- **미디어 포워딩**: 사진, 영상, 문서 등 미디어 파일 포함 전달
- **동시 처리**: 메시지가 몰려도 여러 worker가 병렬로 처리하여 지연 최소화
- **소스 읽음 처리**: 전달 완료 시 소스 채널의 메시지를 자동으로 읽음 처리
- **비공개 채널 지원**: Telethon(개인 계정 API) 기반으로 비공개 채널/그룹 접근 가능

## Architecture

TeleMerge는 **User Client**와 **Bot Client**, 두 개의 Telegram 클라이언트를 함께 사용합니다.

### 왜 두 개의 클라이언트가 필요한가?

일반적인 Telegram 봇은 비공개 채널을 읽을 수 없고, 초대받은 곳에서만 활동할 수 있습니다. 반면 개인 계정(User Client)은 내가 가입한 모든 채널을 읽을 수 있지만, 내 계정으로 메시지를 보내면 자동으로 "읽음" 처리되어 어디까지 확인했는지 알 수 없습니다.

TeleMerge는 이 두 가지를 조합합니다:

- **User Client** (내 계정): 소스 채널의 새 메시지를 감지하고 미디어를 다운로드
- **Bot Client** (봇 계정): 목적지 채널에 메시지를 대신 전송

봇이 보낸 메시지는 내 계정 입장에서 "읽지 않음" 상태로 남기 때문에, 목적지 채널에서 아직 확인하지 않은 메시지를 쉽게 구분할 수 있습니다.

```
Source Channels                TeleMerge                     Destination
┌───────────┐
│ Channel A │──┐
├───────────┤  │   ┌─────────────────────────────┐   ┌───────────────┐
│ Channel B │──┼──▶│ User Client                 │   │               │
├───────────┤  │   │  · Watch new messages       │   │  My Channel   │
│ Channel C │──┘   │  · Download media           │   │               │
└───────────┘      │  · Mark source as read      │   │  Sent by bot  │
                   │           │                 │   │  = stays      │
                   │           ▼                 │   │  "unread"!    │
                   │ Bot Client                  │   │               │
                   │  · Send to destination      │──▶│               │
                   │  · Source header + link     │   │               │
                   └─────────────────────────────┘   └───────────────┘
                            │
                            ├─ Keyword filter (include/exclude)
                            ├─ Media forwarding (photo, video, docs)
                            └─ Concurrent workers (default: 5)
```

> **봇 없이 사용**: `bot_token`을 설정하지 않으면 User Client가 직접 전달합니다. 이 경우 네이티브 포워딩이 가능하지만, 목적지 채널의 메시지가 자동으로 읽음 처리됩니다.

### 동시 처리 (Concurrent Workers)

봇 모드에서는 미디어를 User Client로 다운로드한 뒤 Bot Client로 재업로드하기 때문에 메시지 1건당 수 초가 걸릴 수 있습니다. 여러 채널에서 메시지가 동시에 들어오면 순차 처리로는 지연이 누적됩니다.

TeleMerge는 기본 5개의 worker가 큐에서 메시지를 꺼내 병렬로 처리합니다. worker 수는 `telemerge.py`의 `CONCURRENT_FORWARDS` 값으로 조절할 수 있습니다.

> **참고**: Telegram Bot API는 같은 채널에 약 20건/분의 rate limit이 있습니다. 목적지가 1개라면 worker를 늘려도 이 제한을 넘을 수 없으므로 기본값(5)이면 충분합니다.

## Prerequisites

- Python 3.10+
- [Telegram API credentials](https://my.telegram.org) (`api_id`, `api_hash`)
- [Telegram Bot Token](https://t.me/BotFather) (목적지 채널에 관리자로 추가 필요)

## Installation

```bash
git clone https://github.com/seungho-jeong/TeleMerge.git
cd TeleMerge
pip install -r requirements.txt
```

## Configuration

```bash
cp config.yaml.example config.yaml
```

`config.yaml`을 열고 다음 값을 설정하세요:

| 항목 | 설명 |
|------|------|
| `api_id` | Telegram API ID (my.telegram.org에서 발급) |
| `api_hash` | Telegram API Hash |
| `sources` | 수집할 채널/그룹 목록 (`@username` 또는 채널 ID) |
| `destinations` | 메시지를 전달할 목적지 채널 |
| `bot_token` | BotFather에서 발급받은 봇 토큰 |
| `filters` | include/exclude 키워드 필터 (정규식 지원) |

### 비공개 채널 ID 확인 방법

1. 비공개 채널의 아무 메시지에서 "Copy Message Link" 클릭
2. `https://t.me/c/1234567890/1` 형식의 링크에서 가운데 숫자 확인
3. 앞에 `-100`을 붙여서 사용: `-1001234567890`

## Usage

```bash
python telemerge.py
```

최초 실행 시 Telegram 로그인(전화번호 + 인증코드)이 필요합니다. 이후 세션 파일이 생성되어 재로그인이 불필요합니다.

설정 파일 경로를 지정할 수도 있습니다:

```bash
python telemerge.py /path/to/config.yaml
```

## Deployment

Kubernetes 배포 예시가 `deployment.yaml`에 포함되어 있습니다.

## License

[MIT](LICENSE)
