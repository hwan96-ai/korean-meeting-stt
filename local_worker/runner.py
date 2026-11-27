from __future__ import annotations

import gc
import hashlib
import json
import os
from pathlib import Path
import shutil
import threading
import time
from urllib import parse

from .client import SiteClient
from .core import WorkerError, SingleWorker, atomic_json, job_directory, read_json, safe_id, whisper_runtime_label
from .generation import generate
from .speech import prepare_audio, transcribe, diarize, align, partition_acoustic_intervals
from . import profiles


def diarization_progress(directory: Path):
    """(finished blocks, all blocks) from the newest block checkpoint folder, or None before the first block."""
    newest, found = -1.0, None
    for path in (directory / "diarization-blocks").glob("*/progress.json"):
        try:
            stamp = path.stat().st_mtime
        except OSError:
            continue
        if stamp >= newest:
            value = read_json(path, {})
            done, total = value.get("completed_blocks"), value.get("total_blocks")
            if isinstance(done, int) and isinstance(total, int) and 0 <= done <= total and total > 0:
                newest, found = stamp, (done, total)
    return found


class Worker:
    def __init__(self, config: dict, client=None):
        self.config, self.client = config, client or SiteClient(config)
        self.workspace = Path(config["workspace"])
        self.active = None
        self.stop = threading.Event()
        self.lease_failure = None
        self.deleted_jobs, self.deleted_profiles = [], []
        self.profile_error = ""
        self.last_profile_signature = None
        # Progress rides on the lease heartbeat, but only after the web said it accepts the field:
        # an older web rejects unknown heartbeat fields, and a failed heartbeat would cost the lease.
        self.progress = None
        self.progress_supported = False
        # Set when the web refused a progress report; stays off until the next job.
        self.progress_rejected = False

    def capabilities(self):
        mode = self.speech_mode()
        server = self.config.get("execution_location") == "server"
        speech = "api/" + self.config.get("api_speech_model", "needs_setup") if mode == "api" else "local/faster-whisper/" + self.config["whisper_model"] + "/" + whisper_runtime_label(self.config)
        if server:
            try:
                manifest = read_json(Path(self.config["worker_home"]) / "models" / ("whisper-" + self.config["whisper_model"] + ".json"), {})
                if mode == "local" and (not manifest.get("local_path") or not Path(manifest["local_path"]).is_dir()):
                    speech = "needs_setup: setup-models --whisper " + self.config["whisper_model"]
            except (KeyError, ValueError, OSError, TypeError):
                if mode == "local":
                    speech = "needs_setup: setup-models --whisper " + self.config["whisper_model"]
        from .api_circuit import for_config
        from .meta_inference import capabilities as meta_capabilities
        circuit = for_config(self.config)
        return {"speech": speech, **(circuit.status() if circuit else {}), **(meta_capabilities(self.config) if server else {}),
                "diarization": "local/sherpa-onnx" if Path(self.config["diarization_model"]).is_file() and Path(self.config["embedding_model"]).is_file() else "needs_setup: setup-models --diarization",
                "generation": "unsupported" if server else self.config.get("generation_mode", "unconfigured"),
                "execution_location": "server" if server else "local", "notion": "unsupported" if server else "local_only",
                "profiles": "unsupported" if server else self.profile_error or "local_only"}

    def speech_mode(self):
        mode = self.config.get("speech_mode", "auto")
        if self.config.get("execution_location") == "server":
            if mode not in {"local", "api"} or self.config.get("allow_api_speech_fallback"):
                raise WorkerError("server_native_only", "서버 음성 모드와 fallback 금지 설정을 확인하세요.", "needs_setup")
            if mode == "api":
                from .api_speech import _policy
                _policy(self.config)
            return mode
        if mode not in {"auto", "local", "api"}:
            raise WorkerError("speech_mode", "음성 모드는 auto/local/api 중 선택하세요.", "needs_setup")
        if mode == "auto":
            return "api" if (self.config.get("openai_api_key") or os.environ.get("OPENAI_API_KEY")) and self.config.get("api_speech_model") else "local"
        return mode

    def check(self):
        if self.lease_failure:
            raise self.lease_failure

    def set_progress(self, stage, done=0, total=0, reader=None):
        """Replace (never mutate) the progress record the lease thread reads."""
        self.progress = {"stage": stage, "done": int(done), "total": int(total), "reader": reader}

    def progress_payload(self):
        current = self.progress
        if not current:
            return None
        done, total = current["done"], current["total"]
        if current.get("reader"):
            try:
                found = current["reader"]()
            except Exception:
                found = None
            if not found:
                # Unreadable or not written yet: send nothing, so the web keeps the last count and its speed base.
                return None
            done, total = found
        total = max(0, int(total))
        return {"stage": current["stage"], "done": max(0, min(int(done), total)) if total else 0, "total": total}

    def heartbeat(self, job=None):
        payload = {"capabilities": self.capabilities(), "deleted_job_ids": self.deleted_jobs, "deleted_profile_ids": self.deleted_profiles}
        lease_job = job if job is not None else self.active
        if lease_job:
            payload["lease"] = {"job_id": lease_job["id"], "lease_id": lease_job["lease_id"], "generation": lease_job["generation"]}
            progress = self.progress_payload() if self.progress_supported and not self.progress_rejected else None
            if progress:
                payload["progress"] = progress
        try:
            reply = self.client.call("POST", "/api/worker/heartbeat", payload, retry_safe=True)
        except WorkerError as exc:
            # A web rolled back to a version without progress must not cost the lease: resend once without it.
            if "progress" not in payload or exc.code != "site_http_400":
                raise
            self.progress_rejected = True
            payload.pop("progress")
            reply = self.client.call("POST", "/api/worker/heartbeat", payload, retry_safe=True)
        self.progress_supported = reply.get("accepts_progress") is True
        self.deleted_jobs, self.deleted_profiles = [], []
        for deletion in reply.get("deletions", []):
            target = self.workspace / "jobs" / safe_id(deletion["id"])
            root = (self.workspace / "jobs").resolve()
            if target.exists():
                if target.resolve().parent != root or target.is_symlink():
                    raise WorkerError("unsafe_path", "삭제할 작업 경로가 작업 영역을 벗어납니다.")
                shutil.rmtree(target)
            self.deleted_jobs.append(deletion["id"])
        for deletion in reply.get("profile_deletions", []):
            if self.config.get("execution_location") == "server":
                continue
            profiles.delete_profile(self.workspace / "speakers", deletion["person_id"])
            profiles.delete_pending_profiles(self.workspace / "profile-pending", deletion["person_id"])
            self.deleted_profiles.append(deletion["person_id"])

    def _keep_lease(self, job, stop, interval=30):
        while not stop.wait(interval):
            try:
                self.heartbeat(job)
            except WorkerError as exc:
                # Processing must stop on lost identity/lease, not continue uploading stale data.
                if self.active is job:
                    self.lease_failure = exc
                return
            except Exception:
                if self.active is job:
                    self.lease_failure = WorkerError("heartbeat_local_error", "PC 임대 갱신을 확인할 수 없습니다. 로컬 결과를 보존합니다.", retryable=True)
                return

    def result(self, job: dict, **kwargs):
        self.check()
        result = self.client.result(job, **kwargs)
        atomic_json(job_directory(self.workspace, job["id"]) / "manifest.json", result)
        return result

    def process(self, job: dict, speech_keywords=None):
        directory = job_directory(self.workspace, job["id"])
        atomic_json(directory / "manifest.json", job)
        self.active, self.lease_failure = job, None
        self.progress_rejected = False
        self.set_progress("preparing")
        stop = threading.Event()
        self.stop = stop
        keeper = threading.Thread(target=self._keep_lease, args=(job, stop), name="meeting-lease", daemon=True)
        keeper.start()
        stage = "preparing"
        try:
            if self.config.get("execution_location") == "server":
                self.speech_mode()  # Reject an injected API/fallback policy before audio/provider access.
                from .api_circuit import for_config
                circuit = for_config(self.config)
                if circuit:
                    circuit.assert_open()
            if job.get("pending") == "minutes":
                if self.config.get("execution_location") == "server":
                    raise WorkerError("server_generation_unsupported", "서버 음성 작업기는 회의록 생성을 지원하지 않습니다.")
                if job.get("output_mode", "minutes") == "transcript":
                    raise WorkerError("transcript_only_job", "전사 전용 작업에서는 회의록을 생성할 수 없습니다.")
                stage = "generating"
                draft, usage = generate(job, directory, self.config)
                self.result(job, state="minutes_ready", stage=stage, draft=draft, generation_mode=draft["generation_mode"], usage={**(job.get("usage") or {}), "generation": usage})
                return
            source = directory / "input" / "original.audio"
            self.client.download(job, source)
            self.result(job, state="preparing", stage=stage, local_received=True)
            audio = directory / "prepared" / "analysis.wav"
            metadata = read_json(audio.with_suffix(".json"), {})
            if not audio.is_file() or metadata.get("source_sha256") != job.get("sha256"):
                metadata = prepare_audio(source, audio)
            self.check()
            mode, asr, diarization_error = self.speech_mode(), None, None
            if mode == "local":
                stage = "transcribing"
                self.result(job, state=stage, stage=stage, duration=metadata["duration"])
                self.set_progress("transcribing")
                try:
                    asr = transcribe(audio, directory / "asr.json", self.config, self.check,
                                     progress=lambda done, total: self.set_progress("transcribing", done, total))
                except WorkerError as exc:
                    if exc.code != "whisper_model_missing" or not self.config.get("allow_api_speech_fallback"):
                        raise
                    mode = "api"
                gc.collect()
            if mode == "api" and not (self.config.get("openai_api_key") or os.environ.get("OPENAI_API_KEY")):
                raise WorkerError("api_speech_key_missing", "API 음성 처리용 키·지원 모델·누적 비용 정책을 이 PC에서 설정하세요.", "needs_setup")
            if mode == "api":
                from .api_speech import is_ready
                if not is_ready(self.config):
                    raise WorkerError("api_speech_policy_missing", "configure-api로 지원 음성 모델·현재 단가·누적 비용 상한을 설정하세요.", "needs_setup")
            stage = "diarizing"
            self.result(job, state=stage, stage=stage, duration=metadata["duration"], **({"speech_mode": asr["mode"], "speech_complete": True} if asr else {}))
            # The blocks run in a child process; it leaves progress.json beside each finished block.
            self.set_progress("diarizing", reader=lambda: diarization_progress(directory))
            turns = None
            from . import pipeline_speech
            if mode == "api" and pipeline_speech.selected(self.config, directory):
                def pipeline_progress(step, done, total):
                    nonlocal stage
                    if step == "transcribing" and stage != "transcribing":
                        stage = "transcribing"
                        self.result(job, state=stage, stage=stage, duration=metadata["duration"])
                    if step == "transcribing":
                        self.set_progress("transcribing", done, total)

                try:
                    turns, asr = pipeline_speech.transcribe(audio, directory, self.config, self.check, keywords=speech_keywords,
                                                            progress=pipeline_progress)
                except pipeline_speech.FallBack:
                    turns = None
                except WorkerError as exc:
                    # Paid-request failures during the overlap belong to transcription, not diarization.
                    if exc.code.startswith("api_") or exc.code in {"silent_audio", "invalid_audio"}:
                        stage = "transcribing"
                    raise
            if turns is None:
                try:
                    turns = diarize(audio, directory / "diarization.json", self.config, self.check)
                except WorkerError as exc:
                    if exc.state == "needs_setup":
                        turns, diarization_error = [], exc
                    else:
                        raise
            if mode == "api" and asr is None:
                from .api_speech import transcribe as api_transcribe
                stage = "transcribing"
                self.result(job, state=stage, stage=stage, duration=metadata["duration"])
                self.set_progress("transcribing")
                intervals = partition_acoustic_intervals(turns, metadata["duration"])
                asr = api_transcribe(audio, directory / "api-asr.json", self.config, self.check, intervals=intervals, keywords=speech_keywords,
                                     progress=lambda done, total: self.set_progress("transcribing", done, total))
            if not asr or not asr["segments"]:
                raise WorkerError("no_speech", "전사 가능한 발화를 찾지 못했습니다. 원음을 검토하세요.", "needs_review")
            self.result(job, state="aligning", stage="aligning")
            stage = "aligning"
            self.set_progress("aligning")
            segments = align(asr["segments"], turns, asr["duration"])
            if self.config.get("execution_location") != "server":
                profiles.suggest(segments, turns, audio, self.workspace / "speakers", self.config)
            atomic_json(directory / "transcript.json", segments)
            speech_usage = {"speech": asr.get("usage", {}), "diarization": read_json(directory / "diarization.json", {}).get("diagnostics", {})}
            if not turns:
                error = diarization_error or WorkerError("no_speakers", "화자 구간을 찾지 못했습니다. 원음과 모델 설정을 확인하세요.", "needs_review")
                self.result(job, state="needs_review", stage=stage, segments=segments, duration=asr["duration"], speech_mode=asr["mode"],
                            speech_complete=True, diarization_complete=False, usage=speech_usage,
                            error={"code": error.code, "message": str(error), "retryable": True, "stage": "diarizing"})
            else:
                from .meta_inference import infer
                self.check()  # A lost lease must not buy a text-model call.
                self.set_progress("meta_inference")
                suggestions = infer(job, segments, directory, self.config)
                # Omitted when off: an older web rejects unknown result fields.
                self.result(job, state="transcript_ready", stage=stage, segments=segments, duration=asr["duration"],
                            speech_mode=asr["mode"], speech_complete=True, diarization_complete=True, usage=speech_usage,
                            **({"metadata_suggestions": suggestions} if suggestions is not None else {}))
        except WorkerError as exc:
            atomic_json(directory / "last-error.json", {"code": exc.code, "stage": stage, "state": exc.state})
            if exc.code != "lease_lost" and exc.state != "cancelled" and not self.lease_failure:
                try:
                    self.result(job, state=exc.state, stage=stage, error={"code": exc.code, "message": str(exc), "retryable": exc.retryable, "stage": stage})
                except WorkerError:
                    pass  # Local artifacts survive; no blind result replay after uncertain response.
            print(f"job={job['id']} stage={stage} state={exc.state} code={exc.code}", flush=True)
        except Exception:
            atomic_json(directory / "last-error.json", {"code": "local_processing_error", "stage": stage})
            try:
                self.result(job, state="failed", stage=stage, error={"code": "local_processing_error", "message": "로컬 처리 중 오류가 발생했습니다. 모델·메모리·디스크 상태를 확인하세요.", "retryable": True, "stage": stage})
            except WorkerError:
                pass
            print(f"job={job['id']} stage={stage} code=local_processing_error", flush=True)
        finally:
            stop.set()
            keeper.join(timeout=50)
            self.active = None
            self.progress = None

    def process_profiles(self):
        if self.config.get("execution_location") == "server":
            raise WorkerError("server_profiles_unsupported", "기준 화자 등록은 PC 전용입니다.")
        profiles.recover_enrollments(self.workspace / "speakers", self.workspace / "profile-pending")
        requests = self.client.call("GET", "/api/worker/profile-requests", retry_safe=True).get("requests", [])
        for item in requests:
            job_id = safe_id(item["job_id"])
            directory = job_directory(self.workspace, job_id)
            audio = directory / "prepared" / "analysis.wav"
            if not audio.is_file():
                # Separate explicit request grants access only to this job's reference audio.
                url = f"/api/worker/jobs/{job_id}/profile-audio?" + parse.urlencode({"generation": item["generation"], "request_id": item["request"]["request_id"]})
                source = directory / "input" / "profile.audio"
                source.parent.mkdir(parents=True, exist_ok=True)
                from urllib import request
                with self.client.opener.open(request.Request(self.config["base_url"] + url, headers=self.client.headers()), timeout=60) as response, source.open("wb") as output:
                    total = 0
                    for data in iter(lambda: response.read(1024 * 1024), b""):
                        total += len(data)
                        if total > 512 * 1024 * 1024:
                            raise WorkerError("audio_size", "등록 음성 크기가 허용 범위를 넘습니다.")
                        output.write(data)
                prepare_audio(source, audio)
            pending = profiles.stage_enrollment(self.workspace / "speakers", self.workspace / "profile-pending", audio, item["segments"], item["request"], self.config)
            self.client.call("POST", "/api/worker/profile-requests/complete", {"job_id": job_id, "generation": item["generation"], "request_id": item["request"]["request_id"]})
            profiles.acknowledge_enrollment(pending)
            profiles.commit_enrollment(self.workspace / "speakers", pending)
        public = [{key: profile[key] for key in ["person_id", "display_name", "role", "company", "profile_revision", "model_id"]} for profile in profiles.list_profiles(self.workspace / "speakers")]
        signature = hashlib.sha256(json.dumps(public, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if signature != self.last_profile_signature:
            self.client.call("POST", "/api/worker/profiles", {"profiles": public})
            self.last_profile_signature = signature

    def run(self, once=False):
        with SingleWorker(self.workspace / "worker-state"):
            while True:
                try:
                    self.heartbeat()
                    server = self.config.get("execution_location") == "server"
                    if not server:
                        try:
                            self.process_profiles()
                            self.profile_error = ""
                        except WorkerError as exc:
                            self.profile_error = exc.state + ": " + exc.code
                            print(f"profile state={exc.state} code={exc.code}", flush=True)
                    from .api_circuit import for_config
                    circuit = for_config(self.config)
                    if circuit and circuit.status()["api_blocked"]:
                        if once:
                            return
                        time.sleep(10)
                        continue
                    claim = self.client.call("POST", "/api/worker/claim", {"speech_only": True} if server else {})
                    job = claim.get("job")
                    if job:
                        # Sibling of the job: a site without the field means no terms.
                        self.process(job, claim.get("speech_keywords"))
                except WorkerError as exc:
                    print(f"worker state={exc.state} code={exc.code}", flush=True)
                    if once:
                        raise
                except Exception:
                    print("worker state=failed code=local_runtime_error", flush=True)
                    if once:
                        raise WorkerError("local_runtime_error", "PC 작업 프로그램의 로컬 파일·연결 상태를 확인하세요.", retryable=True) from None
                if once:
                    return
                time.sleep(10)
