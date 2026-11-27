# 음성 처리

## 두 가지 전사 방식

| | API 전사 | 로컬 전사 |
| --- | --- | --- |
| 엔진 | OpenAI `gpt-transcribe` | faster-whisper(OpenAI Whisper의 CTranslate2 변환본) |
| 비용 | 쓴 만큼 요금. 단가는 설정에 직접 넣음 | 없음(전기와 시간) |
| 원음이 가는 곳 | 웹 서버에 저장, 작업기가 조각을 OpenAI로 보냄 | 웹 서버에 저장, 작업기 안에서만 처리 |
| 언어 | 기본 한국어(`ko`). 서버 모드에는 바꾸는 설정 없음 | 한국어 고정 |
| 장치 | 상관없음 | CPU 기본, NVIDIA GPU 설정 가능(실험) |

어느 방식이든 원음은 먼저 브라우저에서 웹 서버로 올라갑니다. 화자 분석은 어느 쪽이든 sherpa-onnx로 CPU에서 돕니다. 서버 모드는 다른 방식으로 자동 전환하지 않습니다. API가 실패하면 로컬로 바꾸지 않고, GPU가 안 되면 CPU로 바꾸지 않고 멈춥니다. (예전 PC 모드에만 로컬 실패 뒤 API로 넘기는 `allow_api_speech_fallback` 설정이 있습니다.)

## API 쪽 안전장치

- **비용 상한**: `local_worker/budget.py`가 전사, 회의록 생성, 재시도를 장부 하나로 셉니다. 요청 전에 예약하고, 상한을 넘을 요청은 보내지 않습니다.
- **전역 차단**: 크레딧 소진, 할당량 초과, 결제 한도, 응답이 끊겨 과금 여부를 알 수 없는 경우에는 서버 전체의 전사 API 호출을 멈춥니다. 사람이 확인하고 풀어야 다시 돕니다(`local_worker/api_circuit.py`). 회의 정보 추정(글 API)에서 크레딧 소진, 할당량, 결제 한도가 나면 같은 전역 차단이 걸립니다. 시간 초과나 원인 모를 429 같은 그 밖의 불분명한 결과는 추정 전용 차단(`api-safety-text/`)만 걸고, 전사는 계속 돌며 과금됩니다. 풀 때는 `python -m local_worker.server --resume-api`(추정 쪽은 `--text`를 붙임)에 확인 옵션을 함께 줍니다.
- **한 번에 하나**: 유료 요청은 동시에 하나만 보냅니다. 보내기 전에 의도를 기록해 두어, 작업기가 중간에 죽어도 같은 조각을 몰래 다시 과금하지 않습니다.
- **전사 용어**: 고유명사를 `keywords`로 함께 보낼 수 있습니다(`lib/speech-keywords.ts`).

회의 정보 추정은 API(`meta_inference_mode="api"`) 말고 ChatGPT 구독의 Codex CLI(`"codex"`)로도 할 수 있게 코드가 남아 있습니다. 작성자 개인 용도이고 약관을 확인하지 않아 안내하지 않습니다.

## 로컬 Whisper 모델

| 이름 | Hugging Face 저장소 | 크기(대략) |
| --- | --- | --- |
| `base` | `Systran/faster-whisper-base` | 0.15GB |
| `small` | `Systran/faster-whisper-small` | 0.49GB |
| `medium` | `Systran/faster-whisper-medium` | 1.5GB |
| `large-v3` | `Systran/faster-whisper-large-v3` | 3.1GB |
| `large-v3-turbo` | `dropbox-dash/faster-whisper-large-v3-turbo` | 1.6GB |

리비전은 `local_worker/core.py`의 `WHISPER_MODELS`에 고정되어 있고, 설치 명령이 그 리비전만 받습니다.

```bash
python -m local_worker setup-models --whisper small --diarization
```

서버 모드는 CPU일 때 `base`, `small`만 받습니다. 작성자 서버의 메모리 제한(3GB)에 맞춘 값입니다. `whisper_device`가 `cuda`이면 큰 모델도 받습니다.

## 장치 설정

| `whisper_device` | 받는 `whisper_compute_type` |
| --- | --- |
| `cpu`(기본) | `int8`(기본), `int8_float32`, `int16`, `float32` |
| `cuda` | `float16`, `int8_float16`, `int8`, `int8_float32`, `float32` |

- `cuda`에는 CUDA 12 cuBLAS와 cuDNN 9가 필요합니다.
- 라이브러리가 없으면 작업이 `needs_setup`(GPU 설정 필요)으로 멈춥니다. 한 번 실패하면 같은 프로세스에서 다시 GPU를 시도하지 않습니다. 실패한 CUDA를 다시 부르면 멈춘 채 응답이 없는 경우를 실제로 겪었기 때문입니다. 라이브러리를 깐 뒤 작업기를 다시 시작하세요.
- 서버 모드는 시작할 때 GPU에서 1초짜리 무음을 한 번 전사해 보고, 안 되면 시작하지 않습니다.
- **작성자는 실제 GPU 전사를 돌려 보지 못했습니다.** 라이브러리가 없을 때 멈추는 동작까지만 2GB 노트북 GPU(MX450)로 확인했습니다.

## 측정 방법 (2026-11-26)

| 항목 | 값 |
| --- | --- |
| 음성 | 25분 4초 합성 회의. 한국어 TTS 한 목소리를 세 음높이로 바꿔 24문장을 돌려 말하게 했습니다. 정답 대본 5,625자 |
| API | 작성자 서버에서 `gpt-transcribe`, 겹치기 끈 실행의 전사 단계 539초 |
| 로컬 | Intel i7-1165G7(4코어), `cpu_threads=4`, `int8`, 600초 조각, beam 5, VAD 켬. 모델 불러오기와 전사를 따로 재고 내려받기 시간은 뺐습니다 |
| 글자 오류율 | 띄어쓰기와 문장부호를 빼고 글자와 숫자만 남긴 뒤 글자 편집 거리를 정답 글자 수로 나눔. 화자는 채점하지 않음 |

| 방식 | 전사 시간 | 음성 길이 대비 | 글자 오류율 |
| --- | --- | --- | --- |
| API `gpt-transcribe` | 539초 | 0.36배 | 4.2% |
| 로컬 `large-v3-turbo` | 590초 | 0.39배 | 5.1% |
| 로컬 `small` | 374초 | 0.25배 | 5.2% |
| 로컬 `base` | 200초 | 0.13배 | 8.4% |

읽을 때 주의할 점입니다.

- 합성 음성이라 잡음과 겹쳐 말하기가 거의 없습니다. 실제 회의 정확도를 대표하지 않습니다.
- API 실행에는 전사 용어 힌트를 보냈고 로컬에는 없었습니다. 대본에 고유명사가 거의 없어 영향은 작다고 보지만 재지는 않았습니다.
- API 시간은 대부분 요청을 기다린 시간입니다(요청 638건). 서버 성능과는 거의 상관이 없습니다.
- 화자 분석은 따로 더해집니다. 같은 회의가 이 노트북에서 176~203초, 작성자 서버에서 393초였습니다. 서버는 API 전사와 겹쳐 전체를 617초에 끝냅니다. 겹치기는 지금 API 전사에서만 됩니다.
- `large-v3-turbo`는 측정 당시 `mobiuslabsgmbh/faster-whisper-large-v3-turbo`라는 이름이었고, 지금은 이름이 바뀐 `dropbox-dash/...` 저장소를 고정해 두었습니다.
- `large-v3`와 `medium`은 재지 않았습니다.
