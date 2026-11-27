from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib import error
import uuid

from local_worker import api_speech, api_circuit
from local_worker.api_circuit import ApiCircuit
from local_worker.core import WorkerError, atomic_json, read_json, defaults
from local_worker.runner import Worker
from test_api_speech import write_audio, reply


def rejection(code=None, status=429, raw=None):
    value = raw if raw is not None else json.dumps({"error": {"code": code, "message": "private-provider-detail"}}).encode()
    return error.HTTPError(api_speech.ENDPOINT, status, "private-provider-detail", {"Retry-After": "0"}, io.BytesIO(value))


class ServerApiCircuitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.audio = self.root / "source.wav"
        write_audio(self.audio)
        self.config = {"execution_location": "server", "speech_mode": "api", "worker_home": str(self.root),
            "openai_api_key": "synthetic-secret-key", "api_speech_model": "gpt-transcribe", "api_credit_only_ack": True,
            "api_speech_price_per_minute": .0045, "api_max_cost_usd": 2, "api_speech_chunk_seconds": 1,
            "api_speech_billing_rounding_seconds": 1}
        self.checkpoint = self.root / "job-a" / "asr.json"
        self.circuit = ApiCircuit(self.root)

    def run_api(self, checkpoint=None, config=None):
        return api_speech.transcribe(self.audio, checkpoint or self.checkpoint, config or self.config)

    def test_success_then_quota_blocks_other_job_restart_and_key_change_without_replay(self):
        with patch.object(api_speech, "_post", side_effect=[reply(), rejection("credit_balance_exhausted")]) as post:
            with self.assertRaises(WorkerError) as failure:
                self.run_api()
            self.assertFalse(failure.exception.retryable)
            self.assertEqual(post.call_count, 2)
        status = self.circuit.status()
        self.assertEqual(status["api_block_reason"], "credit_balance_exhausted")
        self.assertEqual(ApiCircuit(self.root).status(), status)
        with patch.object(api_speech, "_post") as post, self.assertRaises(WorkerError):
            self.run_api(self.root / "job-b" / "asr.json", {**self.config, "openai_api_key": "changed-key"})
        post.assert_not_called()
        cached = read_json(self.checkpoint.with_name("asr-api.json"))
        self.assertEqual(cached["clips"][0]["state"], "complete")
        resume = self.circuit.resume()
        self.assertEqual(resume["api_resolved_block_id"], status["api_block_id"])
        with patch.object(api_speech, "_post", return_value=reply()) as post:
            output = self.run_api()
        self.assertEqual(post.call_count, 2)  # Already successful paid clip was preserved.
        self.assertEqual(len(output["segments"]), 3)

    def test_quota_billing_and_every_429_are_nonretryable_and_global(self):
        cases = [("insufficient_quota", None, "insufficient_quota"), ("billing_hard_limit_reached", None, "billing_limit"),
                 ("rate_limit_exceeded", None, "transient_429"), (None, b"not json", "ambiguous_429"),
                 (None, b"x" * 8193, "ambiguous_429"), (None, b'{"error":{"code":{"nested":"private"}}}', "ambiguous_429")]
        for index, (code, raw, reason) in enumerate(cases):
            config = {**self.config, "worker_home": str(self.root / str(index))}
            with self.subTest(reason=reason), patch.object(api_speech, "_post", side_effect=rejection(code, raw=raw)) as post:
                with self.assertRaises(WorkerError) as failure:
                    self.run_api(self.root / str(index) / "job" / "asr.json", config)
                self.assertFalse(failure.exception.retryable)
                self.assertEqual(ApiCircuit(Path(config["worker_home"])).status()["api_block_reason"], reason)
                with self.assertRaises(WorkerError):
                    self.run_api(self.root / str(index) / "next" / "asr.json", config)
                self.assertEqual(post.call_count, 1)

    def test_timeout_is_globally_paused_and_manual_global_resume_does_not_erase_job_unknown(self):
        with patch.object(api_speech, "_post", side_effect=TimeoutError()) as post:
            with self.assertRaises(WorkerError):
                self.run_api()
            self.assertEqual(post.call_count, 1)
        self.assertEqual(self.circuit.status()["api_block_reason"], "unknown_outcome")
        self.circuit.resume()
        with patch.object(api_speech, "_post") as post, self.assertRaises(WorkerError) as failure:
            self.run_api()
        post.assert_not_called()
        self.assertEqual(failure.exception.state, "unknown_outcome")

    def test_crashed_inflight_intent_is_recovered_before_claim(self):
        atomic_json(self.circuit.path, {"version": 1, "phase": "inflight", "attempt_id": str(uuid.uuid4())})
        self.assertEqual(ApiCircuit(self.root).status()["api_block_reason"], "unknown_outcome")
        self.assertEqual(read_json(self.circuit.path)["phase"], "blocked")

    def test_corrupt_storage_or_write_failure_blocks_without_network(self):
        self.circuit.path.parent.mkdir()
        self.circuit.path.write_text("invalid-json", encoding="utf-8")
        with patch.object(api_speech, "_post") as post, self.assertRaises(WorkerError):
            self.run_api()
        post.assert_not_called()
        self.assertEqual(self.circuit.status()["api_block_reason"], "circuit_storage")
        with tempfile.TemporaryDirectory() as temporary:
            config = {**self.config, "worker_home": temporary}
            with patch("local_worker.api_circuit.atomic_json", side_effect=OSError("private")), patch.object(api_speech, "_post") as post, self.assertRaises(WorkerError):
                self.run_api(self.root / "write-fail" / "asr.json", config)
            post.assert_not_called()
            self.assertTrue(ApiCircuit(Path(temporary)).status()["api_blocked"])

    def test_failure_to_persist_quota_keeps_inflight_for_restart_and_memory_pause(self):
        real = api_circuit.atomic_json
        def fail_block(path, value):
            if value.get("phase") == "blocked":
                raise OSError("private")
            real(path, value)
        with patch("local_worker.api_circuit.atomic_json", side_effect=fail_block), patch.object(api_speech, "_post", side_effect=rejection("insufficient_quota")) as post:
            with self.assertRaises(WorkerError):
                self.run_api()
            self.assertEqual(post.call_count, 1)
        self.assertTrue(self.circuit.status()["api_blocked"])
        api_circuit._STORAGE_LATCHES.discard(str(self.circuit.root.resolve()))  # Simulate fresh process memory.
        self.assertEqual(ApiCircuit(self.root).status()["api_block_reason"], "unknown_outcome")

    def test_blocked_worker_still_heartbeats_and_never_claims(self):
        self.circuit.trip("insufficient_quota")
        client = Mock()
        client.call.return_value = {}
        worker = Worker(defaults({**self.config, "workspace": str(self.root / "workspace")}), client)
        worker.run(once=True)
        self.assertEqual([call.args[1] for call in client.call.call_args_list], ["/api/worker/heartbeat"])
        cap = client.call.call_args.args[2]["capabilities"]
        self.assertTrue(cap["api_blocked"])
        self.assertEqual(cap["api_block_reason"], "insufficient_quota")

    def test_response_headers_and_unselected_fields_never_enter_checkpoints(self):
        raw = json.dumps({"text": "정상 문장", "arbitrary_field": "private-arbitrary-data"}).encode()
        with patch.object(api_speech, "_post", return_value=(raw, "private-header-value")):
            self.run_api()
        content = self.checkpoint.with_name("asr-api.json").read_text(encoding="utf-8")
        self.assertNotIn("private-arbitrary-data", content)
        self.assertNotIn("private-header-value", content)
        self.assertNotIn("response_body", content)

    def test_credential_echo_is_not_saved(self):
        raw = json.dumps({"text": self.config["openai_api_key"]}).encode()
        with patch.object(api_speech, "_post", return_value=(raw, self.config["openai_api_key"])), self.assertRaises(WorkerError):
            self.run_api()
        for path in self.root.rglob("*.json"):
            self.assertNotIn(self.config["openai_api_key"], path.read_text(encoding="utf-8"))
        self.assertEqual(self.circuit.status()["api_block_reason"], "response_invalid")

    def test_server_does_not_take_env_key_or_legacy_model_or_missing_attestation(self):
        for change in [{"openai_api_key": None}, {"api_speech_model": "whisper-1"}, {"api_credit_only_ack": False}]:
            with patch.dict("os.environ", {"OPENAI_API_KEY": "synthetic-env-key"}), patch.object(api_speech, "_post") as post, self.assertRaises(WorkerError):
                self.run_api(config={**self.config, **change})
            post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
