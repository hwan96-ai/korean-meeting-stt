# 내 컴퓨터에서 띄워 보기 (Linux, WSL2)

API 키 없이 내 컴퓨터 CPU로 전사까지 해 보는 길입니다. 웹과 음성 작업기가 모두 `127.0.0.1`에서만 돌고, 터미널이 알려 준 일회용 로그인 주소를 연 브라우저만 소유자로 들어가는 로컬 로그인 모드를 씁니다.

> **혼자 쓰는 컴퓨터에서 쓰세요.** 같은 계정으로 도는 프로그램은 설정 파일을 읽을 수 있습니다. 또 브라우저 쿠키는 포트를 가리지 않아서, 같은 브라우저로 `127.0.0.1`의 다른 웹 서비스를 열면 그 서비스도 이 앱의 로그인 쿠키를 받을 수 있습니다.

2026-11-27에 Windows 11의 WSL2 Ubuntu 24.04에서 새로 받은 저장소로 처음부터 끝까지 해 봤습니다. 다른 배포판이나 macOS에서는 해 보지 않았습니다.

## 필요한 것

- Linux 또는 WSL2(작업기가 Linux 전용입니다). 일반 사용자로 실행합니다(root 거부).
- Node.js 24(`node:sqlite`를 씁니다). 배포판 패키지가 낡았다면 [nodejs.org](https://nodejs.org/en/download)의 Linux 바이너리를 받아 `PATH`에 넣으면 됩니다.
- Python 3.12와 `venv`. Ubuntu에서 `python3 -m venv`가 ensurepip 오류를 내면 `sudo apt install python3.12-venv`를 하거나, sudo가 없으면 아래처럼 pip 없이 만들고 pip를 따로 넣습니다.
- 디스크 약 2GB(npm 패키지, Python 패키지, Whisper `base`와 화자 분리 모델).

## 순서

```bash
git clone https://github.com/hwan96-ai/korean-meeting-stt.git
cd korean-meeting-stt

npm ci
npm run build:oci

python3 -m venv .venv-worker      # ensurepip 오류가 나면 아래 'pip 없이 만들기'
.venv-worker/bin/python -m pip install -r requirements-worker.txt

node scripts/local-setup.mjs      # ~/.korean-meeting-stt 에 설정, DB, 작업기 등록을 만듭니다
```

터미널 두 개에서 각각 띄웁니다.

```bash
scripts/local-run.sh web          # 웹: http://127.0.0.1:3187
scripts/local-run.sh worker       # 작업기: 처음에는 모델을 내려받습니다(몇 분)
```

브라우저마다 처음 한 번은 아래 명령이 출력한 주소를 10분 안에 엽니다. 주소의 코드는 한 번만 쓰이고, 브라우저에는 로그인 쿠키가 남아 `/stt`로 넘어갑니다. 그 사이 주소를 남에게 보내지 마세요.

```bash
scripts/local-run.sh login
```

그다음부터는 **http://127.0.0.1:3187/stt** 를 열면 됩니다. 화면의 로그아웃은 그 브라우저의 쿠키를 지웁니다. 모든 브라우저를 한꺼번에 내보내려면 `node scripts/local-setup.mjs --rotate-login` 뒤 웹을 다시 띄웁니다. 음성 파일을 올리면 작업기가 화자를 나누고 받아 적습니다. 원문을 고치고 화자 이름을 정한 뒤 TXT로 내려받습니다.

시험해 본 2분 9초짜리 합성 회의는 WSL2(스레드 2개, Whisper `base`)에서 올린 뒤 1분 남짓에 녹취가 나왔습니다.

## 로컬 로그인 모드가 하는 일

`local-setup.mjs`가 만든 설정 파일(`~/.korean-meeting-stt/env`)에는 `MEETING_AUTH_MODE=local`이 들어 있습니다.

- 웹은 `HOSTNAME=127.0.0.1`이고 공개 주소가 `http://127.0.0.1:<포트>`일 때만 이 모드로 돕니다. Cloudflare Access 설정과 같이 쓸 수 없습니다. 하나라도 어긋나면 `local-run.sh web`과 `login`이 멈추고, 직접 띄웠더라도 모든 요청을 거부합니다.
- 소유자는 그 주소로 곧장 오고(다른 `Host`나 DNS 재바인딩 페이지는 거부), 프록시나 터널 헤더가 없고, 그 포트의 로그인 쿠키(HttpOnly, SameSite=Strict)를 가진 요청뿐입니다. 쿠키 값은 설정 파일의 비밀값이 아니라 그 비밀값에서 만든 세션 값입니다. 쿠키가 없는 같은 컴퓨터의 다른 사용자나 다른 사이트의 프레임은 소유자가 아닙니다.
- 쓰기 요청은 같은 주소에서 시작한 것만 받습니다. 화면은 다른 사이트의 프레임 안에 넣을 수 없습니다.
- 작업기는 프록시를 거치지 않고 `127.0.0.1`의 웹으로만 토큰을 보냅니다.
- **이 포트를 바깥에 열지 마세요.** 터널, 프록시뿐 아니라 `netsh interface portproxy`, `socat`, `ssh -R`, 편집기의 포트 전달처럼 헤더를 붙이지 않는 TCP 전달도 안 됩니다. 바깥에서 쓸 때는 [SELF-HOSTING.md](SELF-HOSTING.md)의 Cloudflare Access 방식을 씁니다.

모델을 받다가 끊겨 작업기가 계속 멈추면, 작업기를 끄고 `~/.korean-meeting-stt/worker/models` 폴더를 다른 곳으로 옮긴 뒤 다시 띄웁니다.

## pip 없이 만들기 (sudo가 없을 때)

```bash
python3 -m venv --without-pip .venv-worker
curl -fsSLo get-pip.py https://bootstrap.pypa.io/get-pip.py   # 내려받은 파일을 확인한 뒤 실행하세요
.venv-worker/bin/python get-pip.py
```

## API 전사로 바꾸기

`~/.korean-meeting-stt/credentials/worker.json`에 [SELF-HOSTING.md](SELF-HOSTING.md)의 API 키 8개를 더 넣고 작업기를 다시 띄웁니다. 파일은 본인만 읽을 수 있게(0600) 두세요. `local-setup.mjs`는 다시 돌려도 이미 있는 설정과 토큰을 바꾸지 않습니다.

## 지우기

멈춘 뒤 저장소 폴더와 `~/.korean-meeting-stt`를 지우면 됩니다. 올린 원음과 녹취는 `~/.korean-meeting-stt/data` 안에 있습니다.
