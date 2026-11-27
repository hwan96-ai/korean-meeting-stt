"""Bounded official native blocks + SAME-model global embedding reconciliation.

No speaker count is guessed. Local cluster IDs never become global people IDs.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time
import wave

from .core import WorkerError, atomic_json, read_json, sha256

# 2026-11-14: native min_duration_on 0.3 -> 0.5 changes every block result, so old block caches must not be reused.
ADAPTER_VERSION = "bounded-global-2026-11-14.1"
BLOCK_CACHE_VERSION = "bounded-global-2026-11-14.1"


def owned_turns(turns: list[dict], offset: float, core_start: float, core_end: float, block: int) -> list[dict]:
    return [{"start": max(core_start, offset + turn["start"]), "end": min(core_end, offset + turn["end"]),
             "speaker": f"block{block:04d}/{turn['speaker']}"} for turn in turns
            if min(core_end, offset + turn["end"]) > max(core_start, offset + turn["start"])]


def _merge(turns: list[dict], gap: float = 1 / 16000) -> list[dict]:
    groups = {}
    for turn in turns:
        groups.setdefault((turn["speaker"], bool(turn.get("overlap"))), []).append(turn)
    output = []
    for (speaker, overlap), values in groups.items():
        merged = []
        for value in sorted(values, key=lambda item: item["start"]):
            if merged and value["start"] - merged[-1]["end"] < gap:
                merged[-1]["end"] = max(merged[-1]["end"], value["end"])
            else:
                merged.append({"start": value["start"], "end": value["end"], "speaker": speaker,
                               **({"overlap": True} if overlap else {})})
        output.extend(merged)
    return sorted(output, key=lambda item: (item["start"], item["end"], item["speaker"]))


def reconcile(turns: list[dict], features: dict[str, list[float]], threshold: float) -> tuple[list[dict], dict]:
    import numpy as np
    import sherpa_onnx as sherpa
    keys, mapping = sorted(features), {}
    if keys:
        matrix = np.asarray([features[key] for key in keys], dtype=np.float32)
        if matrix.ndim != 2 or not np.isfinite(matrix).all():
            raise WorkerError("speaker_features_invalid", "화자 특징 체크포인트가 올바르지 않습니다.", "needs_review")
        labels = [0] if len(keys) == 1 else sherpa.FastClustering(sherpa.FastClusteringConfig(num_clusters=-1, threshold=float(threshold)))(matrix)
        mapping = {key: f"speaker_{label:03d}" for key, label in zip(keys, labels)}
    output = [{**turn, "speaker": mapping.get(turn["speaker"], "unknown")} for turn in turns]
    # A global merge must not hide actual simultaneous local speakers.
    ordered = sorted(turns, key=lambda item: item["start"])
    for index, first in enumerate(ordered):
        for second in ordered[index + 1:]:
            if second["start"] >= first["end"]:
                break
            if first["speaker"] != second["speaker"]:
                start, end = max(first["start"], second["start"]), min(first["end"], second["end"])
                if end > start:
                    output.append({"start": start, "end": end, "speaker": "unknown", "overlap": True})
    diagnostics = {"adapter_version": ADAPTER_VERSION, "local_clusters": len({turn['speaker'] for turn in turns}),
                   "eligible_reference_clusters": len(keys), "global_clusters": len(set(mapping.values())),
                   "unknown_reference_clusters": len({turn['speaker'] for turn in turns} - set(keys)),
                   "global_cluster_threshold": threshold, "known_speaker_count": None,
                   "quality": "unscored: no labeled speaker reference"}
    return _merge(output), diagnostics


def _write_clip(source_path: Path, target: Path, first: int, last: int, rate: int) -> None:
    if target.is_file():
        with wave.open(str(target), "rb") as existing:
            if existing.getnframes() == last - first and existing.getframerate() == rate:
                return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".partial.wav")
    with wave.open(str(source_path), "rb") as source, wave.open(str(temporary), "wb") as destination:
        destination.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        source.setpos(first)
        remaining = last - first
        while remaining:
            data = source.readframes(min(rate * 10, remaining))
            if not data:
                raise WorkerError("diarization_audio_short", "화자 분석용 원음 프레임 수가 변경되었습니다.")
            destination.writeframesraw(data)
            remaining -= len(data) // 2
    temporary.replace(target)


def _references(audio: Path, turns: list[dict], extractor) -> dict[str, list[float]]:
    import numpy as np
    from .speech import audio_samples, SAMPLE_RATE
    output = {}
    for label in {turn["speaker"] for turn in turns}:
        intervals, seconds = [], 0.0
        for turn in turns:
            if turn["speaker"] != label or turn["end"] - turn["start"] < 1.0:
                continue
            spans = [(turn["start"], turn["end"])]
            for other in turns:
                if other["speaker"] == label:
                    continue
                clean = []
                for start, end in spans:
                    if other["start"] >= end or other["end"] <= start:
                        clean.append((start, end))
                    else:
                        if other["start"] > start:
                            clean.append((start, other["start"]))
                        if other["end"] < end:
                            clean.append((other["end"], end))
                spans = clean
            for start, end in spans:
                if end - start < 1:
                    continue
                end = min(end, start + 3)
                intervals.append((start, end))
                seconds += end - start
                if seconds >= 30:
                    break
            if seconds >= 30:
                break
        if seconds < 3:
            continue
        samples = np.concatenate([audio_samples(audio, start, end) for start, end in intervals])
        stream = extractor.create_stream()
        stream.accept_waveform(SAMPLE_RATE, samples)
        stream.input_finished()
        if not extractor.is_ready(stream):
            continue
        vector = np.asarray(extractor.compute(stream), dtype=np.float32)
        norm = float(np.linalg.norm(vector))
        if norm and np.isfinite(vector).all():
            output[label] = (vector / norm).tolist()
    return output


def layout(path: Path, config: dict) -> dict:
    """Block geometry and cache identity, shared by the child and by a parent that reads finished blocks."""
    from .speech import DIARIZATION_MIN_DURATION_ON, DIARIZATION_MIN_DURATION_OFF
    with wave.open(str(path), "rb") as audio:
        rate, frames = audio.getframerate(), audio.getnframes()
        if rate != 16000 or audio.getnchannels() != 1 or audio.getsampwidth() != 2:
            raise WorkerError("diarization_audio", "화자 분석은 16kHz 단일 채널 PCM 분석 사본이 필요합니다.")
    block_seconds = int(config.get("diarization_block_seconds", 300))
    context_seconds = float(config.get("diarization_context_seconds", 5))
    if not 30 <= block_seconds <= 600 or not 0 <= context_seconds <= 10:
        raise WorkerError("diarization_blocks", "화자 분석 블록은 30~600초, 문맥은 0~10초로 설정하세요.", "needs_setup")
    segmentation, embedding = Path(config["diarization_model"]), Path(config["embedding_model"])
    if not segmentation.is_file() or not embedding.is_file():
        raise WorkerError("diarization_models_missing", "setup-models --diarization과 migrate-models를 실행하세요.", "needs_setup")
    base_identity = {"adapter": BLOCK_CACHE_VERSION, "audio": sha256(path), "segmentation": sha256(segmentation), "embedding": sha256(embedding),
                     "native_threshold": float(config.get("cluster_threshold", 0.5)), "block_seconds": block_seconds,
                     "context_seconds": context_seconds, "reference_minimum_seconds": 3,
                     "min_duration_on": DIARIZATION_MIN_DURATION_ON, "min_duration_off": DIARIZATION_MIN_DURATION_OFF}
    identity = {**base_identity, "adapter": ADAPTER_VERSION, "global_merge_gap_seconds": 1 / 16000,
                "global_threshold": float(config.get("global_cluster_threshold", config.get("cluster_threshold", 0.5)))}
    digest = hashlib.sha256(json.dumps(base_identity, sort_keys=True).encode()).hexdigest()[:24]
    block_size = block_seconds * rate
    return {"rate": rate, "frames": frames, "block_size": block_size, "context": round(context_seconds * rate),
            "total_blocks": (frames + block_size - 1) // block_size, "base_identity": base_identity, "identity": identity,
            "digest": digest, "embedding": embedding, "source": path}


def block_bounds(geometry: dict, index: int) -> tuple[int, int, int, int]:
    """(core first, core last, clip left, clip right) frames of block `index`."""
    first = index * geometry["block_size"]
    last = min(first + geometry["block_size"], geometry["frames"])
    return first, last, max(0, first - geometry["context"]), min(geometry["frames"], last + geometry["context"])


def _same_frames(source: Path, clip: Path, left: int, right: int) -> bool:
    with wave.open(str(source), "rb") as original, wave.open(str(clip), "rb") as block:
        if block.getnframes() != right - left or block.getframerate() != original.getframerate():
            return False
        original.setpos(left)
        remaining = right - left
        while remaining:
            step = min(remaining, 16000 * 30)
            if original.readframes(step) != block.readframes(step):
                return False
            remaining -= step
    return True


def finished_block(output: Path, geometry: dict, index: int) -> list[dict] | None:
    """Owned core turns of block `index` once its native result is final, else None.

    The child writes `block-NNNN.json` atomically with `complete` only after the block is done, and
    its folder name is the digest of audio, models and settings, so a finished file is final.
    """
    directory = output.parent / "diarization-blocks" / geometry["digest"]
    source = geometry["source"]
    try:
        value = read_json(directory / f"block-{index:04d}.json", None)
    except OSError:
        return None  # Being replaced right now (Windows); read again on the next poll.
    if not isinstance(value, dict) or value.get("complete") is not True or not isinstance(value.get("turns"), list):
        return None
    expected = geometry["base_identity"]
    identity = value.get("identity") or {}
    found = (identity.get("segmentation"), identity.get("embedding"), identity.get("threshold"),
             identity.get("min_duration_on"), identity.get("min_duration_off"))
    wanted = (expected["segmentation"], expected["embedding"], expected["native_threshold"],
              expected["min_duration_on"], expected["min_duration_off"])
    clip = directory / f"block-{index:04d}.wav"
    first, last, left, right = block_bounds(geometry, index)
    rate = geometry["rate"]
    # The result must belong to this block's audio, cut from this place in the original, before anyone
    # pays for clips planned from it.
    if found != wanted or not clip.is_file() or identity.get("audio") != sha256(clip) or not _same_frames(source, clip, left, right):
        raise WorkerError("diarization_block_identity", "화자 분석 블록 기록이 현재 설정이나 원음과 다릅니다.", "needs_review")
    return owned_turns(value["turns"], left / rate, first / rate, last / rate, index)


def run(path: Path, output: Path, config: dict, native, check=lambda: None) -> list[dict]:
    import sherpa_onnx as sherpa
    geometry = layout(path, config)
    rate, frames, base_identity, identity, digest = geometry["rate"], geometry["frames"], geometry["base_identity"], geometry["identity"], geometry["digest"]
    embedding = geometry["embedding"]
    block_seconds, context_seconds = base_identity["block_seconds"], base_identity["context_seconds"]
    cached = read_json(output, {})
    if cached.get("identity") == identity and cached.get("complete"):
        return cached["turns"]
    directory = output.parent / "diarization-blocks" / digest
    directory.mkdir(parents=True, exist_ok=True)
    extractor = sherpa.SpeakerEmbeddingExtractor(sherpa.SpeakerEmbeddingExtractorConfig(model=str(embedding), num_threads=int(config["cpu_threads"])))
    all_turns, all_features = [], {}
    block_size, context = geometry["block_size"], geometry["context"]
    elapsed = []
    for index, first in enumerate(range(0, frames, block_size)):
        check()
        started = time.monotonic()
        last = min(first + block_size, frames)
        left, right = max(0, first - context), min(frames, last + context)
        clip, checkpoint = directory / f"block-{index:04d}.wav", directory / f"block-{index:04d}.json"
        _write_clip(path, clip, left, right, rate)
        raw = native(clip, checkpoint, config, check)
        owned = owned_turns(raw, left / rate, first / rate, last / rate, index)
        features_path = directory / f"block-{index:04d}-features.json"
        features = read_json(features_path)
        if features is None:
            local_turns = [{**turn, "start": turn["start"] - left / rate, "end": turn["end"] - left / rate} for turn in owned]
            features = _references(clip, local_turns, extractor)
            atomic_json(features_path, features)
        all_turns.extend(owned)
        all_features.update(features)
        elapsed.append(round(time.monotonic() - started, 3))  # Numbers only; cached blocks show about 0.
        atomic_json(directory / "progress.json", {"identity": base_identity, "completed_blocks": index + 1,
                                                 "total_blocks": geometry["total_blocks"], "block_elapsed_seconds": elapsed})
    check()
    turns, diagnostics = reconcile(all_turns, all_features, identity["global_threshold"])
    diagnostics.update({"segmentation_sha256": base_identity["segmentation"], "embedding_sha256": base_identity["embedding"],
                        "native_cluster_threshold": base_identity["native_threshold"], "block_seconds": block_seconds,
                        "context_seconds": context_seconds, "blocks": (frames + block_size - 1) // block_size})
    atomic_json(output, {"identity": identity, "complete": True, "turns": turns, "diagnostics": diagnostics})
    return turns
