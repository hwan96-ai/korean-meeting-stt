from __future__ import annotations

import copy
import hashlib
import io
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid
import wave

from local_worker import api_speech as api
from local_worker.budget import JobBudget
from local_worker.core import WorkerError, atomic_json, read_json, sha256
from local_worker.speech import align
from test_api_speech import write_audio, reply


class ShortAudioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.audio, self.checkpoint = self.root / "source.wav", self.root / "asr.json"
        self.config = {"openai_api_key": "synthetic-only", "api_speech_model": "gpt-transcribe",
            "api_speech_price_per_minute": .0045, "api_max_cost_usd": 2,
            "api_speech_chunk_seconds": 30, "api_speech_billing_rounding_seconds": 1}

    def legacy(self, boundaries, complete):
        write_audio(self.audio, seconds=boundaries[-1])
        intervals = [{"start": start, "end": end, "local_speaker_id": "A" if index % 2 == 0 else "B", "overlap": False}
                     for index, (start, end) in enumerate(zip(boundaries, boundaries[1:]))]
        frames = round(boundaries[-1] * 16000)
        plan = api._plan(intervals, frames, 16000, 30)
        identity = {"source_sha256": sha256(self.audio), "model": "gpt-transcribe", "language": "ko", "intervals_sha256": api._plan_hash(plan)}
        cached = {"identity": identity, "clips": [], "complete": False}
        budget = JobBudget(self.root, 2)
        with wave.open(str(self.audio), "rb") as source:
            for index in range(complete + 1):
                interval = plan[index]
                encoded, _ = api._encoded_clip(source, interval)
                reserved = math.ceil(interval["end"] - interval["start"]) / 60 * .0045
                attempt = str(uuid.uuid4())
                budget.reserve(attempt, reserved, stage="transcribing", model_id="gpt-transcribe", details={"clip_index": index, "start_frame": interval["start_frame"], "end_frame": interval["end_frame"], "audio_sha256": identity["source_sha256"]})
                entry = {**interval, "encoded_bytes": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest(), "attempt_id": attempt, "reserved_upper_bound_usd": reserved}
                if index < complete:
                    entry.update(state="complete", usage={}, segments=api._parsed_segments({"text": f"성공 구간 {index}"}, interval, "gpt-transcribe"))
                    budget.settle(attempt, state="responded")
                else:
                    entry.update(state="rejected", http_status=400, error_code="api_speech_configuration")
                    budget.settle(attempt, state="needs_setup", charged_upper_bound_usd=0, error_code="api_speech_configuration")
                cached["clips"].append(entry)
        atomic_json(self.root / "asr-api.json", cached)
        return intervals, copy.deepcopy(cached), read_json(self.root / "api-budget.json")

    def test_actual_19_prefix_geometry_is_frozen_and_only_tail_frames_are_submitted(self):
        boundaries = [0, .5709375, 1.229125, 3.03475, 4.266625, 4.772875, 4.9585, 6.3085, 6.764125, 9.801625, 10.949125, 22.964125, 23.8585, 32.46475, 32.7853125, 38.0335, 38.60725, 47.635375, 48.25975, 55.195375, 55.246, 60, 70, 85, 106.112]
        intervals, cached, ledger = self.legacy(boundaries, 19)
        counts = []
        def post(body, *_):
            offset = body.index(b"RIFF")
            length = int.from_bytes(body[offset + 4:offset + 8], "little") + 8
            with wave.open(io.BytesIO(body[offset:offset + length]), "rb") as clip:
                counts.append(clip.getnframes())
            return reply()
        with patch.object(api, "_post", side_effect=post):
            result = api.transcribe(self.audio, self.checkpoint, self.config, intervals=intervals)
        after = read_json(self.root / "asr-api.json")
        self.assertEqual(after["clips"][:19], cached["clips"][:19])
        self.assertEqual(after["retired_clips"], cached["clips"][19:])
        self.assertEqual(read_json(self.root / "api-budget.json")["attempts"][:20], ledger["attempts"])
        self.assertEqual(sum(counts), round((106.112 - 55.195375) * 16000))
        self.assertTrue(all(.5 <= count / 16000 <= 30 for count in counts))
        self.assertTrue(result["coverage_complete"])
        with patch.object(api, "_post") as post:
            self.assertEqual(api.transcribe(self.audio, self.checkpoint, self.config, intervals=intervals), result)
        post.assert_not_called()

    def test_source_model_language_hash_frame_ledger_and_unknown_damage_never_replay(self):
        intervals, cached, ledger = self.legacy([0, 1, 1.05, 3], 1)
        original_audio = self.audio.read_bytes()
        for damage in ("source", "model", "language", "hash", "frame", "ledger", "unknown", "invalid", "corrupt"):
            self.audio.write_bytes(original_audio)
            value, budget, config = copy.deepcopy(cached), copy.deepcopy(ledger), dict(self.config)
            if damage == "source":
                raw = bytearray(original_audio); raw[-1] ^= 1; self.audio.write_bytes(raw)
            elif damage == "model":
                config.update(api_speech_model="whisper-1", api_speech_allow_deprecated=True)
            elif damage == "language":
                config["api_speech_language"] = "en"
            elif damage == "hash":
                value["clips"][0]["sha256"] = "0" * 64
            elif damage == "frame":
                value["clips"][0]["end_frame"] -= 1
            elif damage == "ledger":
                budget["attempts"] = []
            elif damage == "unknown":
                value["clips"][0]["state"] = "unknown_outcome"
            elif damage == "invalid":
                value["clips"][0]["state"] = "response_invalid"
            atomic_json(self.root / "asr-api.json", value)
            atomic_json(self.root / "api-budget.json", budget)
            if damage == "corrupt":
                (self.root / "asr-api.json").write_text("invalid-json", encoding="utf-8")
            with self.subTest(damage=damage), patch.object(api, "_post") as post, self.assertRaises(WorkerError):
                api.transcribe(self.audio, self.checkpoint, config, intervals=intervals)
            post.assert_not_called()

    def test_short_unpaid_end_never_reposts_a_paid_prefix_or_claims_completion(self):
        intervals, cached, ledger = self.legacy([0, .85, .9], 1)
        with patch.object(api, "_post") as post, self.assertRaises(WorkerError) as failure:
            api.transcribe(self.audio, self.checkpoint, self.config, intervals=intervals)
        post.assert_not_called()
        self.assertEqual(failure.exception.code, "api_speech_tail_too_short")
        self.assertEqual(failure.exception.state, "needs_review")
        self.assertEqual(read_json(self.root / "asr-api.json"), cached)
        self.assertEqual(read_json(self.root / "api-budget.json"), ledger)
        self.assertFalse(self.checkpoint.exists())

    def test_whole_audio_below_engineering_floor_is_not_sent(self):
        write_audio(self.audio, seconds=.05)
        with patch.object(api, "_post") as post, self.assertRaises(WorkerError) as failure:
            api.transcribe(self.audio, self.checkpoint, self.config)
        post.assert_not_called()
        self.assertEqual(failure.exception.state, "needs_review")
        self.assertFalse((self.root / "api-budget.json").exists())

    def test_rebalance_respects_maximum_and_minimum_at_end(self):
        plan = api._plan(None, round(30.1 * 16000), 16000, 30)
        result = api._coalesce_tail(plan, 16000, 30)
        self.assertEqual([(entry["start"], entry["end"]) for entry in result], [(0, 29.6), (29.6, 30.1)])
        self.assertEqual(sum(entry["end_frame"] - entry["start_frame"] for entry in result), round(30.1 * 16000))

    def test_sequential_mixed_speaker_unknown_survives_api_to_align_without_false_overlap(self):
        write_audio(self.audio, seconds=1)
        intervals = [{"start": 0, "end": .05, "local_speaker_id": "A"}, {"start": .05, "end": 1, "local_speaker_id": "B"}]
        with patch.object(api, "_post", return_value=reply()) as post:
            result = api.transcribe(self.audio, self.checkpoint, self.config, intervals=intervals)
        self.assertEqual(post.call_count, 1)
        segment = result["segments"][0]
        self.assertEqual(segment["local_speaker_id"], "unknown")
        self.assertTrue(segment["mixed_speakers"])
        self.assertFalse(segment["overlap"])
        aligned = align(result["segments"], [{"start": 0, "end": .05, "speaker": "A"}, {"start": .05, "end": 1, "speaker": "B"}], 1)
        self.assertEqual(aligned[0]["local_speaker_id"], "unknown")
        self.assertFalse(aligned[0]["overlap"])
        self.assertEqual(aligned[0]["review_status"], "uncertain")

    def test_missing_checkpoint_with_paid_ledger_does_not_restart_at_zero(self):
        intervals, _, _ = self.legacy([0, 1, 1.05, 3], 1)
        (self.root / "asr-api.json").unlink()
        with patch.object(api, "_post") as post, self.assertRaises(WorkerError):
            api.transcribe(self.audio, self.checkpoint, self.config, intervals=intervals)
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
