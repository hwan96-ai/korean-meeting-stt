from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .core import WHISPER_MODELS, WorkerError, config_path, defaults, load_config, save_config, atomic_json, read_json, safe_id


def main() -> int:
    parser = argparse.ArgumentParser(description="개인 Windows PC 회의록 작업 프로그램")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("configure").add_argument("--stdin", action="store_true", required=True, help="JSON 설정을 stdin으로만 수신하여 DPAPI 저장")
    pair = commands.add_parser("pair")
    pair.add_argument("--code", required=True)
    commands.add_parser("doctor")
    commands.add_parser("configure-api", help="이 PC 터미널에서 숨김 입력으로 API 키와 모델/비용 정책 설정")
    migrate = commands.add_parser("migrate-models", help="인증 설정을 보존하고 검증된 화자 모델 경로를 명시적으로 갱신")
    migrate.add_argument("--segmentation-precision", choices=["float32", "int8"], default="float32")
    run = commands.add_parser("run")
    run.add_argument("--once", action="store_true")
    models = commands.add_parser("setup-models")
    models.add_argument("--diarization", action="store_true")
    models.add_argument("--whisper", choices=list(WHISPER_MODELS))
    analyze = commands.add_parser("analyze")
    analyze.add_argument("--input", type=Path, required=True)
    analyze.add_argument("--output", type=Path, required=True, help="로컬 결과 폴더; 원본은 읽기만 수행")
    analyze.add_argument("--start", type=float, default=0)
    analyze.add_argument("--seconds", type=float)
    analyze.add_argument("--whisper", default="base")
    analyze.add_argument("--no-diarization", action="store_true")
    export = commands.add_parser("export-notion")
    export.add_argument("--bundle", type=Path)
    export.add_argument("--job-id")
    export.add_argument("--reviewed", action="store_true", help="본인이 최종 회의록과 메타데이터를 검토했음을 확인")
    personal = commands.add_parser("generate-local", help="배포 웹서비스와 분리된 개인 로컬 회의록 작성")
    personal.add_argument("--bundle", type=Path, required=True)
    personal.add_argument("--output", type=Path, required=True)
    personal.add_argument("--reviewed", action="store_true", required=True)
    personal.add_argument("--model", help="개인 로컬 Codex 모델 ID")
    inspect = commands.add_parser("inspect-outcomes", help="본문 없이 로컬 외부 요청 상태 확인")
    inspect.add_argument("--job-id", required=True)
    resolve = commands.add_parser("resolve-outcome", help="사람이 이전 외부 요청에 결과가 없음을 확인한 후에만 재시도 허용")
    resolve.add_argument("--job-id", required=True)
    resolve.add_argument("--attempt-id", required=True)
    resolve.add_argument("--confirmed-no-result", action="store_true", required=True)
    args = parser.parse_args()
    try:
        if args.command == "configure":
            config = json.load(sys.stdin)
            if not isinstance(config, dict):
                raise WorkerError("invalid_config", "JSON 객체 설정이 필요합니다.", "needs_setup")
            save_config(config)
            print(json.dumps({"configured": True, "credential_file": str(config_path())}))
        elif args.command == "setup-models":
            from .models import setup_diarization, setup_whisper
            if args.diarization:
                setup_diarization()
                print("diarization_models=ready")
            if args.whisper:
                setup_whisper(args.whisper)
                print("whisper_model=ready model=" + args.whisper)
        elif args.command == "analyze":
            import time
            from .speech import prepare_audio, transcribe, diarize, align, audio_samples, SAMPLE_RATE
            from .core import local_root
            config = defaults({"whisper_model": args.whisper})
            directory = args.output.resolve()
            directory.mkdir(parents=True, exist_ok=True)
            audio = directory / "analysis.wav"
            if args.input.resolve() == audio:
                raise WorkerError("original_protected", "원본과 다른 결과 폴더를 지정하세요.")
            started = time.monotonic()
            metadata = prepare_audio(args.input, audio)
            if args.start or args.seconds:
                import wave
                samples = audio_samples(audio, args.start, args.start + args.seconds if args.seconds else None)
                if not len(samples):
                    raise WorkerError("sample_range", "선택한 음성 구간이 비어 있습니다.")
                clip = directory / "sample.wav"
                with wave.open(str(clip), "wb") as output:
                    output.setparams((1, 2, SAMPLE_RATE, 0, "NONE", "not compressed"))
                    output.writeframes((samples * 32767).astype("<i2").tobytes())
                audio = clip
            preparation_seconds = time.monotonic() - started
            started = time.monotonic()
            asr = transcribe(audio, directory / "asr.json", config)
            asr_seconds = time.monotonic() - started
            started = time.monotonic()
            turns = [] if args.no_diarization else diarize(audio, directory / "diarization.json", config)
            diarization_seconds = time.monotonic() - started
            segments = align(asr["segments"], turns, asr["duration"])
            atomic_json(directory / "transcript.json", segments)
            metrics = {"duration": asr["duration"], "preparation_seconds": preparation_seconds, "asr_seconds": asr_seconds,
                       "diarization_seconds": diarization_seconds, "segments": len(segments), "speakers": len({turn['speaker'] for turn in turns}),
                       "speech_mode": asr["mode"], "quality": "unscored: no human reference", "source_start_seconds": args.start}
            atomic_json(directory / "metrics.json", metrics)
            print(json.dumps(metrics))
        elif args.command == "generate-local":
            from .generation import generate
            config = load_config() if config_path().is_file() else defaults({})
            job = read_json(args.bundle)
            directory = args.output.resolve()
            directory.mkdir(parents=True, exist_ok=True)
            draft, usage = generate(job, directory, {**config, "generation_mode": "local_codex", **({"codex_model": args.model} if args.model else {})}, personal_local=True)
            atomic_json(directory / "draft.json", {"draft": draft, "usage": usage})
            (directory / "minutes.md").write_text(draft["content"], encoding="utf-8")
            print(json.dumps({"state": "minutes_ready", "generation_mode": "local_codex", "output": str(directory / "minutes.md")}))
        else:
            config = load_config()
            from .client import SiteClient
            client = SiteClient(config)
            if args.command == "migrate-models":
                from .models import migrate_diarization
                updated = migrate_diarization(config, args.segmentation_precision)
                save_config(updated)
                print(json.dumps({"migrated": True, "segmentation_precision": updated["diarization_model_precision"],
                                  "model_sha256": updated["diarization_model_revision"], "cluster_threshold": updated["cluster_threshold"]}))
            elif args.command == "configure-api":
                import getpass
                import math
                if not sys.stdin.isatty() or not sys.stderr.isatty():
                    raise WorkerError("interactive_terminal_required", "API 키 설정은 직접 연 로컬 터미널에서 실행하세요. 키를 채팅이나 명령행에 넣지 마세요.", "needs_setup")
                key = getpass.getpass("OpenAI API 키 (숨김 입력, 빈 값은 기존 키 유지): ").strip() or config.get("openai_api_key")
                if not key:
                    raise WorkerError("api_key_missing", "API 키를 입력하지 않아 기존 설정을 유지했습니다.", "needs_setup")
                model = input("지원되는 API 모델 ID: ").strip() or config.get("api_model", "")
                if not model:
                    raise WorkerError("api_model_missing", "모델을 입력하지 않아 기존 설정을 유지했습니다.", "needs_setup")
                cap = float(input("작업별 최대 비용 USD (양수): ").strip())
                input_price = float(input("확인한 입력 단가 USD / 백만 토큰: ").strip())
                output_price = float(input("확인한 출력 단가 USD / 백만 토큰: ").strip())
                maximum = int(input("최대 출력 토큰 (빈 값:12000): ").strip() or "12000")
                if not all(math.isfinite(value) for value in [cap, input_price, output_price]) or cap <= 0 or min(input_price, output_price) < 0 or not 256 <= maximum <= 32000:
                    raise WorkerError("api_policy_invalid", "비용·단가·최대 출력 토큰을 확인하세요. 기존 설정은 유지했습니다.", "needs_setup")
                speech_model = input("음성 API 모델 ID (빈 값:gpt-transcribe 또는 기존 값): ").strip() or config.get("api_speech_model", "gpt-transcribe")
                speech_price = float(input("확인한 음성 단가 USD / 분: ").strip())
                speech_rounding = int(input("음성 요청별 보수적 청구 반올림 초 (빈 값:60): ").strip() or "60")
                speech_mode = input("음성 처리 방식 auto/local/api (빈 값:auto): ").strip() or "auto"
                if not math.isfinite(speech_price) or speech_price <= 0 or not 1 <= speech_rounding <= 60 or speech_mode not in {"auto", "local", "api"}:
                    raise WorkerError("api_policy_invalid", "음성 모델·단가·청구 단위·실행 방식을 확인하세요. 기존 설정은 유지했습니다.", "needs_setup")
                save_config({**config, "generation_mode": "api", "openai_api_key": key, "api_model": model,
                             "api_max_cost_usd": cap, "api_prices_per_million": {"input": input_price, "output": output_price}, "api_max_output_tokens": maximum,
                             "speech_mode": speech_mode, "api_speech_model": speech_model, "api_speech_price_per_minute": speech_price,
                             "api_speech_billing_rounding_seconds": speech_rounding})
                print("API 키와 정책을 이 PC에 암호화 저장했습니다. 유료 테스트 요청은 실행하지 않았습니다.")
            elif args.command == "pair":
                client.call("POST", "/api/worker/pair", {"code": args.code.upper(), "device_token": config["device_token"]})
                print("paired=true")
            elif args.command == "doctor":
                import importlib.util
                print(json.dumps({"configured": True, "site_url": config["base_url"], "credentials": "DPAPI/current Windows user",
                    "modules": {name: importlib.util.find_spec(name) is not None for name in ["av", "numpy", "faster_whisper", "sherpa_onnx"]},
                    "diarization_models": all(Path(config[key]).is_file() for key in ["diarization_model", "embedding_model"]),
                    "generation_mode": config["generation_mode"], "api_key_configured": bool(config.get("openai_api_key")), "notion": "local_only"}))
            elif args.command == "run":
                from .runner import Worker
                Worker(config, client).run(once=args.once)
            elif args.command in {"inspect-outcomes", "resolve-outcome"}:
                from .core import job_directory
                directory = job_directory(Path(config["workspace"]), args.job_id)
                records = [(path, read_json(path)) for path in directory.glob("generation-*.json")]
                ledger = read_json(directory / "api-budget.json", {"attempts": []})
                speech_records = [record for record in ledger["attempts"] if record.get("stage") == "transcribing"]
                if args.command == "inspect-outcomes":
                    print(json.dumps([{key: record.get(key) for key in ["attempt_id", "started_at", "finished_at", "mode", "stage", "state", "error_code"]} for record in [*(value for _, value in records), *speech_records]]))
                else:
                    selected = [(path, record) for path, record in records if record.get("attempt_id") == args.attempt_id and record.get("state") in {"inflight", "unknown_outcome"}]
                    speech = next((record for record in speech_records if record.get("attempt_id") == args.attempt_id and record.get("state") in {"inflight", "unknown_outcome", "resolved_no_result"}), None)
                    if len(selected) != 1 and speech is None:
                        raise WorkerError("unknown_attempt", "확인할 로컬 외부 요청 기록을 찾을 수 없습니다.", "needs_review")
                    job = client.call("GET", f"/api/worker/jobs/{safe_id(args.job_id)}/export-bundle", retry_safe=True)
                    if job.get("unresolved_external"):
                        job = client.call("POST", f"/api/worker/jobs/{safe_id(args.job_id)}/resolve-outcome", {"generation": job["generation"], "confirmed_no_result": True, "request_id": args.attempt_id})
                    if speech:
                        from .api_speech import resolve_no_result
                        resolve_no_result(directory, args.attempt_id, config["api_max_cost_usd"])
                    else:
                        path, record = selected[0]
                        atomic_json(path, {**record, "state": "operator_confirmed_no_result"})
                        if any(value.get("attempt_id") == args.attempt_id for value in ledger["attempts"]):
                            from .budget import JobBudget
                            JobBudget(directory, config["api_max_cost_usd"]).settle(args.attempt_id, state="operator_confirmed_no_result", error_code="operator_confirmed_no_result")
                    print(json.dumps({"state": job["state"], "resolved_attempt_id": args.attempt_id, "reservation_retained": True}))
            elif args.command == "export-notion":
                from .notion_export import export as export_notion
                if bool(args.bundle) == bool(args.job_id):
                    raise WorkerError("export_input", "--bundle 또는 --job-id 중 하나를 지정하세요.", "needs_setup")
                job = read_json(args.bundle) if args.bundle else client.call("GET", f"/api/worker/jobs/{safe_id(args.job_id)}/export-bundle", retry_safe=True)
                result = export_notion(job, config, reviewed=args.reviewed)
                print(json.dumps({key: result.get(key) for key in ["state", "page_id", "page_url", "completed_blocks"]}))
        return 0
    except WorkerError as exc:
        print(json.dumps({"state": exc.state, "code": exc.code, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except (ValueError, OSError, KeyError):
        print('{"state":"needs_setup","code":"invalid_local_input","message":"로컬 파일과 JSON 설정을 확인하세요."}', file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
