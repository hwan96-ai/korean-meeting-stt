"""Transcribe finished diarization blocks while the child analyses later blocks.

Off unless the server worker setting `speech_pipeline` is true (API speech, bounded_global diarization).
A job keeps the path it started with: an existing checkpoint decides, the setting only picks for new jobs.

Exactly one paid request is in flight, through the same reservation, durable intent, circuit and
response handling as the whole-file path (`api_speech._send_clip`).

Clip frame ranges are planned from block-local speaker turns as soon as a block is final, and only
ranges that later blocks cannot change are frozen. They are frozen in the checkpoint before any paid
request. Speaker label, overlap and `mixed_speakers` are decided once, after global reconciliation,
with the same rule as the whole-file planner: a clip that spans two final speakers, a gap and a
speaker, or overlapping speech stays `unknown`.
"""
from __future__ import annotations

from bisect import bisect_left
import math
from pathlib import Path
import time
import wave

from . import bounded_diarization, native
from .api_circuit import for_config
from .api_speech import (MAX_FILE_BYTES, MIN_CLIP_SECONDS, _checkpoint_error, _coalesce_tail, _policy, _send_clip, _unresolved,
                         _validate_cached_clips, _wave_info, clean_keywords, new_stats, write_stats)
from .budget import JobBudget
from .core import WorkerError, atomic_json, read_json, sha256
from .speech import PARTITION_SECONDS, SAMPLE_RATE, diarization_result, diarization_task, partition_acoustic_intervals

PLAN_VERSION = "pipelined-2026-11-25.1"
# Ends in "-api.json" so operator tools that glob "*-api.json" (resolve_no_result, inspect_jobs) find it.
STATE_NAME = "api-asr-pipeline-api.json"
LEGACY_STATE_NAME = "api-asr-api.json"
RESULT_NAME = "api-asr.json"
MAX_CLIPS = 20000  # Same guard as api_speech._plan.
PLACEHOLDER = {"local_speaker_id": "unknown", "overlap": False}


class FallBack(Exception):
    """Nothing was planned or paid on this path; the caller runs the whole-file path instead."""


def selected(config: dict, directory: Path) -> bool:
    if (directory / STATE_NAME).exists():
        return True
    if (directory / LEGACY_STATE_NAME).exists():
        return False
    return (config.get("speech_pipeline") is True and config.get("execution_location") == "server"
            and config.get("speech_mode") == "api" and config.get("api_speech_model") == "gpt-transcribe"
            and config.get("diarization_strategy", "bounded_global") == "bounded_global")


class Planner:
    """Append-only clip plan from block-local turns. Frozen ranges never change when more blocks arrive."""

    def __init__(self, rate: int, frames: int, policy_seconds: float):
        self.rate, self.frames = rate, frames
        self.partition_max = max(1, round(PARTITION_SECONDS * rate))
        self.policy_max = min(max(1, int(policy_seconds * rate)), (MAX_FILE_BYTES - 4096) // 2)
        self.policy_seconds = policy_seconds
        self.minimum = math.ceil(MIN_CLIP_SECONDS * rate)  # as in _coalesce_tail
        if self.policy_max < 2 * self.minimum:
            raise WorkerError("api_speech_clip_policy", "겹치기 방식은 API 청크 상한이 1초 이상이어야 합니다.", "needs_setup")
        self.intervals: list[list] = []  # [start, end, label, overlap], uncut, not fully frozen
        self.covered = 0
        self.frozen_end = 0
        self.plan: list[dict] = []
        self.finished = False

    def add_block(self, turns: list[dict], first: int, last: int, *, freeze: bool = True) -> list[dict]:
        """`freeze=False` only exists so tests can build the plan in one pass at the end."""
        if self.finished or first != self.covered or not first < last <= self.frames:
            raise _checkpoint_error()
        for piece in self._partition(turns, first, last):
            previous = self.intervals[-1] if self.intervals else None
            if previous and previous[2] == piece[2] and previous[3] == piece[3] and previous[1] == piece[0]:
                previous[1] = piece[1]
            else:
                self.intervals.append(piece)
        self.covered = last
        # The last interval can still grow into the next block, so only earlier ones are closed.
        return self._freeze(self.intervals[:-1], final=False) if freeze else []

    def finish(self) -> list[dict]:
        if self.covered != self.frames:
            raise _checkpoint_error()
        added = self._freeze(self.intervals, final=True) if self.frozen_end < self.frames else []
        self.finished = True
        return added

    def _partition(self, turns, first, last):
        rate, normalized, boundaries = self.rate, [], {first, last}
        for turn in turns:
            start, end = max(first, round(turn["start"] * rate)), min(last, round(turn["end"] * rate))
            if end > start:
                normalized.append((start, end, turn["speaker"]))
                boundaries.update([start, end])
        output, points = [], sorted(boundaries)
        for start, end in zip(points, points[1:]):
            active = {speaker for left, right, speaker in normalized if left < end and right > start}
            label = next(iter(active)) if len(active) == 1 else "unknown"
            overlap = len(active) > 1 or any(turn.get("overlap") and round(turn["start"] * rate) < end and round(turn["end"] * rate) > start for turn in turns)
            if overlap:
                label = "unknown"
            if output and output[-1][2] == label and output[-1][3] == overlap and output[-1][1] == start:
                output[-1][1] = end
            else:
                output.append([start, end, label, overlap])
        return output

    def _pieces(self, intervals):
        pieces = []
        for start, end, label, overlap in intervals:
            for outer in range(start, end, self.partition_max):
                outer_end = min(outer + self.partition_max, end)
                for inner in range(outer, outer_end, self.policy_max):
                    inner_end = min(inner + self.policy_max, outer_end)
                    if inner_end <= self.frozen_end:
                        continue
                    pieces.append({"start_frame": max(inner, self.frozen_end), "end_frame": inner_end,
                                   "start": max(inner, self.frozen_end) / self.rate, "end": inner_end / self.rate,
                                   "local_speaker_id": label, "overlap": overlap})
        return pieces

    def _freeze(self, intervals, final: bool) -> list[dict]:
        pieces = self._pieces(intervals)
        if not pieces:
            if final:
                raise _checkpoint_error()
            return []
        limit = pieces[-1]["end_frame"]
        if final:
            # Same greedy as the whole-file planner, continued from a clip boundary it would also reach.
            clips = _coalesce_tail(pieces, self.rate, self.policy_seconds)
        else:
            # The whole-file greedy, stopped before any clip whose end could depend on frames not yet known:
            # its first boundary past the 0.5 s minimum must be known, and it must end at least 0.5 s
            # before the open edge so the end-of-audio adjustment can never apply to it.
            boundaries, start, clips = [piece["end_frame"] for piece in pieces], self.frozen_end, []
            while True:
                position = bisect_left(boundaries, start + self.minimum)
                if position >= len(boundaries):
                    break
                end = min(boundaries[position], start + self.policy_max)
                if limit - end < self.minimum:
                    break
                clips.append({"start_frame": start, "end_frame": end})
                start = end
        added = []
        for clip in clips:
            if clip["start_frame"] != self.frozen_end:
                break
            if len(self.plan) + len(added) >= MAX_CLIPS:
                raise WorkerError("api_speech_clip_limit", "음성 구간 수가 허용 범위를 초과합니다.", "needs_review")
            entry = {"start_frame": clip["start_frame"], "end_frame": clip["end_frame"], "start": clip["start_frame"] / self.rate,
                     "end": clip["end_frame"] / self.rate, **PLACEHOLDER}
            added.append(entry)
            self.frozen_end = clip["end_frame"]
        self.plan.extend(added)
        return added


def label_plan(plan: list[dict], turns: list[dict], frames: int, rate: int) -> list[dict]:
    """Final label per clip from the reconciled turns, by the whole-file planner's rule."""
    finals = [(round(item["start"] * rate), round(item["end"] * rate), item["local_speaker_id"], item["overlap"])
              for item in partition_acoustic_intervals(turns, frames / rate)]
    output, cursor = [], 0
    for clip in plan:
        while cursor < len(finals) and finals[cursor][1] <= clip["start_frame"]:
            cursor += 1
        active, probe = [], cursor
        while probe < len(finals) and finals[probe][0] < clip["end_frame"]:
            if finals[probe][1] > clip["start_frame"]:
                active.append(finals[probe])
            probe += 1
        labels = {item[2] for item in active}
        mixed, overlap = len(labels) > 1, any(item[3] for item in active)
        output.append({"local_speaker_id": "unknown" if mixed or overlap or not labels else next(iter(labels)), "overlap": overlap,
                       **({"mixed_speakers": True} if mixed else {})})
    return output


def _all_blocks(output: Path, geometry: dict) -> bool:
    """Every block of a cached diarization result is present and usable for planning."""
    for index in range(geometry["total_blocks"]):
        try:
            if bounded_diarization.finished_block(output, geometry, index) is None:
                return False
        except WorkerError:
            return False  # A missing or mismatched block file: the whole-file path can still use the cached result.
    return True


def _start_child(audio: Path, output: Path, config: dict):
    return native.start_isolated(native.DIARIZATION_COMMAND, diarization_task(audio, output, config))


def _load_state(state_path: Path, audio: Path, identity: dict, geometry: dict, ledger_path: Path, rate: int) -> dict:
    try:
        exists = state_path.exists()
        cached = read_json(state_path, {})
        ledger = read_json(ledger_path, {"attempts": []})
        if not isinstance(cached, dict) or not isinstance(ledger, dict):
            raise _checkpoint_error()
        if not exists:
            if any(entry.get("stage") == "transcribing" for entry in ledger.get("attempts", [])):
                raise _checkpoint_error()  # Paid attempts without their checkpoint: never plan them again.
            return {}
        old = cached.get("identity", {})
        if cached.get("plan_version") != PLAN_VERSION or not isinstance(old, dict) or \
                any(old.get(key) != identity[key] for key in ("source_sha256", "model", "language")) or old.get("block_digest") != geometry["digest"]:
            raise _checkpoint_error()
        plan = cached.get("plan")
        if not isinstance(plan, list) or not isinstance(cached.get("clips"), list):
            raise _checkpoint_error()
        previous = 0
        for entry in plan:
            if not isinstance(entry, dict) or type(entry.get("start_frame")) is not int or type(entry.get("end_frame")) is not int or \
                    entry["start_frame"] != previous or entry["end_frame"] <= entry["start_frame"] or \
                    any(entry.get(key) != value for key, value in PLACEHOLDER.items()):
                raise _checkpoint_error()
            previous = entry["end_frame"]
        if plan:
            _validate_cached_clips(cached, plan, audio, identity, ledger, rate)
        elif cached["clips"] or any(entry.get("stage") == "transcribing" for entry in ledger.get("attempts", [])):
            raise _checkpoint_error()
        seconds = cached.get("plan_seconds")
        if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or not 0 < seconds <= 600:
            raise _checkpoint_error()
        return cached
    except WorkerError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError):
        raise _checkpoint_error() from None


def transcribe(audio: Path, directory: Path, config: dict, check=lambda: None, *, keywords=None,
               progress=lambda stage, done, total: None, start_child=_start_child, poll_seconds=0.2):
    """Diarize in the child and transcribe here, overlapped. Returns (final turns, asr result).

    Raises FallBack when the whole-file path should run instead and nothing was planned here.
    """
    policy = _policy(config)
    circuit = for_config(config)
    if circuit:
        circuit.assert_open()
    rate, frames = _wave_info(audio)
    _unresolved(directory)
    budget = JobBudget(directory, policy["cap"])
    state_path = directory / STATE_NAME
    try:
        geometry = bounded_diarization.layout(audio, config)
    except WorkerError as exc:
        if exc.state == "needs_setup" and not state_path.exists():
            raise FallBack() from None
        raise
    if geometry["frames"] != frames or geometry["rate"] != rate or rate != SAMPLE_RATE:
        raise WorkerError("diarization_audio", "화자 분석과 전사의 원음 길이가 다릅니다.", "needs_review")
    identity = {"source_sha256": sha256(audio), "model": policy["model"], "language": policy["language"]}
    cached = _load_state(state_path, audio, identity, geometry, directory / "api-budget.json", rate)
    minimum = math.ceil(MIN_CLIP_SECONDS * rate)
    if not state_path.exists():
        too_short = min(int(policy["seconds"] * rate), (MAX_FILE_BYTES - 4096) // 2) < 2 * minimum
        # Clips are at least 0.5 s, so only audio this long could ever exceed the clip limit.
        too_long = frames > MAX_CLIPS * minimum
        output = directory / "diarization.json"
        finished = read_json(output, {}) if output.exists() else {}
        missing = isinstance(finished, dict) and finished.get("complete") is True and not _all_blocks(output, geometry)
        if too_short or too_long or missing:
            raise FallBack()
    stored_plan = list(cached.get("plan", []))
    if cached:
        paid = any(entry.get("state") == "complete" and not entry.get("digital_silence") for entry in cached["clips"])
        if not paid and policy["model"] == "gpt-transcribe" and cached.get("keywords") != clean_keywords(keywords):
            cached["keywords"] = clean_keywords(keywords)
            atomic_json(state_path, cached)
    else:
        cached = {"identity": {**identity, "block_digest": geometry["digest"]}, "plan_version": PLAN_VERSION, "plan_seconds": policy["seconds"],
                  "keywords": clean_keywords(keywords) if policy["model"] == "gpt-transcribe" else [], "plan": [], "clips": [], "complete": False}
    terms = clean_keywords(cached.get("keywords")) if policy["model"] == "gpt-transcribe" else []
    # A resumed job keeps the clip length it was planned with, so a settings change cannot invalidate paid clips.
    planner = Planner(rate, frames, cached["plan_seconds"])
    output = directory / "diarization.json"
    stats = new_stats()
    deadline = time.monotonic() + float(config.get("native_timeout", 7200))
    child = start_child(audio, output, config)
    turns, next_block, index, finished_ok = None, 0, 0, False
    try:
        with wave.open(str(audio), "rb") as source:
            def read_blocks():
                nonlocal next_block
                found = []
                while next_block < geometry["total_blocks"]:
                    owned = bounded_diarization.finished_block(output, geometry, next_block)
                    if owned is None:
                        break
                    first, last, _, _ = bounded_diarization.block_bounds(geometry, next_block)
                    found += planner.add_block(owned, first, last)
                    next_block += 1
                return found

            while True:
                check()
                added = read_blocks()
                if turns is None:
                    returncode = child.poll()
                    if returncode is None and time.monotonic() > deadline:
                        raise WorkerError("native_timeout", "로컬 화자 분석의 제한 시간을 초과했습니다. 체크포인트는 보존했습니다.", retryable=True)
                    if returncode is not None:
                        check()
                        added += read_blocks()  # The child may have finished its last block just before exiting.
                        try:
                            turns = diarization_result(output, returncode)
                        except WorkerError as exc:
                            if exc.state == "needs_setup" and not state_path.exists():
                                raise FallBack() from None
                            raise
                        if next_block < geometry["total_blocks"]:
                            if not state_path.exists():
                                raise FallBack()  # A cached result without its block files.
                            raise WorkerError("diarization_blocks_missing", "화자 분석 블록 기록이 없어 이어서 전사할 수 없습니다.", "needs_review")
                        added += planner.finish()
                        progress("transcribing", index, len(planner.plan))
                if added:
                    start = len(planner.plan) - len(added)
                    for offset, entry in enumerate(added):
                        position = start + offset
                        if position < len(stored_plan):
                            if stored_plan[position] != entry:
                                raise _checkpoint_error()
                        else:
                            cached["plan"].append(entry)
                    if len(cached["plan"]) > len(stored_plan):
                        stored_plan = list(cached["plan"])
                        atomic_json(state_path, cached)  # Frozen before any paid request uses it.
                if index < len(planner.plan) and index < len(cached["plan"]):
                    interval = cached["plan"][index]
                    if index < len(cached["clips"]):
                        entry = cached["clips"][index]
                        if entry.get("state") == "complete":
                            index += 1
                            continue
                        if entry.get("state") == "response_invalid":
                            raise WorkerError(entry.get("error_code", "api_speech_response_invalid"), "이미 받은 음성 API 응답을 로컬에서 확인하세요. 같은 요청을 자동 재호출하지 않습니다.", "needs_review")
                        if entry.get("retry_at", 0) > time.time():
                            raise WorkerError("api_speech_rate_limit", "제공자가 지정한 재시도 시각까지 기다리세요.", "rate_limited", True)
                        cached["clips"] = cached["clips"][:index]
                    check()  # A lease lost while blocks were read or planned must not buy this request.
                    _send_clip(source, index, interval, cached, state_path, policy, budget, circuit, identity, terms, stats)
                    index += 1
                    if turns is None:
                        progress("diarizing", next_block, geometry["total_blocks"])
                    else:
                        progress("transcribing", index, len(planner.plan))
                    continue
                if turns is not None and planner.finished and index >= len(planner.plan):
                    finished_ok = True
                    break
                if turns is None:
                    progress("diarizing", next_block, geometry["total_blocks"])
                time.sleep(poll_seconds)
    finally:
        native.stop(child)
        if not finished_ok:
            # Numbers from an interrupted run are kept too, so a retry does not hide the requests already sent.
            write_stats(directory, "pipelined_interrupted", stats, clips=len(cached["plan"]), blocks=next_block)
    check()
    plan = cached["plan"]
    if len(plan) != len(planner.plan) or plan != planner.plan or not plan or plan[0]["start_frame"] != 0 or plan[-1]["end_frame"] != frames or \
            any(a["end_frame"] != b["start_frame"] for a, b in zip(plan, plan[1:])) or len(cached["clips"]) != len(plan) or \
            any(entry.get("state") != "complete" for entry in cached["clips"]):
        raise WorkerError("api_speech_coverage", "전체 원음 시간축을 빠짐없이 전사하지 못했습니다.", "needs_review")
    labels = label_plan(plan, turns, frames, rate)
    segments, usage_items = [], []
    for entry, label in zip(cached["clips"], labels):
        for segment in entry.get("segments", []):
            segments.append({**{key: value for key, value in segment.items() if key not in {"local_speaker_id", "overlap", "mixed_speakers"}}, **label})
        if "usage" in entry:
            usage_items.append(entry["usage"])
    cached.update(complete=True, duration=frames / rate)
    atomic_json(state_path, cached)
    result = {"segments": segments, "duration": frames / rate, "mode": "api/openai/" + policy["model"] + "/audio-clip-bounds",
              "clip_level": True, "coverage_complete": True, "pipelined": True,
              "coverage": [{"start": entry["start"], "end": entry["end"], "state": entry["state"], **({"digital_silence": True} if entry.get("digital_silence") else {}), **label}
                           for entry, label in zip(cached["clips"], labels)],
              "usage": {"provider": "openai", "model_id": policy["model"], "chunks": usage_items, "pricing": "configured_duration_rate",
                        "clip_count": len(cached["clips"]), "keywords_count": len(terms),
                        "cost_upper_bound_usd": sum(float(entry["charged_upper_bound_usd"]) for entry in read_json(directory / "api-budget.json", {"attempts": []})["attempts"] if entry.get("stage") == "transcribing")}}
    atomic_json(directory / RESULT_NAME, result)
    write_stats(directory, "pipelined", stats, clips=len(plan), blocks=geometry["total_blocks"])
    return turns, result
