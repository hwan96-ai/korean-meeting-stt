from __future__ import annotations

from pathlib import Path
import tarfile
from urllib import request

from .core import WHISPER_MODELS, WorkerError, atomic_json, local_root, sha256

SEGMENTATION_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
EMBEDDING_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/nemo_en_titanet_small.onnx"
SEGMENTATION_ARCHIVE_SHA256 = "24615ee884c897d9d2ba09bb4d30da6bb1b15e685065962db5b02e76e4996488"
SEGMENTATION_SHA256 = "d582f4b4c6b48205de7e0643c57df0df5615a3c176189be3fc461e9d18827b5d"
SEGMENTATION_FLOAT32_SHA256 = "220ad67ca923bef2fa91f2390c786097bf305bceb5e261d4af67b38e938e1079"
EMBEDDING_SHA256 = "ad4a1802485d8b34c722d2a9d04249662f2ece5d28a7a039063ca22f515a789e"


def download(url: str, path: Path, maximum: int) -> None:
    temporary = path.with_suffix(".download")
    path.parent.mkdir(parents=True, exist_ok=True)
    with request.urlopen(url, timeout=120) as response, temporary.open("wb") as output:
        for data in iter(lambda: response.read(1024 * 1024), b""):
            if output.tell() + len(data) > maximum:
                raise WorkerError("model_size", "모델 다운로드 크기가 허용 범위를 초과했습니다.")
            output.write(data)
    temporary.replace(path)


def setup_diarization() -> dict:
    directory = local_root() / "models"
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / "segmentation.tar.bz2"
    if not archive.is_file():
        download(SEGMENTATION_URL, archive, 100 * 1024 * 1024)
    if sha256(archive) != SEGMENTATION_ARCHIVE_SHA256:
        raise WorkerError("model_checksum", "화자 모델 압축 파일 체크섬이 고정 버전과 다릅니다.", "needs_setup")
    with tarfile.open(archive, "r:bz2") as tar:
        # Never use extractall on remotely supplied archives.
        for name, target in [("model.onnx", "segmentation.float32.onnx"), ("model.int8.onnx", "segmentation.int8.onnx"), ("LICENSE", "segmentation-LICENSE"), ("README.md", "segmentation-README.md")]:
            candidates = [member for member in tar.getmembers() if Path(member.name).name == name and member.isfile()]
            if len(candidates) != 1:
                raise WorkerError("model_archive", "모델 압축 파일 구성이나 라이선스를 확인하세요.")
            member = candidates[0]
            if member.size > 50 * 1024 * 1024:
                raise WorkerError("model_archive", "압축 모델 크기가 허용 범위를 초과합니다.")
            file = tar.extractfile(member)
            (directory / target).write_bytes(file.read())
    embedding = directory / "embedding.onnx"
    if not embedding.is_file():
        download(EMBEDDING_URL, embedding, 100 * 1024 * 1024)
    if sha256(embedding) != EMBEDDING_SHA256 or sha256(directory / "segmentation.int8.onnx") != SEGMENTATION_SHA256 or sha256(directory / "segmentation.float32.onnx") != SEGMENTATION_FLOAT32_SHA256:
        raise WorkerError("model_checksum", "화자 모델 체크섬이 고정 버전과 다릅니다.", "needs_setup")
    if not (directory / "embedding-LICENSE").is_file():
        download("https://raw.githubusercontent.com/NVIDIA/NeMo/main/LICENSE", directory / "embedding-LICENSE", 100000)
    manifest = {"segmentation": {"url": SEGMENTATION_URL, "archive_sha256": sha256(archive), "sha256": SEGMENTATION_FLOAT32_SHA256,
                               "precision": "float32", "file": "segmentation.float32.onnx", "comparison_int8_sha256": SEGMENTATION_SHA256,
                               "license_file": "segmentation-LICENSE", "upstream": "pyannote/segmentation-3.0"},
                "embedding": {"url": EMBEDDING_URL, "sha256": sha256(embedding), "upstream": "NVIDIA NeMo titanet_small",
                              "license": "Apache-2.0 (NGC TitaNet-S model card links NeMo license)", "license_file": "embedding-LICENSE", "license_url": "https://catalog.ngc.nvidia.com/orgs/nvidia/nemo/models/titanet_small"},
                "runtime": "sherpa-onnx==1.13.8", "note": "Local diarization/speaker suggestions require human review; no labeled meeting accuracy is claimed."}
    atomic_json(directory / "manifest.json", manifest)
    return manifest


def migrate_diarization(config: dict, precision: str = "float32") -> dict:
    if precision not in {"float32", "int8"}:
        raise WorkerError("model_precision", "화자 분리 모델 정밀도를 확인하세요.", "needs_setup")
    path = local_root() / "models" / ("segmentation." + precision + ".onnx")
    expected = SEGMENTATION_FLOAT32_SHA256 if precision == "float32" else SEGMENTATION_SHA256
    if not path.is_file() or sha256(path) != expected:
        raise WorkerError("model_setup_required", "같은 MEETING_WORKER_HOME에서 setup-models --diarization을 먼저 실행하세요.", "needs_setup")
    return {**config, "diarization_model": str(path), "diarization_model_precision": precision,
            "diarization_model_revision": expected, "diarization_strategy": "bounded_global",
            "diarization_block_seconds": 300, "diarization_context_seconds": 5}


def setup_whisper(name: str = "base") -> str:
    from faster_whisper.utils import download_model
    if name not in WHISPER_MODELS:
        raise WorkerError("model_name", "지원하는 로컬 Whisper 모델 이름을 선택하세요: " + ", ".join(WHISPER_MODELS), "needs_setup")
    repository, revision = WHISPER_MODELS[name]
    path = download_model(repository, revision=revision, cache_dir=str(local_root() / "models" / "huggingface"))
    atomic_json(local_root() / "models" / ("whisper-" + name + ".json"), {"repository": repository, "revision": revision, "local_path": path})
    return path
