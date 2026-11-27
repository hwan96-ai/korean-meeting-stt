# Linux 서버 음성 작업기

전용 Linux 서비스 사용자로 기존 CPU 전사·익명 화자 분리 파이프라인을 실행한다. 전사는 기본 local이며, 명시적 API 설정으로 gpt-transcribe를 사용할 수 있다. Windows PC의 DPAPI 파일·Notion 인증·기준 화자 파일은 읽거나 복사하지 않는다. 서버는 회의록 문장 생성, Notion 및 기준 화자 등록을 지원하지 않는다. API 설정은 [SELF-HOSTING](../docs/SELF-HOSTING.md)에 있다.

## 실행 계약

- Python 3.12 ARM64, 기존 `requirements-worker.txt` 사용. 2026-10-30 PyPI에서 고정한 6개 직접 의존성의 cp312 aarch64 또는 공통 wheel을 확인했다. 실제 서버 설치·모델 실행 검증은 별도로 필요하다.
- 전용 사용자 `meeting-speech` (root 실행 거부), worker home `/var/lib/meeting-speech`, workspace `/var/lib/meeting-speech/workspace`.
- home/workspace는 절대 경로, root 또는 서비스 사용자 소유, group/other 접근 없는 디렉터리여야 한다. workspace는 home 안의 별도 하위 폴더여야 한다.
- `MEETING_WORKER_MODE=server`, `MEETING_WORKER_HOME=/var/lib/meeting-speech`를 명시한다. `HOME` 등 기존 공통 환경 변수를 덮어쓰는 기능은 없다.
- systemd `LoadCredential=worker.json:/etc/meeting-speech/worker.json`. 원본은 root 0600·부모 root 0700으로 운영자가 만든다. 프로그램은 systemd의 `CREDENTIALS_DIRECTORY/worker.json`만 읽는다. runtime credential은 root 또는 현재 UID 소유 regular file, 기본적으로 0400/0600 등 group/other 권한이 없는 파일이어야 한다. 링크·32KiB 초과·허용하지 않은 설정 키를 거부한다.
- 실제 systemd는 root:root 디렉터리 0550·파일 0440에 현재 서비스 UID의 읽기 ACL을 줄 수 있다. 이 형식은 `/run/credentials/<unit>.service/worker.json`에 한해 허용한다. 같은 열린 fd의 ACL을 검사하여 정확히 root owner와 현재 UID만 r-x(폴더)/r--(파일), group object/other는 0, mask는 해당 읽기 권한이어야 한다. 다른 named user/group·쓰기·일반 0640 파일은 거부한다. `/run/credentials` 자체도 root 소유·그룹/other 비쓰기여야 한다. 이 예외는 home/workspace/source의 일반 권한 검사를 완화하지 않는다.
- source의 root 소유·권한은 systemd 운영 구성에서 관리한다. 프로그램이 실제 읽는 것은 runtime 복사본이며 이 복사본의 UID/권한/형식을 다시 검사한다.

기본 credential JSON 키는 다음 7개다. 명시 API 모드에는 [SELF-HOSTING](../docs/SELF-HOSTING.md)의 API 키 8개를 추가한다. 비밀값을 쉘 인수·명령 기록·소스·채팅에 넣지 않고 서버 운영 도구의 메모리에서 root 전용 파일로 만든다.

| 키 | 내용 |
| --- | --- |
| `base_url` | `http://127.0.0.1:3187`. HTTPS 원격 주소도 지원하며 평문 HTTP는 loopback만 허용 |
| `device_token` | 서버 전용 40자 이상 URL-safe 난수. PC 토큰을 재사용하지 않음 |
| `sites_dispatch_token` | OCI gateway 인증값. 이름은 기존 클라이언트 호환용 |
| `workspace` | `/var/lib/meeting-speech/workspace`; 생략 시 home/workspace |
| `whisper_model` | 초기 `base`, 선택 `small`. `whisper_device`가 `cuda`이면 `medium`, `large-v3`, `large-v3-turbo`도 받는다 |
| `whisper_device`, `whisper_compute_type` | 초기 `cpu`, `int8`. GPU는 `cuda`와 `float16` 또는 `int8_float16`(아래 "로컬 Whisper 모델과 장치") |
| `cpu_threads` | 초기 2, 허용 1–2 |
| `chunk_seconds` | 초기 600, 허용 30–600 |

서버 화자 분리는 float32 segmentation, bounded-global 300초+5초 문맥을 사용한다. 전사는 명시 local(설정한 장치, 기본 CPU/int8) 또는 API 모드이며 자동 전환/fallback은 없다. 분리 임계값은 검증된 사람 식별 정확도를 뜻하지 않는다. 실제 인원 수·이름을 추측하지 않는다.

## 모델 준비와 실행

아래는 전용 사용자 환경에서 실행한다. 모델 설치는 credential이나 Windows 설정이 필요 없다. 서비스는 `UMask=0077`과 전용 자원 제한으로 운영한다.

```bash
/opt/meeting-speech/venv/bin/python -m pip install -r requirements-worker.txt
env MEETING_WORKER_HOME=/var/lib/meeting-speech /opt/meeting-speech/venv/bin/python -m local_worker setup-models --whisper base --diarization
```

systemd가 credential을 공급한 서비스 환경의 entry point:

```bash
/opt/meeting-speech/venv/bin/python -m local_worker.server --doctor
/opt/meeting-speech/venv/bin/python -m local_worker.server
```

`--once`는 한 번 heartbeat·claim·처리 후 종료한다. 모든 서버 실행은 heartbeat 전에 고정 segmentation/embedding SHA와 native 의존성을 검증하고 sherpa OfflineSpeakerDiarization 모델을 실제 로드한다. local 모드에서는 Whisper manifest revision·home 내부 경로를 검사하고 Whisper를 설정한 장치(기본 CPU/int8)로 실제 로드한다. API 모드에서는 정책 형식만 검사하며 HTTP나 Whisper 로드 없이 확인한다. 음성을 처리하지 않는다. 필수 모델이 손상됐거나 로드되지 않으면 `needs_setup` 코드로 종료하며 heartbeat·claim을 보내지 않는다. 서비스의 `ExecStartPre`에도 `--doctor`를 지정할 수 있다.

### 로컬 Whisper 모델과 장치 (`speech_mode="local"`)

`setup-models --whisper <이름>`은 아래 표의 저장소를 고정 리비전으로 받는다(`local_worker/core.py` `WHISPER_MODELS`). 모두 OpenAI Whisper 가중치를 CTranslate2로 바꾼 공개본이고 MIT다.

| 이름 | 저장소 | 크기 |
| --- | --- | --- |
| `base` | `Systran/faster-whisper-base` | 약 0.14GB |
| `small` | `Systran/faster-whisper-small` | 약 0.46GB |
| `medium` | `Systran/faster-whisper-medium` | 약 1.5GB |
| `large-v3` | `Systran/faster-whisper-large-v3` | 약 3.1GB |
| `large-v3-turbo` | `dropbox-dash/faster-whisper-large-v3-turbo`(예전 이름 `mobiuslabsgmbh/...`, 같은 커밋) | 약 1.6GB |

`worker.json`의 `whisper_device`(`cpu` 또는 `cuda`)와 `whisper_compute_type`으로 장치를 고른다. 기본은 `cpu`, `int8`이다. GPU 예: `"whisper_device": "cuda", "whisper_compute_type": "float16"`(메모리가 적으면 `int8_float16`). `cpu`는 `float16`, `int8_float16`을 받지 않는다.

- 서버 모드는 CPU일 때 `base`, `small`만 받는다. 저장소의 유닛은 `MemoryMax=3G`, 스레드 1~2개라 큰 모델을 CPU로 올리지 않는다. `cuda`일 때만 `medium`, `large-v3`, `large-v3-turbo`를 받는다. 큰 모델을 쓰면 유닛의 `MemoryMax`도 그 머신에 맞게 올린다(미측정).
- 시작 검증(`--doctor`)은 `cuda`일 때 모델을 GPU로 불러 1초 무음을 한 번 전사해 본다. CUDA 라이브러리는 첫 전사에서 불러오기 때문이다. 여기서 실패하면 heartbeat 없이 `needs_setup`으로 끝난다.
- 작업 중 cuBLAS나 cuDNN을 못 찾았다는 오류는 `whisper_gpu_unavailable`(`needs_setup`)로 멈춘다. 그 뒤로 같은 작업기 프로세스는 다시 시작할 때까지 `cuda` 작업을 바로 같은 오류로 거절한다. 같은 프로세스에서 CUDA 모델을 다시 만들면 첫 전사에서 멈춰 임대를 붙잡는 것을 작성자 PC(Windows, CTranslate2 4.8.4)에서 확인했기 때문이다.
- 전사 중 GPU 메모리 부족 같은 다른 오류는 예전처럼 다시 시도할 수 있는 처리 오류다. 모델을 불러올 때 CUDA, GPU, compute type을 말하는 오류(불러올 때의 GPU 메모리 부족 포함)는 `whisper_gpu_unavailable`이다. 모델이 없거나 손상된 경우는 장치와 상관없이 `whisper_model_missing`이라, PC 모드의 API 대체 설정이 그대로 적용된다.
- `cuda`는 `int16`을 받지 않는다(CTranslate2가 CUDA int16을 지원하지 않음).
- `ops/server-speech/configure_host.py`를 다시 돌려도 `whisper_model`, `whisper_device`, `whisper_compute_type`은 남는다.
- medium이나 large 모델로 하던 PC 작업이 있었다면, 고정 리비전이 바뀌어 그 작업의 전사 체크포인트는 한 번 처음부터 다시 만든다.
- CUDA 라이브러리가 없을 때 CTranslate2가 파이썬 예외 없이 프로세스를 끝내는 경우가 있다고 알려져 있다(작성자 미확인). 그럴 때는 시작 검증에서 걸러지길 기대하고, 작업 중이었다면 같은 작업을 다시 잡기 전에 설정을 `cpu`로 돌린다.
- GPU에는 NVIDIA 드라이버와 CUDA 12용 cuBLAS, cuDNN 9가 필요하다. 작성자 PC(MX450 2GB)에서는 CTranslate2가 GPU를 찾았지만 cuBLAS가 없어 실행은 확인하지 못했다.
- 장치를 바꾸면 그 작업의 전사 체크포인트는 새로 시작한다(기본 `cpu`/`int8` 체크포인트는 그대로 쓴다).

참고 실측(2026-11-26, 25분 합성 회의, Intel i7-1165G7 노트북 CPU 4스레드, `int8`): `base` 200초(글 오류율 8.4%), `small` 374초(5.2%), `large-v3-turbo` 590초(5.1%). 같은 음성의 `gpt-transcribe` API 전사는 539초(4.2%)였다. 서버 모드는 스레드를 1~2개만 받으므로 이보다 느리다. GPU 실행은 이 숫자에 들어 있지 않다.

`--doctor`는 이 검증을 마친 뒤 `models_verified=true`와 capability만 출력하며 토큰·설정 본문은 출력하지 않는다. PC의 `configure`, `pair`, API/Notion CLI를 호출하지 않는다. startup 검증을 통과하지 못한 서비스의 준비 상태를 heartbeat 성공으로 가장하지 않는다.

## API 동작과 중단

capability는 `execution_location=server`, `generation/notion/profiles=unsupported`이다. claim body는 `{speech_only:true}`이며 정상 전사는 기존 결과 계약을 사용한다. 서버에 회의록 생성 작업이 잘못 전달되면 `server_generation_unsupported`로 실패시키고 생성기를 호출하지 않는다. 허용되지 않은 API 정책·fallback 주입은 음성 접근 전 차단한다. 기준 화자 요청을 poll/commit/제안하지 않고 익명 화자만 남긴다. API 차단 상태에서도 doctor와 heartbeat는 차단 이유를 보고하며 서비스를 재시작해 자동 해제하지 않는다.

Native 화자 분석은 별도 자식 프로세스로 실행하며 부모가 30초 간격 임대 갱신·취소를 확인한다. 자식에는 로컬 모델/음성 경로·계산 파라미터만 보내며 credential 디렉터리와 알려진 API/gateway/device 환경 변수를 제거한다. 임대 실패 시 자식을 끝내고 결과를 게시하지 않는다. 블록·전사 checkpoint는 전용 workspace에만 남겨 재시작 시 재사용한다. 로그는 작업 UUID·단계·오류 코드이며 음성·녹취·자격 본문을 출력하지 않는다.

## 화자 분석과 전사 겹치기 (기본 꺼짐)

`worker.json`에 `"speech_pipeline": true`를 넣고 `speech_mode`가 `"api"`, 모델이 `gpt-transcribe`일 때만 켜진다. 값은 true나 false만 받는다. 켜면 화자 분석 자식 프로세스가 5분 블록 하나를 마칠 때마다, 부모가 그 블록의 전사 조각을 바로 보낸다. 끄면 예전처럼 화자 분석이 다 끝난 뒤 전사를 시작한다. 코드와 시험 기록은 `pipeline_speech.py`와 [속도 개선 기록](../docs/SPEECH.md)에 있다.

- 유료 요청은 지금처럼 한 번에 하나다. 비용 예약, 보내기 전 기록, 전역 차단, 응답 처리는 기존 함수(`api_speech._send_clip`)를 그대로 쓴다.
- 조각 경계는 블록 안 화자 구간으로 정한다. 다음 블록이 와도 바뀌지 않는 부분만 확정해 `api-asr-pipeline-api.json`에 먼저 적고 나서 보낸다. 이름이 `-api.json`으로 끝나서 `resolve_no_result`와 `ops/api-stt/inspect_jobs.py`가 이 파일도 찾는다. 처음 계획한 조각 길이(`plan_seconds`)를 기록에 남겨, 도중에 `api_speech_chunk_seconds`를 바꿔도 그 작업은 처음 길이로 이어 간다. 화자 이름표, 겹침, 섞임은 회의 전체를 다시 묶은 뒤 한 번 붙인다. 두 화자나 빈 구간에 걸친 조각은 예전처럼 `unknown`이다.
- 한 작업은 시작한 방식을 끝까지 쓴다. `api-asr-api.json`(예전 방식 기록)이 있으면 설정과 상관없이 예전 방식으로 이어 가고, `api-asr-pipeline-api.json`이 있으면 설정을 꺼도 겹치기 방식으로 이어 간다. 기록 없이 유료 시도만 장부에 있으면 어느 방식도 다시 계획하지 않고 멈춘다.
- 화자 모델이 없거나 블록 기록 없이 화자 분석 결과만 남아 있으면, 아무것도 계획하지 않은 상태에서만 예전 방식으로 넘어간다.
- 블록 결과는 그 블록 음성의 해시가 맞고, 그 음성이 원음의 같은 위치와 같을 때만 계획에 쓴다. 요청 하나를 보내기 직전마다 임대를 다시 확인한다.
- 예전 방식이라면 돈을 쓰기 전에 거절했을 작업은 아무것도 계획하기 전에 예전 방식으로 넘긴다. 조각 길이 설정이 1초 미만인 작업, 조각 수 상한(20,000개)을 넘을 수 있는 긴 녹음(0.5초 × 20,000 = 약 2시간 47분 초과), 화자 분석 결과는 있는데 블록 기록이 빠진 작업이 그렇다.
- 겹치기 방식은 서버 작업기(`execution_location`이 `server`)에서만 켜진다. 전역 차단은 서버에만 있기 때문이다.
- 화자 분석이 도중에 실패하면 이미 보낸 조각은 기록에 남는다. 다시 시도하면 화자 분석은 끝난 블록부터, 전사는 보내지 않은 조각부터 이어 간다. 화자 모델을 바꾸면 블록 기록 폴더가 달라져 그 작업은 이어 갈 수 없다(`api_speech_checkpoint_invalid`).
- 겹치는 동안 난 유료 요청 오류는 웹에 `transcribing` 단계 오류로 보고한다.
- 웹 단계 표시는 블록이 도는 동안 `diarizing`, 화자 분석이 끝난 뒤 남은 조각을 보내는 동안 `transcribing`이다. 그래서 속도 기록의 화자 분석 시간에는 겹쳐 보낸 전사 시간이 들어간다. 웹은 속도 기록을 `api` 묶음 하나로 모으므로, 켠 뒤 한동안 남은 시간 예상이 어긋날 수 있다.
- 이 키를 아는 릴리스보다 옛 worker로 되돌릴 때는 먼저 `worker.json`에서 `speech_pipeline` 줄을 지운다. 옛 worker는 모르는 키가 있으면 시작하지 않는다. `ops/server-speech/configure_host.py`는 이 키를 보존한다.
- 작업 폴더의 `speech-timing.json`에 그 실행의 요청 수, 요청 대기 시간 합과 중앙값, 그 밖의 시간(`other_seconds`, 겹치기 방식에서는 블록을 기다린 시간 포함)만 숫자로 남긴다. 다시 시도한 실행은 0부터 센다. 겹치기 방식이 도중에 멈추면 그때까지의 숫자를 `pipelined_interrupted`로 남긴다. 이 파일을 쓰다 실패해도 작업은 실패하지 않는다. 화자 분석 `progress.json`에는 블록별 걸린 초(`block_elapsed_seconds`)가 붙는다. 글, 음성, 이름은 남기지 않는다.

## 회의 정보 추정 (기본 꺼짐)

`worker.json`에 `meta_inference_mode="api"`, `api_text_model`, `api_text_prices_per_million`(`{"input","output"}`, 100만 토큰당 달러), `api_meta_max_cost_usd`, 그리고 기존 `openai_api_key`와 `api_max_cost_usd`가 모두 올바를 때만 켜진다. `api_text_max_output_tokens`는 256~4000, 기본 2000이다. 하나라도 없거나 틀리면 꺼진 것으로 보고 전사는 그대로 돈다. capability는 `meta_inference`(`off`, `api/<모델>`, `needs_setup: meta_inference_policy`)와 `meta_inference_blocked`, `meta_inference_block_reason`이다. `api_*` 이름을 쓰지 않으므로 이 차단은 전사 claim을 멈추지 않는다.

회의록 모드 작업의 녹취가 `transcript_ready`가 되기 직전에 Responses API를 한 번 부르고 결과를 같은 결과 요청의 `metadata_suggestions`에 싣는다. 꺼져 있으면 그 필드를 아예 보내지 않는다(옛 웹이 결과 전체를 거부하지 않도록). 실패는 녹취를 막지 않고 이유 코드만 남긴다. 기록은 작업 폴더의 `metadata-inference-<녹취 해시>.json`이다. 해시는 녹취 문장만으로 만들므로 모델이나 상한 설정을 바꿔도 같은 녹취로 다시 과금하지 않는다. 완료 기록은 모델 답을 보관하고, 작업기가 다시 돌아 구간 ID가 바뀌어도 현재 구간으로 다시 검사해 싣는다. 진행 중, 결과 불명확(시간 초과, 5xx, 원인 모를 429), 응답 형식 오류, 출력 상한에 걸린 미완성 응답 기록이 있으면 그 녹취로는 다시 부르지 않는다. 요청 제한 시간 90초는 헤더와 본문을 포함한 요청 전체에 걸린다. 요청은 별도 스레드에서 돌고, 90초(+1초)가 지나면 기다림을 끊고 결과 불명확으로 기록한다. 비용 장부에도 녹취 해시를 남겨, 기록 파일이 사라져도 이미 보낸 녹취는 다시 부르지 않는다. 기록 해시는 자르기 전 모든 줄의 번호와 말 내용만으로 만들어, 긴 녹취에서 화자 이름이 바뀌어도 다시 과금하지 않는다. 길이 제한은 웹과 같은 UTF-16 기준이고, 고객사는 150자, 장소는 200자로 저장 계약과 같다. 참석 후보의 직책과 소속은 인용문에 있을 때만 남긴다. 비용은 같은 `api-budget.json`에 `stage=meta_inference`로 예약하고, 이 단계 합계가 `api_meta_max_cost_usd`를 넘으면 부르지 않는다. 이 단계의 불명확 기록은 전사 재개를 막지 않는다.

크레딧 소진, quota, 결제 한도는 기존 전역 차단(`api-safety/`)을 건다. 음성이 API 모드가 아니어도 같은 전역 차단 파일에 기록하고 추정 전에 검사한다. 그래서 나중에 음성을 API 모드로 바꿔도 크레딧 차단이 유지된다. 이 차단은 heartbeat의 `meta_inference_blocked`에도 보이고, 음성이 로컬 모드여도 `--text` 없는 `--resume-api`로 푼다. 그 밖의 429, 시간 초과, 연결 끊김, 형식 오류, 400/401/403/404는 추정 전용 차단(`api-safety-text/`)만 건다. 미완성 응답과 거절이나 빈 답은 차단 없이 그 작업만 실패로 남기고 사용량대로 정산한다. 그 녹취로는 다시 부르지 않는다. 추정 전용 차단 해제는 같은 절차에 `--text`를 붙인다.

```bash
/opt/meeting-speech/venv/bin/python -m local_worker.server --resume-api --text --confirm-credit-policy --confirm-provider-outcomes-reviewed
```

복구는 전용 서비스만 중지하고 모델 파일·권한·오류 코드를 확인한 뒤 재시작한다. workspace를 삭제하거나 다른 서비스를 재시작하지 않는다. 서버에 credential을 제공한 것만으로 웹 소유자 권한이 생기지 않으며 서버 장치 레코드는 웹 운영 담당자가 같은 소유자로 명시 provision해야 한다.
