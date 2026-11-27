# Windows PC 작업 프로그램

서버에서 PC 없이 음성(로컬 또는 API 전사)을 처리하는 별도 실행 모드는 [Linux 서버 음성 작업기](SERVER.md)를 따른다. Windows DPAPI와 PC 전용 인증은 그대로 유지한다.

사이트의 전사 전용 작업(`output_mode=transcript`)도 같은 음성 처리·화자 정렬 경로를 사용하며 완료 시 `transcript_ready`를 반환한다. 전사 전용 작업이 잘못 회의록 생성 대기 상태로 전달되면 `transcript_only_job` 오류로 차단하고 생성기를 호출하지 않는다. 기존 작업처럼 출력 모드가 없으면 회의록 모드로 처리한다.

이 프로그램은 사이트 작업을 바깥으로 접속해 가져온다. Notion 인증과 기준 화자의 음성·특징값은 이 PC에만 보관한다. 원본 파일은 읽기만 수행한다. 모델/음성/녹취/실행 결과를 Git이나 배포 소스에 포함하지 않는다.

## 설치와 연결

Python 3.12 64bit 환경에서 프로젝트 폴더로 이동한다. 예시의 `python`은 사용할 가상환경 실행 파일 경로로 바꿀 수 있다.

```powershell
python -m pip install -r requirements-worker.txt
python -m local_worker setup-models --whisper base --diarization
```

이전 INT8 화자 모델 경로가 저장된 PC는 같은 `MEETING_WORKER_HOME`에서 `setup-models --diarization` 후 `migrate-models --segmentation-precision float32`를 실행한다. 암호화 인증·PC 연결·API 정책은 보존하고 검증한 모델 경로만 갱신한다. INT8 대비 대표 구간에서 탐지 구간이 크게 달라져 float32를 기본 후보로 사용하며, 이는 화자 정확도 검증을 대신하지 않는다. 클러스터 임계값 기본 0.5는 보정 전 값으로 실제 인원 수를 뜻하지 않는다.

`configure --stdin`은 JSON 객체를 stdin으로 받는다. 설정 전체를 현재 Windows 사용자 DPAPI로 `%LOCALAPPDATA%\CodexMeetingWorker\config.dpapi`에 암호화한다. 비밀값을 명령행 인수·소스·로그에 넣지 않는다. 안전한 호출 프로세스가 JSON을 stdin으로 전달해야 한다. `site_url`과 `sites_bypass_token`은 각각 `base_url`과 `sites_dispatch_token`의 별칭이다.

앱 샌드박스와 일반 터미널에서 같은 저장소를 쓰려면 `MEETING_WORKER_HOME` 환경 변수에 소스 폴더 밖의 절대 경로를 지정한다. 이 전용 변수는 설정·모델·내보내기 기록의 루트를 바꾸며 `HOME`/`LOCALAPPDATA`/`CODEX_HOME`을 변경하지 않는다. `scripts/start-worker.ps1 -WorkerHome 'D:\meeting-worker' -Python '실제 Python 경로'`도 같은 경로를 전달한다. configure/setup-models/run 모두 같은 홈으로 실행해야 한다.

| 설정 | 의미 |
| --- | --- |
| `site_url` | HTTPS 사이트 주소. localhost 미리보기는 HTTP 허용 |
| `sites_bypass_token` | 소유자 전용 Sites 서비스 접근 토큰. 브라우저 소유자 인증 대체 용도가 아님 |
| `device_token` | 선택 사항. 없으면 암호학적 난수로 생성 |
| `workspace` | 로컬 작업 폴더. 기본 프로젝트 상위 `workspace` |
| `whisper_model` | 기본 `base`; `small` 등으로 변경 가능 |
| `cpu_threads`, `chunk_seconds` | 기본 4개 스레드, 600초 전사 청크 |
| `speech_mode` | `auto`는 API 키/음성 모델 설정 시 API, 그 외 로컬. `local`/`api`로 명시 선택 가능 |
| `api_speech_model`, `api_speech_price_per_minute` | 지원 음성 모델과 확인한 분당 단가. 키는 텍스트 API와 같은 로컬 설정 사용 |
| `api_speech_billing_rounding_seconds` | 요청별 보수적 청구 반올림. 기본 60초. 실제 청구 기준을 확인한 경우만 조정 |
| `generation_mode` | 기본 `unconfigured`; 사이트 작업은 명시적인 `api` 설정 필요 |
| `openai_api_key`, `api_model` | 지원되는 Responses API 키와 모델. 이 PC 암호화 설정에만 보관 |
| `api_max_cost_usd` | 작업별 API 비용 상한. 양수 필수 |
| `api_prices_per_million` | 확인한 모델 단가 `{input,output}`. 백만 토큰당 USD |
| `api_max_output_tokens` | 기본 12000. 보수적 입력 바이트 상한과 함께 비용을 사전 제한 |
| `match_threshold`, `match_margin` | 기본 0.75/0.08. 검증된 정확도 수치가 아니며 실제 녹음으로 조정 필요 |
| `notion_targets` | 회의 유형별 로컬 DB/data source 허용 목록. 고객 DB 기본값은 가짜 예시 ID라 자기 DB로 바꿔야 함, 주간 DB 미지정 |
| `notion_selects` | 기존 DB select/status 값. 예: `상태: 완료`. 새 옵션을 자동 생성하지 않음 |
| `notion_token_file` | 선택 사항. 기존 Windows DPAPI Notion token.xml의 고정된 로컬 경로. 앱/일반 터미널 경로가 다를 때 지정 |

```powershell
python -m local_worker configure --stdin
python -m local_worker pair --code BROWSER_CODE
python -m local_worker doctor
python -m local_worker configure-api
python -m local_worker run
```

연결 코드는 로그인한 사이트 브라우저에서 먼저 발급한다. PC 인증은 분리되어 있고 사이트에서 해제할 수 있다. `run --once`는 한 번의 수신/처리/응답만 수행한다. `scripts/start-worker.ps1`도 같은 프로그램을 실행한다.

API 키와 정책 추가는 직접 연 로컬 터미널에서 `configure-api`를 실행한다. 키는 화면에 보이지 않는 입력으로 받고 기존 PC/사이트 설정을 보존하며 DPAPI로 저장한다. 키를 채팅·브라우저·명령행 인수·셸 기록에 넣지 않는다. 이 설정 명령은 유료 요청을 실행하지 않는다.

## 실제 실행 경로

PyAV로 실제 음성을 디코딩하고 별도 16kHz/mono WAV를 만든다. 빈 파일·손상·무음·4시간 초과를 구분한다. faster-whisper(기본 CPU/int8, `whisper_device`로 GPU 선택 가능)가 한국어 단어 시각을 반환한다. 전사는 청크마다 원자적 체크포인트를 저장하며 재시작 시 완성된 청크를 재사용한다.

화자 분석 기본 버전은 `bounded-global-2026-11-14.1`이다. 서버 기준값은 블록 안 0.7, 전역 0.6이고, native 최소 발화 길이는 0.5초다(작성자의 실제 회의 녹음으로 정한 값, 실험 기록은 공개하지 않음). 단어 경계를 ms로 반올림하므로, 4 ms보다 긴 단어에서 2 ms 이하로 옆 구간에 닿기만 하는 것은 겹침으로 보지 않는다. 기준값이나 최소 발화 길이를 바꾸면 화자 구간이 달라진다. 이미 API 전사 체크포인트(`api-asr.json`)가 있는 작업 폴더를 새 worker가 이어받으면 `api_speech_checkpoint_invalid`로 멈추고 이미 낸 비용은 다시 쓰지 않는다. 그래서 배포는 lease 0에서만 한다. 공식 sherpa native 분석을 **300초 블록+양쪽 5초 문맥**으로 제한하고, 같은 TitaNet 모델에서 겹침 없는 3~30초 대표 특징을 추출해 회의 전체에서 다시 군집화한다. 블록별 A/B 번호만으로 동일 인물을 연결하지 않는다. 문맥 중복은 원래 블록 소유 시간으로 잘라내며 실제 겹침은 보존한다. 전역 결과는 겹치거나 맞닿는 구간만 합치고 다른 화자의 짧은 발화를 덮도록 빈 간격을 채우지 않는다. 짧거나 겹친 기준밖에 없으면 이름 연결을 추측하지 않고 미확인으로 유지한다. 블록 결과와 로컬 특징을 체크포인트로 보관하므로 임계값 검토나 재시작 때 완료된 분석을 반복하지 않는다. 모델 SHA·버전·블록 크기·문맥·임계값이 바뀌면 잘못된 캐시를 재사용하지 않는다. 71분 전체를 단일 native 호출로 분석하던 방식은 메모리 회귀가 발견되어 기본 경로에서 제외했다.

이 분석은 별도 로컬 프로세스에서 실행한다. 네이티브 코드가 Python GIL을 오래 점유해도 부모 프로그램은 30초마다 임대를 갱신하고 취소를 확인한다. 자식에는 음성/모델 경로와 계산 설정만 전달하며 인증 설정을 보내지 않는다. 임대가 끊기면 자식을 종료하고 결과를 게시하지 않는다. 단어와 화자 구간의 겹침으로 정렬하고 미확인·겹침은 검토 상태로 남긴다. 이름은 자동 확정하지 않는다.

등록은 사용자가 직접 듣고 확인한 단일 화자 구간만 한 요청당 3~120초 허용한다. 기준 음성과 특징은 로컬 `workspace/speakers`에 저장하며 클라우드에는 이름/직책/회사/모델 식별 정보만 전송한다. 등록 요청 완료 응답을 확인하기 전에는 `profile-pending`에 격리하고 이름 제안에 사용하지 않는다. 응답이 불명확하거나 요청이 변경되면 격리를 유지한다. 같은 인물 ID로 여러 회의의 기준을 추가할 수 있다. 동일 모델 SHA에서 코사인 유사도와 상위 두 후보 간격을 통과해야 이름을 **제안**한다. 등록 취소는 기준 음성과 특징을 함께 지우며 과거 확정 회의록은 변경하지 않는다.

공개 모델은 sherpa 공식 배포의 pyannote segmentation float32(MIT) 및 NVIDIA NeMo TitaNet-S(Apache-2.0: NGC 모델 카드의 NeMo 라이선스 링크)를 사용한다. INT8 모델은 비교용으로만 함께 보관한다. 다운로드 SHA256를 코드에 고정하고 로컬 manifest와 라이선스를 함께 보관한다. Whisper base/small은 코드의 Hugging Face 커밋으로 고정한다. 모델은 PC에서만 다운로드하며 회의 음성은 모델 배포처에 보내지 않는다.

## 회의록 생성

사이트에서 검토해 저장한 녹취 버전만 회의록을 생성한다. API 모델·키·단가·비용 상한이 없으면 `needs_setup`으로 멈춘다. 개인 구독으로 STT를 지원한다고 표시하지 않는다. API 음성 처리 모드는 파일과 브라우저 녹음을 같은 경로로 처리하며 먼저 로컬 화자 분석을 수행한다. 실제 원음 시간축 전체를 작은 클립으로 나누며 화자 구간 사이의 미확인/겹침 부분도 빠뜨리지 않는다. 현대 음성 API의 텍스트 응답에는 원음 클립의 실제 시작/종료 시각을 연결하고 단어 시각으로 표시하지 않는다. 각 요청은 25MB 미만으로 검증하고 클립 단위 결과/진행을 보존한다.

모호한 외부 응답은 `unknown_outcome`으로 기록하고 자동 재호출하지 않는다. 작업별 공통 API 예산 장부는 음성의 모든 클립과 회의록 요청·수정본·재시도 비용을 합산한다. 회의록 요청 전에 JSON 전체 크기·프레이밍 여유분·최대 출력량의 비용을 예약한다. 사용량이 반환되면 보고된 토큰 수와 설정 단가를 사용한 보수적 비용으로 정산하고, 검증에 실패한 유료 출력도 합산한다. 캐시 할인 등 실제 청구 차이는 이 계산에 반영하지 않으므로 청구 확정액으로 표시하지 않는다. 결과가 불명확하면 예약액을 유지한다. 단가는 운영자가 현재 모델 가격과 맞춰야 한다.

`inspect-outcomes --job-id UUID`는 본문 없이 로컬 요청 ID·시각·결과 상태를 보여준다. API 제공자 기록과 로컬 결과를 확인해 이전 요청에 결과가 없다는 사실을 사람이 확인한 경우에만 `resolve-outcome --job-id UUID --attempt-id ATTEMPT_UUID --confirmed-no-result`로 차단을 해제한다. 완료 여부를 확인하지 못했으면 해제하지 않는다. 이 명령은 새 생성 요청을 실행하지 않는다.

개인 Codex 구독은 배포 사이트의 처리 경로에서 사용할 수 없다. 웹 작업에서 `local_codex`를 설정해도 실행을 거부한다. 개인 로컬 CLI 작업은 별도 명령으로 명시적으로 선택한다.

```powershell
python -m local_worker generate-local --bundle reviewed.json --output LOCAL_OUTPUT --reviewed
```

이 명령은 현재 PC의 Codex 로그인만 사용한다. 인증 파일을 복사하지 않는다. 사용자 설정/MCP/플러그인/앱/쉘/브라우저/다중 에이전트 등을 비활성화하고 read-only로 실행한다. 모델 출력은 검토용 초안이다. 결론/실행 항목의 녹취 근거는 로컬 생성 기록에 남긴다. 모든 생성 경로가 번들 `templates` 규칙과 섹션/표를 검사하고 제목·메타데이터는 결정적으로 붙인다. 누락 날짜를 파일명에서 추측하지 않는다.

## Notion: 이 PC에서 최종 검토 후 저장

```powershell
python -m local_worker export-notion --bundle reviewed.json --reviewed
python -m local_worker export-notion --job-id JOB_UUID --reviewed
```

`--reviewed`는 운영자가 최종 본문과 메타데이터를 확인했다는 명시적 확인이다. 최신 녹취에 대한 회의록인지, 필수 메타데이터, 허용 DB와 현재 속성 형식/기존 select 옵션을 검증한다. 제목과 메타데이터는 DB 속성에 넣고 본문은 `1. 회의 결론`부터 실제 제목·글머리표·표로 만든다. 저장 후 본문/표/강조를 읽어 비교한다. Notion 토큰은 기존 `%LOCALAPPDATA%\CodexNotionLocal\token.xml`에서 현재 사용자 권한으로 해독하며 클라우드에 전달하지 않는다.

내보내기는 버전/대상별 OS 잠금으로 직렬화한다. 페이지 생성·블록 추가 직전에 로컬 의도를 기록하며 같은 버전 재실행은 같은 페이지/진행을 사용한다. 응답이 불명확하면 중복 페이지나 블록을 만들지 않도록 재전송을 차단한다. 이 경우 로컬 `%LOCALAPPDATA%\CodexMeetingWorker\exports` 기록과 Notion 페이지를 대조해 사람이 확인해야 한다. 저장 완료된 기록을 지우고 재실행하면 중복 생성될 수 있으므로 운영 기록을 보존한다.

## 검증

```powershell
python -m unittest discover -s tests/local_worker -v
python -m local_worker analyze --input ORIGINAL_AUDIO --output LOCAL_OUTPUT --start 2100 --seconds 60 --whisper base
```

`analyze`는 원본을 변경하지 않으며 결과 텍스트는 로컬 파일에만 저장한다. stdout에는 실행 시간/구간 수만 표시한다. 사람의 정답 녹취·화자 라벨이 없으면 CER나 인물 재식별 정확도를 계산하거나 합격으로 주장하지 않는다. 부분 표본을 전체 파일 처리 완료로 취급하지 않는다.

원문: [sherpa Python 예제](https://github.com/k2-fsa/sherpa-onnx/blob/master/python-api-examples/offline-speaker-diarization.py), [TitaNet-S 모델/라이선스](https://catalog.ngc.nvidia.com/orgs/nvidia/nemo/models/titanet_small), [Codex 비대화형 실행](https://learn.chatgpt.com/docs/non-interactive-mode), [OpenAI 텍스트 생성](https://developers.openai.com/api/docs/guides/text).
