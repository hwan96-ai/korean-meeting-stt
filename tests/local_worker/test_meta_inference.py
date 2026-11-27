from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib import error
import uuid

from local_worker import meta_inference, api_speech
from local_worker.api_circuit import ApiCircuit
from local_worker.core import defaults, read_json, atomic_json
from local_worker.runner import Worker

KEY = "synthetic-secret-key"


def segment(text, speaker="speaker_000", name=""):
    return {"segment_id": str(uuid.uuid4()), "start_ms": 0, "end_ms": 1000, "text": text, "local_speaker_id": speaker,
            "speaker_name": name, "overlap": False}


def answer(output, usage=None, status="completed"):
    return json.dumps({"status": status, "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(output, ensure_ascii=False)}]}],
                       "usage": usage if usage is not None else {"input_tokens": 1000, "output_tokens": 200}}, ensure_ascii=False).encode()


EMPTY = {"value": None, "segment": 0, "quote": ""}


def model_output(**values):
    return {"organization": EMPTY, "place": EMPTY, "meeting_format": EMPTY, "attendee_candidates": [], **values}


def rejection(status=429, code=None):
    body = json.dumps({"error": {"code": code, "message": "private-provider-detail"}}).encode()
    return error.HTTPError(meta_inference.ENDPOINT, status, "private-provider-detail", {}, io.BytesIO(body))


class MetaInferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / "workspace" / "jobs" / "job-a"
        self.directory.mkdir(parents=True)
        self.config = {"execution_location": "server", "speech_mode": "api", "worker_home": str(self.root),
                       "openai_api_key": KEY, "api_max_cost_usd": 2, "meta_inference_mode": "api", "api_text_model": "text-model-x",
                       "api_text_prices_per_million": {"input": 1.0, "output": 4.0}, "api_text_max_output_tokens": 2000,
                       "api_meta_max_cost_usd": 0.5}
        self.segments = [segment("안녕하세요, 저는 한빛제조 김민수 팀장입니다."), segment("오늘은 판교 본사 3층 회의실에서 뵙네요.", "speaker_001"),
                         segment("김 부장님께도 전달드릴게요.", "speaker_001")]
        self.job = {"id": "job-a", "output_mode": "minutes"}

    def run_infer(self, transport, job=None, config=None, segments=None, directory=None):
        return meta_inference.infer(job or self.job, segments or self.segments, directory or self.directory, config or self.config, transport=transport)

    def good(self):
        return answer(model_output(
            organization={"value": "한빛제조", "segment": 1, "quote": "저는 한빛제조 김민수 팀장입니다."},
            place={"value": "판교 본사 3층 회의실", "segment": 2, "quote": "판교 본사 3층 회의실에서 뵙네요"},
            meeting_format={"value": "오프라인", "segment": 2, "quote": "회의실에서 뵙네요"},
            attendee_candidates=[{"name": "김민수", "role": "팀장", "company": "한빛제조", "segment": 1, "quote": "저는 한빛제조 김민수 팀장입니다."}]))

    def test_off_stt_and_pc_never_call_and_omit_the_field(self):
        transport = Mock()
        cases = [({**self.config, "meta_inference_mode": None}, self.job), ({**self.config, "execution_location": "pc"}, self.job),
                 (self.config, {**self.job, "output_mode": "transcript"}), ({**self.config, "api_text_prices_per_million": {"input": 1}}, self.job),
                 ({**self.config, "openai_api_key": None}, self.job)]
        for config, job in cases:
            with patch.dict("os.environ", {"OPENAI_API_KEY": "synthetic-env-key"}):
                self.assertIsNone(self.run_infer(transport, job, config))
        transport.assert_not_called()

    def test_valid_answer_keeps_quoted_items_and_records_cost(self):
        transport = Mock(return_value=self.good())
        result = self.run_infer(transport)
        self.assertEqual(result["status"], "ok")
        self.assertEqual({item["key"]: item["value"] for item in result["items"]},
                         {"organization": "한빛제조", "place": "판교 본사 3층 회의실", "meeting_format": "오프라인", "attendee": "김민수 팀장 (한빛제조)"})
        self.assertEqual(result["items"][0]["segment_ids"], [self.segments[0]["segment_id"]])
        body = transport.call_args.args[0].decode("utf-8")
        self.assertIn("한빛제조", body)  # Raw UTF-8, not \\u escapes.
        self.assertIn('"store": false', body)
        self.assertIn("<untrusted_meeting_data>", body)
        ledger = read_json(self.directory / "api-budget.json")
        entry = ledger["attempts"][0]
        self.assertEqual((entry["stage"], entry["state"]), ("meta_inference", "responded"))
        self.assertAlmostEqual(entry["charged_upper_bound_usd"], (1000 * 1 + 200 * 4) / 1_000_000)

    def test_completed_record_is_reused_without_a_second_charge(self):
        transport = Mock(return_value=self.good())
        first = self.run_infer(transport)
        second = self.run_infer(transport)
        self.assertEqual(first, second)
        self.assertEqual(transport.call_count, 1)

    def test_unquoted_or_out_of_range_values_are_dropped(self):
        bad = answer(model_output(
            organization={"value": "없는회사", "segment": 1, "quote": "저는 한빛제조 김민수 팀장입니다."},
            place={"value": "판교", "segment": 9, "quote": "판교"},
            meeting_format={"value": "전화", "segment": 2, "quote": "회의실에서 뵙네요"},
            attendee_candidates=[{"name": "김 부장", "role": "", "company": "", "segment": 3, "quote": "지어낸 문장"},
                                 {"name": "박서연", "role": "", "company": "", "segment": 1, "quote": "저는 한빛제조 김민수 팀장입니다."}]))
        result = self.run_infer(Mock(return_value=bad))
        self.assertEqual((result["status"], result["items"]), ("ok", []))

    def test_saved_speaker_names_win_over_model_candidates(self):
        segments = [segment("안녕하세요, 저는 한빛제조 김민수 팀장입니다.", name="김민수 팀장 (한빛제조)"), *self.segments[1:]]
        output = answer(model_output(attendee_candidates=[{"name": "김민수", "role": "팀장", "company": "", "segment": 1, "quote": "저는 한빛제조 김민수 팀장입니다."}]))
        result = self.run_infer(Mock(return_value=output), segments=segments)
        self.assertEqual(result["items"], [])

    def test_timeout_pauses_only_inference_and_never_repeats(self):
        result = self.run_infer(Mock(side_effect=TimeoutError()))
        self.assertEqual((result["status"], result["reason"]), ("failed", "meta_unknown_outcome"))
        self.assertFalse(ApiCircuit(self.root).status()["api_blocked"])
        self.assertTrue(ApiCircuit(self.root, "api-safety-text").status()["api_blocked"])
        transport = Mock(return_value=self.good())
        again = self.run_infer(transport)
        self.assertEqual(again["reason"], "meta_unknown_outcome")
        other = self.run_infer(transport, directory=self.root / "workspace" / "jobs" / "job-b")
        self.assertEqual(other["reason"], "meta_inference_blocked")
        transport.assert_not_called()

    def test_credit_error_trips_the_global_pause_and_ambiguous_429_only_the_text_pause(self):
        result = self.run_infer(Mock(side_effect=rejection(429, "insufficient_quota")))
        self.assertEqual(result["reason"], "meta_credit_blocked")
        self.assertEqual(ApiCircuit(self.root).status()["api_block_reason"], "insufficient_quota")
        self.assertFalse(ApiCircuit(self.root, "api-safety-text").status()["api_blocked"])
        home = self.root / "second"
        config = {**self.config, "worker_home": str(home)}
        result = self.run_infer(Mock(side_effect=rejection(429)), config=config, directory=home / "job")
        self.assertEqual(result["reason"], "meta_http_429")
        self.assertFalse(ApiCircuit(home).status()["api_blocked"])
        self.assertEqual(ApiCircuit(home, "api-safety-text").status()["api_block_reason"], "ambiguous_429")

    def test_blocked_global_pause_skips_without_a_call(self):
        ApiCircuit(self.root).trip("insufficient_quota")
        transport = Mock()
        self.assertEqual(self.run_infer(transport)["reason"], "api_blocked")
        transport.assert_not_called()

    def test_invalid_response_is_charged_paused_and_secret_free(self):
        # An "incomplete" answer is a job-only failure now (test_incomplete_answer_fails_only_this_job).
        for raw in [b"not json", answer(model_output(), status="failed"), json.dumps({"status": "completed", "echo": KEY}).encode()]:
            home = self.root / str(abs(hash(raw)))
            config = {**self.config, "worker_home": str(home)}
            result = self.run_infer(Mock(return_value=raw), config=config, directory=home / "job")
            self.assertEqual(result["reason"], "meta_response_invalid")
            self.assertEqual(ApiCircuit(home, "api-safety-text").status()["api_block_reason"], "response_invalid")
            for path in home.rglob("*.json"):
                content = path.read_text(encoding="utf-8")
                self.assertNotIn(KEY, content)
                self.assertNotIn("private-provider-detail", content)

    def test_small_cap_and_job_cap_skip_before_network(self):
        transport = Mock()
        self.assertEqual(self.run_infer(transport, config={**self.config, "api_meta_max_cost_usd": 0.000001})["reason"], "meta_cost_limit")
        atomic_json(self.directory / "api-budget.json", {"attempts": [{"attempt_id": "x", "stage": "transcribing", "state": "responded", "charged_upper_bound_usd": 1.9999}]})
        self.assertEqual(self.run_infer(transport)["reason"], "meta_cost_limit")
        transport.assert_not_called()

    def test_long_transcript_sends_head_and_tail_only(self):
        segments = [segment(f"{index}번째 문장입니다. " + "가" * 200) for index in range(600)]
        lines, truncated = meta_inference.transcript_lines(segments)
        self.assertTrue(truncated)
        self.assertEqual(lines[0][0], 1)
        self.assertEqual(lines[-1][0], 600)
        self.assertLessEqual(sum(len(line) + 1 for _, line in lines), meta_inference.INPUT_LIMIT)
        transport = Mock(return_value=answer(model_output(place={"value": "가가", "segment": 300, "quote": "가가가"})))
        result = meta_inference.infer(self.job, segments, self.directory, self.config, transport=transport)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["items"], [])  # Line 300 was never sent.

    def test_unresolved_inference_attempt_does_not_block_the_transcript(self):
        atomic_json(self.directory / "api-budget.json", {"attempts": [{"attempt_id": "x", "stage": "meta_inference", "state": "inflight", "charged_upper_bound_usd": 0.01}]})
        api_speech._unresolved(self.directory)  # No exception.
        atomic_json(self.directory / "api-budget.json", {"attempts": [{"attempt_id": "x", "stage": "transcribing", "state": "inflight", "charged_upper_bound_usd": 0.01}]})
        with self.assertRaises(Exception):
            api_speech._unresolved(self.directory)


class RunnerMetaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = defaults({"execution_location": "server", "speech_mode": "api", "worker_home": str(self.root), "workspace": str(self.root / "workspace"),
                                "openai_api_key": KEY, "api_speech_model": "gpt-transcribe", "api_credit_only_ack": True,
                                "api_speech_price_per_minute": .0045, "api_max_cost_usd": 2, "base_url": "http://127.0.0.1:1", "device_token": "x" * 40})

    def heartbeat_caps(self, config):
        client = Mock()
        client.call.return_value = {}
        Worker(config, client).heartbeat()
        return client.call.call_args.args[2]["capabilities"]

    def test_text_pause_is_reported_apart_and_does_not_stop_claims(self):
        self.assertEqual(self.heartbeat_caps(self.config)["meta_inference"], "off")
        config = {**self.config, "meta_inference_mode": "api", "api_text_model": "text-model-x",
                  "api_text_prices_per_million": {"input": 1.0, "output": 4.0}, "api_meta_max_cost_usd": 0.5}
        ApiCircuit(self.root, "api-safety-text").trip("ambiguous_429")
        caps = self.heartbeat_caps(config)
        self.assertEqual((caps["meta_inference"], caps["meta_inference_blocked"], caps["meta_inference_block_reason"]), ("api/text-model-x", True, "ambiguous_429"))
        self.assertFalse(caps["api_blocked"])
        client = Mock()
        client.call.return_value = {}
        Worker(config, client).run(once=True)
        self.assertIn("/api/worker/claim", [call.args[1] for call in client.call.call_args_list])

    def process(self, config, transport_result=None):
        client = Mock()
        client.result.side_effect = lambda job, **values: {"transcript_revision": 1, **values}
        worker = Worker(config, client)
        segments = [segment("저는 한빛제조 김민수 팀장입니다.")]
        audio_meta = {"duration": 5.0, "source_sha256": "x"}
        with patch("local_worker.runner.prepare_audio", return_value=audio_meta), \
             patch("local_worker.runner.diarize", return_value=[{"start": 0, "end": 5, "speaker": "speaker_000"}]), \
             patch("local_worker.runner.partition_acoustic_intervals", return_value=[]), \
             patch("local_worker.runner.align", return_value=segments), \
             patch("local_worker.api_speech.transcribe", return_value={"segments": [{"text": "x"}], "duration": 5.0, "mode": "api/openai/gpt-transcribe", "usage": {}}), \
             patch("local_worker.api_speech.is_ready", return_value=True), \
             patch.object(meta_inference, "post", return_value=transport_result or answer(model_output())) as post:
            worker.process({"id": str(uuid.uuid4()), "lease_id": "l", "generation": 1, "transcript_revision": 0, "byte_size": 1, "sha256": "x", "output_mode": "minutes"})
        final = [call.kwargs for call in client.result.call_args_list if call.kwargs.get("state") == "transcript_ready"]
        return final, post

    def test_off_result_has_no_field_and_on_result_carries_it(self):
        final, post = self.process(self.config)
        self.assertEqual(len(final), 1)
        self.assertNotIn("metadata_suggestions", final[0])
        post.assert_not_called()
        config = {**self.config, "meta_inference_mode": "api", "api_text_model": "text-model-x",
                  "api_text_prices_per_million": {"input": 1.0, "output": 4.0}, "api_meta_max_cost_usd": 0.5}
        final, post = self.process(config)
        self.assertEqual(final[0]["metadata_suggestions"]["status"], "ok")
        self.assertEqual(post.call_count, 1)

    def test_a_lost_lease_never_buys_a_text_model_call(self):
        from local_worker.core import WorkerError
        config = {**self.config, "meta_inference_mode": "api", "api_text_model": "text-model-x",
                  "api_text_prices_per_million": {"input": 1.0, "output": 4.0}, "api_meta_max_cost_usd": 0.5}
        client = Mock()
        client.result.side_effect = lambda job, **values: {"transcript_revision": 1, **values}
        worker = Worker(config, client)
        segments = [segment("저는 한빛제조 김민수 팀장입니다.")]
        def lose_lease(*args, **kwargs):
            worker.lease_failure = WorkerError("lease_lost", "lost", "cancelled")
            return segments
        with patch("local_worker.runner.prepare_audio", return_value={"duration": 5.0, "source_sha256": "x"}), \
             patch("local_worker.runner.diarize", return_value=[{"start": 0, "end": 5, "speaker": "speaker_000"}]), \
             patch("local_worker.runner.partition_acoustic_intervals", return_value=[]), \
             patch("local_worker.runner.align", side_effect=lose_lease), \
             patch("local_worker.api_speech.transcribe", return_value={"segments": [{"text": "x"}], "duration": 5.0, "mode": "api", "usage": {}}), \
             patch("local_worker.api_speech.is_ready", return_value=True), \
             patch.object(meta_inference, "post") as post:
            worker.process({"id": str(uuid.uuid4()), "lease_id": "l", "generation": 1, "transcript_revision": 0, "byte_size": 1, "sha256": "x", "output_mode": "minutes"})
        post.assert_not_called()
        self.assertNotIn("transcript_ready", [call.kwargs.get("state") for call in client.result.call_args_list])

    def test_inference_failure_still_delivers_the_transcript(self):
        config = {**self.config, "meta_inference_mode": "api", "api_text_model": "text-model-x",
                  "api_text_prices_per_million": {"input": 1.0, "output": 4.0}, "api_meta_max_cost_usd": 0.5}
        with patch.object(meta_inference, "_infer", side_effect=RuntimeError("boom")):
            final, _ = self.process(config)
        self.assertEqual(final[0]["metadata_suggestions"]["reason"], "meta_local_error")



class ReviewFixTests(unittest.TestCase):
    """Regressions from the 2026-11-14 pre-landing review."""
    setUp = MetaInferenceTests.setUp
    run_infer = MetaInferenceTests.run_infer
    good = MetaInferenceTests.good

    def test_astral_quote_fits_the_web_utf16_limit(self):
        text = "회의실 " + "😀" * 290
        segments = [segment(text)]
        output = model_output(place={"value": "회의실", "segment": 1, "quote": text[:300]})
        for item in meta_inference.validate(output, segments, {1}):
            self.assertLessEqual(len(item["quote"].encode("utf-16-le")) // 2, 300)


    def test_5xx_is_unknown_and_never_repeats(self):
        result = self.run_infer(Mock(side_effect=rejection(500)))
        self.assertEqual(result["reason"], "meta_http_500")
        ApiCircuit(self.root, "api-safety-text").resume()
        transport = Mock(return_value=self.good())
        self.assertEqual(self.run_infer(transport)["reason"], "meta_unknown_outcome")
        transport.assert_not_called()

    def test_ambiguous_429_is_not_repeated_after_the_pause_is_lifted(self):
        self.run_infer(Mock(side_effect=rejection(429)))
        ApiCircuit(self.root, "api-safety-text").resume()
        transport = Mock(return_value=self.good())
        self.assertEqual(self.run_infer(transport)["reason"], "meta_unknown_outcome")
        transport.assert_not_called()

    def test_config_change_does_not_bill_the_same_transcript_again(self):
        transport = Mock(return_value=self.good())
        self.run_infer(transport)
        self.run_infer(transport, config={**self.config, "api_text_model": "other-model", "api_text_max_output_tokens": 3000})
        self.assertEqual(transport.call_count, 1)

    def test_reuse_after_realign_points_at_the_new_segment_ids(self):
        self.run_infer(Mock(return_value=self.good()))
        realigned = [{**s, "segment_id": str(uuid.uuid4())} for s in self.segments]
        result = self.run_infer(Mock(), segments=realigned)
        ids = {s["segment_id"] for s in realigned}
        self.assertTrue(result["items"])
        self.assertTrue(all(set(item["segment_ids"]) <= ids for item in result["items"]))

    def test_role_and_company_need_the_quote_too(self):
        output = answer(model_output(attendee_candidates=[{"name": "김민수", "role": "대표이사", "company": "가짜회사", "segment": 1, "quote": "저는 한빛제조 김민수 팀장입니다."}]))
        result = self.run_infer(Mock(return_value=output))
        self.assertEqual([item["value"] for item in result["items"]], ["김민수"])

    def test_long_organization_that_cannot_be_saved_is_dropped(self):
        name = "가" * 151
        segments = [segment(f"저는 {name} 소속입니다.")]
        output = model_output(organization={"value": name, "segment": 1, "quote": f"저는 {name} 소속입니다."})
        self.assertEqual(meta_inference.validate(output, segments, {1}), [])

    def test_incomplete_answer_fails_only_this_job(self):
        result = self.run_infer(Mock(return_value=answer(model_output(), status="incomplete")))
        self.assertEqual(result["reason"], "meta_incomplete")
        self.assertFalse(ApiCircuit(self.root, "api-safety-text").status()["api_blocked"])
        entry = read_json(self.directory / "api-budget.json")["attempts"][0]
        self.assertAlmostEqual(entry["charged_upper_bound_usd"], (1000 * 1 + 200 * 4) / 1_000_000)
        transport = Mock(return_value=self.good())
        self.assertEqual(self.run_infer(transport)["reason"], "meta_incomplete")
        transport.assert_not_called()


class SecondPassTests(unittest.TestCase):
    """Regressions from the second review pass (2026-11-14)."""
    setUp = MetaInferenceTests.setUp
    run_infer = MetaInferenceTests.run_infer
    good = MetaInferenceTests.good

    def test_lost_checkpoint_with_a_billed_ledger_entry_is_not_called_again(self):
        transport = Mock(return_value=self.good())
        self.run_infer(transport)
        for path in self.directory.glob("metadata-inference-*.json"):
            path.unlink()
        self.assertEqual(self.run_infer(transport)["reason"], "meta_unknown_outcome")
        self.assertEqual(transport.call_count, 1)

    def test_credit_error_in_local_speech_mode_sets_the_global_pause(self):
        config = {**self.config, "speech_mode": "local"}
        result = self.run_infer(Mock(side_effect=rejection(429, "insufficient_quota")), config=config)
        self.assertEqual(result["reason"], "meta_credit_blocked")
        # The pause the speech worker checks once it is switched to API mode.
        self.assertEqual(ApiCircuit(self.root).status()["api_block_reason"], "insufficient_quota")
        transport = Mock()
        other = self.run_infer(transport, config=config, directory=self.root / "workspace" / "jobs" / "job-b")
        self.assertEqual(other["reason"], "api_blocked")
        transport.assert_not_called()

    def test_a_credit_pause_in_local_mode_is_visible_and_can_be_lifted(self):
        from local_worker import server
        config = {**self.config, "speech_mode": "local"}
        self.run_infer(Mock(side_effect=rejection(429, "insufficient_quota")), config=config)
        caps = meta_inference.capabilities(config)
        self.assertEqual((caps["meta_inference_blocked"], caps["meta_inference_block_reason"]), (True, "insufficient_quota"))
        argv = ["server", "--resume-api", "--confirm-credit-policy", "--confirm-provider-outcomes-reviewed"]
        with patch("local_worker.server.load_server_config", return_value=config), patch("sys.argv", argv), \
             patch("local_worker.server.os.umask", create=True), patch("builtins.print"):
            self.assertEqual(server.main(), 0)
        self.assertFalse(ApiCircuit(self.root).status()["api_blocked"])
        self.assertFalse(meta_inference.capabilities(config)["meta_inference_blocked"])

    def test_refusal_or_empty_output_fails_only_this_job(self):
        empty = json.dumps({"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}],
                            "usage": {"input_tokens": 10, "output_tokens": 2}}).encode()
        result = self.run_infer(Mock(return_value=empty))
        self.assertEqual(result["reason"], "meta_no_output")
        self.assertFalse(ApiCircuit(self.root, "api-safety-text").status()["api_blocked"])
        transport = Mock(return_value=self.good())
        self.assertEqual(self.run_infer(transport)["reason"], "meta_no_output")
        transport.assert_not_called()

    def test_a_speaker_relabel_does_not_bill_the_same_words_again(self):
        transport = Mock(return_value=self.good())
        self.run_infer(transport)
        relabelled = [{**s, "local_speaker_id": "speaker_009", "speaker_name": "이름"} for s in self.segments]
        self.run_infer(transport, segments=relabelled)
        self.assertEqual(transport.call_count, 1)

    def test_a_relabel_on_a_long_transcript_does_not_bill_again(self):
        segments = [segment(f"{index}번째 문장입니다. " + "가" * 200, "unknown") for index in range(400)]
        transport = Mock(return_value=answer(model_output()))
        meta_inference.infer(self.job, segments, self.directory, self.config, transport=transport)
        relabelled = [{**s, "local_speaker_id": "speaker_000"} for s in segments]
        meta_inference.infer(self.job, relabelled, self.directory, self.config, transport=transport)
        self.assertEqual(transport.call_count, 1)

    def test_unresolved_records_never_post_even_after_the_pause_is_lifted(self):
        for state in ["inflight", "unknown_outcome", "response_invalid", "incomplete", "no_output"]:
            directory = self.root / "workspace" / "jobs" / state
            directory.mkdir(parents=True)
            transport = Mock(return_value=self.good())
            self.run_infer(Mock(side_effect=TimeoutError()), directory=directory)  # creates the record
            for path in directory.glob("metadata-inference-*.json"):
                atomic_json(path, {"state": state})
            (directory / "api-budget.json").unlink()  # Only the record may block the call here.
            ApiCircuit(self.root, "api-safety-text").resume()
            reason = self.run_infer(transport, directory=directory)["reason"]
            self.assertEqual(reason, {"incomplete": "meta_incomplete", "no_output": "meta_no_output"}.get(state, "meta_unknown_outcome"))
            transport.assert_not_called()

    def test_definitive_rejection_allows_one_new_call_after_resume(self):
        self.run_infer(Mock(side_effect=rejection(401)))
        ApiCircuit(self.root, "api-safety-text").resume()
        transport = Mock(return_value=self.good())
        self.assertEqual(self.run_infer(transport)["status"], "ok")
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(len(read_json(self.directory / "api-budget.json")["attempts"]), 2)

    def test_attendee_cap(self):
        names = [f"참석자{index:02d}" for index in range(25)]
        segments = [segment(f"저는 {name}입니다.") for name in names]
        output = model_output(attendee_candidates=[{"name": name, "role": "", "company": "", "segment": i + 1, "quote": f"저는 {name}입니다."} for i, name in enumerate(names)])
        items = meta_inference.validate(output, segments, set(range(1, 26)))
        self.assertEqual(len(items), meta_inference.MAX_ATTENDEES)

    def test_capabilities_never_raise_into_the_heartbeat(self):
        self.assertEqual(meta_inference.capabilities({**self.config, "api_text_prices_per_million": {"input": 1}})["meta_inference"], "needs_setup: meta_inference_policy")
        with patch("local_worker.api_circuit.for_text_config", side_effect=RuntimeError("disk")):
            self.assertEqual(meta_inference.capabilities(self.config)["meta_inference"], "needs_setup: meta_inference_storage")

    def test_storage_failure_after_reservation_pauses_and_reports(self):
        real = meta_inference.atomic_json
        def failing(path, value):
            if Path(path).name.startswith("metadata-inference-"):
                raise OSError("disk full")
            return real(path, value)
        transport = Mock()
        with patch.object(meta_inference, "atomic_json", side_effect=failing):
            result = self.run_infer(transport)
        self.assertEqual(result["reason"], "meta_local_error")
        transport.assert_not_called()
        self.assertTrue(ApiCircuit(self.root, "api-safety-text").status()["api_blocked"])


class PostDeadlineTests(unittest.TestCase):
    """Real sockets: the total deadline must hold for slow headers and trickling bodies."""

    def serve(self, handler):
        import http.server
        import threading
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}/v1/responses"

    def test_a_slow_first_byte_within_the_deadline_still_succeeds(self):
        import http.server
        import time as clock

        class Slow(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                clock.sleep(1.2)  # Longer than any per-operation socket timeout guess.
                body = b'{"status":"completed"}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        url = self.serve(Slow)
        with patch.object(meta_inference, "ENDPOINT", url):
            self.assertEqual(meta_inference.post(b"{}", KEY, 3), b'{"status":"completed"}')

    def test_the_first_byte_may_take_the_whole_timeout(self):
        # A non-streamed answer sends its header only when done: no shorter socket cap.
        opener = Mock()
        opener.open.side_effect = TimeoutError()
        with patch("local_worker.meta_inference.request.build_opener", return_value=opener), self.assertRaises(TimeoutError):
            meta_inference._post(b"{}", KEY, meta_inference.TIMEOUT_SECONDS)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], meta_inference.TIMEOUT_SECONDS)

    def test_dripping_headers_are_cut_at_the_total_deadline(self):
        import socketserver
        import time as clock

        class Drip(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.recv(65536)
                try:
                    self.request.sendall(b"HTTP/1.1 200 OK\r\n")
                    for index in range(40):
                        self.request.sendall(f"X-Drip-{index}: x\r\n".encode())
                        clock.sleep(0.15)
                except OSError:
                    pass

        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Drip)
        server.daemon_threads = True
        import threading
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}/v1/responses"
        started = clock.monotonic()
        with patch.object(meta_inference, "ENDPOINT", url), self.assertRaises(TimeoutError):
            meta_inference.post(b"{}", KEY, 0.4)
        self.assertLess(clock.monotonic() - started, 2.0)

    def test_a_real_credit_429_body_reaches_the_failure_classifier(self):
        import http.server
        from local_worker.api_speech import _http_failure

        class Quota(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                body = b'{"error":{"code":"insufficient_quota"}}'
                self.send_response(429)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        url = self.serve(Quota)
        with patch.object(meta_inference, "ENDPOINT", url), self.assertRaises(error.HTTPError) as caught:
            meta_inference.post(b"{}", KEY, 3)
        self.assertEqual(_http_failure(caught.exception)[0], "insufficient_quota")

    def test_a_trickling_body_is_cut_at_the_total_deadline(self):
        import http.server
        import time as clock

        class Trickle(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Length", "100")
                self.end_headers()
                try:
                    for _ in range(100):
                        self.wfile.write(b"x")
                        self.wfile.flush()
                        clock.sleep(0.2)
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        url = self.serve(Trickle)
        started = clock.monotonic()
        with patch.object(meta_inference, "ENDPOINT", url), self.assertRaises(TimeoutError):
            meta_inference.post(b"{}", KEY, 1)
        self.assertLess(clock.monotonic() - started, 2.5)



class CodexModeTests(unittest.TestCase):
    """Subscription mode: the owner's ChatGPT plan through a dedicated Codex login."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / "workspace" / "jobs" / "job-a"
        self.directory.mkdir(parents=True)
        self.binary = self.root / "codex-bin"
        self.binary.write_text("not run in these tests")
        self.binary.chmod(0o755)
        self.home = self.root / "codex-home"
        self.home.mkdir()
        self.home.chmod(0o700)  # The policy refuses a login home others can read (POSIX).
        self.config = {"execution_location": "server", "speech_mode": "api", "worker_home": str(self.root),
                       "openai_api_key": KEY, "api_max_cost_usd": 2, "meta_inference_mode": "codex",
                       "meta_codex_path": str(self.binary), "meta_codex_home": str(self.home)}
        self.segments = [segment("안녕하세요, 저는 한빛제조 김민수 팀장입니다."), segment("오늘은 판교 본사 3층 회의실에서 뵙네요.", "speaker_001")]
        self.job = {"id": "job-a", "output_mode": "minutes"}
        meta_inference._cooldown.update(until=0.0, reason="")
        self.addCleanup(meta_inference._cooldown.update, until=0.0, reason="")
        # Fake processes carry made-up pids: never signal a real process group from a test.
        killpg = patch("local_worker.meta_inference.os.killpg", create=True)
        self.killpg = killpg.start()
        self.addCleanup(killpg.stop)
        self.output = model_output(
            organization={"value": "한빛제조", "segment": 1, "quote": "저는 한빛제조 김민수 팀장입니다."},
            place={"value": "판교 본사 3층 회의실", "segment": 2, "quote": "판교 본사 3층 회의실에서 뵙네요"},
            attendee_candidates=[{"name": "김민수", "role": "팀장", "company": "한빛제조", "segment": 1, "quote": "저는 한빛제조 김민수 팀장입니다."}])

    def run_infer(self, runner, config=None):
        return meta_inference.infer(self.job, self.segments, self.directory, config or self.config, codex=runner)

    def fake(self, text=None, failure=None):
        seen = {}

        def runner(prompt, workdir, value):
            seen.update(prompt=prompt, workdir=workdir, exists=workdir.is_dir(), value=value, calls=seen.get("calls", 0) + 1)
            if failure:
                raise meta_inference.CodexFailure(failure)
            return json.dumps(self.output, ensure_ascii=False) if text is None else text
        return runner, seen

    def test_capability_needs_the_dedicated_login_then_reports_codex(self):
        self.assertEqual(meta_inference.capabilities(self.config), {"meta_inference": "needs_setup: meta_inference_login"})
        (self.home / "auth.json").write_text("{}")
        self.assertEqual(meta_inference.capabilities(self.config), {"meta_inference": "codex/codex-default", "meta_inference_blocked": False})
        caps = meta_inference.capabilities({**self.config, "meta_codex_model": "model-y"})
        self.assertEqual(caps["meta_inference"], "codex/model-y")

    def test_unsafe_or_incomplete_settings_count_as_off(self):
        outside = self.root.parent / "elsewhere-codex-home"
        for change in [{"meta_codex_home": str(outside)}, {"meta_codex_home": str(self.root)}, {"meta_codex_home": "relative"},
                       {"meta_codex_path": str(self.root / "missing")}, {"meta_codex_path": "codex"},
                       {"meta_codex_timeout": 5}, {"meta_codex_timeout": 60.5}, {"meta_codex_timeout": 181}, {"meta_codex_model": "bad model"}]:
            config = {**self.config, **change}
            self.assertEqual(meta_inference.capabilities(config), {"meta_inference": "needs_setup: meta_inference_policy"}, change)
            self.assertIsNone(self.run_infer(self.fake()[0], config=config), change)

    def test_valid_answer_is_validated_recorded_and_bills_nothing(self):
        runner, seen = self.fake()
        result = self.run_infer(runner)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["model_id"], "codex-default")
        self.assertEqual([item["key"] for item in result["items"]], ["organization", "place", "attendee"])
        self.assertIn("[1] speaker_000: 안녕하세요, 저는 한빛제조 김민수 팀장입니다.", seen["prompt"])
        self.assertTrue(seen["exists"])
        self.assertFalse(seen["workdir"].exists())  # Removed after the call.
        self.assertFalse((self.directory / "api-budget.json").exists())
        record = read_json(next(self.directory.glob("metadata-inference-*.json")))
        self.assertEqual((record["state"], record["mode"]), ("completed", "codex"))
        again = self.run_infer(Mock(side_effect=AssertionError("must reuse the record")))
        self.assertEqual(again["items"], result["items"])

    def test_a_failure_fails_this_job_only_and_a_later_run_may_retry(self):
        for reason in ["meta_codex_usage_limit", "meta_codex_login", "meta_codex_timeout", "meta_codex_failed"]:
            meta_inference._cooldown.update(until=0.0)  # Cool-down has its own test.
            runner, seen = self.fake(failure=reason)
            self.assertEqual(self.run_infer(runner), {"model_id": "codex-default", "created_at": unittest.mock.ANY, "items": [],
                                                      "status": "failed", "reason": reason})
            self.assertEqual(read_json(next(self.directory.glob("metadata-inference-*.json")))["state"], "failed")
        meta_inference._cooldown.update(until=0.0)
        runner, seen = self.fake()
        self.assertEqual(self.run_infer(runner)["status"], "ok")

    def test_empty_or_malformed_answers_fail_cleanly(self):
        self.assertEqual(self.run_infer(self.fake(text=" ")[0])["reason"], "meta_no_output")
        self.assertEqual(self.run_infer(self.fake(text="not json")[0])["reason"], "meta_response_invalid")
        self.assertEqual(self.run_infer(self.fake(text="[]")[0])["reason"], "meta_response_invalid")


    def test_pauses_and_crashes_never_latch_the_subscription_path(self):
        (self.home / "auth.json").write_text("{}")
        ApiCircuit(self.root).trip("insufficient_quota")
        ApiCircuit(self.root, "api-safety-text").trip("unknown_outcome")
        self.assertFalse(meta_inference.capabilities(self.config)["meta_inference_blocked"])
        self.assertEqual(self.run_infer(self.fake()[0])["status"], "ok")
        path = next(self.directory.glob("metadata-inference-*.json"))
        for state in ["inflight", "unknown_outcome", "incomplete", "failed"]:
            atomic_json(path, {"state": state})  # A deploy or crash mid-call, or an earlier API-mode record.
            runner, seen = self.fake()
            self.assertEqual(self.run_infer(runner)["status"], "ok", state)
            self.assertEqual(seen["calls"], 1, state)

    def test_any_unexpected_error_fails_this_job_only_and_stays_retryable(self):
        def broken(prompt, workdir, value):
            raise ValueError("bug")
        self.assertEqual(self.run_infer(broken)["reason"], "meta_local_error")
        self.assertEqual(read_json(next(self.directory.glob("metadata-inference-*.json")))["state"], "failed")
        self.assertFalse(list(self.directory.glob("meta-codex-*")))
        self.assertEqual(self.run_infer(self.fake()[0])["status"], "ok")

    def test_malformed_settings_never_raise(self):
        for change in [{"worker_home": 5}, {"meta_codex_home": "/x\x00y"}, {"meta_codex_path": 7}]:
            config = {**self.config, **change}
            self.assertEqual(meta_inference.capabilities(config), {"meta_inference": "needs_setup: meta_inference_policy"}, change)
            self.assertIsNone(self.run_infer(self.fake()[0], config=config), change)

    def test_timeout_bounds(self):
        for timeout in [30, 180]:
            self.assertEqual(meta_inference._codex_policy({**self.config, "meta_codex_timeout": timeout})["timeout"], timeout)
        for timeout in [29, 181, 600]:
            self.assertEqual(meta_inference.capabilities({**self.config, "meta_codex_timeout": timeout}),
                             {"meta_inference": "needs_setup: meta_inference_policy"})

    def popen(self, returncode=0, stderr=b"", write=True, timeout=False, events=b""):
        calls = {}

        class Fake:
            def __init__(self, args, **kwargs):
                calls.update(args=args, **kwargs)
                self.args, self.pid, self.returncode, self.killed = args, 4242, None, False
                calls["process"] = self

            def communicate(self, data=None, timeout=None):
                if data is not None:
                    calls["input"], calls["timeout"] = data, timeout
                    if timeout_flag:
                        import subprocess
                        raise subprocess.TimeoutExpired("codex", timeout)
                    if write:
                        Path(self.args[self.args.index("--output-last-message") + 1]).write_text('{"ok": true}', encoding="utf-8")
                    # Model text derived from the meeting arrives as item events: never classified.
                    calls["stdout"].write(b'{"type":"item.completed","item":{"text":"login 401 quota usage limit"}}\n')
                    calls["stdout"].write(events)
                    calls["stderr"].write(stderr)
                self.returncode = returncode
                return None, None

            def kill(self):
                self.killed = True

        timeout_flag = timeout
        return Fake, calls

    def test_run_codex_locks_down_the_cli_and_its_environment(self):
        value = meta_inference._codex_policy({**self.config, "meta_codex_timeout": 90})
        workdir = self.root / "work"
        workdir.mkdir()
        fake, calls = self.popen()
        with patch.dict("os.environ", {"OPENAI_API_KEY": "env-secret", "CODEX_API_KEY": "env-secret-2", "HOME": "/home/x"}), \
             patch.object(meta_inference, "_enabled_features", return_value=["shell_tool", "sleep_tool", "enable_request_compression"]), \
             patch("local_worker.meta_inference.subprocess.Popen", fake):
            self.assertEqual(meta_inference.run_codex("한국어 녹취", workdir, value), '{"ok": true}')
        args = calls["args"]
        self.assertEqual(args[0], str(self.binary))
        for flag in ["--ignore-user-config", "--ignore-rules", "--ephemeral", "--skip-git-repo-check", "--output-schema", "--json"]:
            self.assertIn(flag, args)
        self.assertEqual(args[args.index("--sandbox") + 1], "read-only")
        disabled = [args[n + 1] for n, a in enumerate(args) if a == "--disable"]
        self.assertEqual(disabled, ["shell_tool", "sleep_tool"])  # Everything on, except what the transport needs.
        self.assertNotIn("--model", args)
        self.assertEqual(set(calls["env"]), {"CODEX_HOME", "HOME", "PATH", "LANG", "LC_ALL", "NO_COLOR"})
        self.assertEqual((calls["env"]["CODEX_HOME"], calls["env"]["HOME"]), (str(self.home), str(self.home)))
        self.assertNotIn("env-secret", json.dumps(calls["env"]))
        self.assertEqual(calls["input"], "한국어 녹취".encode("utf-8"))
        self.assertEqual((calls["timeout"], calls["cwd"]), (90, workdir))
        import os
        self.assertEqual(calls["start_new_session"], os.name == "posix")
        self.assertEqual(read_json(workdir / "schema.json"), meta_inference.SCHEMA)

    def test_enabled_features_come_from_the_binary_itself(self):
        value = meta_inference._codex_policy(self.config)
        listing = (b"apps                stable             true\n"
                   b"goals               stable             true\n"
                   b"sqlite              removed            true\n"
                   b"artifact            under development  false\n"
                   b"instant_interrupt   under development  true\n")
        with patch("local_worker.meta_inference.subprocess.run", return_value=Mock(returncode=0, stdout=listing)) as run:
            self.assertEqual(meta_inference._enabled_features(value), ["apps", "goals", "instant_interrupt"])
        self.assertEqual(run.call_args.args[0][1:], ["features", "list"])
        for bad in [Mock(returncode=1, stdout=listing), Mock(returncode=0, stdout=b""), Mock(returncode=0, stdout=b"BAD;name x true\n")]:
            with patch("local_worker.meta_inference.subprocess.run", return_value=bad), self.assertRaises(meta_inference.CodexFailure):
                meta_inference._enabled_features(value)

    def test_account_wide_failures_cool_down_for_a_while(self):
        (self.home / "auth.json").write_text("{}")
        runner, seen = self.fake(failure="meta_codex_usage_limit")
        self.assertEqual(self.run_infer(runner)["reason"], "meta_codex_usage_limit")
        caps = meta_inference.capabilities(self.config)
        self.assertEqual((caps["meta_inference_blocked"], caps["meta_inference_block_reason"]), (True, "meta_codex_usage_limit"))
        runner, seen = self.fake()
        self.assertEqual(self.run_infer(runner)["reason"], "meta_codex_cooldown")
        self.assertNotIn("calls", seen)
        meta_inference._cooldown.update(until=0.0)
        self.assertEqual(self.run_infer(runner)["status"], "ok")
        runner, seen = self.fake(failure="meta_codex_timeout")
        for path in self.directory.glob("metadata-inference-*.json"):
            path.unlink()
        self.run_infer(runner)
        self.assertEqual(meta_inference.capabilities(self.config)["meta_inference_block_reason"], "meta_codex_timeout")
        self.assertLess(meta_inference._cooldown["until"] - __import__("time").monotonic(), 301)  # Shorter than account-wide.
        meta_inference._cooldown.update(until=0.0)
        runner, seen = self.fake(failure="meta_codex_failed")
        for path in self.directory.glob("metadata-inference-*.json"):
            path.unlink()
        self.run_infer(runner)
        self.assertFalse(meta_inference.capabilities(self.config)["meta_inference_blocked"])

    def test_a_config_file_in_the_login_home_is_refused(self):
        (self.home / "config.toml").write_text("[features]\nsleep_tool = false\n")
        self.assertEqual(meta_inference.capabilities(self.config), {"meta_inference": "needs_setup: meta_inference_policy"})

    def test_failure_words_must_be_whole(self):
        workdir = self.root / "w"
        workdir.mkdir()
        events, errors = workdir / "e.jsonl", workdir / "s.txt"
        for stderr, reason in [(b"request id 4012abc took 1401 ms", "meta_codex_failed"), (b"HTTP 401 Unauthorized", "meta_codex_login"),
                               (b"refresh token was revoked", "meta_codex_login"), (b"quota exceeded for plan", "meta_codex_usage_limit"),
                               (b"checking quota window", "meta_codex_failed")]:
            events.write_bytes(b"")
            errors.write_bytes(stderr)
            self.assertEqual(meta_inference._failure_reason(events, errors), reason, stderr)

    def test_stale_scratch_folders_are_removed_and_api_records_are_left_alone(self):
        (self.directory / "meta-codex-deadbeef").mkdir()
        api_record = self.directory / "metadata-inference-0123.json"
        atomic_json(api_record, {"state": "unknown_outcome"})
        self.assertEqual(self.run_infer(self.fake()[0])["status"], "ok")
        self.assertFalse((self.directory / "meta-codex-deadbeef").exists())
        self.assertEqual(read_json(api_record), {"state": "unknown_outcome"})
        self.assertEqual(len(list(self.directory.glob("metadata-inference-codex-*.json"))), 1)

    def test_a_codex_leftover_never_blocks_api_mode_after_a_switch(self):
        self.run_infer(self.fake()[0])
        for path in self.directory.glob("metadata-inference-codex-*.json"):
            atomic_json(path, {"state": "inflight", "mode": "codex"})
        api = {**self.config, "meta_inference_mode": "api", "api_text_model": "text-model-x",
               "api_text_prices_per_million": {"input": 1.0, "output": 4.0}, "api_meta_max_cost_usd": 0.5}
        transport = Mock(return_value=answer(self.output))
        result = meta_inference.infer(self.job, self.segments, self.directory, api, transport=transport)
        self.assertEqual((result["status"], transport.call_count), ("ok", 1))

    @unittest.skipUnless(__import__("os").name == "posix", "POSIX permissions")
    def test_a_login_home_others_can_read_is_refused(self):
        self.home.chmod(0o755)
        self.assertEqual(meta_inference.capabilities(self.config), {"meta_inference": "needs_setup: meta_inference_policy"})
        self.home.chmod(0o700)
        self.assertEqual(meta_inference.capabilities(self.config), {"meta_inference": "needs_setup: meta_inference_login"})

    def test_run_codex_classifies_failures_from_stderr_only(self):
        value = meta_inference._codex_policy({**self.config, "meta_codex_model": "model-y"})
        workdir = self.root / "work"
        workdir.mkdir()
        # Item events always mention "login 401 quota usage limit" (meeting text): never the reason.
        cases = [(1, b"Error: You've hit your usage limit.", b"", True, "meta_codex_usage_limit"),
                 (1, b"Not logged in", b"", True, "meta_codex_login"),
                 (1, b"", b'{"type":"turn.failed","error":{"message":"You have hit your usage limit. Try again later."}}\n', True, "meta_codex_usage_limit"),
                 (1, b"", b'{"type":"error","message":"401 Unauthorized"}\n', True, "meta_codex_login"),
                 (1, b"something else broke", b"", True, "meta_codex_failed"),
                 (0, b"", b"", False, "meta_codex_failed")]
        for returncode, stderr, events, write, reason in cases:
            fake, calls = self.popen(returncode=returncode, stderr=stderr, write=write, events=events)
            target = workdir / "last-message.txt"
            if target.exists():
                target.unlink()
            with patch.object(meta_inference, "_enabled_features", return_value=["shell_tool"]), \
                 patch("local_worker.meta_inference.subprocess.Popen", fake), self.assertRaises(meta_inference.CodexFailure) as caught:
                meta_inference.run_codex("x", workdir, value)
            self.assertEqual(caught.exception.reason, reason, (stderr, events))
            self.assertEqual(calls["args"][calls["args"].index("--model") + 1], "model-y")
        fake, calls = self.popen(timeout=True)
        self.killpg.reset_mock()
        killpg = self.killpg
        with patch.object(meta_inference, "_enabled_features", return_value=["shell_tool"]), \
             patch("local_worker.meta_inference.subprocess.Popen", fake), \
             self.assertRaises(meta_inference.CodexFailure) as caught:
            meta_inference.run_codex("x", workdir, value)
        self.assertEqual(caught.exception.reason, "meta_codex_timeout")
        import os
        if os.name == "posix":
            self.assertEqual(killpg.call_args.args[0], 4242)  # The whole group, not just the launcher.
        else:
            self.assertTrue(calls["process"].killed)
        with patch.object(meta_inference, "_enabled_features", return_value=["shell_tool"]), \
             patch("local_worker.meta_inference.subprocess.Popen", side_effect=OSError("private")), \
             self.assertRaises(meta_inference.CodexFailure) as caught:
            meta_inference.run_codex("x", workdir, value)
        self.assertEqual(caught.exception.reason, "meta_codex_start")

if __name__ == "__main__":
    unittest.main()
