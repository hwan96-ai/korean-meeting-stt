"""Pipelined transcription: plan equivalence, billing safety and resume, with a fake child and provider."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import random
from tempfile import TemporaryDirectory
import unittest
import unittest.mock
from unittest.mock import patch
import wave

from local_worker import api_speech, bounded_diarization, pipeline_speech
from local_worker.core import WorkerError, atomic_json, read_json, sha256

RATE = 16000


def write_audio(path: Path, seconds: float):
    # Random noise: never digital silence, and no two clips share bytes.
    data = random.Random(3).randbytes(2 * round(seconds * RATE))
    with wave.open(str(path), "wb") as w:
        w.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
        w.writeframes(data)


def random_turns(seconds: float, seed: int, speakers=3):
    """Speech turns with gaps, short slivers, long monologues and some overlap."""
    rng, turns = random.Random(seed), []
    t = rng.uniform(0, 2)
    while t < seconds:
        length = rng.choice([0.3, 0.6, 1.2, 3, 7, 15, 45])
        speaker = f"S{rng.randrange(speakers)}"
        turns.append({"start": round(t, 3), "end": round(min(seconds, t + length), 3), "speaker": speaker})
        t += length + rng.choice([-0.4, 0.0, 0.1, 0.3, 0.8, 2.0, 5.0])
    return [turn for turn in turns if turn["end"] > turn["start"]]


class FakeDiarization:
    """Writes one finished block per poll, like the child, then the reconciled result."""

    def __init__(self, directory: Path, audio: Path, config: dict, truth: list[dict], blocks_per_poll=1, fail=None,
                 finish_with_last=False):
        """`fail`: "now" fails on the first poll; an int fails once that many blocks are done.
        `finish_with_last`: write the last block and exit in the same poll (the race the parent must survive)."""
        self.geometry = bounded_diarization.layout(audio, config)
        self.audio, self.finish_with_last = audio, finish_with_last
        self.output = directory / "diarization.json"
        self.truth, self.done, self.blocks_per_poll, self.fail = truth, 0, blocks_per_poll, fail
        self.block_dir = directory / "diarization-blocks" / self.geometry["digest"]
        self.block_dir.mkdir(parents=True, exist_ok=True)
        base = self.geometry["base_identity"]
        self.identity = {"segmentation": base["segmentation"], "embedding": base["embedding"], "threshold": base["native_threshold"],
                         "min_duration_on": base["min_duration_on"], "min_duration_off": base["min_duration_off"]}
        self.stopped = False

    def owned(self, index):
        first, last, left, right = bounded_diarization.block_bounds(self.geometry, index)
        rate = self.geometry["rate"]
        raw = []
        for turn in self.truth:
            start, end = max(turn["start"], left / rate), min(turn["end"], right / rate)
            if end > start:
                # Native labels are per block and not the true names.
                raw.append({"start": start - left / rate, "end": end - left / rate, "speaker": "spk" + turn["speaker"][1:]})
        return raw

    def write_block(self, index):
        _, _, left, right = bounded_diarization.block_bounds(self.geometry, index)
        clip = self.block_dir / f"block-{index:04d}.wav"
        with wave.open(str(self.audio), "rb") as source, wave.open(str(clip), "wb") as target:
            target.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
            source.setpos(left)
            target.writeframes(source.readframes(right - left))
        atomic_json(self.block_dir / f"block-{index:04d}.json", {"identity": {**self.identity, "audio": sha256(clip)}, "complete": True, "turns": self.owned(index)})

    def poll(self):
        if self.fail == "now" or (isinstance(self.fail, int) and self.done >= self.fail):
            atomic_json(self.output.with_suffix(".error.json"), {"code": "diarization_native_error", "message": "x", "state": "failed", "retryable": True})
            return 2
        total = self.geometry["total_blocks"]
        if self.done < total:
            for _ in range(self.blocks_per_poll):
                if self.done < total:
                    self.write_block(self.done)
                    self.done += 1
            if not (self.finish_with_last and self.done == total):
                return None
        all_owned = []
        for index in range(total):
            all_owned += bounded_diarization.finished_block(self.output, self.geometry, index)
        vectors = {f"S{n}": [1.0 if k == n else 0.0 for k in range(4)] for n in range(4)}
        features = {label: vectors["S" + label.split("/spk")[1]] for label in {turn["speaker"] for turn in all_owned}}
        turns, _ = bounded_diarization.reconcile(all_owned, features, 0.6)
        atomic_json(self.output, {"identity": {}, "complete": True, "turns": turns})
        return 0

    def terminate(self):
        self.stopped = True

    def wait(self, timeout=None):
        return 0


class FakeProvider:
    def __init__(self, fail_at=None):
        self.calls, self.fail_at = [], fail_at

    def __call__(self, body, content_type, key, timeout):
        boundary = content_type.split("boundary=")[1].encode()
        audio = next(part for part in body.split(b"--" + boundary) if b'name="file"' in part).split(b"\r\n\r\n", 1)[1][:-2]
        digest = hashlib.sha256(audio).hexdigest()
        self.calls.append(digest)
        if self.fail_at is not None and len(self.calls) == self.fail_at:
            raise TimeoutError("synthetic timeout")
        with wave.open(__import__("io").BytesIO(audio)) as w:
            seconds = w.getnframes() / w.getframerate()
        return json.dumps({"text": "t" + digest[:8], "usage": {"type": "duration", "seconds": math.ceil(seconds)}}).encode(), "req_synthetic"


def config_for(root: Path, **extra):
    models = root / "models"
    models.mkdir(exist_ok=True)
    (models / "seg.onnx").write_bytes(b"segmentation")
    (models / "emb.onnx").write_bytes(b"embedding")
    home = root / "home"
    home.mkdir(exist_ok=True)
    return {"execution_location": "server", "worker_home": str(home), "speech_mode": "api", "speech_pipeline": True,
            "api_speech_model": "gpt-transcribe", "openai_api_key": "sk-synthetic-not-real", "api_credit_only_ack": True,
            "api_speech_price_per_minute": 0.0045, "api_max_cost_usd": 50.0, "cpu_threads": 2,
            "diarization_model": str(models / "seg.onnx"), "embedding_model": str(models / "emb.onnx"),
            "diarization_strategy": "bounded_global", "diarization_block_seconds": 30, "diarization_context_seconds": 5,
            "cluster_threshold": 0.7, "global_cluster_threshold": 0.6, **extra}


def one_pass_plan(fake: FakeDiarization, policy_seconds=30):
    planner = pipeline_speech.Planner(RATE, fake.geometry["frames"], policy_seconds)
    for index in range(fake.geometry["total_blocks"]):
        first, last, _, _ = bounded_diarization.block_bounds(fake.geometry, index)
        planner.add_block(bounded_diarization.finished_block(fake.output, fake.geometry, index), first, last, freeze=False)
    planner.finish()
    return planner.plan


class PlannerTests(unittest.TestCase):
    def test_incremental_freezing_equals_one_pass_for_many_random_meetings(self):
        for seed in range(40):
            with self.subTest(seed=seed), TemporaryDirectory() as directory:
                root = Path(directory)
                seconds = 95 + seed
                write_audio(root / "a.wav", seconds)
                config = config_for(root)
                fake = FakeDiarization(root, root / "a.wav", config, random_turns(seconds, seed))
                while fake.poll() is None:
                    pass
                planner = pipeline_speech.Planner(RATE, fake.geometry["frames"], 30)
                for index in range(fake.geometry["total_blocks"]):
                    first, last, _, _ = bounded_diarization.block_bounds(fake.geometry, index)
                    planner.add_block(bounded_diarization.finished_block(fake.output, fake.geometry, index), first, last)
                planner.finish()
                reference = one_pass_plan(fake)
                self.assertEqual(planner.plan, reference)
                self.assertEqual(reference[0]["start_frame"], 0)
                self.assertEqual(reference[-1]["end_frame"], fake.geometry["frames"])
                for a, b in zip(reference, reference[1:]):
                    self.assertEqual(a["end_frame"], b["start_frame"])
                minimum, maximum = math.ceil(api_speech.MIN_CLIP_SECONDS * RATE), 30 * RATE
                self.assertTrue(all(minimum <= clip["end_frame"] - clip["start_frame"] <= maximum for clip in reference))

    def test_labels_follow_the_whole_file_rule_after_reconciliation(self):
        turns = [{"start": 0.0, "end": 0.2, "speaker": "speaker_000"}, {"start": 0.2, "end": 2.0, "speaker": "speaker_001"},
                 {"start": 3.0, "end": 5.0, "speaker": "speaker_000"}, {"start": 4.0, "end": 4.5, "speaker": "unknown", "overlap": True}]
        plan = [{"start_frame": 0, "end_frame": 32000}, {"start_frame": 32000, "end_frame": 48000},
                {"start_frame": 48000, "end_frame": 64000}, {"start_frame": 64000, "end_frame": 96000}]
        labels = pipeline_speech.label_plan(plan, turns, 96000, RATE)
        # A 0.2 s speaker and a 1.8 s speaker in one clip stay unknown (not given to the longer one).
        self.assertEqual(labels[0], {"local_speaker_id": "unknown", "overlap": False, "mixed_speakers": True})
        self.assertEqual(labels[1]["local_speaker_id"], "unknown")  # gap only
        self.assertEqual(labels[2], {"local_speaker_id": "speaker_000", "overlap": False})
        self.assertTrue(labels[3]["overlap"])
        self.assertEqual(labels[3]["local_speaker_id"], "unknown")

    def test_seam_turn_is_not_cut_where_the_one_pass_plan_would_not_cut(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_audio(root / "a.wav", 70)
            config = config_for(root)
            # One speaker talks across the 30 s block seam.
            fake = FakeDiarization(root, root / "a.wav", config, [{"start": 25.0, "end": 38.0, "speaker": "S1"}])
            while fake.poll() is None:
                pass
            plan = one_pass_plan(fake)
            self.assertIn(30 * RATE, [clip["end_frame"] for clip in plan])  # block-local labels differ per block
            labels = pipeline_speech.label_plan(plan, read_json(fake.output)["turns"], fake.geometry["frames"], RATE)
            seam = [label for clip, label in zip(plan, labels) if 25 * RATE <= clip["start_frame"] < 38 * RATE]
            self.assertEqual({label["local_speaker_id"] for label in seam}, {"speaker_000"})


class PipelineTests(unittest.TestCase):
    def run_job(self, root, config, truth, provider, check=lambda: None, blocks_per_poll=1, fake=None):
        fake = fake or FakeDiarization(root / "job", root / "a.wav", config, truth, blocks_per_poll)
        with patch("local_worker.api_speech._post", provider):
            return pipeline_speech.transcribe(root / "a.wav", root / "job", config, check, keywords=["시험"],
                                              start_child=lambda *a: fake, poll_seconds=0), fake

    def setup_job(self, directory, seconds=100, seed=5):
        root = Path(directory)
        (root / "job").mkdir()
        write_audio(root / "a.wav", seconds)
        return root, config_for(root), random_turns(seconds, seed)

    def test_pipelined_job_sends_each_frozen_clip_once_and_covers_the_timeline(self):
        with TemporaryDirectory() as directory:
            root, config, truth = self.setup_job(directory)
            provider = FakeProvider()
            (turns, result), fake = self.run_job(root, config, truth, provider)
            plan = read_json(root / "job" / pipeline_speech.STATE_NAME)["plan"]
            self.assertEqual(plan, one_pass_plan(fake))
            self.assertEqual(len(provider.calls), len(set(provider.calls)))
            self.assertEqual(len(provider.calls), len(plan))
            self.assertTrue(result["coverage_complete"])
            self.assertEqual(sum(c["end"] - c["start"] for c in result["coverage"]), fake.geometry["frames"] / RATE)
            timing = read_json(root / "job" / "speech-timing.json")
            self.assertEqual(timing["pipelined"]["requests"], len(plan))
            # Diagnostics hold numbers only.
            self.assertTrue(all(value is None or isinstance(value, (int, float)) for value in timing["pipelined"].values()))

    def test_requests_start_before_diarization_finishes(self):
        with TemporaryDirectory() as directory:
            root, config, truth = self.setup_job(directory, seconds=150)
            seen = []
            provider = FakeProvider()
            fake = FakeDiarization(root / "job", root / "a.wav", config, truth)

            def spy(*args):
                seen.append(fake.done)
                return provider(*args)
            self.run_job(root, config, truth, spy, fake=fake)
            self.assertLess(min(seen), fake.geometry["total_blocks"])

    def test_lease_loss_midway_resumes_without_resending_paid_clips(self):
        with TemporaryDirectory() as directory:
            root, config, truth = self.setup_job(directory)
            provider, calls = FakeProvider(), {"n": 0}

            def check():
                calls["n"] += 1
                if len(provider.calls) == 9:
                    raise WorkerError("lease_lost", "test", "cancelled")
            with self.assertRaises(WorkerError):
                self.run_job(root, config, truth, provider, check)
            first = list(provider.calls)
            (_, result), fake = self.run_job(root, config, truth, provider)
            plan = read_json(root / "job" / pipeline_speech.STATE_NAME)["plan"]
            self.assertEqual(len(provider.calls), len(plan))
            self.assertEqual(provider.calls[:len(first)], first)
            self.assertEqual(len(set(provider.calls)), len(provider.calls))
            self.assertTrue(result["coverage_complete"])

    def test_ambiguous_outcome_trips_the_circuit_and_never_resends(self):
        with TemporaryDirectory() as directory:
            root, config, truth = self.setup_job(directory)
            provider = FakeProvider(fail_at=4)
            with self.assertRaises(WorkerError) as caught:
                self.run_job(root, config, truth, provider)
            self.assertEqual(caught.exception.state, "unknown_outcome")
            again = FakeProvider()
            with self.assertRaises(WorkerError):
                self.run_job(root, config, truth, again)
            self.assertEqual(again.calls, [])

    def test_job_keeps_the_path_it_started_with(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = {"speech_pipeline": True, "execution_location": "server", "speech_mode": "api", "api_speech_model": "gpt-transcribe"}
            self.assertTrue(pipeline_speech.selected(config, root))
            (root / pipeline_speech.LEGACY_STATE_NAME).write_text("{}")
            self.assertFalse(pipeline_speech.selected(config, root))
            (root / pipeline_speech.LEGACY_STATE_NAME).unlink()
            (root / pipeline_speech.STATE_NAME).write_text("{}")
            self.assertTrue(pipeline_speech.selected({"speech_pipeline": False}, root))
            (root / pipeline_speech.STATE_NAME).unlink()
            self.assertFalse(pipeline_speech.selected({"speech_pipeline": False, "speech_mode": "api"}, root))
            self.assertFalse(pipeline_speech.selected({"speech_pipeline": True, "speech_mode": "local"}, root))
            self.assertFalse(pipeline_speech.selected({**config, "execution_location": "local"}, root))
            self.assertFalse(pipeline_speech.selected({"speech_pipeline": True, "speech_mode": "api", "api_speech_model": "whisper-1"}, root))

    def test_paid_attempts_without_their_checkpoint_block_the_job(self):
        with TemporaryDirectory() as directory:
            root, config, truth = self.setup_job(directory)
            atomic_json(root / "job" / "api-budget.json", {"attempts": [{"attempt_id": "x", "stage": "transcribing", "state": "responded", "charged_upper_bound_usd": 0.01}]})
            provider = FakeProvider()
            with self.assertRaises(WorkerError) as caught:
                self.run_job(root, config, truth, provider)
            self.assertEqual(caught.exception.code, "api_speech_checkpoint_invalid")
            self.assertEqual(provider.calls, [])

    def test_missing_models_fall_back_before_anything_is_planned(self):
        with TemporaryDirectory() as directory:
            root, config, truth = self.setup_job(directory)
            Path(config["diarization_model"]).unlink()
            with patch("local_worker.api_speech._post", FakeProvider()), self.assertRaises(pipeline_speech.FallBack):
                pipeline_speech.transcribe(root / "a.wav", root / "job", config, keywords=[], start_child=lambda *a: None)
            self.assertFalse((root / "job" / pipeline_speech.STATE_NAME).exists())

    def test_child_failure_stops_sending_and_keeps_the_frozen_prefix(self):
        with TemporaryDirectory() as directory:
            root, config, truth = self.setup_job(directory)
            fake = FakeDiarization(root / "job", root / "a.wav", config, truth)
            fake.fail = "now"
            provider = FakeProvider()
            with self.assertRaises(WorkerError) as caught:
                self.run_job(root, config, truth, provider, fake=fake)
            self.assertEqual(caught.exception.code, "diarization_native_error")
            self.assertEqual(provider.calls, [])


    def test_child_failure_after_paid_clips_keeps_them_and_resume_never_resends(self):
        with TemporaryDirectory() as directory:
            root, config, truth = self.setup_job(directory, seconds=150)
            provider = FakeProvider()
            fake = FakeDiarization(root / "job", root / "a.wav", config, truth, fail=3)
            with self.assertRaises(WorkerError):
                self.run_job(root, config, truth, provider, fake=fake)
            paid = list(provider.calls)
            self.assertTrue(paid)
            (_, result), _ = self.run_job(root, config, truth, provider)
            self.assertEqual(provider.calls[:len(paid)], paid)
            self.assertEqual(len(set(provider.calls)), len(provider.calls))
            self.assertTrue(result["coverage_complete"])

    def test_lease_loss_stops_the_child(self):
        with TemporaryDirectory() as directory:
            root, config, truth = self.setup_job(directory, seconds=300)
            provider = FakeProvider()
            fake = FakeDiarization(root / "job", root / "a.wav", config, truth)

            def check():
                if provider.calls:
                    raise WorkerError("lease_lost", "t", "cancelled")
            with self.assertRaises(WorkerError):
                self.run_job(root, config, truth, provider, check, fake=fake)
            self.assertTrue(fake.stopped)
            self.assertLess(fake.done, fake.geometry["total_blocks"])
            self.assertEqual(len(provider.calls), 1)

    def test_last_block_written_as_the_child_exits_is_still_read(self):
        with TemporaryDirectory() as directory:
            root, config, truth = self.setup_job(directory)
            fake = FakeDiarization(root / "job", root / "a.wav", config, truth, blocks_per_poll=2, finish_with_last=True)
            (_, result), _ = self.run_job(root, config, truth, FakeProvider(), fake=fake)
            self.assertTrue(result["coverage_complete"])

    def test_operator_no_result_confirmation_lets_a_pipelined_job_resume(self):
        with TemporaryDirectory() as directory:
            root, config, truth = self.setup_job(directory)
            with self.assertRaises(WorkerError):
                self.run_job(root, config, truth, FakeProvider(fail_at=4))
            ledger = read_json(root / "job" / "api-budget.json")
            unknown = [entry["attempt_id"] for entry in ledger["attempts"] if entry["state"] == "unknown_outcome"]
            self.assertEqual(len(unknown), 1)
            self.assertTrue(api_speech.resolve_no_result(root / "job", unknown[0], 50.0))
            from local_worker.api_circuit import ApiCircuit
            ApiCircuit(Path(config["worker_home"])).resume()
            provider = FakeProvider()
            (_, result), _ = self.run_job(root, config, truth, provider)
            plan = read_json(root / "job" / pipeline_speech.STATE_NAME)["plan"]
            # The confirmed clip and the rest are sent; the 3 paid ones never again.
            self.assertEqual(len(provider.calls), len(plan) - 3)
            self.assertTrue(result["coverage_complete"])

    def test_plans_the_whole_file_planner_would_refuse_fall_back_before_any_request(self):
        for seconds, chunk in [(100, 0.3), (100, 0.6), (100, 0.99)]:
            with self.subTest(chunk=chunk), TemporaryDirectory() as directory:
                root, _, truth = self.setup_job(directory, seconds=seconds)
                config = config_for(root, api_speech_chunk_seconds=chunk)
                provider = FakeProvider()
                with self.assertRaises(pipeline_speech.FallBack):
                    self.run_job(root, config, truth, provider)
                self.assertEqual(provider.calls, [])
                self.assertFalse((root / "job" / pipeline_speech.STATE_NAME).exists())
        with TemporaryDirectory() as directory:
            root, config, truth = self.setup_job(directory, seconds=60)
            provider = FakeProvider()
            with patch.object(pipeline_speech, "MAX_CLIPS", 100), self.assertRaises(pipeline_speech.FallBack):
                self.run_job(root, config, truth, provider)  # 60 s could need 120 clips of 0.5 s
            self.assertEqual(provider.calls, [])

    def test_a_cached_result_with_missing_block_files_falls_back_before_planning(self):
        with TemporaryDirectory() as directory:
            root, config, truth = self.setup_job(directory)
            fake = FakeDiarization(root / "job", root / "a.wav", config, truth)
            while fake.poll() is None:
                pass
            (fake.block_dir / "block-0002.json").unlink()
            provider = FakeProvider()
            with self.assertRaises(pipeline_speech.FallBack):
                self.run_job(root, config, truth, provider, fake=fake)
            self.assertEqual(provider.calls, [])

    def test_a_cached_result_with_a_missing_block_wav_falls_back_before_planning(self):
        with TemporaryDirectory() as directory:
            root, config, truth = self.setup_job(directory)
            fake = FakeDiarization(root / "job", root / "a.wav", config, truth)
            while fake.poll() is None:
                pass
            (fake.block_dir / "block-0001.wav").unlink()
            provider = FakeProvider()
            with self.assertRaises(pipeline_speech.FallBack):
                self.run_job(root, config, truth, provider, fake=fake)
            self.assertEqual(provider.calls, [])

    def test_paid_attempts_without_a_checkpoint_stop_before_any_fall_back(self):
        with TemporaryDirectory() as directory:
            root, _, truth = self.setup_job(directory)
            config = config_for(root, api_speech_chunk_seconds=0.8)  # would otherwise fall back
            atomic_json(root / "job" / "api-budget.json", {"attempts": [{"attempt_id": "x", "stage": "transcribing", "state": "responded", "charged_upper_bound_usd": 0.01}]})
            with self.assertRaises(WorkerError) as caught:
                self.run_job(root, config, truth, FakeProvider())
            self.assertEqual(caught.exception.code, "api_speech_checkpoint_invalid")

    def test_a_child_that_finished_in_time_is_not_a_timeout(self):
        with TemporaryDirectory() as directory:
            root, config, truth = self.setup_job(directory)
            config = {**config, "native_timeout": 0}
            fake = FakeDiarization(root / "job", root / "a.wav", config, truth, blocks_per_poll=100, finish_with_last=True)
            (_, result), _ = self.run_job(root, config, truth, FakeProvider(), fake=fake)
            self.assertTrue(result["coverage_complete"])

    def test_a_block_cut_from_another_place_in_the_original_is_refused(self):
        with TemporaryDirectory() as directory:
            root, config, truth = self.setup_job(directory)
            fake = FakeDiarization(root / "job", root / "a.wav", config, truth)
            fake.write_block(1)
            fake.write_block(2)
            for name in ("wav", "json"):
                one, two = fake.block_dir / f"block-0001.{name}", fake.block_dir / f"block-0002.{name}"
                one_bytes, two_bytes = one.read_bytes(), two.read_bytes()
                one.write_bytes(two_bytes)
                two.write_bytes(one_bytes)
            with self.assertRaises(WorkerError) as caught:
                bounded_diarization.finished_block(fake.output, fake.geometry, 1)
            self.assertEqual(caught.exception.code, "diarization_block_identity")

    def test_an_emptied_checkpoint_with_paid_attempts_never_replans(self):
        with TemporaryDirectory() as directory:
            root, config, truth = self.setup_job(directory)
            self.run_job(root, config, truth, FakeProvider())
            state = read_json(root / "job" / pipeline_speech.STATE_NAME)
            atomic_json(root / "job" / pipeline_speech.STATE_NAME, {**state, "plan": [], "clips": [], "complete": False})
            again = FakeProvider()
            with self.assertRaises(WorkerError) as caught:
                self.run_job(root, config, truth, again)
            self.assertEqual(caught.exception.code, "api_speech_checkpoint_invalid")
            self.assertEqual(again.calls, [])

    def test_a_changed_clip_length_setting_keeps_the_planned_length(self):
        with TemporaryDirectory() as directory:
            root, _, truth = self.setup_job(directory)
            config = config_for(root, api_speech_chunk_seconds=10)
            provider = FakeProvider()

            def check():
                if len(provider.calls) == 5:
                    raise WorkerError("lease_lost", "t", "cancelled")
            with self.assertRaises(WorkerError):
                self.run_job(root, config, truth, provider, check)
            (_, result), _ = self.run_job(root, config_for(root, api_speech_chunk_seconds=30), truth, provider)
            plan = read_json(root / "job" / pipeline_speech.STATE_NAME)["plan"]
            self.assertTrue(all(clip["end_frame"] - clip["start_frame"] <= 10 * RATE for clip in plan))
            self.assertEqual(len(set(provider.calls)), len(provider.calls))
            self.assertTrue(result["coverage_complete"])

    def test_a_block_result_that_does_not_match_its_audio_is_refused(self):
        with TemporaryDirectory() as directory:
            root, config, truth = self.setup_job(directory)
            fake = FakeDiarization(root / "job", root / "a.wav", config, truth)
            fake.write_block(0)
            value = read_json(fake.block_dir / "block-0000.json")
            atomic_json(fake.block_dir / "block-0000.json", {**value, "identity": {**value["identity"], "audio": "0" * 64}})
            with self.assertRaises(WorkerError) as caught:
                bounded_diarization.finished_block(fake.output, fake.geometry, 0)
            self.assertEqual(caught.exception.code, "diarization_block_identity")

    def test_operator_tools_find_the_pipelined_checkpoint(self):
        self.assertTrue(pipeline_speech.STATE_NAME.endswith("-api.json"))
        self.assertNotEqual(pipeline_speech.STATE_NAME, pipeline_speech.LEGACY_STATE_NAME)


class RunnerPipelineTests(unittest.TestCase):
    def worker(self, directory, **extra):
        from local_worker.core import defaults
        from local_worker.runner import Worker
        config = defaults({"workspace": directory, "worker_home": directory, "execution_location": "server", "speech_mode": "api",
                           "openai_api_key": "sk-synthetic", "api_speech_model": "gpt-transcribe", "api_speech_price_per_minute": 0.0045,
                           "api_max_cost_usd": 5, "api_credit_only_ack": True, "speech_pipeline": True, "generation_mode": "unsupported", **extra})
        client = unittest.mock.Mock()
        client.result.side_effect = lambda job, **kwargs: {"ok": True}
        return Worker(config, client), client

    def run_process(self, directory, effect):
        worker, client = self.worker(directory)
        job = {"id": "b3ff231c-d7d4-4b74-b41e-c20280974534", "lease_id": "l", "generation": 1, "sha256": "x", "output_mode": "transcript"}
        asr = {"segments": [{"start": 0, "end": 1, "text": "t", "local_speaker_id": "speaker_000", "overlap": False, "words": [],
                             "timing_provenance": "audio_clip_bounds"}],
               "duration": 2, "mode": "api/openai/gpt-transcribe/audio-clip-bounds"}
        turns = [{"start": 0, "end": 2, "speaker": "speaker_000"}]
        with patch.object(worker, "_keep_lease"), patch("local_worker.runner.prepare_audio", return_value={"duration": 2}), \
             patch("local_worker.pipeline_speech.transcribe", side_effect=effect(turns, asr)) as pipelined, \
             patch("local_worker.api_speech.transcribe", return_value=asr) as whole_file, \
             patch("local_worker.runner.diarize", return_value=turns) as diarize, patch("local_worker.runner.profiles.suggest"):
            worker.process(job)
        states = [call.kwargs.get("state") for call in client.result.call_args_list]
        return pipelined, whole_file, diarize, states

    def test_pipelined_job_never_runs_the_whole_file_paid_path(self):
        with TemporaryDirectory() as directory:
            pipelined, whole_file, diarize, states = self.run_process(directory, lambda turns, asr: (lambda *a, **k: (turns, asr)))
            pipelined.assert_called_once()
            whole_file.assert_not_called()
            diarize.assert_not_called()
            self.assertEqual(states[-1], "transcript_ready")

    def test_fall_back_runs_the_whole_file_path_once(self):
        def effect(turns, asr):
            def raise_fallback(*a, **k):
                raise pipeline_speech.FallBack()
            return raise_fallback
        with TemporaryDirectory() as directory:
            pipelined, whole_file, diarize, states = self.run_process(directory, effect)
            whole_file.assert_called_once()
            diarize.assert_called_once()
            self.assertEqual(states[-1], "transcript_ready")


if __name__ == "__main__":
    unittest.main()
