# 다른 저작물

이 저장소의 코드는 [MIT](LICENSE)입니다. 함께 들어 있거나 실행할 때 내려받는 다른 저작물은 각자의 조건을 따릅니다.

## 저장소에 들어 있는 것

| 파일 | 출처 | 라이선스 |
| --- | --- | --- |
| `vendor/shadcn-tailwind-4.13.0.css` | shadcn | MIT, [`vendor/shadcn-tailwind-4.13.0.LICENSE.md`](vendor/shadcn-tailwind-4.13.0.LICENSE.md) |
| `scripts/vite/sites-vite-plugin.ts` 등 `scripts/` 일부 | 처음 시작한 앱 스타터 | MIT, [`scripts/vite/sites-vite-plugin.LICENSE`](scripts/vite/sites-vite-plugin.LICENSE) |

npm과 PyPI 의존성은 `package.json`, `package-lock.json`, `requirements-worker.txt`에 있고 각 패키지의 라이선스를 따릅니다.

## 설치할 때 내려받는 모델 (저장소에 없음)

| 모델 | 받는 곳 | 조건 |
| --- | --- | --- |
| Whisper `base`, `small`, `medium`, `large-v3` | Hugging Face `Systran/faster-whisper-*` | MIT (OpenAI Whisper 가중치의 CTranslate2 변환본) |
| Whisper `large-v3-turbo` | Hugging Face `dropbox-dash/faster-whisper-large-v3-turbo` | MIT |
| 화자 구간 나누기 | sherpa-onnx 배포본(원본 `pyannote/segmentation-3.0`) | 원본 MIT. 원본은 Hugging Face에서 접근 동의가 필요합니다. 변환 배포본의 조건은 따로 확인하세요 |
| 화자 임베딩 | sherpa-onnx 배포본(원본 NVIDIA NeMo TitaNet-S) | NGC 모델 카드가 NeMo 라이선스(Apache-2.0)를 가리킵니다. 직접 확인하세요 |

주소, 리비전, SHA-256은 `local_worker/core.py`(`WHISPER_MODELS`)와 `local_worker/models.py`에 고정되어 있습니다. 설치 명령은 받은 라이선스 파일을 모델 폴더에 함께 보관합니다.
