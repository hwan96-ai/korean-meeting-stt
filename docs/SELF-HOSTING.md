# 직접 띄우기 (작성자 방식)

작성자가 서버 한 대에 띄운 방식을 적어 둡니다. **다른 환경에서 처음부터 재현해 보지는 않았습니다.** 따라 하다 막히는 곳이 있으면 이슈로 알려 주세요.

## 먼저 알아 둘 것

- 웹 앱은 **Cloudflare Access** 뒤에서만 로그인됩니다. Access가 넘긴 JWT의 이메일이 설정한 소유자일 때만 들어갑니다. 루프백에서 쓰는 로컬 로그인 모드는 아직 없습니다.
- 소유자는 한 명입니다. 여러 사람이 쓰는 구조가 아닙니다.
- **`npm run build`, `npm start`는 쓰지 마세요.** 처음 시작한 OpenAI Sites 호스팅용 빌드라, 호스트가 넣는 로그인 헤더(`oai-authenticated-user-*`)를 그대로 믿습니다. 다른 곳에 띄우면 그 헤더를 꾸며 누구나 들어올 수 있습니다. 직접 띄울 때는 아래 `build:oci`만 씁니다.
- 작업기 장치 등록은 작성자 운영 스크립트(공개하지 않음)로 DB에 직접 넣습니다. 아래 4절에 넣는 값을 적었습니다. 한 명령으로 줄이는 것은 남은 과제입니다.

## 1. 웹 앱

Node.js 24, Linux 기준입니다.

```bash
npm ci
npm run build:oci          # Next standalone 빌드
npm run migrate:oci        # MEETING_DATA_DIR 안에 meeting.sqlite 생성
HOSTNAME=127.0.0.1 PORT=3187 NODE_ENV=production node .next/standalone/server.js
```

| 환경 변수 | 뜻 |
| --- | --- |
| `MEETING_DATA_DIR` | 소스 밖의 절대 경로. SQLite와 원음 파일이 여기에 쌓입니다 |
| `MEETING_OWNER_ID` | 소유자 고정 ID(마이그레이션 때 저장) |
| `MEETING_OWNER_EMAIL` | Access에 로그인할 소유자 이메일 |
| `MEETING_ACCESS_ISSUER` | `https://<팀이름>.cloudflareaccess.com` |
| `MEETING_ACCESS_AUD` | Access 앱의 AUD 태그 |
| `MEETING_PUBLIC_ORIGIN` | 바깥 주소(`https://...`) |
| `MEETING_WORKER_GATEWAY_TOKEN` | 작업기가 보내는 40자 이상 난수 |
| `MEETING_SPEECH_EXECUTION` | 서버 작업기를 쓰면 `server` |
| `MEETING_VAPID_PUBLIC_KEY`, `MEETING_VAPID_PRIVATE_KEY` | 선택. 휴대폰 알림. 없으면 기능이 꺼집니다 |
| `INTEGRATION_TOKEN` | 선택. 다른 프로그램이 통화 녹음을 넣는 입구. 43자 이상 난수. 없으면 그 경로는 모두 404입니다 |

환경 파일은 root 전용(0600)으로 두고 systemd `EnvironmentFile=`로 읽게 했습니다. 데이터 폴더는 웹 전용 사용자만 읽게 합니다. 백업할 때는 웹을 멈추고 폴더 전체를 복사합니다. 실행 중에 `meeting.sqlite`만 복사하면 WAL의 최근 변경이 빠질 수 있습니다.

## 2. 바깥에 열기

`cloudflared` 터널로 `127.0.0.1:3187`을 내보내고, 같은 호스트 이름에 Cloudflare Access 앱을 붙여 소유자 이메일만 허용합니다. 서버에 열린 인바운드 포트는 없습니다.

통화 입구(`/api/integration/v1`)는 루프백 요청만 받고, 바깥에서 온 요청은 `Host`가 공개 이름이거나 `cf-*`, `x-forwarded-*` 같은 헤더가 붙어 있는 것으로 알아봅니다(`lib/integration-gate.ts`). Cloudflare Tunnel은 이 조건을 지킵니다. nginx처럼 `Host`를 `127.0.0.1:3187`로 바꾸고 전달 헤더를 붙이지 않는 프록시 뒤에 두면 이 방어가 소리 없이 사라지고 토큰 하나만 남습니다. 다른 프록시를 쓰면 `INTEGRATION_TOKEN`을 비워 두세요.

## 3. 음성 작업기

Python 3.12, 전용 사용자 `meeting-speech`로 돌립니다. 자세한 조건은 [local_worker/SERVER.md](../local_worker/SERVER.md)에 있습니다.

```bash
python -m venv /opt/meeting-speech/venv
/opt/meeting-speech/venv/bin/python -m pip install -r requirements-worker.txt
env MEETING_WORKER_HOME=/var/lib/meeting-speech /opt/meeting-speech/venv/bin/python -m local_worker setup-models --whisper base --diarization
```

설정 파일 `/etc/meeting-speech/worker.json`(root 0600, 부모 폴더 root 0700)을 systemd `LoadCredential=worker.json:/etc/meeting-speech/worker.json`로 넘깁니다. 작업기는 `CREDENTIALS_DIRECTORY` 안의 사본만 읽고, root로 실행하면 거부합니다. 작성자가 쓰는 유닛 파일은 [deploy/meeting-speech.service](../deploy/meeting-speech.service)입니다(`MEETING_WORKER_MODE=server`, 메모리 3GB 제한 등). 이 유닛은 두 가지를 전제합니다. 웹 앱이 `meeting-notes.service`라는 systemd 서비스로 떠 있어야 하고(`Requires=`), 소스가 `/opt/meeting-speech/current`에 있어야 합니다(`WorkingDirectory=`). 웹을 셸에서 직접 띄웠다면 `Requires=`와 `After=` 줄을 자기 환경에 맞게 고치세요. 작업 폴더 `/var/lib/meeting-speech/workspace`는 미리 만들어 둡니다(전용 사용자 소유, 0700).

```json
{
  "base_url": "http://127.0.0.1:3187",
  "device_token": "<40자 이상 난수>",
  "sites_dispatch_token": "<MEETING_WORKER_GATEWAY_TOKEN과 같은 값>",
  "whisper_model": "base",
  "cpu_threads": 2,
  "chunk_seconds": 600
}
```

API 전사를 쓰려면 같은 `worker.json`에 아래 키를 더 넣습니다. 키 값 자체를 파일에 넣습니다(파일 경로가 아님).

| 키 | 값 |
| --- | --- |
| `speech_mode` | `"api"` |
| `openai_api_key` | OpenAI API 키 값 |
| `api_speech_model` | `"gpt-transcribe"`(서버 모드는 이것만 받음) |
| `api_speech_price_per_minute` | 분당 단가(USD). OpenAI 가격표의 현재 값을 직접 넣으세요 |
| `api_max_cost_usd` | 작업 하나의 누적 비용 상한(USD) |
| `api_speech_billing_rounding_seconds` | 요금 반올림 단위 초(1~60, 기본 60) |
| `api_speech_chunk_seconds` | 한 번에 보내는 조각 길이 초(기본 30) |
| `api_credit_only_ack` | `true`. 크레딧과 결제 상태를 직접 확인했다는 표시입니다. 실제 잔액을 앱이 확인하지는 않습니다. 없으면 `needs_setup`으로 멈춥니다 |

화자 분석과 전사를 겹치려면 `"speech_pipeline": true`를 더 넣습니다(API 전사일 때만).

## 4. 작업기를 웹에 등록

웹은 `settings` 테이블의 `device` 행으로 작업기를 알아봅니다. 작성자 스크립트는 웹 전용 사용자 권한으로 아래 값을 넣습니다.

```json
{ "owner": "<MEETING_OWNER_ID>", "token_hash": "<device_token의 SHA-256 hex>", "execution_location": "server" }
```

넣은 뒤 `heartbeat`, `pair` 행을 지우고 웹과 작업기를 다시 시작합니다.

작업기는 셸에서 바로 돌리지 않고 systemd로 시작합니다. 위 유닛은 시작 전에 `python -m local_worker.server --doctor`(모델 해시, 설정 형식, GPU일 때 1초 시험 전사)를 돌리고, 통과해야 본체 `python -m local_worker.server`를 띄웁니다. `--doctor`도 같은 환경(전용 사용자, `MEETING_WORKER_MODE=server`, `LoadCredential`)이 있어야 돌아갑니다.
