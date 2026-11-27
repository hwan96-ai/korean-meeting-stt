from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import uuid
import wave

from .core import WorkerError, atomic_json, read_json, sha256, local_root, whisper_runtime

SAMPLE_RATE = 16000
# Longest acoustic interval before the API planner cuts it (whole-file and pipelined paths).
PARTITION_SECONDS = 30


def partition_acoustic_intervals(turns: list[dict], duration: float, max_seconds: float = PARTITION_SECONDS) -> list[dict]:
    """Cover every real source sample, including gaps/overlap, with bounded clips.

    These are container interval timestamps, never provider word timestamps.
    """
    import math
    if not math.isfinite(duration) or duration <= 0 or not math.isfinite(max_seconds) or max_seconds <= 0:
        raise WorkerError("clip_intervals", "유효한 음성 길이와 API 클립 길이가 필요합니다.")
    end_sample = round(duration * SAMPLE_RATE)
    maximum = max(1, round(max_seconds * SAMPLE_RATE))
    normalized, boundaries = [], {0, end_sample}
    for turn in turns:
        start, end = max(0, round(turn["start"] * SAMPLE_RATE)), min(end_sample, round(turn["end"] * SAMPLE_RATE))
        if end > start:
            normalized.append((start, end, turn["speaker"]))
            boundaries.update([start, end])
    intervals = []
    points = sorted(boundaries)
    for start, end in zip(points, points[1:]):
        active = {speaker for left, right, speaker in normalized if left < end and right > start}
        label = next(iter(active)) if len(active) == 1 else "unknown"
        overlap = len(active) > 1 or any(turn.get("overlap") and round(turn["start"] * SAMPLE_RATE) < end and round(turn["end"] * SAMPLE_RATE) > start for turn in turns)
        if overlap:
            label = "unknown"
        if intervals and intervals[-1]["speaker"] == label and intervals[-1]["overlap"] == overlap and intervals[-1]["end_sample"] == start:
            intervals[-1]["end_sample"] = end
        else:
            intervals.append({"start_sample": start, "end_sample": end, "speaker": label, "overlap": overlap})
    result = []
    for interval in intervals:
        for start in range(interval["start_sample"], interval["end_sample"], maximum):
            end = min(start + maximum, interval["end_sample"])
            result.append({"start": start / SAMPLE_RATE, "end": end / SAMPLE_RATE,
                           "local_speaker_id": interval["speaker"], "overlap": interval["overlap"],
                           "timestamp_granularity": "source_clip"})
    return result


def prepare_audio(source: Path, destination: Path, *, max_seconds: float = 14400) -> dict:
    """Decode a separate copy with PyAV, never rename/write the user's original."""
    import av
    import numpy as np
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".partial.wav")
    samples, peak, square_sum = 0, 0, 0.0
    try:
        with av.open(str(source)) as container:
            if not container.streams.audio:
                raise WorkerError("no_audio", "음성 트랙이 없는 파일입니다.")
            stream = container.streams.audio[0]
            original_rate = stream.codec_context.sample_rate
            original_channels = stream.codec_context.channels
            resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
            with wave.open(str(temporary), "wb") as output:
                output.setparams((1, 2, SAMPLE_RATE, 0, "NONE", "not compressed"))
                def write(frame):
                    nonlocal samples, peak, square_sum
                    data = frame.to_ndarray().reshape(-1)
                    samples += len(data)
                    if samples > max_seconds * SAMPLE_RATE:
                        raise WorkerError("duration_limit", "허용 녹음 길이를 초과했습니다.")
                    if len(data):
                        peak = max(peak, int(np.abs(data.astype(np.int32)).max()))
                        square_sum += float(np.square(data.astype(np.float64)).sum())
                        output.writeframesraw(data.astype("<i2", copy=False).tobytes())
                for frame in container.decode(stream):
                    for converted in resampler.resample(frame):
                        write(converted)
                for converted in resampler.resample(None):
                    write(converted)
        if not samples:
            raise WorkerError("empty_audio", "디코딩된 음성이 비어 있습니다.")
        rms = (square_sum / samples) ** 0.5 / 32768
        if peak < 16 or rms < 0.00005:
            raise WorkerError("silent_audio", "무음으로 판단되는 파일입니다. 녹음을 확인하세요.", "needs_review")
        temporary.replace(destination)
        metadata = {"duration": samples / SAMPLE_RATE, "sample_rate": SAMPLE_RATE,
                    "samples": samples, "channels": 1, "original_sample_rate": original_rate,
                    "original_channels": original_channels, "rms": rms, "source_sha256": sha256(source)}
        atomic_json(destination.with_suffix(".json"), metadata)
        return metadata
    except WorkerError:
        temporary.unlink(missing_ok=True)
        raise
    except Exception:
        temporary.unlink(missing_ok=True)
        raise WorkerError("decode_failed", "음성 파일이 손상되었거나 지원하지 않는 형식입니다.") from None


def audio_samples(path: Path, start: float = 0, end: float | None = None):
    import numpy as np
    with wave.open(str(path), "rb") as audio:
        first = min(audio.getnframes(), max(0, round(start * SAMPLE_RATE)))
        count = audio.getnframes() - first if end is None else max(0, min(audio.getnframes(), round(end * SAMPLE_RATE)) - first)
        audio.setpos(first)
        return np.frombuffer(audio.readframes(count), dtype="<i2").astype(np.float32) / 32768


GPU_HELP = ("GPU로 Whisper를 돌리지 못했습니다. NVIDIA 드라이버, CUDA 12용 cuBLAS와 cuDNN 9 라이브러리, "
            "whisper_device와 whisper_compute_type 설정을 확인하세요. CPU로 돌리려면 whisper_device를 cpu로 두세요.")


def cuda_library_missing(exc: BaseException) -> bool:
    """CTranslate2 reports a missing CUDA library as 'Library cublas64_12.dll is not found or cannot be loaded'."""
    text = str(exc).lower()
    return ("cublas" in text or "cudnn" in text) and ("not found" in text or "cannot be loaded" in text or "cannot open" in text)


def cuda_error(exc: BaseException) -> bool:
    """A load failure that names the GPU side (driver, device, CUDA library or a compute type the device lacks)."""
    text = str(exc).lower()
    return cuda_library_missing(exc) or any(word in text for word in ("cuda", "cublas", "cudnn", "gpu", "compute type"))


# After CTranslate2 fails to load a CUDA library, a second CUDA model in the same process hangs in its
# first encode (seen on Windows with CTranslate2 4.8.4). Refuse CUDA here until the worker restarts.
_cuda_broken = False


def _gpu_unavailable() -> WorkerError:
    return WorkerError("whisper_gpu_unavailable", GPU_HELP, "needs_setup")


def transcribe(path: Path, output: Path, config: dict, check=lambda: None, progress=lambda n, t: None) -> dict:
    from faster_whisper import WhisperModel
    model_name = config["whisper_model"]
    manifest = read_json(local_root() / "models" / ("whisper-" + Path(model_name).name + ".json"), {})
    local_path = Path(manifest.get("local_path", "__not_installed__"))
    installed = manifest.get("revision") == config.get("whisper_revision") and local_path.is_dir() and (not os.environ.get("MEETING_WORKER_HOME") or local_path.resolve().is_relative_to(local_root().resolve()))
    if installed:
        model_name = str(local_path)
    cache_directory = str(local_root() / "models" / "huggingface") if os.environ.get("MEETING_WORKER_HOME") else None
    global _cuda_broken
    device, compute = whisper_runtime(config)
    if device == "cuda" and _cuda_broken:
        raise _gpu_unavailable()
    try:
        model = WhisperModel(model_name, device=device, compute_type=compute,
                             cpu_threads=int(config["cpu_threads"]), local_files_only=True,
                             revision=config.get("whisper_revision"), download_root=cache_directory)
    except Exception as exc:
        # Only a GPU-side failure becomes the GPU setup error. A missing or damaged model stays
        # whisper_model_missing on every device, so reinstalling (or the PC API fallback) still applies.
        if device != "cpu" and cuda_error(exc):
            raise _gpu_unavailable() from None
        raise WorkerError("whisper_model_missing", "로컬 Whisper 모델이 없습니다. setup-models --whisper로 내려받으세요.", "needs_setup") from None
    with wave.open(str(path), "rb") as audio:
        duration = audio.getnframes() / SAMPLE_RATE
    chunk_seconds = max(30, min(900, int(config.get("chunk_seconds", 600))))
    identity = {"audio_sha256": sha256(path), "model": config["whisper_model"],
                "revision": config.get("whisper_revision"), "chunk_seconds": chunk_seconds, "language": "ko"}
    if (device, compute) != ("cpu", "int8"):
        # Keeps checkpoints from earlier cpu/int8 runs valid while a device change starts a fresh transcript.
        identity["runtime"] = device + "/" + compute
    cached = read_json(output, {})
    if cached.get("identity") != identity:
        cached = {"identity": identity, "chunks": [], "complete": False}
    total = int((duration + chunk_seconds - 0.001) // chunk_seconds)
    for index in range(len(cached["chunks"]), total):
        check()
        offset = index * chunk_seconds
        segments = []
        try:
            raw, _ = model.transcribe(audio_samples(path, offset, min(duration, offset + chunk_seconds)), language="ko",
                                      beam_size=5, vad_filter=True, word_timestamps=True,
                                      condition_on_previous_text=False, vad_parameters={"min_silence_duration_ms": 500})
            for segment in raw:
                check()
                words = [{"start": offset + word.start, "end": min(duration, offset + word.end), "text": word.word} for word in segment.words or []]
                segments.append({"start": offset + segment.start, "end": min(duration, offset + segment.end),
                                 "text": segment.text.strip(), "words": words})
        except RuntimeError as exc:
            # CUDA libraries are loaded on the first encode, not at model load. Other runtime errors
            # (GPU out of memory included) stay retryable local processing errors.
            if device == "cpu" or not cuda_library_missing(exc):
                raise
            _cuda_broken = True
            raise _gpu_unavailable() from None
        cached["chunks"].append(segments)
        atomic_json(output, cached)
        progress(index + 1, total)
    cached["complete"] = True
    cached["duration"] = duration
    atomic_json(output, cached)
    return {"segments": [segment for chunk in cached["chunks"] for segment in chunk], "duration": duration,
            "mode": "local/faster-whisper/" + config["whisper_model"] + "/" + device + "-" + compute}


def diarization_task(path: Path, output: Path, config: dict) -> dict:
    # Only trusted local paths and numerical model parameters cross this pipe.
    public_config = {key: config[key] for key in ["diarization_model", "embedding_model", "cpu_threads"]}
    public_config.update({key: config[key] for key in ["cluster_threshold", "global_cluster_threshold", "diarization_strategy", "diarization_block_seconds", "diarization_context_seconds"] if key in config})
    return {"audio": str(path.resolve()), "output": str(output.resolve()), "config": public_config}


def diarization_result(output: Path, returncode: int) -> list[dict]:
    if returncode:
        error = read_json(output.with_suffix(".error.json"), {})
        raise WorkerError(error.get("code", "diarization_child_failed"), error.get("message", "별도 화자 분석 프로세스가 종료되었습니다. 체크포인트를 보존했습니다."), error.get("state", "failed"), error.get("retryable", True))
    cached = read_json(output, {})
    if not cached.get("complete"):
        raise WorkerError("diarization_incomplete", "화자 분석 결과를 완성하지 못했습니다.")
    return cached["turns"]


def diarize(path: Path, output: Path, config: dict, check=lambda: None) -> list[dict]:
    from .native import diarization_child
    result = diarization_child(diarization_task(path, output, config), check, float(config.get("native_timeout", 7200)))
    check()
    return diarization_result(output, result)


def diarize_native(path: Path, output: Path, config: dict, check=lambda: None) -> list[dict]:
    if config.get("diarization_strategy", "bounded_global") != "native_full":
        from functools import partial
        from .bounded_diarization import run
        # One engine and one model digest per child, reused by every block (same model, same settings).
        return run(path, output, config, partial(diarize_whole, reuse={}), check)
    return diarize_whole(path, output, config, check)


# 0.3 s let sub-0.5 s slivers through, which the API clip planner later reports as mixed speakers
# (2026-11-13 38-minute meeting experiment: unknown speech 21.7% -> 17.1% at 0.5 s).
DIARIZATION_MIN_DURATION_ON = 0.5
DIARIZATION_MIN_DURATION_OFF = 0.5
# Unit bounds are rounded to whole ms; a neighbouring turn that only grazes a bound by this much is not overlap.
ALIGN_EDGE_TOLERANCE_SECONDS = 0.002


def diarize_whole(path: Path, output: Path, config: dict, check=lambda: None, *, reuse: dict | None = None) -> list[dict]:
    """`reuse` (one dict per child process) keeps model digests and the loaded engine across blocks."""
    import sherpa_onnx as sherpa
    segmentation, embedding = Path(config["diarization_model"]), Path(config["embedding_model"])
    if not segmentation.is_file() or not embedding.is_file():
        raise WorkerError("diarization_models_missing", "화자 모델이 없습니다. setup-models --diarization을 실행하세요.", "needs_setup")
    reuse = {} if reuse is None else reuse
    digests = reuse.setdefault("digests", {})
    for model in (segmentation, embedding):
        if str(model) not in digests:
            digests[str(model)] = sha256(model)
    identity = {"audio": sha256(path), "segmentation": digests[str(segmentation)], "embedding": digests[str(embedding)],
                "threshold": config.get("cluster_threshold", 0.5),
                "min_duration_on": DIARIZATION_MIN_DURATION_ON, "min_duration_off": DIARIZATION_MIN_DURATION_OFF}
    cached = read_json(output, {})
    if cached.get("identity") == identity and cached.get("complete"):
        return cached["turns"]
    settings = sherpa.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(segmentation)),
            num_threads=int(config["cpu_threads"])),
        embedding=sherpa.SpeakerEmbeddingExtractorConfig(model=str(embedding), num_threads=int(config["cpu_threads"])),
        clustering=sherpa.FastClusteringConfig(num_clusters=-1, threshold=float(config.get("cluster_threshold", 0.5))),
        min_duration_on=DIARIZATION_MIN_DURATION_ON, min_duration_off=DIARIZATION_MIN_DURATION_OFF)
    if not settings.validate():
        raise WorkerError("diarization_configuration", "화자 모델 형식을 확인하세요.", "needs_setup")
    key = (str(segmentation), str(embedding), int(config["cpu_threads"]), float(config.get("cluster_threshold", 0.5)))
    if reuse.get("engine_key") != key:
        reuse["engine"], reuse["engine_key"] = sherpa.OfflineSpeakerDiarization(settings), key
    engine = reuse["engine"]
    check()
    # Whole-meeting clustering maintains a single speaker namespace across all ASR chunks.
    result = engine.process(audio_samples(path), callback=lambda processed, total: (check() or 0))
    turns = [{"start": float(turn.start), "end": float(turn.end), "speaker": f"speaker_{turn.speaker:02d}"}
             for turn in result.sort_by_start_time()]
    check()
    atomic_json(output, {"identity": identity, "complete": True, "turns": turns})
    return turns


def align(asr_segments: list[dict], turns: list[dict], duration: float) -> list[dict]:
    """Assign words by interval overlap; do not invent text splits without word timing."""
    def speaker(start, end):
        weights = {}
        active = []
        edge = ALIGN_EDGE_TOLERANCE_SECONDS
        for turn in turns:
            overlap = max(0.0, min(end, turn["end"]) - max(start, turn["start"]))
            if not overlap:
                continue
            grazes = turn["start"] <= start or turn["end"] >= end
            if end - start > 2 * edge and overlap <= edge + 1e-9 and grazes:
                continue  # Only grazes a rounded unit bound; counting it would mark clean speech as overlap.
            weights[turn["speaker"]] = weights.get(turn["speaker"], 0) + overlap
            active.append(turn)
        overlap_speech = any(turn.get("overlap") for turn in active) or any(a["speaker"] != b["speaker"] and min(a["end"], b["end"], end) > max(a["start"], b["start"], start)
                             for n, a in enumerate(active) for b in active[n + 1:])
        ordered = sorted(weights, key=weights.get, reverse=True)
        winner = ordered[0] if ordered and weights[ordered[0]] >= (end - start) * 0.5 else "unknown"
        if len(ordered) > 1 and weights[ordered[1]] >= weights[ordered[0]] * 0.8:
            winner = "unknown"
        if overlap_speech:
            winner = "unknown"
        return winner, overlap_speech
    result = []
    for source in asr_segments:
        units = source.get("words") or [{"start": source["start"], "end": source["end"], "text": source["text"]}]
        provenance = source.get("timing_provenance", "asr_words" if source.get("words") else "asr_segments")
        for unit in units:
            start, end = max(0, round(unit["start"] * 1000)), min(round(duration * 1000), round(unit["end"] * 1000))
            if end <= start or not unit["text"].strip():
                continue
            label, overlap = speaker(start / 1000, end / 1000)
            overlap = overlap or source.get("overlap") is True
            if source.get("mixed_speakers") is True or overlap:
                label = "unknown"
            if result and result[-1]["local_speaker_id"] == label and result[-1]["overlap"] == overlap and result[-1]["timing_provenance"] == provenance and start - result[-1]["end_ms"] < 800 and end - result[-1]["start_ms"] <= 20000:
                result[-1]["text"] += (" " if not source.get("words") else "") + unit["text"]
                result[-1]["end_ms"] = end
            else:
                result.append({"segment_id": str(uuid.uuid4()), "start_ms": start, "end_ms": end,
                               "text": unit["text"], "local_speaker_id": label, "proposed_person_id": None,
                               "confirmed_person_id": None, "speaker_name": "", "overlap": overlap,
                               "timing_provenance": provenance,
                               "review_status": "uncertain" if overlap or label == "unknown" else "unreviewed"})
    for segment in result:
        segment["text"] = segment["text"].strip()
    return result
