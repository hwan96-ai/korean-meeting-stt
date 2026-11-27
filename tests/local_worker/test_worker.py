from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import threading
import sys
import time
import unittest
from unittest.mock import Mock, patch
import uuid
import wave

from local_worker.core import WorkerError, SingleWorker, atomic_json, defaults, job_directory, read_json
from local_worker.generation import generate, document, validate_body, title_of, missing_metadata
from local_worker.notion_export import export, markdown_blocks, rich_text
from local_worker.profiles import match
from local_worker.profiles import enroll, stage_enrollment, commit_enrollment, acknowledge_enrollment, recover_enrollments, list_profiles, delete_pending_profiles
from local_worker.speech import align, prepare_audio, partition_acoustic_intervals

ID = "b3ff231c-d7d4-4b74-b41e-c20280974534"
SEGMENT_ID = "5c098fb8-d156-47f8-ad04-c3556b0f90e6"
BODY = "## 1. 회의 결론\n\n- **테스트 결정**\n\n## 2. 핵심 논의사항\n\n| 구분 | 내용 |\n| --- | --- |\n| 테스트 | **내용** |\n\n## 3. Next Step\n\n| No. | Action Item | 내용 | 담당 |\n| --- | --- | --- | --- |\n"


def job():
    result = {"id": ID, "meeting_type": "customer", "developer_requests": False, "stage": "reviewed", "transcript_revision": 2,
        "metadata": {"organization": "시험 고객", "datetime": "2026-10-28T09:30:00+09:00", "place": "시험 장소", "meeting_format": "대면", "author": "시험 작성자", "attendees": "고객 직책 (고객사)\n담당 직책 (자사)", "week": ""},
        "segments": [{"segment_id": SEGMENT_ID, "start_ms": 0, "end_ms": 1000, "text": "테스트 결정", "review_status": "confirmed"}]}
    result["draft"] = {"revision": 1, "transcript_revision": 2, "content": document(result, BODY)}
    return result


class FakeNotion:
    def __init__(self, failure=None):
        self.pages, self.blocks, self.calls, self.failure = [], [], [], failure

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path.startswith("/data_sources/"):
            config = defaults({})["notion_targets"]["customer"]
            return {"id": config["data_source_id"], "parent": {"database_id": config["database_id"]},
                "properties": {"제목": {"type": "title"}, "일시": {"type": "date"}, **{name: {"type": "rich_text"} for name in ["장소", "참석자", "작성자"]},
                    "회의 형태": {"type": "select", "select": {"options": [{"name": "대면"}]}}}}
        if method == "POST" and path == "/pages":
            self.pages.append(body)
            if self.failure == "create":
                raise WorkerError("notion_unknown", "test", "unknown_outcome")
            return {"id": ID, "url": "https://notion.so/" + ID}
        if method == "PATCH":
            self.blocks += body["children"]
            if self.failure == "append":
                raise WorkerError("notion_unknown", "test", "unknown_outcome")
            return {"results": body["children"]}
        if method == "GET":
            return {"results": self.blocks, "has_more": False}
        raise AssertionError((method, path))


class SpeechTests(unittest.TestCase):
    def test_cross_block_global_reconciliation_links_same_vectors_and_separates_others(self):
        from local_worker.bounded_diarization import reconcile, owned_turns
        turns = owned_turns([{"start": 8, "end": 12, "speaker": "localA"}], 0, 0, 10, 0)
        turns += owned_turns([{"start": 3, "end": 7, "speaker": "localA"}], 5, 10, 20, 1)
        turns += [{"start": 13, "end": 15, "speaker": "block0000/localB"}, {"start": 15, "end": 17, "speaker": "block0001/localB"}]
        features = {"block0000/localA": [1, 0], "block0001/localA": [0.99, 0.01], "block0000/localB": [0, 1], "block0001/localB": [0.01, 0.99]}
        result, diagnostics = reconcile(turns, features, 0.5)
        self.assertEqual(diagnostics["global_clusters"], 2)
        self.assertEqual([(turn["start"], turn["end"]) for turn in result], [(8, 12), (13, 17)])
        self.assertNotEqual(result[0]["speaker"], result[1]["speaker"])
        self.assertIsNone(diagnostics["known_speaker_count"])

    def test_repeated_native_numeric_id_does_not_link_different_block_voices(self):
        from local_worker.bounded_diarization import reconcile
        turns = [{"start": 0, "end": 3, "speaker": "block0000/speaker_00"},
                 {"start": 4, "end": 7, "speaker": "block0001/speaker_00"}]
        result, diagnostics = reconcile(turns, {turns[0]["speaker"]: [1, 0], turns[1]["speaker"]: [0, 1]}, 0.5)
        self.assertEqual(diagnostics["global_clusters"], 2)
        self.assertNotEqual(result[0]["speaker"], result[1]["speaker"])

    def test_global_reconciliation_preserves_overlap_and_unknown_references(self):
        from local_worker.bounded_diarization import reconcile
        turns = [{"start": 0, "end": 2, "speaker": "first"}, {"start": 1, "end": 3, "speaker": "second"}, {"start": 4, "end": 5, "speaker": "short"}]
        result, _ = reconcile(turns, {"first": [1, 0], "second": [1, 0]}, 0.5)
        self.assertTrue(any(turn.get("overlap") and turn["start"] == 1 and turn["end"] == 2 for turn in result))
        self.assertTrue(any(turn["speaker"] == "unknown" and turn["start"] == 4 for turn in result))
        aligned = align([{"start": 1.1, "end": 1.8, "text": "겹침"}], result, 5)
        self.assertEqual(aligned[0]["local_speaker_id"], "unknown")
        self.assertTrue(aligned[0]["overlap"])
        unknown, _ = reconcile(turns[:2], {}, 0.5)
        aligned_unknown = align([{"start": 1.1, "end": 1.8, "text": "겹침"}], unknown, 3)
        self.assertTrue(aligned_unknown[0]["overlap"])
        self.assertTrue(any(part["overlap"] and part["start"] == 1 and part["end"] == 2 for part in partition_acoustic_intervals(unknown, 3)))

    def test_api_intervals_cover_all_real_source_samples_including_gaps_and_overlap(self):
        turns = [{"start": 0.5, "end": 2, "speaker": "A"}, {"start": 1.5, "end": 3, "speaker": "B"}]
        intervals = partition_acoustic_intervals(turns, 4, max_seconds=1)
        self.assertEqual(intervals[0]["start"], 0)
        self.assertEqual(intervals[-1]["end"], 4)
        self.assertTrue(all(a["end"] == b["start"] for a, b in zip(intervals, intervals[1:])))
        self.assertTrue(all(0 < item["end"] - item["start"] <= 1 for item in intervals))
        overlap = next(item for item in intervals if item["start"] == 1.5)
        self.assertEqual(overlap["local_speaker_id"], "unknown")
        self.assertTrue(overlap["overlap"])
        self.assertEqual(sum(item["end"] - item["start"] for item in intervals), 4)
        self.assertEqual({item["timestamp_granularity"] for item in intervals}, {"source_clip"})

    def test_profile_registry_stays_private_until_server_ack_and_recovers_commit(self):
        import numpy as np
        request = {"request_id": str(uuid.uuid4()), "segment_ids": [SEGMENT_ID], "display_name": "합성 인물", "role": "직책", "company": "회사", "confirmed_single_speaker": True}
        segments = [{"segment_id": SEGMENT_ID, "start_ms": 0, "end_ms": 4000, "overlap": False}]
        with tempfile.TemporaryDirectory() as directory, patch("local_worker.profiles.embedding", return_value=([1.0, 0.0], np.zeros(64000), "sha")):
            root, pending = Path(directory) / "speakers", Path(directory) / "pending"
            staged = stage_enrollment(root, pending, Path("unused.wav"), segments, request, defaults({}))
            self.assertEqual(list_profiles(root), [])
            recover_enrollments(root, pending)
            self.assertEqual(list_profiles(root), [])  # Lost/stale ACK must remain quarantined.
            with self.assertRaises(WorkerError):
                commit_enrollment(root, staged)
            acknowledge_enrollment(staged)
            recover_enrollments(root, pending)
            self.assertEqual(len(list_profiles(root)), 1)
            self.assertFalse(staged.exists())
            person = list_profiles(root)[0]["person_id"]
            request.update(request_id=str(uuid.uuid4()), person_id=person)
            staged = stage_enrollment(root, pending, Path("unused.wav"), segments, request, defaults({}))
            self.assertEqual(list_profiles(root)[0]["profile_revision"], 1)
            acknowledge_enrollment(staged)
            commit_enrollment(root, staged)
            self.assertEqual(list_profiles(root)[0]["profile_revision"], 2)
            self.assertEqual(len(list_profiles(root)[0]["embeddings"]), 2)

    def test_enrollment_requires_confirmation_and_retries_keep_person_id(self):
        import numpy as np
        request = {"request_id": str(uuid.uuid4()), "segment_ids": [SEGMENT_ID], "display_name": "합성 인물", "role": "직책", "company": "회사", "confirmed_single_speaker": False}
        segments = [{"segment_id": SEGMENT_ID, "start_ms": 0, "end_ms": 4000, "overlap": False}]
        with tempfile.TemporaryDirectory() as directory, patch("local_worker.profiles.embedding", return_value=([1.0, 0.0], np.zeros(64000), "sha")) as extractor:
            with self.assertRaises(WorkerError):
                enroll(Path(directory), Path("unused.wav"), segments, request, defaults({}))
            request["confirmed_single_speaker"] = True
            first = enroll(Path(directory), Path("unused.wav"), segments, request, defaults({}))
            second = enroll(Path(directory), Path("unused.wav"), segments, request, defaults({}))
            self.assertEqual(first["person_id"], second["person_id"])
            self.assertEqual(second["profile_revision"], 1)
            self.assertEqual(extractor.call_count, 1)

    def test_word_alignment_splits_speaker_turns_without_fabricating_text(self):
        source = [{"start": 0, "end": 2, "text": "하나 둘", "words": [{"start": 0, "end": 0.8, "text": "하나"}, {"start": 1.2, "end": 2, "text": " 둘"}]}]
        result = align(source, [{"start": 0, "end": 1, "speaker": "A"}, {"start": 1, "end": 2, "speaker": "B"}], 2)
        self.assertEqual([segment["local_speaker_id"] for segment in result], ["A", "B"])
        self.assertEqual([segment["text"] for segment in result], ["하나", "둘"])
        self.assertTrue(all(segment["confirmed_person_id"] is None for segment in result))

    def test_rounded_unit_bound_grazing_a_neighbour_overlap_is_not_overlap(self):
        turns = [{"start": 0, "end": 2.0014, "speaker": "A"}, {"start": 1.9986, "end": 2.0014, "speaker": "unknown", "overlap": True},
                 {"start": 1.9986, "end": 4, "speaker": "B"}]
        result = align([{"start": 2.0, "end": 3.5, "text": "깨끗한 발화"}], turns, 4)[0]
        self.assertFalse(result["overlap"])
        self.assertEqual(result["local_speaker_id"], "B")
        real = align([{"start": 1.99, "end": 3.5, "text": "겹친 발화"}], turns, 4)[0]
        self.assertTrue(real["overlap"])
        self.assertEqual(real["local_speaker_id"], "unknown")
        tail = align([{"start": 0.5, "end": 2.0, "text": "끝 경계"}], turns, 4)[0]
        self.assertFalse(tail["overlap"])
        self.assertEqual(tail["local_speaker_id"], "A")
        exact = align([{"start": 2.0, "end": 3.0, "text": "정확히 2 ms"}], [{"start": 0, "end": 2.002, "speaker": "A"}, {"start": 2.0, "end": 3, "speaker": "B"}], 3)[0]
        self.assertEqual(exact["local_speaker_id"], "B")
        middle = align([{"start": 1.0, "end": 2.0, "text": "가운데 겹침"}], [{"start": 0, "end": 3, "speaker": "A"}, {"start": 1.5, "end": 1.501, "speaker": "unknown", "overlap": True}], 3)[0]
        self.assertTrue(middle["overlap"])
        short = align([{"start": 1.0, "end": 1.001, "text": "짧음"}], [{"start": 0, "end": 1.0015, "speaker": "A"}], 2)[0]
        self.assertEqual(short["local_speaker_id"], "A")

    def test_overlap_and_unknown_remain_uncertain(self):
        source = [{"start": 0, "end": 2, "text": "겹침"}]
        turns = [{"start": 0, "end": 2, "speaker": "A"}, {"start": 0, "end": 2, "speaker": "B"}]
        result = align(source, turns, 2)[0]
        self.assertTrue(result["overlap"])
        self.assertEqual(result["local_speaker_id"], "unknown")
        self.assertEqual(result["review_status"], "uncertain")

    def test_silence_and_corruption_rejected_without_touching_original(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "original.wav"
            with wave.open(str(original), "wb") as output:
                output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                output.writeframes(b"\0" * 32000)
            before = original.read_bytes()
            with self.assertRaises(WorkerError) as failure:
                prepare_audio(original, root / "prepared.wav")
            self.assertEqual(failure.exception.code, "silent_audio")
            self.assertEqual(original.read_bytes(), before)
            original.write_bytes(b"invalid")
            with self.assertRaises(WorkerError) as failure:
                prepare_audio(original, root / "prepared.wav")
            self.assertEqual(failure.exception.code, "decode_failed")

    def test_profile_threshold_margin_and_model_revision(self):
        one = {"person_id": "one", "model_revision": "v1", "embeddings": [[1.0, 0.0]]}
        two = {"person_id": "two", "model_revision": "v1", "embeddings": [[0.99, 0.01]]}
        self.assertEqual(match([1, 0], [one], "v1"), "one")
        self.assertIsNone(match([0, 1], [one], "v1"))
        self.assertIsNone(match([1, 0], [one, two], "v1"))
        self.assertIsNone(match([1, 0], [one], "v2"))


class GenerationTests(unittest.TestCase):
    def test_explicit_new_web_generation_can_regenerate_but_same_lease_recovers_cache(self):
        config = defaults({"generation_mode": "api", "api_model": "synthetic", "api_max_cost_usd": 100,
                           "openai_api_key": "synthetic-key", "api_prices_per_million": {"input": 1, "output": 1}})
        output = {"content": BODY, "evidence": [{"claim": "테스트 결정", "segment_ids": [SEGMENT_ID]}]}
        with tempfile.TemporaryDirectory() as directory, patch("local_worker.generation.api_generate", return_value=(output, "synthetic", {"input_tokens": 10, "output_tokens": 10})) as api:
            for generation in [1, 1, 2, 2]:
                generate({**job(), "generation": generation}, Path(directory), config)
            self.assertEqual(api.call_count, 2)

    def test_paid_invalid_output_keeps_usage_and_cumulative_retry_budget(self):
        config = defaults({"generation_mode": "api", "api_model": "synthetic", "api_max_cost_usd": 1,
                           "openai_api_key": "synthetic-key", "api_prices_per_million": {"input": 1, "output": 1}})
        with tempfile.TemporaryDirectory() as directory, patch("local_worker.generation._api_policy", return_value=("synthetic-key", "synthetic", b"{}", 0.6)), patch("local_worker.generation.api_generate", return_value=({"content": "invalid paid output", "evidence": []}, "synthetic", {"input_tokens": 250000, "output_tokens": 250000})) as api:
            root = Path(directory)
            with self.assertRaises(WorkerError) as first:
                generate(job(), root, config)
            self.assertEqual(first.exception.code, "invalid_minutes")
            ledger = read_json(root / "api-budget.json")
            self.assertEqual(ledger["attempts"][0]["usage_based_upper_bound_usd"], 0.5)
            outcome = read_json(next(root.glob("generation-*.json")))
            self.assertEqual(outcome["usage"]["output_tokens"], 250000)
            with self.assertRaises(WorkerError) as second:
                generate(job(), root, config)
            self.assertEqual(second.exception.code, "api_cumulative_cost_limit")
            self.assertEqual(api.call_count, 1)

    def test_remote_site_cannot_use_subscription_or_cached_local_result(self):
        with tempfile.TemporaryDirectory() as directory, patch("local_worker.generation.codex_generate") as codex:
            for url in ["https://private.chatgpt.site", "http://127.0.0.1:5173"]:
                with self.assertRaises(WorkerError) as failure:
                    generate(job(), Path(directory), defaults({"base_url": url, "generation_mode": "local_codex"}))
                self.assertEqual(failure.exception.code, "hosted_subscription_unsupported")
            codex.assert_not_called()

    def test_personal_local_is_explicit_and_model_change_changes_cache(self):
        value = {"content": BODY, "evidence": [{"claim": "테스트 결정", "segment_ids": [SEGMENT_ID]}]}
        with tempfile.TemporaryDirectory() as directory, patch("local_worker.generation.codex_generate", return_value=(value, "test", {})) as codex:
            config = defaults({"generation_mode": "local_codex", "codex_model": "first"})
            draft, _ = generate(job(), Path(directory), config, personal_local=True)
            generate(job(), Path(directory), config, personal_local=True)
            self.assertEqual(codex.call_count, 1)
            generate(job(), Path(directory), {**config, "codex_model": "second"}, personal_local=True)
            self.assertEqual(codex.call_count, 2)
            self.assertTrue(draft["content"].startswith("# [시험 고객] 미팅 회의록 (261028)"))

    def test_unknown_generation_cannot_be_retried_with_new_model(self):
        with tempfile.TemporaryDirectory() as directory, patch("local_worker.generation.api_generate", side_effect=WorkerError("api_unknown", "test", "unknown_outcome")) as api:
            config = defaults({"generation_mode": "api", "api_model": "first", "openai_api_key": "synthetic-key",
                               "api_max_cost_usd": 100, "api_prices_per_million": {"input": 1, "output": 1}})
            for model in ["first", "second"]:
                with self.assertRaises(WorkerError) as failure:
                    generate(job(), Path(directory), {**config, "api_model": model})
                self.assertEqual(failure.exception.state, "unknown_outcome")
            self.assertEqual(api.call_count, 1)

    def test_api_no_key_or_cost_cap_never_calls_network(self):
        from local_worker.generation import api_generate
        with patch("local_worker.generation.request.urlopen") as network, patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(WorkerError) as failure:
                api_generate("test", {})
            self.assertEqual(failure.exception.state, "needs_setup")
            network.assert_not_called()

    def test_all_three_template_variants_and_metadata(self):
        customer = job()
        validate_body(BODY, customer)
        developer = {**customer, "developer_requests": True}
        validate_body(BODY + "\n## 4. 개발팀 답변 요청사항\n\n| No. | 구분 | 개발팀 확인 및 결정 요청사항 | 개발팀 답변 |\n| --- | --- | --- | --- |", developer)
        weekly = {**customer, "meeting_type": "weekly"}
        validate_body("## 1. 회의 결론\n\n## 2. 담당자별 업무\n\n### 담당자\n\n- 업무\n\n## 3. 주요 일정", weekly)
        self.assertEqual(missing_metadata(weekly), ["week"])
        customer["metadata"]["datetime"] = ""
        self.assertIn("(미확인)", title_of(customer))
        customer["metadata"]["datetime"] = "2026-10-27T23:30:00Z"
        self.assertIn("261028", title_of(customer))


class NotionTests(unittest.TestCase):
    def test_typed_metadata_real_blocks_and_idempotent_export(self):
        fake = FakeNotion()
        with tempfile.TemporaryDirectory() as directory:
            first = export(job(), defaults({}), reviewed=True, client=fake, state_root=Path(directory))
            second = export(job(), defaults({}), reviewed=True, client=fake, state_root=Path(directory))
            self.assertEqual(first, second)
            self.assertEqual(len(fake.pages), 1)
            self.assertEqual(fake.pages[0]["properties"]["일시"]["date"]["start"], "2026-10-28T09:30:00+09:00")
            self.assertEqual(fake.blocks[0]["type"], "heading_1")
            self.assertEqual(fake.blocks[0]["heading_1"]["rich_text"][0]["text"]["content"], "1. 회의 결론")
            self.assertEqual(len([block for block in fake.blocks if block["type"] == "table"]), 2)

    def test_uncertain_create_and_append_never_auto_retry(self):
        for stage in ["create", "append"]:
            fake = FakeNotion(stage)
            with tempfile.TemporaryDirectory() as directory:
                for _ in range(2):
                    with self.assertRaises(WorkerError) as failure:
                        export(job(), defaults({}), reviewed=True, client=fake, state_root=Path(directory))
                    self.assertEqual(failure.exception.state, "unknown_outcome")
                self.assertEqual(len(fake.pages), 1)
                self.assertEqual(len([call for call in fake.calls if call[0] == "PATCH"]), int(stage == "append"))

    def test_review_missing_metadata_stale_revision_and_target_allowlist(self):
        cases = [(job(), False), ({**job(), "transcript_revision": 3}, True), ({**job(), "meeting_type": "weekly"}, True)]
        for item, reviewed in cases:
            with tempfile.TemporaryDirectory() as directory, self.assertRaises(WorkerError):
                export(item, defaults({}), reviewed=reviewed, client=FakeNotion(), state_root=Path(directory))

    def test_changed_draft_with_same_revision_rejected(self):
        fake = FakeNotion()
        with tempfile.TemporaryDirectory() as directory:
            export(job(), defaults({}), reviewed=True, client=fake, state_root=Path(directory))
            changed = job()
            changed["draft"]["content"] += "\n수정됨"
            with self.assertRaises(WorkerError) as failure:
                export(changed, defaults({}), reviewed=True, client=fake, state_root=Path(directory))
            self.assertEqual(failure.exception.code, "export_revision_reused")

    def test_concurrent_export_cannot_create_two_pages(self):
        fake, entered, release, errors = FakeNotion(), threading.Event(), threading.Event(), []
        original = fake.call
        def blocking(method, path, body=None):
            if method == "POST" and path == "/pages":
                entered.set()
                release.wait(5)
            return original(method, path, body)
        fake.call = blocking
        with tempfile.TemporaryDirectory() as directory:
            def first():
                try:
                    export(job(), defaults({}), reviewed=True, client=fake, state_root=Path(directory))
                except Exception as exc:
                    errors.append(exc)
            thread = threading.Thread(target=first)
            thread.start()
            self.assertTrue(entered.wait(5))
            try:
                with self.assertRaises(WorkerError) as failure:
                    export(job(), defaults({}), reviewed=True, client=fake, state_root=Path(directory))
                self.assertEqual(failure.exception.code, "worker_running")
            finally:
                release.set()
                thread.join(5)
            self.assertFalse(errors)
            self.assertEqual(len(fake.pages), 1)


class RunnerOutputModeTests(unittest.TestCase):
    def test_transcript_job_cannot_generate_even_when_pending_is_minutes(self):
        from local_worker.runner import Worker
        with tempfile.TemporaryDirectory() as directory:
            client = Mock()
            client.result.side_effect = lambda current, **values: values
            worker = Worker(defaults({"workspace": directory}), client)
            task = {**job(), "output_mode": "transcript", "pending": "minutes"}
            with patch("local_worker.runner.generate") as generate_minutes, patch.object(worker, "_keep_lease"):
                worker.process(task)
            generate_minutes.assert_not_called()
            client.download.assert_not_called()
            client.call.assert_not_called()
            result = client.result.call_args.kwargs
            self.assertEqual(result["state"], "failed")
            self.assertEqual(result["error"]["code"], "transcript_only_job")
            self.assertFalse(result["error"]["retryable"])
            self.assertEqual(result["stage"], "preparing")
            self.assertNotIn("draft", result)

    def test_minutes_and_legacy_jobs_keep_generation_path(self):
        from local_worker.runner import Worker
        for output_mode in (None, "minutes"):
            with self.subTest(output_mode=output_mode), tempfile.TemporaryDirectory() as directory:
                client = Mock()
                client.result.side_effect = lambda current, **values: values
                worker = Worker(defaults({"workspace": directory}), client)
                task = {**job(), "pending": "minutes"}
                if output_mode is not None:
                    task["output_mode"] = output_mode
                draft = {"content": "synthetic", "generation_mode": "api"}
                with patch("local_worker.runner.generate", return_value=(draft, {})) as generate_minutes, patch.object(worker, "_keep_lease"):
                    worker.process(task)
                generate_minutes.assert_called_once()
                self.assertEqual(client.result.call_args.kwargs["state"], "minutes_ready")

    def test_transcript_job_finishes_after_speech_without_generation(self):
        from local_worker.runner import Worker
        with tempfile.TemporaryDirectory() as directory:
            client = Mock()
            client.result.side_effect = lambda current, **values: values
            worker = Worker(defaults({"workspace": directory, "speech_mode": "local"}), client)
            task = {**job(), "output_mode": "transcript", "pending": "transcribe"}
            asr = {"segments": [{"start": 0, "end": 2, "text": "시험 전사", "words": []}], "duration": 2, "mode": "local/test"}
            with patch("local_worker.runner.generate") as generate_minutes, patch.object(worker, "_keep_lease"), \
                 patch("local_worker.runner.prepare_audio", return_value={"duration": 2}), \
                 patch("local_worker.runner.transcribe", return_value=asr) as speech, \
                 patch("local_worker.runner.diarize", return_value=[{"start": 0, "end": 2, "speaker": "speaker_00"}]), \
                 patch("local_worker.runner.profiles.suggest"):
                worker.process(task)
            speech.assert_called_once()
            generate_minutes.assert_not_called()
            result = client.result.call_args.kwargs
            self.assertEqual(result["state"], "transcript_ready")
            self.assertTrue(result["speech_complete"])
            self.assertTrue(result["diarization_complete"])
            self.assertEqual(result["segments"][0]["text"], "시험 전사")
            self.assertNotIn("draft", result)

    def test_claimed_term_list_reaches_only_the_api_transcriber(self):
        from local_worker.runner import Worker
        for claim, expected in [({"speech_keywords": ["로깅", "DLP"]}, ["로깅", "DLP"]), ({}, None)]:
            with self.subTest(claim=claim), tempfile.TemporaryDirectory() as directory:
                client = Mock()
                client.result.side_effect = lambda current, **values: values
                client.call.side_effect = lambda method, path, *args, **kwargs: {"job": {**job(), "output_mode": "transcript", "pending": "transcribe"}, **claim} if path == "/api/worker/claim" else {}
                worker = Worker(defaults({"workspace": directory, "speech_mode": "api", "openai_api_key": "synthetic-test-key", "api_speech_model": "gpt-transcribe",
                                          "api_speech_price_per_minute": 0.01, "api_max_cost_usd": 1}), client)
                asr = {"segments": [{"start": 0, "end": 2, "text": "시험 전사", "words": [], "timing_provenance": "audio_clip_bounds"}], "duration": 2, "mode": "api/test"}
                with patch.dict("os.environ", {}, clear=True), patch.object(worker, "_keep_lease"),                      patch("local_worker.runner.prepare_audio", return_value={"duration": 2}),                      patch("local_worker.api_speech.transcribe", return_value=asr) as speech,                      patch("local_worker.runner.diarize", return_value=[{"start": 0, "end": 2, "speaker": "speaker_00"}]),                      patch("local_worker.runner.profiles.suggest"), patch.object(worker, "process_profiles"):
                    worker.run(once=True)
                self.assertEqual(speech.call_args.kwargs["keywords"], expected)
                result = client.result.call_args.kwargs
                self.assertEqual(result["state"], "transcript_ready")
                self.assertNotIn("로깅", json.dumps(result, ensure_ascii=False))


class StorageTests(unittest.TestCase):
    def test_worker_identifies_itself_and_preserves_both_auth_headers(self):
        from local_worker.client import SiteClient
        config = {"device_token": "synthetic-device", "sites_dispatch_token": "synthetic-gateway"}
        headers = SiteClient(config).headers()
        self.assertEqual(headers["User-Agent"], "CodexMeetingLocalWorker/1.0")
        self.assertEqual(headers["Authorization"], "Bearer synthetic-device")
        self.assertEqual(headers["OAI-Sites-Authorization"], "synthetic-gateway")
        self.assertEqual(config, {"device_token": "synthetic-device", "sites_dispatch_token": "synthetic-gateway"})

    def test_lost_lease_terminates_child_before_late_output(self):
        from local_worker.native import run_isolated
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "late-output"
            started = time.monotonic()
            def check():
                if time.monotonic() - started > 0.2:
                    raise WorkerError("lease_lost", "test", "cancelled")
            script = "import json,sys,time,pathlib; t=json.load(sys.stdin); time.sleep(2); pathlib.Path(t['marker']).write_text('late')"
            with self.assertRaises(WorkerError) as failure:
                run_isolated([sys.executable, "-c", script], {"marker": str(marker)}, check, timeout=5)
            self.assertEqual(failure.exception.code, "lease_lost")
            self.assertLess(time.monotonic() - started, 2)
            self.assertFalse(marker.exists())

    def test_diarization_child_receives_only_nonsecret_local_parameters(self):
        from local_worker.speech import diarize
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "diarization.json"
            def child(task, check, timeout):
                self.assertTrue(set(task["config"]) <= {"diarization_model", "embedding_model", "cpu_threads", "cluster_threshold", "global_cluster_threshold", "diarization_strategy", "diarization_block_seconds", "diarization_context_seconds"})
                self.assertNotIn("synthetic-secret", json.dumps(task))
                atomic_json(output, {"complete": True, "turns": []})
                return 0
            with patch("local_worker.native.diarization_child", side_effect=child):
                self.assertEqual(diarize(root / "audio.wav", output, defaults({"device_token": "synthetic-secret", "openai_api_key": "synthetic-secret"})), [])

    def test_gil_blocking_native_child_does_not_starve_parent_lease(self):
        from local_worker.native import run_isolated
        from local_worker.runner import Worker
        worker = Worker(defaults({}), client=object())
        active = {"id": ID, "lease_id": str(uuid.uuid4()), "generation": 1}
        worker.active = active
        renewals = []
        worker.heartbeat = lambda job=None: renewals.append(time.monotonic())
        stop = threading.Event()
        keeper = threading.Thread(target=worker._keep_lease, args=(active, stop, 0.05))
        keeper.start()
        # PyDLL intentionally holds the child's Python GIL for the entire native sleep.
        script = "import ctypes,os,json,sys; json.load(sys.stdin); lib=ctypes.PyDLL('kernel32' if os.name=='nt' else None); (lib.Sleep(900) if os.name=='nt' else lib.usleep(900000))"
        try:
            self.assertEqual(run_isolated([sys.executable, "-c", script], {}, worker.check, timeout=5), 0)
        finally:
            stop.set()
            keeper.join(2)
        self.assertGreaterEqual(len(renewals), 5)
        self.assertLess(max(b - a for a, b in zip(renewals, renewals[1:])), 0.5)

    def test_old_lease_thread_cannot_cancel_new_job(self):
        from local_worker.runner import Worker
        worker = Worker(defaults({}), client=object())
        old, new = {"id": "old"}, {"id": "new"}
        worker.active = new
        worker.heartbeat = lambda job=None: (_ for _ in ()).throw(WorkerError("lease_lost", "test", "cancelled"))
        class Once:
            def wait(self, seconds):
                return False
        worker._keep_lease(old, Once())
        self.assertIsNone(worker.lease_failure)

    def test_explicit_worker_home_roundtrip_and_relative_path_rejected(self):
        import os
        from local_worker.core import save_config, load_config, config_path, local_root
        if os.name != "nt":
            self.skipTest("Windows DPAPI only")
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"MEETING_WORKER_HOME": directory}):
            save_config({"site_url": "http://127.0.0.1:5173", "device_token": "synthetic-" * 7})
            self.assertEqual(config_path(), Path(directory).resolve() / "config.dpapi")
            self.assertEqual(load_config()["base_url"], "http://127.0.0.1:5173")
            self.assertTrue(Path(load_config()["diarization_model"]).is_relative_to(Path(directory)))
        with patch.dict("os.environ", {"MEETING_WORKER_HOME": "relative-runtime"}):
            with self.assertRaises(WorkerError) as failure:
                local_root()
            self.assertEqual(failure.exception.code, "invalid_worker_home")

    def test_checkpoint_replace_retries_preserving_old_valid_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.json"
            atomic_json(path, {"complete": False})
            original = Path.replace
            failures = []
            def transient(source, destination):
                if not failures:
                    failures.append(True)
                    self.assertEqual(read_json(path), {"complete": False})
                    raise PermissionError("simulated Windows sharing contention")
                return original(source, destination)
            with patch.object(Path, "replace", transient):
                atomic_json(path, {"complete": True})
            self.assertEqual(read_json(path), {"complete": True})

    def test_dpapi_roundtrip_does_not_store_plaintext(self):
        import os
        from local_worker.core import save_config, load_config
        if os.name != "nt":
            self.skipTest("Windows DPAPI only")
        with tempfile.TemporaryDirectory() as directory, patch("local_worker.core.config_path", return_value=Path(directory) / "config.dpapi"):
            save_config({"site_url": "http://127.0.0.1:5173", "device_token": "test-credential-" * 5, "sites_bypass_token": "synthetic-bypass"})
            saved = (Path(directory) / "config.dpapi").read_bytes()
            self.assertNotIn(b"synthetic-bypass", saved)
            self.assertEqual(load_config()["sites_dispatch_token"], "synthetic-bypass")

    def test_path_traversal_and_single_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(WorkerError):
                job_directory(root, "../../arbitrary")
            self.assertTrue(job_directory(root, ID).is_relative_to(root))
            with SingleWorker(root / "state"):
                with self.assertRaises(WorkerError):
                    with SingleWorker(root / "state"):
                        pass
            with SingleWorker(root / "state"):
                pass



class ProgressHeartbeatTests(unittest.TestCase):
    def worker(self, directory, replies):
        from local_worker.runner import Worker
        client = Mock()
        sent = []

        def call(method, path, body=None, **kwargs):
            sent.append(copy.deepcopy(body))
            answer = replies.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        client.call.side_effect = call
        worker = Worker(defaults({"workspace": directory}), client)
        worker.capabilities = lambda: {}
        return worker, sent

    def test_progress_waits_for_the_web_to_accept_it(self):
        with tempfile.TemporaryDirectory() as directory:
            worker, sent = self.worker(directory, [{}, {"accepts_progress": True}, {"accepts_progress": True}])
            lease = {"id": ID, "lease_id": "lease", "generation": 1}
            worker.set_progress("transcribing", 4, 10)
            worker.heartbeat(lease)
            worker.heartbeat(lease)
            self.assertNotIn("progress", sent[0])
            self.assertNotIn("progress", sent[1])
            self.assertTrue(worker.progress_supported)
            worker.heartbeat(lease)
            self.assertEqual(sent[2]["progress"], {"stage": "transcribing", "done": 4, "total": 10})

    def test_rejected_progress_is_dropped_without_losing_the_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            # The retry's reply still says the web accepts progress (it refused only this value).
            worker, sent = self.worker(directory, [WorkerError("site_http_400", "refused", "failed", True), {"accepts_progress": True}, {"accepts_progress": True}])
            worker.progress_supported = True
            worker.set_progress("diarizing", 1, 3)
            lease = {"id": ID, "lease_id": "lease", "generation": 1}
            worker.heartbeat(lease)
            self.assertIn("progress", sent[0])
            self.assertNotIn("progress", sent[1])
            worker.heartbeat(lease)
            self.assertEqual(len(sent), 3, "one refusal must not turn every later heartbeat into two calls")
            self.assertNotIn("progress", sent[2])
            self.assertTrue(worker.progress_rejected)

    def test_no_progress_without_a_lease_and_other_errors_still_raise(self):
        with tempfile.TemporaryDirectory() as directory:
            worker, sent = self.worker(directory, [{"accepts_progress": True}, WorkerError("lease_lost", "gone", "cancelled")])
            worker.progress_supported = True
            worker.set_progress("diarizing", 1, 3)
            worker.heartbeat()
            self.assertNotIn("progress", sent[0])
            with self.assertRaises(WorkerError):
                worker.heartbeat({"id": ID, "lease_id": "lease", "generation": 1})
            self.assertEqual(len(sent), 2)

    def test_diarization_progress_reads_the_newest_block_folder(self):
        import os
        from local_worker.runner import diarization_progress
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertIsNone(diarization_progress(root))
            old, new = root / "diarization-blocks" / "old", root / "diarization-blocks" / "new"
            atomic_json(old / "progress.json", {"identity": {}, "completed_blocks": 9, "total_blocks": 9})
            atomic_json(new / "progress.json", {"identity": {}, "completed_blocks": 2, "total_blocks": 21})
            os.utime(old / "progress.json", (1_000_000, 1_000_000))
            self.assertEqual(diarization_progress(root), (2, 21))
            atomic_json(new / "progress.json", {"completed_blocks": 30, "total_blocks": 21})
            self.assertEqual(diarization_progress(root), (9, 9))

    def test_progress_payload_clamps_and_survives_a_broken_reader(self):
        with tempfile.TemporaryDirectory() as directory:
            worker, _ = self.worker(directory, [])
            worker.set_progress("diarizing", reader=lambda: (_ for _ in ()).throw(OSError("busy")))
            self.assertIsNone(worker.progress_payload(), "an unreadable checkpoint sends nothing rather than 0/0")
            worker.set_progress("diarizing", reader=lambda: None)
            self.assertIsNone(worker.progress_payload())
            worker.set_progress("diarizing", reader=lambda: (3, 21))
            self.assertEqual(worker.progress_payload(), {"stage": "diarizing", "done": 3, "total": 21})
            worker.set_progress("transcribing", 12, 10)
            self.assertEqual(worker.progress_payload(), {"stage": "transcribing", "done": 10, "total": 10})
            worker.progress = None
            self.assertIsNone(worker.progress_payload())

    def test_process_reports_each_step_and_clears_progress_at_the_end(self):
        from local_worker.runner import Worker
        with tempfile.TemporaryDirectory() as directory:
            client = Mock()
            client.result.side_effect = lambda current, **values: values
            worker = Worker(defaults({"workspace": directory, "speech_mode": "local"}), client)
            seen, rejected = [], []
            asr = {"segments": [{"start": 0, "end": 2, "text": "시험 전사", "words": []}], "duration": 2, "mode": "local/test"}

            def speech(*args, progress, **kwargs):
                progress(1, 2)
                seen.append(worker.progress_payload())
                progress(2, 2)
                seen.append(worker.progress_payload())
                return asr

            def speakers(*args, **kwargs):
                seen.append(worker.progress["stage"])
                rejected.append(worker.progress_rejected)
                return [{"start": 0, "end": 2, "speaker": "speaker_00"}]
            worker.progress_rejected = True  # left over from an earlier job
            with patch.object(worker, "_keep_lease"), patch("local_worker.runner.prepare_audio", return_value={"duration": 2}), \
                 patch("local_worker.runner.transcribe", side_effect=speech), patch("local_worker.runner.diarize", side_effect=speakers), \
                 patch("local_worker.runner.profiles.suggest"):
                worker.process({**job(), "output_mode": "transcript", "pending": "transcribe"})
            self.assertEqual(seen, [{"stage": "transcribing", "done": 1, "total": 2}, {"stage": "transcribing", "done": 2, "total": 2}, "diarizing"])
            self.assertEqual(rejected, [False], "a new job may report progress again")
            self.assertEqual(client.result.call_args.kwargs["state"], "transcript_ready")
            self.assertIsNone(worker.progress)


if __name__ == "__main__":
    unittest.main()
