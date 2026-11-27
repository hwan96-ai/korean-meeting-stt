"""Local-only OpenAI file transcription, with factual source-clip timing.

Modern GPT-Transcribe returns text, not word timestamps. Each text response is
associated with the exact PCM clip submitted; no text-based timestamp splitting
is performed. Whole-meeting speaker analysis remains a separate local stage.

Official contracts checked 2026-10-28:
https://developers.openai.com/api/docs/guides/speech-to-text
https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions/methods/create
https://developers.openai.com/api/docs/models/gpt-transcribe
https://developers.openai.com/api/docs/deprecations

`keywords[]` for gpt-transcribe checked 2026-11-13 in the same guide and reference:
literal terms as repeated form fields; the provider rejects the whole request for
`<`, `>`, CR or LF. No count or length limit is documented.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import time
import unicodedata
import uuid
import wave
from bisect import bisect_left
from email.utils import parsedate_to_datetime
from contextlib import nullcontext
from urllib import error, request

from .budget import JobBudget
from .client import NoRedirect
from .core import WorkerError, atomic_json, read_json, sha256
from .api_circuit import for_config

ENDPOINT = "https://api.openai.com/v1/audio/transcriptions"
MAX_FILE_BYTES = 25_000_000
MAX_RESPONSE_BYTES = 2_000_000
LEGACY_SHUTDOWN = dt.date(2027, 2, 26)
# Application engineering guard after an observed 50.625ms HTTP400. This is
# deliberately NOT represented as a documented provider minimum duration.
MIN_CLIP_SECONDS = 0.5
PLAN_VERSION = "coalesced-min500ms-2026-10-30.1"
# Application guards for the provider `keywords` hint. The provider documents
# no count or length limit, so these are deliberately NOT provider limits.
MAX_KEYWORDS = 100
MAX_KEYWORD_CHARS = 40
MAX_KEYWORDS_TOTAL_CHARS = 1500
# Bounds on work done before normalization, so a huge stored value stays cheap.
MAX_RAW_KEYWORD_ITEMS = 1000
MAX_RAW_KEYWORD_CHARS = 400


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def clean_keywords(value) -> list[str]:
    """Return only terms that are safe to send as `keywords[]` form fields.

    The list comes from the site, so it is validated here independently of the
    web. An entry that breaks the app rules is dropped; it never fails the job.
    A list the provider itself rejects fails the clip like any other HTTP 4xx.
    """
    if not isinstance(value, list):
        return []
    output, seen, total = [], set(), 0
    for item in value[:MAX_RAW_KEYWORD_ITEMS]:
        if not isinstance(item, str) or len(item) > MAX_RAW_KEYWORD_CHARS:
            continue
        # split() also removes CR, LF and Unicode line separators.
        term = " ".join(unicodedata.normalize("NFC", item).split())
        if not term or len(term) > MAX_KEYWORD_CHARS or any(c in "<>" or unicodedata.category(c).startswith("C") for c in term):
            continue
        if term.casefold() in seen:
            continue
        if len(output) >= MAX_KEYWORDS:
            break
        if total + len(term) > MAX_KEYWORDS_TOTAL_CHARS:
            continue  # A shorter later term may still fit.
        seen.add(term.casefold())
        output.append(term)
        total += len(term)
    return output


def _today() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


def _policy(config: dict) -> dict:
    server = config.get("execution_location") == "server"
    key = config.get("openai_api_key") if server else config.get("openai_api_key") or os.environ.get("OPENAI_API_KEY")
    model = config.get("api_speech_model")
    if server and (config.get("speech_mode") != "api" or model != "gpt-transcribe" or config.get("api_credit_only_ack") is not True):
        raise WorkerError("server_api_policy", "서버 API 모드·gpt-transcribe·크레딧 사용 확인을 명시하세요.", "needs_setup")
    price, cap = config.get("api_speech_price_per_minute"), config.get("api_max_cost_usd")
    if not isinstance(key, str) or not key.strip() or any(c in key for c in "\r\n"):
        raise WorkerError("api_speech_key_missing", "음성 API 키를 로컬 암호화 설정에 저장하세요.", "needs_setup")
    if model not in {"gpt-transcribe", "whisper-1"}:
        raise WorkerError("api_speech_model_missing", "지원되는 음성 API 모델을 명시적으로 설정하세요: gpt-transcribe 또는 허용 기간의 whisper-1.", "needs_setup")
    if model == "whisper-1" and (config.get("api_speech_allow_deprecated") is not True or _today() >= LEGACY_SHUTDOWN):
        raise WorkerError("api_speech_model_expired", "whisper-1은 2027-02-26 종료 예정입니다. 종료 전 명시적 허용 또는 현재 지원 모델이 필요합니다.", "needs_setup")
    if not _number(price) or price <= 0 or not _number(cap) or cap <= 0:
        raise WorkerError("api_speech_policy_missing", "음성 API의 최신 분당 단가와 작업별 누적 비용 상한을 로컬에서 설정하세요.", "needs_setup")
    rounding = config.get("api_speech_billing_rounding_seconds", 60)
    seconds = config.get("api_speech_chunk_seconds", 30)
    timeout = config.get("api_speech_timeout_seconds", 300)
    language = config.get("api_speech_language", "ko")
    if not _number(rounding) or not 1 <= rounding <= 60 or not _number(seconds) or not 0 < seconds <= 600 or not _number(timeout) or not 1 <= timeout <= 600:
        raise WorkerError("api_speech_policy_invalid", "음성 API의 청크·요금 반올림·요청 제한 시간 설정을 확인하세요.", "needs_setup")
    if not isinstance(language, str) or not re.fullmatch(r"[a-z]{2,3}(?:-[a-z]{2,4})?", language):
        raise WorkerError("api_speech_language_invalid", "지원되는 음성 언어 코드만 설정하세요.", "needs_setup")
    return {"key": key.strip(), "model": model, "price": float(price), "cap": float(cap),
            "rounding": float(rounding), "seconds": float(seconds), "timeout": float(timeout), "language": language}


def is_ready(config: dict) -> bool:
    try:
        _policy(config)
        return True
    except WorkerError:
        return False


def _wave_info(path: Path) -> tuple[int, int]:
    try:
        with wave.open(str(path), "rb") as source:
            rate, frames = source.getframerate(), source.getnframes()
            if source.getnchannels() != 1 or source.getsampwidth() != 2 or source.getcomptype() != "NONE" or not 8000 <= rate <= 48000 or not 0 < frames <= rate * 14400:
                raise ValueError()
            nonzero = False
            counted = 0
            while True:
                raw = source.readframes(rate * 10)
                if not raw:
                    break
                counted += len(raw) // 2
                nonzero = nonzero or any(raw)
            if counted != frames:
                raise ValueError()
            if not nonzero:
                raise WorkerError("silent_audio", "디지털 무음 파일은 음성 API에 보내지 않습니다.", "needs_review")
            return rate, frames
    except (OSError, EOFError, wave.Error, ValueError):
        raise WorkerError("invalid_audio", "디코딩한 단일 채널 PCM 음성을 확인하세요. 빈 파일·손상·허용 길이 초과는 API에 보내지 않습니다.", "needs_review") from None


def _plan(intervals: list[dict] | None, frames: int, rate: int, max_seconds: float) -> list[dict]:
    duration = frames / rate
    if intervals is None:
        intervals = [{"start": 0.0, "end": duration, "local_speaker_id": "unknown", "overlap": False}]
    if not isinstance(intervals, list) or not intervals or len(intervals) > 20000:
        raise WorkerError("api_speech_coverage", "전체 원음 시간축의 실제 구간 명세가 필요합니다.", "needs_review")
    # Leave generous room for WAV and multipart headers below the 25 MB limit.
    maximum = min(max(1, int(max_seconds * rate)), (MAX_FILE_BYTES - 4096) // 2)
    output, previous = [], 0
    for value in intervals:
        if not isinstance(value, dict) or not _number(value.get("start")) or not _number(value.get("end")):
            raise WorkerError("api_speech_coverage", "음성 구간 시각은 유한한 실제 초 단위여야 합니다.", "needs_review")
        start, end = round(value["start"] * rate), round(value["end"] * rate)
        if start != previous or end <= start or end > frames:
            raise WorkerError("api_speech_coverage", "구간의 누락·겹침·범위 이탈을 수정하세요. 미확인 간격도 실제 오디오 구간으로 포함해야 합니다.", "needs_review")
        label = value.get("local_speaker_id", value.get("speaker", "unknown"))
        overlap = value.get("overlap", False)
        if not isinstance(label, str) or not 1 <= len(label) <= 100 or not isinstance(overlap, bool):
            raise WorkerError("api_speech_coverage", "화자 구간 명세 형식을 확인하세요.", "needs_review")
        while start < end:
            stop = min(end, start + maximum)
            output.append({"start_frame": start, "end_frame": stop,
                           "start": start / rate, "end": stop / rate,
                           "local_speaker_id": "unknown" if overlap else label, "overlap": overlap})
            if len(output) > 20000:
                raise WorkerError("api_speech_clip_limit", "음성 구간 수가 허용 범위를 초과합니다.", "needs_review")
            start = stop
        previous = end
    if previous != frames:
        raise WorkerError("api_speech_coverage", "끝 구간까지 원음 시간축을 포함해야 합니다.", "needs_review")
    return output


def _encoded_clip(source: wave.Wave_read, interval: dict) -> tuple[bytes, bool]:
    source.setpos(interval["start_frame"])
    raw = source.readframes(interval["end_frame"] - interval["start_frame"])
    if len(raw) != 2 * (interval["end_frame"] - interval["start_frame"]):
        raise WorkerError("invalid_audio", "분석 음성의 실제 프레임 수가 달라 API 전송을 중단했습니다.", "needs_review")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as clip:
        clip.setparams((1, 2, source.getframerate(), 0, "NONE", "not compressed"))
        clip.writeframes(raw)
    encoded = buffer.getvalue()
    if len(encoded) >= MAX_FILE_BYTES:
        raise WorkerError("api_speech_file_limit", "인코딩한 음성 조각은 25MB 미만이어야 합니다.", "needs_setup")
    return encoded, not any(raw)


def _plan_hash(plan):
    return hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()


def _coalesce_tail(plan: list[dict], rate: int, max_seconds: float) -> list[dict]:
    """Merge/rebalance only UNPAID real frames; never extend into a paid prefix."""
    if not plan:
        return []
    minimum = math.ceil(MIN_CLIP_SECONDS * rate)
    maximum = min(int(max_seconds * rate), (MAX_FILE_BYTES - 4096) // 2)
    start, finish = plan[0]["start_frame"], plan[-1]["end_frame"]
    if finish - start < minimum:
        raise WorkerError("api_speech_tail_too_short", "남은 실제 음성이 앱의 0.5초 안전 기준보다 짧습니다. 성공 구간은 재전송하지 않으며 원음 검토가 필요합니다.", "needs_review")
    if maximum < minimum:
        raise WorkerError("api_speech_clip_policy", "API 청크 상한이 앱의 0.5초 안전 기준보다 작습니다.", "needs_setup")
    boundaries = [entry["end_frame"] for entry in plan]
    output = []
    while start < finish:
        index = bisect_left(boundaries, start + minimum)
        end = min(boundaries[index] if index < len(boundaries) else finish, start + maximum)
        if 0 < finish - end < minimum:
            end = finish if finish - start <= maximum else finish - minimum
        if end - start < minimum or end - start > maximum:
            raise WorkerError("api_speech_clip_policy", "남은 원음 길이와 청크 상한으로 안전하게 분할할 수 없습니다. 성공 구간은 보존했습니다.", "needs_review")
        active = [entry for entry in plan if entry["start_frame"] < end and entry["end_frame"] > start]
        labels = {entry["local_speaker_id"] for entry in active}
        mixed = len(labels) > 1 or any(entry.get("mixed_speakers") for entry in active)
        overlap = any(entry["overlap"] for entry in active)
        output.append({"start_frame": start, "end_frame": end, "start": start / rate, "end": end / rate,
                       "local_speaker_id": "unknown" if mixed or overlap else next(iter(labels)), "overlap": overlap,
                       **({"mixed_speakers": True} if mixed else {})})
        start = end
    return output


def _checkpoint_error():
    return WorkerError("api_speech_checkpoint_invalid", "이전 API 구간과 원음·계획·비용 기록이 일치하지 않습니다. 성공 구간을 재전송하지 않고 확인을 요청합니다.", "needs_review")


def _validate_plan(plan, raw_plan, rate, maximum):
    if not isinstance(plan, list) or not plan:
        raise _checkpoint_error()
    previous, finish = 0, raw_plan[-1]["end_frame"]
    for entry in plan:
        start, end = entry.get("start_frame"), entry.get("end_frame")
        if type(start) is not int or type(end) is not int or start != previous or not start < end <= finish or end - start > maximum or entry.get("start") != start / rate or entry.get("end") != end / rate:
            raise _checkpoint_error()
        active = [old for old in raw_plan if old["start_frame"] < end and old["end_frame"] > start]
        labels = {old["local_speaker_id"] for old in active}
        mixed, overlap = len(labels) > 1, any(old["overlap"] for old in active)
        label = "unknown" if mixed or overlap else next(iter(labels))
        if entry.get("local_speaker_id") != label or entry.get("overlap") is not overlap or bool(entry.get("mixed_speakers")) != mixed:
            raise _checkpoint_error()
        previous = end
    if previous != finish:
        raise _checkpoint_error()


def _validate_cached_clips(cached, plan, audio, identity, ledger, rate):
    """Verify existing clips against original frames AND their paid intent ledger."""
    clips = cached.get("clips")
    if not isinstance(clips, list) or len(clips) > len(plan) or not isinstance(ledger.get("attempts"), list):
        raise _checkpoint_error()
    attempts = ledger["attempts"]
    ids = [entry.get("attempt_id") for entry in attempts]
    if len(set(ids)) != len(ids):
        raise _checkpoint_error()
    by_id = {entry.get("attempt_id"): entry for entry in attempts}
    complete, referenced = 0, set()
    with wave.open(str(audio), "rb") as source:
        for index, entry in enumerate(clips):
            expected = plan[index]
            if not isinstance(entry, dict) or any(entry.get(k) != expected.get(k) for k in ("start_frame", "end_frame", "start", "end", "local_speaker_id", "overlap", "mixed_speakers")):
                raise _checkpoint_error()
            encoded, silent = _encoded_clip(source, expected)
            if entry.get("encoded_bytes") != len(encoded) or entry.get("sha256") != hashlib.sha256(encoded).hexdigest():
                raise _checkpoint_error()
            state = entry.get("state")
            if state == "response_invalid":
                raise WorkerError("api_speech_response_invalid", "이미 받은 응답 검토가 필요합니다. 계획 변경으로 재전송하지 않습니다.", "needs_review")
            if state in {"inflight", "unknown_outcome"}:
                raise WorkerError("api_speech_unknown", "이전 요청 결과가 불명확해 계획 변경과 재전송을 차단했습니다.", "unknown_outcome")
            if state not in {"complete", "rejected"} or (state != "complete" and index != len(clips) - 1) or (state == "complete" and complete != index):
                raise _checkpoint_error()
            if entry.get("digital_silence") is True:
                if not silent or state != "complete" or entry.get("segments") != [] or entry.get("attempt_id"):
                    raise _checkpoint_error()
            else:
                attempt = by_id.get(entry.get("attempt_id"))
                if not isinstance(attempt, dict) or not _number(entry.get("reserved_upper_bound_usd")) or entry["reserved_upper_bound_usd"] < 0:
                    raise _checkpoint_error()
                if str(uuid.UUID(entry["attempt_id"])) != entry["attempt_id"]:
                    raise _checkpoint_error()
                details = attempt.get("details", {})
                if attempt.get("stage") != "transcribing" or attempt.get("model_id") != identity["model"] or attempt.get("reserved_upper_bound_usd") != entry["reserved_upper_bound_usd"] or any(details.get(k) != v for k, v in {"clip_index": index, "start_frame": expected["start_frame"], "end_frame": expected["end_frame"], "audio_sha256": identity["source_sha256"]}.items()):
                    raise _checkpoint_error()
                if not _number(attempt.get("charged_upper_bound_usd")) or attempt["charged_upper_bound_usd"] < 0:
                    raise _checkpoint_error()
                if state == "complete" and attempt.get("state") != "responded":
                    raise _checkpoint_error()
                if state == "rejected" and (attempt.get("state") not in {"needs_setup", "rate_limited", "resolved_no_result"} or entry.get("error_code") != attempt.get("error_code") or (entry.get("http_status") not in {400, 401, 403, 404, 413, 415, 422, 429} and attempt.get("state") != "resolved_no_result")):
                    raise _checkpoint_error()
                referenced.add(entry["attempt_id"])
            if state == "complete":
                segments = entry.get("segments")
                if not isinstance(segments, list):
                    raise _checkpoint_error()
                for segment in segments:
                    if not isinstance(segment, dict) or not isinstance(segment.get("text"), str) or not _number(segment.get("start")) or not _number(segment.get("end")) or not expected["start"] <= segment["start"] < segment["end"] <= expected["end"] + 1e-6:
                        raise _checkpoint_error()
                    if segment.get("local_speaker_id") != expected["local_speaker_id"] or segment.get("overlap") is not expected["overlap"] or bool(segment.get("mixed_speakers")) != bool(expected.get("mixed_speakers")):
                        raise _checkpoint_error()
                    if identity["model"] == "gpt-transcribe" and (segment.get("start") != expected["start"] or segment.get("end") != expected["end"] or segment.get("words") != [] or segment.get("timing_provenance") != "audio_clip_bounds"):
                        raise _checkpoint_error()
                complete += 1
    if any(entry.get("stage") == "transcribing" and entry.get("state") in {"responded", "response_invalid"} and entry.get("attempt_id") not in referenced for entry in attempts):
        raise _checkpoint_error()
    if cached.get("complete") and (complete != len(plan) or len(clips) != len(plan)):
        raise _checkpoint_error()
    return complete


def _resume_plan(audio, state_path, raw_plan, policy, rate, identity, keywords=()):
    """Upgrade a verified legacy checkpoint by freezing its complete prefix."""
    try:
        exists = state_path.exists()
        cached = read_json(state_path, {})
        ledger = read_json(state_path.parent / "api-budget.json", {"attempts": []})
        if not isinstance(cached, dict) or not isinstance(ledger, dict):
            raise _checkpoint_error()
        raw_hash = _plan_hash(raw_plan)
        if exists:
            old = cached.get("identity", {})
            if not isinstance(old, dict) or any(old.get(k) != identity[k] for k in ("source_sha256", "model", "language")):
                raise _checkpoint_error()
            if cached.get("plan_version") == PLAN_VERSION:
                plan = cached.get("plan")
                if not isinstance(plan, list) or cached.get("source_plan_sha256") != raw_hash or old.get("intervals_sha256") != _plan_hash(plan):
                    raise _checkpoint_error()
                _validate_plan(plan, raw_plan, rate, min(int(policy["seconds"] * rate), (MAX_FILE_BYTES - 4096) // 2))
                complete = _validate_cached_clips(cached, plan, audio, identity, ledger, rate)
                if any(entry["end_frame"] - entry["start_frame"] < math.ceil(MIN_CLIP_SECONDS * rate) for entry in plan[complete:]):
                    raise _checkpoint_error()
                return plan, cached
            if cached.get("plan_version") is not None or old.get("intervals_sha256") != raw_hash:
                raise _checkpoint_error()
            complete = _validate_cached_clips(cached, raw_plan, audio, identity, ledger, rate)
            prefix = raw_plan[:complete]
            tail = _coalesce_tail(raw_plan[complete:], rate, policy["seconds"])
            plan = prefix + tail
            # Keep the 19 paid clip objects unchanged; retain the definitive
            # rejected attempt as history rather than erasing it on migration.
            retired = [*cached.get("retired_clips", []), *cached["clips"][complete:]]
            cached = {**cached, "clips": cached["clips"][:complete], "retired_clips": retired, "complete": False}
        else:
            if any(entry.get("stage") == "transcribing" for entry in ledger.get("attempts", [])):
                raise _checkpoint_error()
            plan = _coalesce_tail(raw_plan, rate, policy["seconds"])
            cached = {"clips": [], "complete": False, "keywords": list(keywords)}
        cached.update(identity={**identity, "intervals_sha256": _plan_hash(plan)}, plan_version=PLAN_VERSION,
                      source_plan_sha256=raw_hash, plan=plan)
        atomic_json(state_path, cached)
        return plan, cached
    except WorkerError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError):
        raise _checkpoint_error() from None


def _multipart(encoded: bytes, policy: dict, index: int, keywords=()) -> tuple[bytes, str]:
    boundary = "meeting_" + uuid.uuid4().hex
    parts = []
    fields = [("model", policy["model"]), ("response_format", "verbose_json" if policy["model"] == "whisper-1" else "json")]
    if policy["model"] == "whisper-1":
        fields += [("language", policy["language"]), ("timestamp_granularities[]", "word"), ("timestamp_granularities[]", "segment")]
    else:
        fields += [("languages[]", policy["language"])]
        if policy["model"] == "gpt-transcribe":
            fields += [("keywords[]", term) for term in clean_keywords(list(keywords))]
    for name, value in fields:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    # Never send an original filename, local path, credential or arbitrary form field.
    # The only free text is the validated term list above.
    parts += [f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="clip-{index:05d}.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode(), encoded,
              f'\r\n--{boundary}--\r\n'.encode()]
    return b"".join(parts), "multipart/form-data; boundary=" + boundary


def _post(body: bytes, content_type: str, key: str, timeout: float) -> tuple[bytes, str | None]:
    req = request.Request(ENDPOINT, body, {"Authorization": "Bearer " + key, "Content-Type": content_type, "User-Agent": "CodexMeetingSpeech/1.0"}, method="POST")
    with request.build_opener(NoRedirect()).open(req, timeout=timeout) as response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise WorkerError("api_speech_response_limit", "음성 API 응답이 허용 크기를 초과했습니다. 로컬 요청 기록을 확인하세요.", "unknown_outcome")
        return raw, response.headers.get("x-request-id")


def _usage(response: dict) -> dict:
    raw = response.get("usage", {})
    if not isinstance(raw, dict):
        return {}
    clean = {key: value for key, value in raw.items() if key in {"seconds", "input_tokens", "output_tokens", "total_tokens"} and _number(value) and value >= 0}
    if raw.get("type") in {"duration", "tokens"}:
        clean["type"] = raw["type"]
    return clean


def _parsed_segments(response: dict, interval: dict, model: str) -> list[dict]:
    if not isinstance(response, dict) or not isinstance(response.get("text"), str) or len(response["text"]) > 200000:
        raise WorkerError("api_speech_response_invalid", "음성 API 응답 형식을 검증하지 못했습니다. 저장한 응답을 검토하며 자동 재호출하지 않습니다.", "needs_review")
    duration, offset = interval["end"] - interval["start"], interval["start"]
    base = {"local_speaker_id": interval["local_speaker_id"], "overlap": interval["overlap"],
            **({"mixed_speakers": True} if interval.get("mixed_speakers") else {})}
    if model != "whisper-1":
        text = response["text"].strip()
        return [{**base, "start": offset, "end": interval["end"], "text": text, "words": [],
                 "timing_provenance": "audio_clip_bounds"}] if text else []
    items = response.get("segments")
    if not isinstance(items, list) or (response["text"].strip() and not items):
        raise WorkerError("api_speech_timestamps_missing", "선택한 API 경로가 실제 구간 시각을 반환하지 않았습니다. 저장된 응답을 검토하세요.", "needs_review")
    output, seen = [], set()
    for item in items:
        start, end = item.get("start"), item.get("end")
        if not _number(start) or not _number(end) or not 0 <= start < end <= duration + 0.0001 or not isinstance(item.get("text"), str):
            raise WorkerError("api_speech_timestamps_invalid", "API 구간 시각이 실제 전송 음성 범위를 벗어납니다. 시각을 만들어 보정하지 않습니다.", "needs_review")
        text, end = item["text"].strip(), min(end, duration)
        key = (start, end, text)
        if not text or key in seen:
            continue
        seen.add(key)
        output.append({**base, "start": offset + start, "end": offset + end, "text": text, "words": [],
                       "timing_provenance": "provider_segment"})
    # Provider words are used only when individually valid; never split text uniformly.
    words = response.get("words", [])
    if words and not isinstance(words, list):
        raise WorkerError("api_speech_timestamps_invalid", "API 단어 시각 형식이 올바르지 않습니다.", "needs_review")
    for word in words:
        start, end = word.get("start"), word.get("end")
        if not _number(start) or not _number(end) or not 0 <= start < end <= duration + 0.0001 or not isinstance(word.get("word"), str):
            raise WorkerError("api_speech_timestamps_invalid", "API 단어 시각이 실제 전송 음성 범위를 벗어납니다.", "needs_review")
        candidates = [s for s in output if offset + start >= s["start"] - 0.0001 and offset + end <= s["end"] + 0.0001]
        if len(candidates) == 1:
            candidates[0]["words"].append({"start": offset + start, "end": offset + min(end, duration), "text": word["word"]})
    return output


def _retry_at(headers) -> float | None:
    value = headers.get("Retry-After") if headers else None
    if not value:
        return None
    try:
        return time.time() + min(86400, max(0, float(value)))
    except ValueError:
        try:
            return parsedate_to_datetime(value).timestamp()
        except (ValueError, TypeError):
            return None


def _unresolved(directory: Path) -> None:
    ledger = read_json(directory / "api-budget.json", {"attempts": []})
    # Meeting-info inference keeps its own record and never blocks the transcript.
    if any(entry.get("state") in {"inflight", "unknown_outcome"} and entry.get("stage") != "meta_inference" for entry in ledger.get("attempts", [])):
        raise WorkerError("api_speech_unknown", "이 작업의 이전 유료 요청 결과가 불명확합니다. 모델·구간을 바꿔 재호출하지 말고 PC에서 먼저 확인하세요.", "unknown_outcome")


def resolve_no_result(directory: Path, attempt_id: str, max_cost_usd: float) -> bool:
    """Explicit operator attestation only; retains a conservative charge reservation.

    The CLI must obtain human confirmation/provider evidence before calling this.
    A no-result response is not proof of no charge, so this never refunds budget.
    """
    ledger = read_json(directory / "api-budget.json", {"attempts": []})
    entry = next((e for e in ledger["attempts"] if e.get("attempt_id") == attempt_id and e.get("stage") == "transcribing"), None)
    if entry is None:
        return False
    if entry.get("state") not in {"inflight", "unknown_outcome", "resolved_no_result"}:
        raise WorkerError("api_resolution_invalid", "불명확한 음성 요청만 명시적으로 해소할 수 있습니다.", "needs_review")
    JobBudget(directory, max_cost_usd).settle(attempt_id, state="resolved_no_result", error_code="operator_confirmed_no_result")
    found = False
    for path in directory.glob("*-api.json"):
        cached = read_json(path, {})
        for clip in cached.get("clips", []):
            if clip.get("attempt_id") == attempt_id:
                clip.update(state="rejected", error_code="operator_confirmed_no_result",
                            confirmed_no_result_at=dt.datetime.now(dt.timezone.utc).isoformat())
                found = True
        if found:
            atomic_json(path, cached)
            break
    return True


ERROR_CODES = {"credit_balance_exhausted", "insufficient_quota", "billing_hard_limit_reached", "billing_limit_reached",
               "billing_not_active", "usage_limit_reached", "rate_limit_exceeded", "requests_limit_exceeded", "tokens_limit_exceeded"}


def _http_failure(exc) -> tuple[str, str | None]:
    """Read bounded provider errors; retain only known code/type literals."""
    safe = None
    try:
        body = exc.read(8193)
        if len(body) <= 8192:
            value = json.loads(body)
            value = value.get("error", {}) if isinstance(value, dict) else {}
            if isinstance(value, dict):
                safe = next((value.get(k) for k in ("code", "type") if value.get(k) in ERROR_CODES), None)
    except Exception:
        pass
    if safe in {"credit_balance_exhausted", "insufficient_quota"}:
        return safe, safe
    if safe in {"billing_hard_limit_reached", "billing_limit_reached", "billing_not_active", "usage_limit_reached"}:
        return "billing_limit", safe
    if exc.code == 429:
        return ("transient_429" if safe in {"rate_limit_exceeded", "requests_limit_exceeded", "tokens_limit_exceeded"} else "ambiguous_429"), safe
    return ("unknown_outcome" if exc.code >= 500 or 300 <= exc.code < 400 else "provider_configuration"), safe


def _submit_clip(body, content_type, policy, budget, attempt_id, entry, cached, state_path, interval, circuit, stats=None):
    started = time.monotonic()
    try:
        raw, request_id = _post(body, content_type, policy["key"], policy["timeout"])
        if stats is not None:
            stats["requests"] += 1
            stats["post_seconds"].append(time.monotonic() - started)
    except error.HTTPError as exc:
        reason, provider_code = _http_failure(exc)
        if circuit:
            circuit.trip(reason)
        definitive = exc.code in {400, 401, 403, 404, 413, 415, 422, 429}
        state = "rate_limited" if exc.code == 429 else "needs_setup" if definitive else "unknown_outcome"
        code = "api_speech_rate_limit" if exc.code == 429 else "api_speech_configuration" if definitive else "api_speech_unknown"
        if reason in {"credit_balance_exhausted", "insufficient_quota", "billing_limit"}:
            code, state = "api_speech_credit_blocked", "needs_setup"
        entry.update(state="rejected" if definitive else "unknown_outcome", error_code=code, http_status=exc.code)
        if provider_code:
            entry["provider_error_code"] = provider_code
        # Keep old personal-mode Retry-After behavior; server requires explicit resume.
        retry = _retry_at(exc.headers) if exc.code == 429 and not circuit else None
        if retry:
            entry["retry_at"] = retry
        budget.settle(attempt_id, state=state, charged_upper_bound_usd=0 if definitive and reason != "ambiguous_429" else None, error_code=code)
        atomic_json(state_path, cached)
        raise WorkerError(code, "음성 API를 안전 중단했습니다. 운영자가 크레딧·결제 조건과 요청 상태를 확인하세요.", state, False) from None
    except (error.URLError, TimeoutError, OSError, WorkerError):
        if circuit:
            circuit.trip("unknown_outcome")
        entry.update(state="unknown_outcome", error_code="api_speech_unknown")
        budget.settle(attempt_id, state="unknown_outcome", error_code="api_speech_unknown")
        atomic_json(state_path, cached)
        raise WorkerError("api_speech_unknown", "음성 API 응답이 끊겨 재호출을 차단했습니다. 이전 요청을 확인하세요.", "unknown_outcome") from None
    # Arbitrary headers, raw JSON and echoed credentials never enter checkpoints.
    if isinstance(request_id, str) and re.fullmatch(r"req_[A-Za-z0-9_-]{1,128}", request_id) and policy["key"] not in request_id:
        entry["request_id"] = request_id
    entry["response_received"] = True
    try:
        if policy["key"].encode() in raw:
            raise ValueError("credential echo")
        response = json.loads(raw)
        usage = _usage(response) if isinstance(response, dict) else {}
        parsed = _parsed_segments(response, interval, policy["model"])
        if policy["key"] in json.dumps(parsed, ensure_ascii=False):
            raise ValueError("credential echo")
    except (ValueError, TypeError, AttributeError, WorkerError) as exc:
        code = exc.code if isinstance(exc, WorkerError) else "api_speech_response_invalid"
        if circuit:
            circuit.trip("response_invalid")
        entry.update(state="response_invalid", error_code=code)
        budget.settle(attempt_id, state="response_invalid", error_code=code)
        atomic_json(state_path, cached)
        raise WorkerError(code, "유료 응답 형식을 확인하지 못해 중단했습니다. 비용 예약과 성공 구간을 보존하며 자동 재호출하지 않습니다.", "needs_review") from None
    billed = None
    if usage.get("type") == "duration" and _number(usage.get("seconds")):
        billed = usage["seconds"] / 60 * policy["price"]
    budget.settle(attempt_id, state="responded", charged_upper_bound_usd=billed, usage=usage)
    entry.update(state="complete", segments=parsed, usage=usage)
    atomic_json(state_path, cached)
    return parsed, usage


def _send_clip(source, index, interval, cached, state_path, policy, budget, circuit, identity, keywords, stats=None):
    """Encode, durably record intent, and send one planned clip; None for proven digital silence."""
    encoded, silent = _encoded_clip(source, interval)
    entry = {**interval, "encoded_bytes": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest()}
    if silent:
        entry.update(state="complete", segments=[], digital_silence=True)
        cached["clips"].append(entry)
        atomic_json(state_path, cached)
        return None
    seconds = interval["end"] - interval["start"]
    reserved = math.ceil(seconds / policy["rounding"]) * policy["rounding"] / 60 * policy["price"]
    attempt_id = str(uuid.uuid4())
    body, content_type = _multipart(encoded, policy, index, keywords)
    if circuit:
        circuit.assert_open()
    budget.reserve(attempt_id, reserved, stage="transcribing", model_id=policy["model"],
                   details={"clip_index": index, "start_frame": interval["start_frame"], "end_frame": interval["end_frame"], "audio_sha256": identity["source_sha256"],
                            # Lets an operator tell whether a rejected request carried terms.
                            "keywords_count": len(keywords)})
    entry.update(attempt_id=attempt_id, state="inflight", reserved_upper_bound_usd=reserved)
    cached["clips"].append(entry)
    atomic_json(state_path, cached)
    with circuit.request(attempt_id) if circuit else nullcontext():
        return _submit_clip(body, content_type, policy, budget, attempt_id, entry, cached, state_path, interval, circuit, stats)


def new_stats() -> dict:
    """Numbers only: request count and seconds spent waiting on the provider."""
    return {"requests": 0, "post_seconds": [], "started": time.monotonic()}


def write_stats(directory: Path, name: str, stats: dict, **extra) -> None:
    """Merge this run's timing into `speech-timing.json`. No text, no audio, no identifiers.

    Counts cover this run only (a resumed job starts again from zero), and only responses that
    came back. Diagnostics never fail a job, so any problem here is ignored.
    """
    try:
        posts = sorted(stats["post_seconds"])
        total = time.monotonic() - stats["started"]
        quantile = lambda q: round(posts[min(len(posts) - 1, int(q * len(posts)))], 3) if posts else None
        record = read_json(directory / "speech-timing.json", {})
        if not isinstance(record, dict):
            record = {}
        record[name] = {"wall_seconds": round(total, 3), "requests": stats["requests"], "post_seconds_total": round(sum(posts), 3),
                        "post_p50": quantile(0.5), "post_p90": quantile(0.9), "other_seconds": round(total - sum(posts), 3), **extra}
        atomic_json(directory / "speech-timing.json", record)
    except Exception:
        pass


def transcribe(audio: Path, checkpoint: Path, config: dict, check=lambda: None, *, intervals: list[dict] | None = None, keywords=None,
               progress=lambda done, total: None) -> dict:
    policy = _policy(config)
    circuit = for_config(config)
    if circuit:
        circuit.assert_open()
    rate, frames = _wave_info(audio)
    plan = _plan(intervals, frames, rate, policy["seconds"])
    directory = checkpoint.parent
    _unresolved(directory)
    budget = JobBudget(directory, policy["cap"])
    state_path = checkpoint.with_name(checkpoint.stem + "-api.json")
    identity = {"source_sha256": sha256(audio), "model": policy["model"], "language": policy["language"]}
    plan, cached = _resume_plan(audio, state_path, plan, policy, rate, identity,
                                clean_keywords(keywords) if policy["model"] == "gpt-transcribe" else [])
    # The list is frozen once any clip has paid text, so one job never mixes two
    # lists and a list change is never a reason to re-send a paid clip. Before
    # that (fresh job, or only rejected clips, e.g. a provider 400 caused by the
    # list) the current list is taken, so emptying a bad list and retrying works.
    # A checkpoint from before this field existed that already has paid text
    # keeps sending none.
    paid = any(entry.get("state") == "complete" and not entry.get("digital_silence") for entry in cached["clips"])
    if not paid and policy["model"] == "gpt-transcribe":
        current = clean_keywords(keywords)
        if cached.get("keywords") != current:
            cached["keywords"] = current
            atomic_json(state_path, cached)
    keywords = clean_keywords(cached.get("keywords")) if policy["model"] == "gpt-transcribe" else []
    all_segments, usage_items = [], []
    stats = new_stats()
    with wave.open(str(audio), "rb") as source:
        for index, interval in enumerate(plan):
            check()
            progress(index, len(plan))
            if index < len(cached["clips"]):
                entry = cached["clips"][index]
                if entry.get("state") == "complete":
                    all_segments.extend(entry["segments"])
                    if "usage" in entry:
                        usage_items.append(entry["usage"])
                    continue
                if entry.get("state") == "response_invalid":
                    raise WorkerError(entry.get("error_code", "api_speech_response_invalid"), "이미 받은 음성 API 응답을 로컬에서 확인하세요. 같은 요청을 자동 재호출하지 않습니다.", "needs_review")
                if entry.get("retry_at", 0) > time.time():
                    raise WorkerError("api_speech_rate_limit", "제공자가 지정한 재시도 시각까지 기다리세요.", "rate_limited", True)
                cached["clips"] = cached["clips"][:index]
            sent = _send_clip(source, index, interval, cached, state_path, policy, budget, circuit, identity, keywords, stats)
            if sent is None:
                continue
            parsed, usage = sent
            all_segments.extend(parsed)
            usage_items.append(usage)
            check()
    progress(len(plan), len(plan))
    cached.update(complete=True, duration=frames / rate)
    atomic_json(state_path, cached)
    result = {"segments": all_segments, "duration": frames / rate,
              "mode": "api/openai/" + policy["model"] + ("/provider-timestamps" if policy["model"] == "whisper-1" else "/audio-clip-bounds"),
              "clip_level": policy["model"] != "whisper-1", "coverage_complete": True,
              "coverage": [{key: value for key, value in clip.items() if key in {"start", "end", "state", "digital_silence", "local_speaker_id", "overlap", "mixed_speakers"}} for clip in cached["clips"]],
              "usage": {"provider": "openai", "model_id": policy["model"], "chunks": usage_items,
                        "pricing": "configured_duration_rate", "clip_count": len(cached["clips"]),
                        "keywords_count": len(keywords),
                        "cost_upper_bound_usd": sum(float(entry["charged_upper_bound_usd"]) for entry in read_json(directory / "api-budget.json", {"attempts": []})["attempts"] if entry.get("stage") == "transcribing")}}
    atomic_json(checkpoint, result)
    write_stats(directory, "transcribing", stats, clips=len(plan))
    return result
