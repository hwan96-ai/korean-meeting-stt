from __future__ import annotations

from pathlib import Path
import hashlib
import json
import shutil
import uuid
import wave

from .core import WorkerError, atomic_json, read_json, safe_id, sha256
from .speech import audio_samples, SAMPLE_RATE


def embedding(path: Path, intervals: list[tuple[float, float]], config: dict):
    import numpy as np
    import sherpa_onnx as sherpa
    model = Path(config["embedding_model"])
    if not model.is_file():
        raise WorkerError("embedding_model_missing", "setup-models --diarization으로 화자 특징 모델을 설치하세요.", "needs_setup")
    if sum(end - start for start, end in intervals) > 120:
        raise WorkerError("reference_long", "한 번에 최대 120초의 단일 화자 기준 구간을 선택하세요. 추가 기준은 나누어 등록할 수 있습니다.", "needs_review")
    extractor = sherpa.SpeakerEmbeddingExtractor(sherpa.SpeakerEmbeddingExtractorConfig(model=str(model), num_threads=int(config["cpu_threads"])))
    samples = np.concatenate([audio_samples(path, start, end) for start, end in intervals])
    if len(samples) < 3 * SAMPLE_RATE:
        raise WorkerError("reference_short", "겹침 없는 단일 화자 구간을 합계 3초 이상 선택하세요.", "needs_review")
    stream = extractor.create_stream()
    stream.accept_waveform(SAMPLE_RATE, samples)
    stream.input_finished()
    if not extractor.is_ready(stream):
        raise WorkerError("reference_unusable", "선택한 구간에서 화자 특징을 추출할 수 없습니다.", "needs_review")
    vector = np.asarray(extractor.compute(stream), dtype=np.float32)
    norm = float(np.linalg.norm(vector))
    if not norm or not np.isfinite(vector).all():
        raise WorkerError("reference_unusable", "유효한 화자 특징을 추출할 수 없습니다.", "needs_review")
    return (vector / norm).tolist(), samples, sha256(model)


def enroll(root: Path, audio: Path, segments: list[dict], request: dict, config: dict) -> dict:
    if request.get("confirmed_single_speaker") is not True:
        raise WorkerError("confirmation_required", "원음을 듣고 한 사람만 말하는 구간임을 확인해야 등록할 수 있습니다.", "needs_review")
    selected = [segment for segment in segments if segment["segment_id"] in request["segment_ids"]]
    if len(selected) != len(set(request["segment_ids"])) or any(segment.get("overlap") for segment in selected):
        raise WorkerError("invalid_reference", "선택 구간이 없거나 겹침 발화가 포함되어 있습니다.", "needs_review")
    intervals = sorted((segment["start_ms"] / 1000, segment["end_ms"] / 1000) for segment in selected)
    if any(a[1] > b[0] for a, b in zip(intervals, intervals[1:])):
        raise WorkerError("overlapping_reference", "서로 겹치는 기준 구간은 등록할 수 없습니다.", "needs_review")
    person_id = safe_id(request.get("person_id") or str(uuid.uuid5(uuid.NAMESPACE_URL, "meeting-speaker-request:" + safe_id(request["request_id"]))))
    directory = root / person_id
    old = read_json(directory / "profile.json", {})
    if request.get("request_id") in old.get("request_ids", []):
        return old
    vector, samples, revision = embedding(audio, intervals, config)
    if old and old["model_revision"] != revision:
        raise WorkerError("profile_model_changed", "기준 화자 모델이 달라 새 프로필로 등록하거나 기존 음성을 재계산해야 합니다.", "needs_setup")
    directory.mkdir(parents=True, exist_ok=True)
    filename = str(uuid.uuid4()) + ".wav"
    with wave.open(str(directory / filename), "wb") as output:
        output.setparams((1, 2, SAMPLE_RATE, 0, "NONE", "not compressed"))
        output.writeframes((samples * 32767).astype("<i2").tobytes())
    profile = {"person_id": person_id, "display_name": request["display_name"], "role": request["role"], "company": request["company"],
               "profile_revision": old.get("profile_revision", 0) + 1, "model_id": "sherpa-onnx/" + Path(config["embedding_model"]).name,
               "model_revision": revision, "embeddings": old.get("embeddings", []) + [vector],
               "reference_artifacts": old.get("reference_artifacts", []) + [filename], "request_ids": old.get("request_ids", []) + [request.get("request_id")]}
    atomic_json(directory / "profile.json", profile)
    return profile


def match(vector: list[float], profiles: list[dict], model_revision: str, threshold=0.75, margin=0.08):
    import numpy as np
    scores = []
    value = np.asarray(vector)
    for profile in profiles:
        if profile["model_revision"] != model_revision:
            continue
        references = np.asarray(profile["embeddings"])
        if references.ndim != 2 or references.shape[1] != len(value):
            continue
        centroid = references.mean(axis=0)
        norm = float(np.linalg.norm(centroid) * np.linalg.norm(value))
        if norm:
            scores.append((float(np.dot(value, centroid) / norm), profile["person_id"]))
    scores.sort(reverse=True)
    if not scores or scores[0][0] < threshold or (len(scores) > 1 and scores[0][0] - scores[1][0] < margin):
        return None
    return scores[0][1]


def list_profiles(root: Path) -> list[dict]:
    return [read_json(file) for file in root.glob("*/profile.json")]


def suggest(segments: list[dict], turns: list[dict], audio: Path, root: Path, config: dict) -> None:
    profiles = list_profiles(root)
    if not profiles:
        return
    for label in {segment["local_speaker_id"] for segment in segments} - {"unknown"}:
        # Avoid overlap turns and cap reference processing; suggestions never become confirmations.
        intervals = [(turn["start"], min(turn["end"], turn["start"] + 3)) for turn in turns if turn["speaker"] == label and turn["end"] - turn["start"] >= 1.5
                     and not any(other["speaker"] != label and min(other["end"], turn["end"]) > max(other["start"], turn["start"]) for other in turns)][:20]
        if sum(end - start for start, end in intervals) < 3:
            continue
        vector, _, revision = embedding(audio, intervals, config)
        person = match(vector, profiles, revision, float(config["match_threshold"]), float(config["match_margin"]))
        for segment in segments:
            if segment["local_speaker_id"] == label:
                segment["proposed_person_id"] = person


def delete_profile(root: Path, person: str) -> None:
    target = root / safe_id(person)
    if target.resolve().parent != root.resolve() or target.is_symlink():
        raise WorkerError("unsafe_path", "화자 삭제 경로를 확인하세요.")
    if target.exists():
        shutil.rmtree(target)


def stage_enrollment(root: Path, pending: Path, audio: Path, segments: list[dict], request: dict, config: dict) -> Path:
    """Prepare private references without publishing them into the live registry."""
    request_id = safe_id(request["request_id"])
    directory = pending / request_id
    identity = hashlib.sha256(json.dumps({"request": request, "segments": segments}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    manifest = read_json(directory / "state.json", {})
    if manifest:
        if manifest["input_hash"] != identity:
            raise WorkerError("profile_request_changed", "등록 요청 내용이 변경되었습니다. 이전 임시 등록을 검토하세요.", "needs_review")
        if manifest["state"] != "preparing":
            return directory
    person = safe_id(request.get("person_id") or str(uuid.uuid5(uuid.NAMESPACE_URL, "meeting-speaker-request:" + request_id)))
    source, staged = root / person, directory / "people" / person
    existing = read_json(source / "profile.json", {})
    if (directory / "people").exists():
        shutil.rmtree(directory / "people")
    atomic_json(directory / "state.json", {"state": "preparing", "request_id": request_id, "person_id": person,
                                         "input_hash": identity, "previous_revision": existing.get("profile_revision", 0)})
    if existing:
        if source.resolve().parent != root.resolve() or source.is_symlink():
            raise WorkerError("unsafe_profile_path", "기준 화자 경로를 확인하세요.")
        staged.mkdir(parents=True, exist_ok=True)
        atomic_json(staged / "profile.json", existing)
        for filename in existing["reference_artifacts"]:
            reference = source / filename
            if reference.resolve().parent != source.resolve() or reference.is_symlink():
                raise WorkerError("unsafe_profile_path", "기준 화자 음성 경로를 확인하세요.")
            shutil.copyfile(reference, staged / filename)
    profile = enroll(directory / "people", audio, segments, request, config)
    atomic_json(directory / "state.json", {"state": "staged", "request_id": request_id, "person_id": person,
                                         "input_hash": identity, "previous_revision": existing.get("profile_revision", 0),
                                         "new_revision": profile["profile_revision"]})
    return directory


def commit_enrollment(root: Path, directory: Path) -> None:
    manifest = read_json(directory / "state.json")
    if manifest["state"] not in {"acknowledged", "committing"}:
        raise WorkerError("profile_ack_required", "등록 완료 응답을 확인한 경우에만 화자 프로필을 공개합니다.", "needs_review")
    person = safe_id(manifest["person_id"])
    target, staged, backup = root / person, directory / "people" / person, directory / "previous"
    if target.resolve().parent != root.resolve() or target.is_symlink():
        raise WorkerError("unsafe_profile_path", "화자 등록 대상 경로를 확인하세요.")
    current = read_json(target / "profile.json", {})
    if manifest["request_id"] in current.get("request_ids", []):
        # Recovery after the final rename succeeded but before pending cleanup.
        shutil.rmtree(directory)
        return
    if current.get("profile_revision", 0) != manifest["previous_revision"] and not (backup.exists() and not target.exists()):
        raise WorkerError("profile_revision_conflict", "다른 화자 프로필 수정이 있습니다. 임시 등록을 검토하세요.", "needs_review")
    manifest["state"] = "committing"
    atomic_json(directory / "state.json", manifest)
    root.mkdir(parents=True, exist_ok=True)
    if target.exists() and not backup.exists():
        target.rename(backup)
    if staged.exists():
        staged.rename(target)
    if not (target / "profile.json").is_file():
        raise WorkerError("profile_commit_incomplete", "화자 등록 파일 이동을 복구해야 합니다.", "needs_review")
    shutil.rmtree(directory)


def acknowledge_enrollment(directory: Path) -> None:
    state = read_json(directory / "state.json")
    atomic_json(directory / "state.json", {**state, "state": "acknowledged"})


def recover_enrollments(root: Path, pending: Path) -> None:
    for state_file in pending.glob("*/state.json"):
        if read_json(state_file).get("state") in {"acknowledged", "committing"}:
            commit_enrollment(root, state_file.parent)


def delete_pending_profiles(pending: Path, person: str) -> None:
    person = safe_id(person)
    for state_file in pending.glob("*/state.json"):
        if read_json(state_file).get("person_id") == person:
            target = state_file.parent
            if target.resolve().parent != pending.resolve() or target.is_symlink():
                raise WorkerError("unsafe_profile_path", "임시 화자 삭제 경로를 확인하세요.")
            shutil.rmtree(target)
