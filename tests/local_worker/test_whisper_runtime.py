import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import wave

from local_worker import server
from local_worker.core import WHISPER_MODELS, WorkerError, defaults, whisper_runtime, whisper_runtime_label


def write_silence(path: Path, seconds: float = 1.0):
    with wave.open(str(path), "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\0\0" * int(16000 * seconds))


class WhisperRuntimeSettingTests(unittest.TestCase):
    def test_default_is_cpu_int8(self):
        config = defaults({})
        self.assertEqual(whisper_runtime(config), ("cpu", "int8"))
        self.assertEqual(whisper_runtime_label(config), "cpu-int8")

    def test_gpu_pairs_are_accepted_and_cpu_rejects_gpu_only_types(self):
        self.assertEqual(whisper_runtime({"whisper_device": "cuda", "whisper_compute_type": "float16"}), ("cuda", "float16"))
        self.assertEqual(whisper_runtime({"whisper_device": "cuda", "whisper_compute_type": "int8_float16"}), ("cuda", "int8_float16"))
        for bad in [{"whisper_device": "gpu"}, {"whisper_device": "auto"}, {"whisper_device": "cpu", "whisper_compute_type": "float16"},
                    {"whisper_device": "cuda", "whisper_compute_type": "int16"},
                    {"whisper_device": "cpu", "whisper_compute_type": "int8_float16"}, {"whisper_compute_type": "fp16"},
                    {"whisper_device": ["cuda"]}]:
            with self.subTest(bad=bad), self.assertRaises(WorkerError) as failure:
                whisper_runtime(bad)
            self.assertEqual(failure.exception.code, "whisper_runtime")

    def test_every_model_has_a_pinned_revision_and_turbo_uses_the_existing_conversion(self):
        for name, (repository, revision) in WHISPER_MODELS.items():
            with self.subTest(name=name):
                self.assertRegex(revision, r"^[0-9a-f]{40}$")
                self.assertEqual(defaults({"whisper_model": name})["whisper_revision"], revision)
        self.assertEqual(WHISPER_MODELS["large-v3-turbo"][0], "dropbox-dash/faster-whisper-large-v3-turbo")

    def test_pinned_revision_wins_over_one_an_older_version_saved(self):
        for saved in [None, "0" * 40]:
            with self.subTest(saved=saved):
                config = defaults({"whisper_model": "large-v3-turbo", "whisper_revision": saved})
                self.assertEqual(config["whisper_revision"], WHISPER_MODELS["large-v3-turbo"][1])

    def test_save_config_refuses_an_invalid_device_pair_without_writing(self):
        from local_worker import core
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"MEETING_WORKER_HOME": home}):
            with self.assertRaises(WorkerError) as failure:
                core.save_config({"base_url": "https://example.com", "whisper_device": "cpu", "whisper_compute_type": "float16"})
            self.assertEqual(failure.exception.code, "whisper_runtime")
            self.assertFalse(core.config_path().exists())

    def test_capabilities_name_the_configured_device(self):
        from local_worker.runner import Worker
        with tempfile.TemporaryDirectory() as directory:
            config = defaults({"workspace": directory, "worker_home": directory, "speech_mode": "local",
                               "whisper_device": "cuda", "whisper_compute_type": "float16"})
            self.assertEqual(Worker(config, Mock()).capabilities()["speech"], "local/faster-whisper/base/cuda-float16")

    def test_setup_whisper_downloads_the_table_repository_at_the_pinned_revision(self):
        from local_worker import models
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"MEETING_WORKER_HOME": home}):
            download = Mock(return_value=str(Path(home) / "snapshot"))
            with patch.dict("sys.modules", {"faster_whisper.utils": SimpleNamespace(download_model=download)}):
                models.setup_whisper("large-v3-turbo")
            repository, revision = WHISPER_MODELS["large-v3-turbo"]
            self.assertEqual(download.call_args.args[0], repository)
            self.assertEqual(download.call_args.kwargs["revision"], revision)
            manifest = json.loads((Path(home) / "models" / "whisper-large-v3-turbo.json").read_text(encoding="utf-8"))
            self.assertEqual((manifest["repository"], manifest["revision"]), (repository, revision))
            with self.assertRaises(WorkerError):
                models.setup_whisper("large-v2")


class TranscribeDeviceTests(unittest.TestCase):
    def setUp(self):
        from local_worker import speech
        speech._cuda_broken = False
        self.addCleanup(setattr, speech, "_cuda_broken", False)

    def test_after_a_missing_cuda_library_this_process_refuses_cuda_without_loading_again(self):
        with tempfile.TemporaryDirectory() as directory:
            model = Mock()
            model.return_value.transcribe.side_effect = RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")
            with self.assertRaises(WorkerError):
                self.run_transcribe(directory, model, whisper_device="cuda", whisper_compute_type="float16")
            second = Mock()
            with self.assertRaises(WorkerError) as failure:
                self.run_transcribe(directory, second, whisper_device="cuda", whisper_compute_type="int8_float16")
            self.assertEqual(failure.exception.code, "whisper_gpu_unavailable")
            second.assert_not_called()
            cpu = Mock()
            cpu.return_value.transcribe.return_value = ([], None)
            self.run_transcribe(directory, cpu)
            cpu.assert_called_once()

    def test_damaged_installed_model_on_cuda_is_model_missing_not_gpu(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"MEETING_WORKER_HOME": directory}):
            self.install_model(directory)
            with self.assertRaises(WorkerError) as failure:
                self.run_transcribe(directory, Mock(side_effect=RuntimeError("Unable to open file 'model.bin' in model")),
                                    whisper_device="cuda", whisper_compute_type="float16")
            self.assertEqual(failure.exception.code, "whisper_model_missing")

    def run_transcribe(self, directory, model, **extra):
        from local_worker import speech
        audio = Path(directory) / "a.wav"
        write_silence(audio)
        config = defaults({"whisper_model": "base", **extra})
        with patch.dict("sys.modules", {"faster_whisper": SimpleNamespace(WhisperModel=model)}):
            return speech.transcribe(audio, Path(directory) / "asr.json", config)

    def test_device_and_compute_type_reach_whisper_and_label_the_result(self):
        with tempfile.TemporaryDirectory() as directory:
            model = Mock()
            model.return_value.transcribe.return_value = ([], None)
            result = self.run_transcribe(directory, model, whisper_device="cuda", whisper_compute_type="float16")
            self.assertEqual(model.call_args.kwargs["device"], "cuda")
            self.assertEqual(model.call_args.kwargs["compute_type"], "float16")
            self.assertTrue(result["mode"].endswith("/cuda-float16"))
            identity = json.loads((Path(directory) / "asr.json").read_text(encoding="utf-8"))["identity"]
            self.assertEqual(identity["runtime"], "cuda/float16")

    def test_default_cpu_keeps_the_earlier_checkpoint_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            model = Mock()
            model.return_value.transcribe.return_value = ([], None)
            result = self.run_transcribe(directory, model)
            self.assertEqual((model.call_args.kwargs["device"], model.call_args.kwargs["compute_type"]), ("cpu", "int8"))
            self.assertTrue(result["mode"].endswith("/cpu-int8"))
            identity = json.loads((Path(directory) / "asr.json").read_text(encoding="utf-8"))["identity"]
            self.assertNotIn("runtime", identity)

    def test_missing_cuda_libraries_become_a_setup_error_without_library_text(self):
        with tempfile.TemporaryDirectory() as directory:
            model = Mock()
            model.return_value.transcribe.side_effect = RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")
            with self.assertRaises(WorkerError) as failure:
                self.run_transcribe(directory, model, whisper_device="cuda", whisper_compute_type="float16")
            self.assertEqual(failure.exception.code, "whisper_gpu_unavailable")
            self.assertNotIn("cublas64_12.dll", str(failure.exception))

    def test_gpu_out_of_memory_stays_a_retryable_runtime_error(self):
        with tempfile.TemporaryDirectory() as directory:
            model = Mock()
            model.return_value.transcribe.side_effect = RuntimeError("CUDA failed with error out of memory")
            with self.assertRaises(RuntimeError) as failure:
                self.run_transcribe(directory, model, whisper_device="cuda", whisper_compute_type="float16")
            self.assertNotIsInstance(failure.exception, WorkerError)

    def install_model(self, home):
        snapshot = Path(home) / "models" / "snapshot"
        snapshot.mkdir(parents=True)
        (Path(home) / "models" / "whisper-base.json").write_text(json.dumps({"local_path": str(snapshot), "revision": WHISPER_MODELS["base"][1]}), encoding="utf-8")

    def test_gpu_load_failure_with_an_installed_model_is_a_gpu_setup_error(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"MEETING_WORKER_HOME": directory}):
            self.install_model(directory)
            with self.assertRaises(WorkerError) as failure:
                self.run_transcribe(directory, Mock(side_effect=RuntimeError("cuda init failed")), whisper_device="cuda", whisper_compute_type="float16")
            self.assertEqual(failure.exception.code, "whisper_gpu_unavailable")

    def test_missing_model_on_gpu_stays_model_missing_so_api_fallback_still_applies(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"MEETING_WORKER_HOME": directory}):
            with self.assertRaises(WorkerError) as failure:
                self.run_transcribe(directory, Mock(side_effect=RuntimeError("no snapshot")), whisper_device="cuda", whisper_compute_type="float16")
            self.assertEqual(failure.exception.code, "whisper_model_missing")

    def test_device_change_discards_the_cpu_checkpoint_and_cpu_reuses_it(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Mock()
            first.return_value.transcribe.return_value = ([], None)
            self.run_transcribe(directory, first)
            again = Mock()
            again.return_value.transcribe.return_value = ([], None)
            self.run_transcribe(directory, again)
            again.return_value.transcribe.assert_not_called()
            gpu = Mock()
            gpu.return_value.transcribe.return_value = ([], None)
            self.run_transcribe(directory, gpu, whisper_device="cuda", whisper_compute_type="float16")
            gpu.return_value.transcribe.assert_called()

    def test_cpu_runtime_errors_are_not_relabelled_as_gpu_problems(self):
        with tempfile.TemporaryDirectory() as directory:
            model = Mock()
            model.return_value.transcribe.side_effect = RuntimeError("synthetic cpu failure")
            with self.assertRaises(RuntimeError):
                self.run_transcribe(directory, model)

    def test_invalid_pair_stops_before_loading_a_model(self):
        with tempfile.TemporaryDirectory() as directory:
            model = Mock()
            with self.assertRaises(WorkerError):
                self.run_transcribe(directory, model, whisper_device="cpu", whisper_compute_type="float16")
            model.assert_not_called()


class ServerWhisperSettingTests(unittest.TestCase):
    base = {"base_url": "http://127.0.0.1:3187", "device_token": "d" * 48, "sites_dispatch_token": "g" * 48}

    def load(self, raw):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            env = {"MEETING_WORKER_MODE": "server", "MEETING_WORKER_HOME": str(home), "CREDENTIALS_DIRECTORY": str(home / "credentials")}
            with patch("local_worker.server.sys.platform", "linux"), patch("local_worker.server.os.geteuid", return_value=1001, create=True), \
                 patch("local_worker.server.private_directory"), patch("local_worker.server.read_private_credential", return_value=raw):
                return server.load_server_config(env)

    def test_server_accepts_device_settings_and_large_models_only_on_cuda(self):
        self.assertTrue({"whisper_device", "whisper_compute_type"} <= server.ALLOWED_SETTINGS)
        for name in WHISPER_MODELS:
            with self.subTest(name=name):
                config = self.load({**self.base, "whisper_model": name, "whisper_device": "cuda", "whisper_compute_type": "int8_float16"})
                self.assertEqual((config["whisper_device"], config["whisper_compute_type"]), ("cuda", "int8_float16"))
                self.assertEqual(config["whisper_revision"], WHISPER_MODELS[name][1])
        self.assertEqual((self.load(self.base)["whisper_device"], self.load(self.base)["whisper_compute_type"]), ("cpu", "int8"))
        self.assertEqual(self.load({**self.base, "whisper_model": "small"})["whisper_model"], "small")

    def test_server_rejects_bad_device_pairs_unknown_models_and_large_cpu_models(self):
        for change in [{"whisper_device": "cpu", "whisper_compute_type": "float16"}, {"whisper_device": "tpu"}, {"whisper_device": "auto"},
                       {"whisper_model": "large-v2"}, {"whisper_model": "medium"}, {"whisper_model": "large-v3"}, {"whisper_model": "large-v3-turbo"}]:
            with self.subTest(change=change), self.assertRaises(WorkerError) as failure:
                self.load({**self.base, **change})
            self.assertEqual(failure.exception.code, "server_native_policy")

    def test_cuda_probe_loads_on_gpu_and_encodes_one_second_of_silence(self):
        from local_worker.models import SEGMENTATION_FLOAT32_SHA256, EMBEDDING_SHA256
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            models = home / "models"
            (models / "whisper-cache").mkdir(parents=True)
            segmentation, embedding = models / "segmentation.float32.onnx", models / "embedding.onnx"
            segmentation.write_bytes(b"synthetic")
            embedding.write_bytes(b"synthetic")
            config = defaults({"workspace": directory, "worker_home": directory, "execution_location": "server", "speech_mode": "local",
                               "diarization_model": str(segmentation), "embedding_model": str(embedding),
                               "whisper_device": "cuda", "whisper_compute_type": "float16"})
            (models / "whisper-base.json").write_text(json.dumps({"local_path": str(models / "whisper-cache"), "revision": config["whisper_revision"]}), encoding="utf-8")
            whisper = Mock()
            whisper.return_value.transcribe.return_value = (iter([]), None)
            sherpa = Mock()
            sherpa.OfflineSpeakerDiarizationConfig.return_value.validate.return_value = True
            numpy = SimpleNamespace(zeros=Mock(return_value="silence"), float32="float32")
            digest = lambda path: SEGMENTATION_FLOAT32_SHA256 if path == segmentation else EMBEDDING_SHA256
            with patch("local_worker.server.sha256", side_effect=digest), patch.dict("sys.modules", {"av": SimpleNamespace(), "numpy": numpy, "faster_whisper": SimpleNamespace(WhisperModel=whisper), "sherpa_onnx": sherpa}):
                server.probe_models(config)
            self.assertEqual((whisper.call_args.kwargs["device"], whisper.call_args.kwargs["compute_type"]), ("cuda", "float16"))
            whisper.return_value.transcribe.assert_called_once()
            whisper.return_value.transcribe.side_effect = RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")
            with patch("local_worker.server.sha256", side_effect=digest), patch.dict("sys.modules", {"av": SimpleNamespace(), "numpy": numpy, "faster_whisper": SimpleNamespace(WhisperModel=whisper), "sherpa_onnx": sherpa}), \
                 self.assertRaises(WorkerError) as failure:
                server.probe_models(config)
            self.assertEqual(failure.exception.code, "server_native_model_load_failed")


if __name__ == "__main__":
    unittest.main()
