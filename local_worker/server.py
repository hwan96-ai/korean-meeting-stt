"""Explicit Linux native-speech entry point using systemd LoadCredential."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import sys
from urllib.parse import urlsplit

from .core import WHISPER_MODELS, WorkerError, defaults, read_json, sha256, whisper_runtime

CREDENTIAL_NAME = "worker.json"
ALLOWED_SETTINGS = {"base_url", "device_token", "sites_dispatch_token", "workspace", "whisper_model", "cpu_threads", "chunk_seconds"}
# Local Whisper device (cpu or cuda) and compute type. Default cpu/int8.
ALLOWED_SETTINGS |= {"whisper_device", "whisper_compute_type"}
SERVER_CPU_WHISPER_MODELS = {"base", "small"}
ALLOWED_SETTINGS |= {"speech_mode", "openai_api_key", "api_speech_model", "api_speech_price_per_minute", "api_max_cost_usd",
                     "api_speech_billing_rounding_seconds", "api_speech_chunk_seconds", "api_credit_only_ack"}
# Meeting-info inference (ops/meeting-info-inference/SPEC.md). Missing or invalid means off.
ALLOWED_SETTINGS |= {"meta_inference_mode", "api_text_model", "api_text_prices_per_million", "api_text_max_output_tokens", "api_meta_max_cost_usd"}
# Subscription mode: this project's own Codex binary and login home (never a shared ~/.codex).
ALLOWED_SETTINGS |= {"meta_codex_path", "meta_codex_home", "meta_codex_model", "meta_codex_timeout"}
# Off unless true: transcribe finished diarization blocks while later blocks are still analysed.
ALLOWED_SETTINGS |= {"speech_pipeline"}


def _failure(code: str) -> WorkerError:
    # Never include supplied paths, configuration values or OS exception text.
    return WorkerError(code, "서버 음성 작업기의 실행 모드·전용 경로·credential 권한을 확인하세요.", "needs_setup")


def validate_private(metadata, *, directory: bool, uid: int) -> None:
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077 or metadata.st_uid not in {0, uid}:
        raise _failure("server_credential_permissions")


def validate_systemd_acl(metadata, data: bytes, *, directory: bool, uid: int) -> None:
    """Accept only systemd's root + this service UID read-only POSIX ACL."""
    permission = 5 if directory else 4
    mode = 0o550 if directory else 0o440
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_type(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_gid != 0 or stat.S_IMODE(metadata.st_mode) != mode:
        raise _failure("server_credential_permissions")
    if len(data) < 4 or (len(data) - 4) % 8 or struct.unpack_from("<I", data)[0] != 2:
        raise _failure("server_credential_acl")
    entries = [struct.unpack_from("<HHI", data, index) for index in range(4, len(data), 8)]
    undefined = 0xffffffff
    expected = {(1, permission, undefined), (2, permission, uid), (4, 0, undefined), (16, permission, undefined), (32, 0, undefined)}
    if len(entries) != 5 or set(entries) != expected:
        raise _failure("server_credential_acl")


def systemd_credential_path(path: Path, *, directory: bool) -> bool:
    value = PurePosixPath(os.fspath(path))
    unit = value if directory else value.parent
    if unit.parent != PurePosixPath("/run/credentials") or not unit.name.endswith(".service") or (not directory and value.name != CREDENTIAL_NAME):
        return False
    root = Path("/run/credentials").lstat()
    return stat.S_ISDIR(root.st_mode) and root.st_uid == 0 and root.st_gid == 0 and not stat.S_IMODE(root.st_mode) & 0o022


def validate_credential_descriptor(path: Path, descriptor: int, uid: int, *, directory: bool) -> None:
    metadata = os.fstat(descriptor)
    try:
        validate_private(metadata, directory=directory, uid=uid)
    except WorkerError:
        if not systemd_credential_path(path, directory=directory):
            raise _failure("server_credential_permissions") from None
        # Read ACL from the same open object checked by fstat, never a re-resolved path.
        try:
            acl = os.getxattr(descriptor, "system.posix_acl_access")
        except OSError:
            raise _failure("server_credential_acl") from None
        validate_systemd_acl(metadata, acl, directory=directory, uid=uid)


def private_directory(path: Path, uid: int, *, credential: bool = False) -> None:
    if not path.is_absolute():
        raise _failure("server_absolute_path_required")
    if credential:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_DIRECTORY", 0))
        try:
            validate_credential_descriptor(path, descriptor, uid, directory=True)
        finally:
            os.close(descriptor)
    else:
        validate_private(path.lstat(), directory=True, uid=uid)


def read_private_credential(path: Path, uid: int) -> dict:
    # O_NOFOLLOW + fstat closes the final-component symlink/replacement race.
    if path.is_symlink():
        raise _failure("server_credential_permissions")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as file:
        validate_credential_descriptor(path, file.fileno(), uid, directory=False)
        data = file.read(32769)
    if len(data) > 32768:
        raise _failure("server_credential_size")
    value = json.loads(data)
    if not isinstance(value, dict) or set(value) - ALLOWED_SETTINGS:
        raise _failure("server_credential_fields")
    return value


def load_server_config(environ=None) -> dict:
    environ = os.environ if environ is None else environ
    if not sys.platform.startswith("linux") or environ.get("MEETING_WORKER_MODE") != "server":
        raise _failure("explicit_linux_server_mode_required")
    uid = os.geteuid()
    if uid == 0:
        raise _failure("server_unprivileged_user_required")
    try:
        home = Path(environ["MEETING_WORKER_HOME"])
        credentials = Path(environ["CREDENTIALS_DIRECTORY"])
        private_directory(home, uid)
        private_directory(credentials, uid, credential=True)
        raw = read_private_credential(credentials / CREDENTIAL_NAME, uid)
        address = urlsplit(raw.get("base_url", ""))
        if (address.scheme != "https" and not (address.scheme == "http" and address.hostname in {"localhost", "127.0.0.1", "::1"})) or not address.hostname or address.username or address.password or address.query or address.fragment or address.path not in {"", "/"}:
            raise _failure("server_site_url")
        for key in ("device_token", "sites_dispatch_token"):
            if not isinstance(raw.get(key), str) or not re.fullmatch(r"[A-Za-z0-9_-]{40,512}", raw[key]):
                raise _failure("server_auth_required")
        workspace = Path(raw.get("workspace", str(home / "workspace")))
        private_directory(workspace, uid)
        if not workspace.resolve().is_relative_to(home.resolve()) or workspace.resolve() == home.resolve():
            raise _failure("server_workspace_outside_home")
        model = raw.get("whisper_model", "base")
        threads = raw.get("cpu_threads", 2)
        chunk = raw.get("chunk_seconds", 600)
        try:
            device, compute = whisper_runtime(raw)
        except WorkerError:
            raise _failure("server_native_policy") from None
        # CPU keeps the measured small models (unit MemoryMax=3G, 1-2 threads); larger ones need cuda.
        models = WHISPER_MODELS if device == "cuda" else SERVER_CPU_WHISPER_MODELS
        if model not in models or type(threads) is not int or not 1 <= threads <= 2 or type(chunk) is not int or not 30 <= chunk <= 600:
            raise _failure("server_native_policy")
        mode = raw.get("speech_mode", "local")
        if mode not in {"local", "api"}:
            raise _failure("server_speech_mode")
        if type(raw.get("speech_pipeline", False)) is not bool:
            raise _failure("server_speech_pipeline")
        config = defaults({**raw, "base_url": raw["base_url"].rstrip("/"), "workspace": str(workspace),
            "whisper_model": model, "cpu_threads": threads, "chunk_seconds": chunk,
            "whisper_device": device, "whisper_compute_type": compute,
            "worker_home": str(home),
            "execution_location": "server", "speech_only": True, "speech_mode": mode,
            "allow_api_speech_fallback": False, "generation_mode": "unsupported", "notion_targets": {},
            "speech_pipeline": raw.get("speech_pipeline", False) is True and mode == "api",
            "diarization_model": str(home / "models" / "segmentation.float32.onnx"),
            "embedding_model": str(home / "models" / "embedding.onnx"),
            "diarization_strategy": "bounded_global", "diarization_block_seconds": 300, "diarization_context_seconds": 5,
            # Fixed here because configure_host rewrites only its own keys. 0.5/0.5 split each main speaker
            # into two IDs on the 2026-11-13 38-minute meeting; 0.7/0.6 kept one ID per person.
            "cluster_threshold": 0.7, "global_cluster_threshold": 0.6})
        if mode == "api":
            from .api_speech import _policy
            _policy(config)  # Key/model/rate/attestation format only; no network.
        return config
    except WorkerError:
        raise
    except (OSError, ValueError, KeyError, TypeError):
        raise _failure("server_credential_invalid") from None


def probe_models(config: dict) -> None:
    """Load trusted models before any heartbeat or claim: CPU diarization, and Whisper on the configured device."""
    from .models import SEGMENTATION_FLOAT32_SHA256, EMBEDDING_SHA256
    from .speech import DIARIZATION_MIN_DURATION_ON, DIARIZATION_MIN_DURATION_OFF
    try:
        home = Path(config["worker_home"]).resolve()
        segmentation, embedding = Path(config["diarization_model"]), Path(config["embedding_model"])
        for path, expected in [(segmentation, SEGMENTATION_FLOAT32_SHA256), (embedding, EMBEDDING_SHA256)]:
            if not path.is_file() or not path.resolve().is_relative_to(home) or sha256(path) != expected:
                raise _failure("server_model_checksum")
        import av  # noqa: F401 - require native decode dependency at startup
        import numpy  # noqa: F401
        import sherpa_onnx as sherpa
        asr = None
        if config.get("speech_mode", "local") == "local":
            manifest = read_json(home / "models" / ("whisper-" + config["whisper_model"] + ".json"), {})
            whisper = Path(manifest.get("local_path", "__not_installed__"))
            if manifest.get("revision") != config["whisper_revision"] or not whisper.is_dir() or not whisper.resolve().is_relative_to(home):
                raise _failure("server_whisper_model_missing")
            from faster_whisper import WhisperModel
            device, compute = whisper_runtime(config)
            asr = WhisperModel(str(whisper), device=device, compute_type=compute, cpu_threads=config["cpu_threads"],
                               local_files_only=True, revision=config["whisper_revision"])
            if device != "cpu":
                # CUDA libraries load on the first encode; one second of silence surfaces them before claiming work.
                segments, _ = asr.transcribe(numpy.zeros(16000, dtype=numpy.float32), language="ko", beam_size=1, vad_filter=False)
                list(segments)
        else:
            from .api_speech import _policy
            _policy(config)
        settings = sherpa.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(segmentation)),
                num_threads=config["cpu_threads"]),
            embedding=sherpa.SpeakerEmbeddingExtractorConfig(model=str(embedding), num_threads=config["cpu_threads"]),
            clustering=sherpa.FastClusteringConfig(num_clusters=-1, threshold=config["cluster_threshold"]),
            min_duration_on=DIARIZATION_MIN_DURATION_ON, min_duration_off=DIARIZATION_MIN_DURATION_OFF)
        if not settings.validate():
            raise _failure("server_native_model_configuration")
        diarization = sherpa.OfflineSpeakerDiarization(settings)
        del asr, diarization
        import gc
        gc.collect()
    except WorkerError:
        raise
    except Exception:
        raise _failure("server_native_model_load_failed") from None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--doctor", action="store_true")
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--resume-api", action="store_true")
    parser.add_argument("--confirm-credit-policy", action="store_true")
    parser.add_argument("--confirm-provider-outcomes-reviewed", action="store_true")
    parser.add_argument("--text", action="store_true", help="with --resume-api: resume only the meeting-info text pause")
    args = parser.parse_args()
    try:
        config = load_server_config()
        os.umask(0o077)
        if args.resume_api:
            if not args.confirm_credit_policy or not args.confirm_provider_outcomes_reviewed:
                raise _failure("api_resume_confirmation_required")
            from .api_circuit import ApiCircuit, for_config, for_text_config
            # Meeting-info inference sets the global pause file even when speech is local.
            circuit = for_text_config(config) if args.text else (for_config(config) or ApiCircuit(Path(config["worker_home"])))
            if not circuit:
                raise _failure("server_api_policy")
            circuit.status()  # Recover a crashed POST intent into a durable block.
            print(json.dumps(circuit.resume()))
            return 0
        probe_models(config)
        if args.doctor:
            from .runner import Worker
            print(json.dumps({"configured": True, "models_verified": True, "execution_location": "server", "speech_only": True,
                              "credentials": "systemd/LoadCredential", "capabilities": Worker(config).capabilities()}))
        else:
            # Private runtime files remain private even outside the service's UMask.
            os.umask(0o077)
            from .runner import Worker
            Worker(config).run(once=args.once)
        return 0
    except WorkerError as exc:
        print(json.dumps({"state": exc.state, "code": exc.code}), file=sys.stderr)
        return 2
    except Exception:
        print('{"state":"failed","code":"server_runtime_error"}', file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
