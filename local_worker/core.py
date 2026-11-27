from __future__ import annotations

import ctypes
import hashlib
import json
import os
import time
from pathlib import Path
import uuid


class WorkerError(Exception):
    def __init__(self, code: str, message: str, state: str = "failed", retryable: bool = False):
        super().__init__(message)
        self.code, self.state, self.retryable = code, state, retryable


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".partial")
    with temporary.open("w", encoding="utf-8", newline="\n") as file:
        # One-shot compact encoding uses the C encoder. `json.dump(..., indent=2)` re-encodes in pure
        # Python, and speech checkpoints are rewritten after every clip, so that cost grew with the
        # square of the clip count.
        file.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        file.flush()
        os.fsync(file.fileno())
    # Windows antivirus/indexers can briefly hold a just-written target without
    # FILE_SHARE_DELETE. Preserve the previous valid checkpoint until replace succeeds.
    for attempt in range(6):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.05 * 2 ** attempt)


def read_json(path: Path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for data in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(data)
    return digest.hexdigest()


def safe_id(value: str) -> str:
    try:
        parsed = str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise WorkerError("invalid_id", "서버 작업 ID 형식이 잘못되었습니다.") from None
    if value.lower() != parsed:
        raise WorkerError("invalid_id", "표준 UUID 작업 ID만 허용합니다.")
    return parsed


def job_directory(workspace: Path, value: str) -> Path:
    root = workspace.resolve()
    target = root / "jobs" / safe_id(value)
    if not target.resolve().is_relative_to(root):
        raise WorkerError("unsafe_path", "작업 폴더 외부 경로를 차단했습니다.")
    # Windows junctions/reparse points, as well as POSIX symlinks, cannot redirect output.
    for path in [target, *target.parents]:
        if path == root.parent:
            break
        if path.is_symlink() or (path.exists() and os.name == "nt" and getattr(path.lstat(), "st_file_attributes", 0) & 0x400):
            raise WorkerError("unsafe_path", "연결된 작업 폴더는 사용할 수 없습니다.")
    target.mkdir(parents=True, exist_ok=True)
    return target


def local_root() -> Path:
    override = os.environ.get("MEETING_WORKER_HOME")
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise WorkerError("invalid_worker_home", "MEETING_WORKER_HOME에는 절대 경로를 지정하세요.", "needs_setup")
        return path.resolve()
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local"))) / "CodexMeetingWorker"


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data: bytes, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise WorkerError("windows_required", "인증 저장은 현재 Windows 사용자 DPAPI가 필요합니다.", "needs_setup")
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source, result = _Blob(len(data), buffer), _Blob()
    if decrypt:
        ok = ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result))
    else:
        ok = ctypes.windll.crypt32.CryptProtectData(ctypes.byref(source), "Meeting worker", None, None, None, 1, ctypes.byref(result))
    if not ok:
        raise WorkerError("credential_store", "현재 Windows 사용자로 인증 정보를 읽거나 저장할 수 없습니다.", "needs_setup")
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(result.pbData)


def config_path() -> Path:
    return local_root() / "config.dpapi"


def load_config() -> dict:
    if not config_path().is_file():
        raise WorkerError("not_configured", "configure --stdin으로 사이트와 PC 인증을 설정하세요.", "needs_setup")
    config = json.loads(_dpapi(config_path().read_bytes(), True).decode("utf-8"))
    return defaults(config)


# Local Whisper models: name -> (Hugging Face repository, pinned revision). All are CTranslate2
# conversions of OpenAI Whisper weights. Systran has no large-v3-turbo conversion; the one
# faster-whisper's alias table uses moved from mobiuslabsgmbh to dropbox-dash (same commit).
WHISPER_MODELS = {
    "base": ("Systran/faster-whisper-base", "ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66"),
    "small": ("Systran/faster-whisper-small", "536b0662742c02347bc0e980a01041f333bce120"),
    "medium": ("Systran/faster-whisper-medium", "08e178d48790749d25932bbc082711ddcfdfbc4f"),
    "large-v3": ("Systran/faster-whisper-large-v3", "edaa852ec7e145841d8ffdb056a99866b5f0a478"),
    "large-v3-turbo": ("dropbox-dash/faster-whisper-large-v3-turbo", "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf"),
}
# No "auto": the checkpoint identity, error text and result label must name the device that ran.
WHISPER_DEVICES = {"cpu", "cuda"}
WHISPER_COMPUTE_TYPES = {"int8", "int8_float32", "int8_float16", "int16", "float16", "float32"}
_GPU_ONLY_COMPUTE = {"int8_float16", "float16"}
_CPU_ONLY_COMPUTE = {"int16"}  # CTranslate2 4.8.4 has no efficient int16 on CUDA


def whisper_runtime(config: dict) -> tuple[str, str]:
    """Device and compute type for local Whisper. Default cpu/int8, the only pair the author has measured."""
    device = config.get("whisper_device", "cpu")
    compute = config.get("whisper_compute_type", "int8")
    if type(device) is not str or type(compute) is not str or device not in WHISPER_DEVICES or compute not in WHISPER_COMPUTE_TYPES \
            or (device == "cpu" and compute in _GPU_ONLY_COMPUTE) or (device == "cuda" and compute in _CPU_ONLY_COMPUTE):
        raise WorkerError("whisper_runtime", "whisper_device는 cpu 또는 cuda, whisper_compute_type은 그 장치가 쓸 수 있는 값이어야 합니다(cpu는 int8, int8_float32, int16, float32, cuda는 int8, int8_float16, int8_float32, float16, float32).", "needs_setup")
    return device, compute


def whisper_runtime_label(config: dict) -> str:
    return str(config.get("whisper_device", "cpu")) + "-" + str(config.get("whisper_compute_type", "int8"))


def defaults(config: dict) -> dict:
    result = _defaults(config)
    if result.get("whisper_model") in WHISPER_MODELS:
        # The pinned table wins over a revision an older version saved (None for models it did not pin).
        result["whisper_revision"] = WHISPER_MODELS[result["whisper_model"]][1]
    return result


def _defaults(config: dict) -> dict:
    return {
        "workspace": str(Path(__file__).resolve().parents[2] / "workspace"),
        "whisper_model": "base", "cpu_threads": 4, "chunk_seconds": 600,
        "whisper_device": "cpu", "whisper_compute_type": "int8",
        "speech_mode": "auto", "allow_api_speech_fallback": False,
        "whisper_revision": WHISPER_MODELS.get(config.get("whisper_model", "base"), (None, None))[1],
        "generation_mode": "unconfigured", "codex_model": "", "codex_timeout": 900,
        "diarization_model": str(local_root() / "models" / "segmentation.float32.onnx"),
        "diarization_model_precision": "float32", "cluster_threshold": 0.5,
        "diarization_strategy": "bounded_global", "diarization_block_seconds": 300, "diarization_context_seconds": 5,
        "embedding_model": str(local_root() / "models" / "embedding.onnx"),
        "match_threshold": 0.75, "match_margin": 0.08,
        "notion_targets": {"customer": {"database_id": "00000000-0000-4000-8000-00000000c0de", "data_source_id": "00000000-0000-4000-8000-00000000da7a"}},
        **config,
    }


def save_config(config: dict) -> None:
    from urllib.parse import urlparse
    if "site_url" in config:
        config["base_url"] = config.pop("site_url")
    if "sites_bypass_token" in config:
        config["sites_dispatch_token"] = config.pop("sites_bypass_token")
    config = defaults(config)
    whisper_runtime(config)
    parsed = urlparse(config.get("base_url", ""))
    if (parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"})) or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise WorkerError("invalid_site", "HTTPS 사이트 주소 또는 localhost 미리보기 주소만 입력하세요.", "needs_setup")
    config["base_url"] = config["base_url"].rstrip("/")
    import secrets
    config.setdefault("device_token", secrets.token_urlsafe(48))
    config_path().parent.mkdir(parents=True, exist_ok=True)
    path = config_path().with_suffix(".partial")
    path.write_bytes(_dpapi(json.dumps(config, ensure_ascii=False).encode("utf-8")))
    path.replace(config_path())


class SingleWorker:
    """OS-held byte lock; stale process files do not block restart."""
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "worker.lock"

    def __enter__(self):
        self.file = self.path.open("a+b")
        try:
            if self.path.stat().st_size == 0:
                self.file.write(b"0")
                self.file.flush()
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise WorkerError("worker_running", "이미 실행 중인 PC 작업 프로그램이 있습니다.") from None
        return self

    def __exit__(self, *args):
        self.file.close()
