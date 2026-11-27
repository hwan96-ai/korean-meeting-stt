from __future__ import annotations

from array import array
import datetime as dt
import io
import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch, Mock
from urllib import error
import wave

from local_worker import api_speech
from local_worker.budget import JobBudget
from local_worker.core import WorkerError, read_json


def write_audio(path: Path, seconds=3, silent=False):
    samples = array("h", [0] * round(seconds * 16000) if silent else (int(5000 * math.sin(2 * math.pi * 233 * n / 16000)) for n in range(round(seconds * 16000))))
    with wave.open(str(path), "wb") as w:
        w.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        w.writeframes(samples.tobytes())


def reply(text="검증 텍스트", usage=None):
    return json.dumps({"text": text, **({"usage": usage} if usage else {})}, ensure_ascii=False).encode(), "req_synthetic_test"


class ApiSpeechTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.audio, self.checkpoint = self.root / "source.wav", self.root / "asr.json"
        write_audio(self.audio)
        self.config = {"openai_api_key": "synthetic-test-key", "api_speech_model": "gpt-transcribe",
                       "api_speech_price_per_minute": 0.01, "api_max_cost_usd": 1,
                       "api_speech_chunk_seconds": 1, "api_speech_billing_rounding_seconds": 60}
        self.env = patch.dict("os.environ", {}, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def run_api(self, **kwargs):
        return api_speech.transcribe(self.audio, self.checkpoint, self.config, **kwargs)

    def test_missing_policy_never_calls_api(self):
        for key in ["openai_api_key", "api_speech_model", "api_speech_price_per_minute", "api_max_cost_usd"]:
            config = {k: v for k, v in self.config.items() if k != key}
            with patch.object(api_speech, "_post") as post, self.assertRaises(WorkerError) as ctx:
                api_speech.transcribe(self.audio, self.checkpoint, config)
            self.assertEqual(ctx.exception.state, "needs_setup")
            post.assert_not_called()
        self.assertFalse((self.root / "api-budget.json").exists())

    def test_silent_and_corrupt_audio_are_rejected_before_api(self):
        for corrupted in [False, True]:
            if corrupted:
                self.audio.write_bytes(b"not an audio file")
            else:
                write_audio(self.audio, silent=True)
            with patch.object(api_speech, "_post") as post, self.assertRaises(WorkerError):
                self.run_api()
            post.assert_not_called()
        self.assertFalse((self.root / "api-budget.json").exists())

    def test_real_clip_bounds_and_unknown_gap_are_not_fabricated_word_times(self):
        intervals = [{"start": 0, "end": 1, "local_speaker_id": "speaker_00", "overlap": False},
                     {"start": 1, "end": 2, "local_speaker_id": "unknown", "overlap": False},
                     {"start": 2, "end": 3, "local_speaker_id": "speaker_01", "overlap": True}]
        captured = []
        def fake(body, content_type, key, timeout):
            captured.append(body)
            self.assertEqual(key, "synthetic-test-key")
            self.assertIn(b'name="languages[]"', body)
            self.assertNotIn(b'name="language"', body)
            self.assertNotIn(str(self.audio).encode(), body)
            at = body.index(b"RIFF")
            size = int.from_bytes(body[at + 4:at + 8], "little") + 8
            with wave.open(io.BytesIO(body[at:at + size]), "rb") as clip:
                self.assertEqual(clip.getnframes(), 16000)
            return reply("동일한 말. 두 번째 문장.", {"type": "duration", "seconds": 1})
        with patch.object(api_speech, "_post", side_effect=fake):
            output = self.run_api(intervals=intervals)
            again = self.run_api(intervals=intervals)
        self.assertEqual(len(captured), 3)
        self.assertEqual(output, again)
        self.assertEqual([(s["start"], s["end"]) for s in output["segments"]], [(0, 1), (1, 2), (2, 3)])
        self.assertEqual([s["local_speaker_id"] for s in output["segments"]], ["speaker_00", "unknown", "unknown"])
        self.assertTrue(all(s["words"] == [] and s["timing_provenance"] == "audio_clip_bounds" for s in output["segments"]))
        self.assertTrue(output["coverage_complete"])
        self.assertEqual(len(output["coverage"]), 3)
        self.assertEqual(read_json(self.root / "asr-api.json")["clips"][0]["request_id"], "req_synthetic_test")

    def test_missing_or_overlapping_coverage_stops_before_api(self):
        for intervals in [[{"start": 1, "end": 3}], [{"start": 0, "end": 2}, {"start": 1, "end": 3}], [{"start": 0, "end": 2}]]:
            with patch.object(api_speech, "_post") as post, self.assertRaises(WorkerError) as ctx:
                self.run_api(intervals=intervals)
            self.assertEqual(ctx.exception.code, "api_speech_coverage")
            post.assert_not_called()

    def test_encoded_file_limit_splits_real_frames_and_keeps_coverage(self):
        self.config["api_speech_chunk_seconds"] = 600
        sizes, counts = [], []
        def fake(body, *_):
            at = body.index(b"RIFF")
            size = int.from_bytes(body[at + 4:at + 8], "little") + 8
            sizes.append(size)
            with wave.open(io.BytesIO(body[at:at + size]), "rb") as clip:
                counts.append(clip.getnframes())
            return reply()
        with patch.object(api_speech, "MAX_FILE_BYTES", 64000), patch.object(api_speech, "_post", side_effect=fake):
            output = self.run_api()
        self.assertTrue(all(size < 64000 for size in sizes))
        self.assertEqual(sum(counts), 48000)
        self.assertEqual(output["segments"][-1]["end"], 3)

    def test_speech_and_text_share_cumulative_budget_before_network(self):
        JobBudget(self.root, 0.015).reserve("prior-text", 0.01, stage="generating", model_id="test-text")
        JobBudget(self.root, 0.015).settle("prior-text", state="responded")
        self.config["api_max_cost_usd"] = 0.015
        with patch.object(api_speech, "_post") as post, self.assertRaises(WorkerError) as ctx:
            self.run_api()
        self.assertEqual(ctx.exception.code, "api_cumulative_cost_limit")
        post.assert_not_called()

    def test_cap_midway_resume_does_not_repeat_successful_paid_clip(self):
        self.config["api_max_cost_usd"] = 0.015
        with patch.object(api_speech, "_post", return_value=reply()) as post:
            with self.assertRaises(WorkerError):
                self.run_api()
            self.assertEqual(post.call_count, 1)
            self.config["api_max_cost_usd"] = 0.04
            output = self.run_api()
            self.assertEqual(post.call_count, 3)
        self.assertEqual(len(output["segments"]), 3)
        self.assertAlmostEqual(sum(e["charged_upper_bound_usd"] for e in read_json(self.root / "api-budget.json")["attempts"]), 0.03)

    def test_unknown_outcome_blocks_checkpoint_model_and_plan_changes(self):
        with patch.object(api_speech, "_post", side_effect=TimeoutError()) as post:
            with self.assertRaises(WorkerError) as ctx:
                self.run_api()
            self.assertEqual(ctx.exception.state, "unknown_outcome")
            self.config.update(api_speech_model="whisper-1", api_speech_allow_deprecated=True)
            with patch.object(api_speech, "_today", return_value=dt.date(2026, 10, 28)):
                with self.assertRaises(WorkerError) as again:
                    api_speech.transcribe(self.audio, self.root / "other.json", self.config)
            self.assertEqual(again.exception.state, "unknown_outcome")
            self.assertEqual(post.call_count, 1)
        entry = read_json(self.root / "api-budget.json")["attempts"][0]
        self.assertEqual(entry["charged_upper_bound_usd"], 0.01)
        self.assertTrue(api_speech.resolve_no_result(self.root, entry["attempt_id"], 1))
        self.assertEqual(read_json(self.root / "api-budget.json")["attempts"][0]["charged_upper_bound_usd"], 0.01)

    def test_definitive_rejection_releases_reservation(self):
        rejection = error.HTTPError(api_speech.ENDPOINT, 401, "unauthorized", {}, None)
        with patch.object(api_speech, "_post", side_effect=rejection), self.assertRaises(WorkerError) as ctx:
            self.run_api()
        self.assertEqual(ctx.exception.state, "needs_setup")
        self.assertEqual(read_json(self.root / "api-budget.json")["attempts"][0]["charged_upper_bound_usd"], 0)

    def test_retry_after_is_honored_without_paid_retry(self):
        rejection = error.HTTPError(api_speech.ENDPOINT, 429, "rate", {"Retry-After": "60"}, None)
        with patch.object(api_speech, "_post", side_effect=rejection) as post:
            for _ in range(2):
                with self.assertRaises(WorkerError) as ctx:
                    self.run_api()
                self.assertEqual(ctx.exception.state, "rate_limited")
            self.assertEqual(post.call_count, 1)

    def test_invalid_paid_response_is_saved_charged_and_not_blindly_repeated(self):
        with patch.object(api_speech, "_post", return_value=(b'{"bad":"response"}', "rid")) as post:
            for _ in range(2):
                with self.assertRaises(WorkerError) as ctx:
                    self.run_api()
                self.assertEqual(ctx.exception.state, "needs_review")
            self.assertEqual(post.call_count, 1)
        clip = read_json(self.root / "asr-api.json")["clips"][0]
        self.assertTrue(clip["response_received"])
        self.assertNotIn("response_body", clip)
        self.assertEqual(read_json(self.root / "api-budget.json")["attempts"][0]["charged_upper_bound_usd"], 0.01)

    def test_legacy_word_timestamps_have_global_offsets_and_expiry_gate(self):
        self.config.update(api_speech_model="whisper-1", api_speech_allow_deprecated=True)
        response = {"text": "검증", "segments": [{"start": 0.1, "end": 0.9, "text": "검증"}],
                    "words": [{"start": 0.1, "end": 0.9, "word": "검증"}], "usage": {"type": "duration", "seconds": 1}}
        with patch.object(api_speech, "_today", return_value=dt.date(2026, 10, 28)), patch.object(api_speech, "_post", return_value=(json.dumps(response).encode(), "id")):
            output = self.run_api()
        self.assertEqual(output["segments"][1]["start"], 1.1)
        self.assertEqual(output["segments"][2]["words"][0]["start"], 2.1)
        self.assertFalse(output["clip_level"])
        with patch.object(api_speech, "_today", return_value=dt.date(2027, 2, 26)), patch.object(api_speech, "_post") as post, self.assertRaises(WorkerError):
            self.run_api()
        post.assert_not_called()

    def test_missing_legacy_timestamps_are_not_replaced_with_fake_times(self):
        self.config.update(api_speech_model="whisper-1", api_speech_allow_deprecated=True)
        with patch.object(api_speech, "_today", return_value=dt.date(2026, 10, 28)), patch.object(api_speech, "_post", return_value=reply("시간 없는 텍스트")), self.assertRaises(WorkerError) as ctx:
            self.run_api()
        self.assertEqual(ctx.exception.code, "api_speech_timestamps_missing")
        self.assertEqual(read_json(self.root / "api-budget.json")["attempts"][0]["charged_upper_bound_usd"], 0.01)

    def keyword_fields(self, body):
        marker = b'name="keywords[]"\r\n\r\n'
        return [part.split(b"\r\n", 1)[0].decode() for part in body.split(marker)[1:]]

    def test_keywords_are_validated_app_side_before_any_form_field(self):
        clean = api_speech.clean_keywords
        self.assertEqual(clean(["  회의록 ", "STT", "stt", "화자   분리", "", 7, None, "a<b", "x>y", "줄\n바꿈", "제어\x00문자", "숨김​문자", "가" * 41]),
                         ["회의록", "STT", "화자 분리", "줄 바꿈"])
        for value in [None, "회의록", {"회의록": 1}, ("회의록",)]:
            self.assertEqual(clean(value), [])
        # Decomposed Hangul from another OS is sent composed.
        self.assertEqual(clean(["회의록"]), ["회의록"])
        many = clean([f"용어{n}" for n in range(500)])
        self.assertEqual(len(many), api_speech.MAX_KEYWORDS)
        long = clean([f"{n:03d}" + "가" * 37 for n in range(100)])
        self.assertLessEqual(sum(map(len, long)), api_speech.MAX_KEYWORDS_TOTAL_CHARS)
        self.assertEqual(len(long), 37)
        # A term that overflows the total is skipped; a shorter later one still fits.
        self.assertEqual(clean([f"{n:03d}" + "가" * 37 for n in range(37)] + ["가" * 40, "짧은"])[-1], "짧은")

    def test_keywords_are_sent_as_repeated_fields_and_not_echoed(self):
        captured = []
        def fake(body, *_):
            captured.append(body)
            return reply()
        with patch.object(api_speech, "_post", side_effect=fake):
            output = self.run_api(keywords=["회의록", "premium plan", "a<b", "제어\x00문자"])
        self.assertEqual(len(captured), 3)
        for body in captured:
            self.assertEqual(self.keyword_fields(body), ["회의록", "premium plan"])
            self.assertEqual(body.count(b'name="model"'), 1)
            self.assertIn(b'name="languages[]"', body)
        self.assertEqual(output["usage"]["keywords_count"], 2)
        self.assertNotIn("회의록", json.dumps(output, ensure_ascii=False))
        self.assertEqual(read_json(self.root / "asr-api.json")["keywords"], ["회의록", "premium plan"])

    def test_keyword_line_breaks_cannot_add_form_fields(self):
        injected = 'x\r\n--boundary\r\nContent-Disposition: form-data; name="model"\r\n\r\nother'
        with patch.object(api_speech, "_post", return_value=reply()) as post:
            self.run_api(keywords=[injected[:40], "회의록\r\nname=\"prompt\""])
        for call in post.call_args_list:
            body = call.args[0]
            head = body[:body.index(b"RIFF")]
            # model, response_format, languages[], two keywords[] and the file: nothing else.
            self.assertEqual(head.count(b"\r\nContent-Disposition: form-data; name="), 6)
            self.assertEqual(head.count(b'Content-Disposition: form-data; name="model"\r\n\r\n'), 1)
            self.assertNotIn(b'Content-Disposition: form-data; name="prompt"', head)
            self.assertEqual(len(self.keyword_fields(body)), 2)
            for term in self.keyword_fields(body):
                self.assertNotIn("\r", term)
                self.assertNotIn("\n", term)

    def test_no_keywords_keeps_the_request_unchanged(self):
        for value in (None, [], ["<>"]):
            with self.subTest(value=value), TemporaryDirectory() as other:
                audio = Path(other) / "source.wav"
                write_audio(audio)
                with patch.object(api_speech, "_post", return_value=reply()) as post:
                    output = api_speech.transcribe(audio, Path(other) / "asr.json", self.config, keywords=value)
                self.assertEqual(post.call_count, 3)
                self.assertTrue(all(b"keywords" not in call.args[0] for call in post.call_args_list))
                self.assertEqual(output["usage"]["keywords_count"], 0)

    def test_keyword_list_is_frozen_for_a_resumed_job_and_never_repays(self):
        self.config["api_max_cost_usd"] = 0.015
        with patch.object(api_speech, "_post", return_value=reply()) as post:
            with self.assertRaises(WorkerError):
                self.run_api(keywords=["회의록"])
            self.config["api_max_cost_usd"] = 0.04
            self.run_api(keywords=["예시제품", "프록시"])
            self.assertEqual(post.call_count, 3)
            self.assertEqual([self.keyword_fields(call.args[0]) for call in post.call_args_list], [["회의록"]] * 3)
            # A finished job replays its paid text; a new list is not a reason to pay again.
            again = self.run_api(keywords=["체크섬"])
            self.assertEqual(post.call_count, 3)
        self.assertEqual(again["usage"]["keywords_count"], 1)
        self.assertEqual(read_json(self.root / "asr-api.json")["keywords"], ["회의록"])

    def test_rejected_first_clip_takes_the_current_list_on_retry(self):
        # A provider 400 on the first clip may be caused by the list itself. Until a
        # clip has paid text the list is not frozen, so emptying it and retrying works.
        rejection = error.HTTPError(api_speech.ENDPOINT, 400, "bad request", {}, io.BytesIO(b"{}"))
        with patch.object(api_speech, "_post", side_effect=rejection) as post, self.assertRaises(WorkerError):
            self.run_api(keywords=["회의록"])
        self.assertEqual(self.keyword_fields(post.call_args.args[0]), ["회의록"])
        self.assertEqual(read_json(self.root / "api-budget.json")["attempts"][0]["details"]["keywords_count"], 1)
        with patch.object(api_speech, "_post", return_value=reply()) as post:
            output = self.run_api(keywords=[])
        self.assertEqual(post.call_count, 3)
        self.assertTrue(all(b"keywords" not in call.args[0] for call in post.call_args_list))
        self.assertEqual(output["usage"]["keywords_count"], 0)
        self.assertEqual(read_json(self.root / "asr-api.json")["keywords"], [])

    def test_checkpoint_from_before_keywords_resumes_without_them(self):
        self.config["api_max_cost_usd"] = 0.015
        state = self.root / "asr-api.json"
        with patch.object(api_speech, "_post", return_value=reply()) as post:
            with self.assertRaises(WorkerError):
                self.run_api()
            legacy = read_json(state)
            del legacy["keywords"]
            state.write_text(json.dumps(legacy), encoding="utf-8")
            self.config["api_max_cost_usd"] = 0.04
            output = self.run_api(keywords=["회의록"])
            self.assertEqual(post.call_count, 3)
            self.assertTrue(all(b"keywords" not in call.args[0] for call in post.call_args_list))
        self.assertEqual(len(output["segments"]), 3)

    def test_legacy_model_never_receives_keywords(self):
        self.config.update(api_speech_model="whisper-1", api_speech_allow_deprecated=True)
        response = {"text": "검증", "segments": [{"start": 0.1, "end": 0.9, "text": "검증"}]}
        with patch.object(api_speech, "_today", return_value=dt.date(2026, 10, 28)), patch.object(api_speech, "_post", return_value=(json.dumps(response).encode(), "id")) as post:
            output = self.run_api(keywords=["회의록"])
        self.assertTrue(all(b"keywords" not in call.args[0] for call in post.call_args_list))
        self.assertEqual(output["usage"]["keywords_count"], 0)
        self.assertEqual(read_json(self.root / "asr-api.json")["keywords"], [])

    def test_transport_uses_fixed_origin_and_no_redirect_handler(self):
        opener = Mock()
        opener.open.side_effect = error.HTTPError(api_speech.ENDPOINT, 302, "redirect", {"Location": "https://example.invalid"}, None)
        with patch.object(api_speech.request, "build_opener", return_value=opener) as make, self.assertRaises(error.HTTPError):
            api_speech._post(b"file", "multipart/form-data; boundary=test", "synthetic-test-key", 30)
        self.assertIsInstance(make.call_args.args[0], api_speech.NoRedirect)
        self.assertEqual(opener.open.call_args.args[0].full_url, api_speech.ENDPOINT)
        self.assertEqual(opener.open.call_count, 1)


if __name__ == "__main__":
    unittest.main()


class ApiSpeechProgressTests(unittest.TestCase):
    setUp, tearDown, run_api = ApiSpeechTests.setUp, ApiSpeechTests.tearDown, ApiSpeechTests.run_api

    def test_progress_counts_every_clip_and_resume_starts_from_the_checkpoint(self):
        seen = []
        self.config["api_max_cost_usd"] = 0.015
        with patch.object(api_speech, "_post", return_value=reply()):
            with self.assertRaises(WorkerError):
                self.run_api(progress=lambda done, total: seen.append((done, total)))
            self.assertEqual(seen, [(0, 3), (1, 3)])
            seen.clear()
            self.config["api_max_cost_usd"] = 0.04
            self.run_api(progress=lambda done, total: seen.append((done, total)))
        self.assertEqual(seen, [(0, 3), (1, 3), (2, 3), (3, 3)])
