from __future__ import annotations

import json
import io
import os
from pathlib import Path, PurePosixPath
import stat
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from local_worker.core import WorkerError, defaults
from local_worker.runner import Worker
from local_worker import server

ID = "e6590ed1-4882-4460-9edc-6b076e911969"


class ServerCredentialTests(unittest.TestCase):
    def acl(self, permission):
        return struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *entry) for entry in [(1, permission, 0xffffffff), (2, permission, 992), (4, 0, 0xffffffff), (16, permission, 0xffffffff), (32, 0, 0xffffffff)])

    def test_systemd_actual_read_only_acl_accepts_only_root_and_service_uid(self):
        for directory, mode, permission in [(True, stat.S_IFDIR | 0o550, 5), (False, stat.S_IFREG | 0o440, 4)]:
            metadata = SimpleNamespace(st_mode=mode, st_uid=0, st_gid=0)
            server.validate_systemd_acl(metadata, self.acl(permission), directory=directory, uid=992)
            with self.assertRaises(WorkerError):
                server.validate_private(metadata, directory=directory, uid=992)
            for change in [dict(st_uid=992), dict(st_gid=982), dict(st_mode=mode | 0o020), dict(st_mode=mode | 0o200)]:
                with self.subTest(change=change), self.assertRaises(WorkerError):
                    server.validate_systemd_acl(SimpleNamespace(**{**vars(metadata), **change}), self.acl(permission), directory=directory, uid=992)
            for data in [self.acl(permission) + struct.pack("<HHI", 2, permission, 993), self.acl(permission) + struct.pack("<HHI", 8, permission, 982), self.acl(permission).replace(struct.pack("<HHI", 4, 0, 0xffffffff), struct.pack("<HHI", 4, permission, 0xffffffff)), self.acl(permission).replace(struct.pack("<HHI", 32, 0, 0xffffffff), struct.pack("<HHI", 32, permission, 0xffffffff)), self.acl(permission + 2), struct.pack("<I", 1) + self.acl(permission)[4:], self.acl(permission)[:-1]]:
                with self.subTest(data_size=len(data)), self.assertRaises(WorkerError):
                    server.validate_systemd_acl(metadata, data, directory=directory, uid=992)

    def test_acl_runtime_path_exception_never_applies_to_generic_config_paths(self):
        root = SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0, st_gid=0)
        with patch.object(Path, "lstat", return_value=root):
            self.assertTrue(server.systemd_credential_path(PurePosixPath("/run/credentials/meeting-speech.service/worker.json"), directory=False))
            for path in ["/etc/meeting-speech/worker.json", "/run/credentials/nested/meeting-speech.service/worker.json", "/run/credentials/meeting-speech.service/other.json", "/run/credentials/ordinary/worker.json"]:
                self.assertFalse(server.systemd_credential_path(PurePosixPath(path), directory=False))
        with patch.object(Path, "lstat", return_value=SimpleNamespace(st_mode=stat.S_IFDIR | 0o777, st_uid=0, st_gid=0)):
            self.assertFalse(server.systemd_credential_path(PurePosixPath("/run/credentials/meeting-speech.service/worker.json"), directory=False))

    def test_acl_exception_requires_runtime_path_and_same_open_descriptor(self):
        metadata = SimpleNamespace(st_mode=stat.S_IFREG | 0o440, st_uid=0, st_gid=0)
        with patch("local_worker.server.os.fstat", return_value=metadata), patch("local_worker.server.systemd_credential_path", return_value=True), patch("local_worker.server.os.getxattr", return_value=self.acl(4), create=True) as acl:
            server.validate_credential_descriptor(Path("worker.json"), 71, 992, directory=False)
            acl.assert_called_once_with(71, "system.posix_acl_access")
        with patch("local_worker.server.os.fstat", return_value=metadata), patch("local_worker.server.systemd_credential_path", return_value=False), patch("local_worker.server.os.getxattr", create=True) as acl:
            with self.assertRaises(WorkerError):
                server.validate_credential_descriptor(Path("outside.json"), 71, 992, directory=False)
            acl.assert_not_called()

    def test_private_file_directory_and_owner_permissions(self):
        for mode, directory, owner in [(stat.S_IFREG | 0o400, False, 1001), (stat.S_IFREG | 0o600, False, 0), (stat.S_IFDIR | 0o700, True, 1001)]:
            server.validate_private(SimpleNamespace(st_mode=mode, st_uid=owner), directory=directory, uid=1001)
        for mode, directory, owner in [(stat.S_IFREG | 0o640, False, 1001), (stat.S_IFREG | 0o600, False, 2000), (stat.S_IFLNK | 0o700, True, 1001), (stat.S_IFDIR | 0o700, False, 1001)]:
            with self.subTest(mode=mode, owner=owner), self.assertRaises(WorkerError):
                server.validate_private(SimpleNamespace(st_mode=mode, st_uid=owner), directory=directory, uid=1001)

    def test_credential_parser_rejects_extra_secrets_and_oversize(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "worker.json"
            with patch("local_worker.server.validate_private"):
                for value in [{"api_model": "synthetic"}, {"notion_token_file": "synthetic"}, {"generation_mode": "api"}, {"padding": "x" * 32768}]:
                    path.write_text(json.dumps(value), encoding="utf-8")
                    with self.assertRaises(WorkerError):
                        server.read_private_credential(path, 1001)

    def test_credential_symlink_is_rejected_before_open(self):
        with patch.object(Path, "is_symlink", return_value=True), patch("local_worker.server.os.open") as opened:
            with self.assertRaises(WorkerError):
                server.read_private_credential(Path("worker.json"), 1001)
            opened.assert_not_called()

    def test_explicit_linux_mode_and_non_root_required(self):
        with patch("local_worker.server.sys.platform", "linux"):
            with self.assertRaises(WorkerError) as failure:
                server.load_server_config({})
            self.assertEqual(failure.exception.code, "explicit_linux_server_mode_required")
            with patch("local_worker.server.os.geteuid", return_value=0, create=True), self.assertRaises(WorkerError) as failure:
                server.load_server_config({"MEETING_WORKER_MODE": "server"})
            self.assertEqual(failure.exception.code, "server_unprivileged_user_required")

    def test_server_loads_only_explicit_credential_and_keeps_dpapi_untouched(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            raw = {"base_url": "http://127.0.0.1:3187", "device_token": "d" * 48, "sites_dispatch_token": "g" * 48}
            env = {"MEETING_WORKER_MODE": "server", "MEETING_WORKER_HOME": str(home), "CREDENTIALS_DIRECTORY": str(home / "credentials")}
            with patch("local_worker.server.sys.platform", "linux"), patch("local_worker.server.os.geteuid", return_value=1001, create=True), \
                 patch("local_worker.server.private_directory"), patch("local_worker.server.read_private_credential", return_value=raw) as read, \
                 patch("local_worker.core._dpapi") as dpapi:
                config = server.load_server_config(env)
            read.assert_called_once_with(home / "credentials" / "worker.json", 1001)
            dpapi.assert_not_called()
            self.assertEqual(config["execution_location"], "server")
            self.assertEqual(config["speech_mode"], "local")
            self.assertEqual(config["generation_mode"], "unsupported")
            self.assertFalse(config["allow_api_speech_fallback"])
            self.assertEqual(config["notion_targets"], {})
            self.assertEqual(config["cpu_threads"], 2)
            self.assertEqual(config["device_token"], raw["device_token"])
            self.assertEqual((config["cluster_threshold"], config["global_cluster_threshold"]), (0.7, 0.6))

    def test_invalid_url_auth_and_workspace_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            base = {"base_url": "http://127.0.0.1:3187", "device_token": "d" * 48, "sites_dispatch_token": "g" * 48}
            env = {"MEETING_WORKER_MODE": "server", "MEETING_WORKER_HOME": str(home), "CREDENTIALS_DIRECTORY": str(home / "credentials")}
            for change in [{"base_url": "http://example.com"}, {"base_url": "https://user:secret@example.com"}, {"device_token": "short"}, {"workspace": str(home.parent)}, {"cpu_threads": True}]:
                with self.subTest(change=change), patch("local_worker.server.sys.platform", "linux"), patch("local_worker.server.os.geteuid", return_value=1001, create=True), \
                     patch("local_worker.server.private_directory"), patch("local_worker.server.read_private_credential", return_value={**base, **change}), self.assertRaises(WorkerError):
                    server.load_server_config(env)

    def test_explicit_server_api_config_is_validated_without_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            raw = {"base_url": "http://127.0.0.1:3187", "device_token": "d" * 48, "sites_dispatch_token": "g" * 48,
                "speech_mode": "api", "openai_api_key": "synthetic-key", "api_speech_model": "gpt-transcribe",
                "api_speech_price_per_minute": .0045, "api_max_cost_usd": 2, "api_credit_only_ack": True}
            env = {"MEETING_WORKER_MODE": "server", "MEETING_WORKER_HOME": str(home), "CREDENTIALS_DIRECTORY": str(home / "credentials")}
            with patch("local_worker.server.sys.platform", "linux"), patch("local_worker.server.os.geteuid", return_value=1001, create=True), \
                 patch("local_worker.server.private_directory"), patch("local_worker.server.read_private_credential", return_value=raw), \
                 patch("local_worker.api_speech._post") as post:
                config = server.load_server_config(env)
            post.assert_not_called()
            self.assertEqual(config["speech_mode"], "api")
            self.assertFalse(config["allow_api_speech_fallback"])

    def test_speech_pipeline_setting_is_boolean_and_only_on_in_api_mode(self):
        self.assertIn("speech_pipeline", server.ALLOWED_SETTINGS)
        base = {"base_url": "http://127.0.0.1:3187", "device_token": "d" * 48, "sites_dispatch_token": "g" * 48}
        api = {"speech_mode": "api", "openai_api_key": "synthetic-key", "api_speech_model": "gpt-transcribe",
               "api_speech_price_per_minute": .0045, "api_max_cost_usd": 2, "api_credit_only_ack": True}
        cases = [({**base, **api}, False), ({**base, **api, "speech_pipeline": True}, True),
                 ({**base, "speech_pipeline": True}, False), ({**base, **api, "speech_pipeline": "true"}, "error")]
        for raw, expected in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as temporary:
                home = Path(temporary)
                env = {"MEETING_WORKER_MODE": "server", "MEETING_WORKER_HOME": str(home), "CREDENTIALS_DIRECTORY": str(home / "credentials")}
                with patch("local_worker.server.sys.platform", "linux"), patch("local_worker.server.os.geteuid", return_value=1001, create=True), \
                     patch("local_worker.server.private_directory"), patch("local_worker.server.read_private_credential", return_value=raw):
                    if expected == "error":
                        with self.assertRaises(WorkerError):
                            server.load_server_config(env)
                    else:
                        self.assertIs(server.load_server_config(env)["speech_pipeline"], expected)

    def test_resume_text_lifts_only_the_meeting_info_pause(self):
        from local_worker.api_circuit import ApiCircuit
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            ApiCircuit(home).trip("insufficient_quota")
            ApiCircuit(home, "api-safety-text").trip("ambiguous_429")
            config = {"execution_location": "server", "speech_mode": "api", "worker_home": str(home)}
            argv = ["server", "--resume-api", "--text", "--confirm-credit-policy", "--confirm-provider-outcomes-reviewed"]
            with patch("local_worker.server.load_server_config", return_value=config), patch("sys.argv", argv),                  patch("local_worker.server.os.umask", create=True), patch("builtins.print"):
                self.assertEqual(server.main(), 0)
            self.assertFalse(ApiCircuit(home, "api-safety-text").status()["api_blocked"])
            self.assertTrue(ApiCircuit(home).status()["api_blocked"])

    def test_meeting_info_keys_are_accepted_and_reach_the_config(self):
        meta = {"meta_inference_mode": "api", "api_text_model": "text-model-x", "api_text_prices_per_million": {"input": 1, "output": 4},
                "api_text_max_output_tokens": 2000, "api_meta_max_cost_usd": 0.05}
        self.assertTrue(set(meta) <= server.ALLOWED_SETTINGS)
        self.assertTrue({"meta_codex_path", "meta_codex_home", "meta_codex_model", "meta_codex_timeout"} <= server.ALLOWED_SETTINGS)
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            raw = {"base_url": "http://127.0.0.1:3187", "device_token": "d" * 48, "sites_dispatch_token": "g" * 48, **meta}
            env = {"MEETING_WORKER_MODE": "server", "MEETING_WORKER_HOME": str(home), "CREDENTIALS_DIRECTORY": str(home / "credentials")}
            with patch("local_worker.server.sys.platform", "linux"), patch("local_worker.server.os.geteuid", return_value=1001, create=True),                  patch("local_worker.server.private_directory"), patch("local_worker.server.read_private_credential", return_value=raw):
                config = server.load_server_config(env)
            self.assertEqual(config["api_text_model"], "text-model-x")


class ServerWorkerTests(unittest.TestCase):
    def config(self, directory, **extra):
        return defaults({"workspace": directory, "worker_home": directory, "execution_location": "server", "speech_mode": "local", "generation_mode": "unsupported", **extra})

    def test_claim_speech_only_and_never_poll_pc_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            client = Mock()
            client.call.side_effect = lambda method, path, body, **kwargs: {"job": None} if path.endswith("claim") else {}
            worker = Worker(self.config(directory), client)
            with patch.object(worker, "process_profiles") as profile:
                worker.run(once=True)
            profile.assert_not_called()
            claim = next(call for call in client.call.call_args_list if call.args[1].endswith("claim"))
            self.assertEqual(claim.args[2], {"speech_only": True})
            capabilities = client.call.call_args_list[0].args[2]["capabilities"]
            self.assertEqual(capabilities["execution_location"], "server")
            self.assertEqual(capabilities["generation"], "unsupported")
            self.assertEqual(capabilities["profiles"], "unsupported")
            self.assertTrue(capabilities["speech"].startswith("needs_setup"))

    def test_minutes_and_api_policy_cannot_reach_providers(self):
        with tempfile.TemporaryDirectory() as directory:
            for extra, pending, code in [({}, "minutes", "server_generation_unsupported"), ({"speech_mode": "api"}, "transcribe", "server_api_policy"), ({"allow_api_speech_fallback": True}, "transcribe", "server_native_only")]:
                with self.subTest(extra=extra):
                    client = Mock()
                    client.result.side_effect = lambda current, **values: values
                    worker = Worker(self.config(directory, **extra), client)
                    with patch.object(worker, "_keep_lease"), patch("local_worker.runner.generate") as generate, \
                         patch("local_worker.api_speech.transcribe") as api, patch("local_worker.notion_export.export") as notion:
                        worker.process({"id": ID, "pending": pending, "output_mode": "minutes"})
                    generate.assert_not_called()
                    api.assert_not_called()
                    notion.assert_not_called()
                    client.download.assert_not_called()
                    self.assertEqual(client.result.call_args.kwargs["error"]["code"], code)

    def test_server_native_transcript_has_no_person_suggestions(self):
        with tempfile.TemporaryDirectory() as directory:
            client = Mock()
            client.result.side_effect = lambda current, **values: values
            worker = Worker(self.config(directory), client)
            asr = {"segments": [{"start": 0, "end": 2, "text": "시험 발화", "words": []}], "duration": 2, "mode": "local/test"}
            with patch.object(worker, "_keep_lease"), patch("local_worker.runner.prepare_audio", return_value={"duration": 2}), \
                 patch("local_worker.runner.transcribe", return_value=asr), \
                 patch("local_worker.runner.diarize", return_value=[{"start": 0, "end": 2, "speaker": "speaker_00"}]), \
                 patch("local_worker.runner.profiles.suggest") as suggest, patch("local_worker.runner.generate") as generate:
                worker.process({"id": ID, "pending": "transcribe", "output_mode": "transcript"})
            suggest.assert_not_called()
            generate.assert_not_called()
            result = client.result.call_args.kwargs
            self.assertEqual(result["state"], "transcript_ready")
            self.assertIsNone(result["segments"][0]["proposed_person_id"])

    def test_profiles_direct_call_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            client = Mock()
            worker = Worker(self.config(directory), client)
            with self.assertRaises(WorkerError):
                worker.process_profiles()
            client.call.assert_not_called()

    def test_child_receives_no_server_credential_environment(self):
        from local_worker.native import run_isolated
        child = Mock()
        child.poll.return_value = 0
        child.returncode = 0
        with patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": "synthetic", "MEETING_WORKER_GATEWAY_TOKEN": "synthetic", "MEETING_SERVER_DEVICE_TOKEN": "synthetic", "openai_api_key": "synthetic"}), patch("local_worker.native.subprocess.Popen", return_value=child) as spawn:
            self.assertEqual(run_isolated(["trusted-test-child"], {}), 0)
        env = spawn.call_args.kwargs["env"]
        self.assertFalse({key.upper() for key in env} & {"CREDENTIALS_DIRECTORY", "MEETING_WORKER_GATEWAY_TOKEN", "MEETING_SERVER_DEVICE_TOKEN", "OPENAI_API_KEY"})

    def test_model_probe_loads_pinned_models_without_audio(self):
        from local_worker.models import SEGMENTATION_FLOAT32_SHA256, EMBEDDING_SHA256
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            models = home / "models"
            models.mkdir()
            (models / "whisper-cache").mkdir()
            segmentation, embedding = models / "segmentation.float32.onnx", models / "embedding.onnx"
            segmentation.write_bytes(b"synthetic")
            embedding.write_bytes(b"synthetic")
            config = self.config(directory, diarization_model=str(segmentation), embedding_model=str(embedding))
            (models / "whisper-base.json").write_text(json.dumps({"local_path": str(models / "whisper-cache"), "revision": config["whisper_revision"]}), encoding="utf-8")
            whisper = Mock()
            sherpa = Mock()
            sherpa.OfflineSpeakerDiarizationConfig.return_value.validate.return_value = True
            def digest(path):
                return SEGMENTATION_FLOAT32_SHA256 if path == segmentation else EMBEDDING_SHA256
            with patch("local_worker.server.sha256", side_effect=digest), patch.dict("sys.modules", {"av": SimpleNamespace(), "numpy": SimpleNamespace(), "faster_whisper": SimpleNamespace(WhisperModel=whisper), "sherpa_onnx": sherpa}):
                server.probe_models(config)
            self.assertEqual(whisper.call_args.kwargs["device"], "cpu")
            self.assertEqual(whisper.call_args.kwargs["compute_type"], "int8")
            self.assertTrue(whisper.call_args.kwargs["local_files_only"])
            whisper.return_value.transcribe.assert_not_called()
            sherpa.OfflineSpeakerDiarization.return_value.process.assert_not_called()
            with patch("local_worker.server.sha256", return_value="wrong"), self.assertRaises(WorkerError) as failure:
                server.probe_models(config)
            self.assertEqual(failure.exception.code, "server_model_checksum")

    def test_failed_startup_probe_cannot_heartbeat_or_claim(self):
        for arguments in (["server", "--doctor"], ["server", "--once"]):
            with self.subTest(arguments=arguments), patch("sys.argv", arguments), patch("local_worker.server.load_server_config", return_value={}), \
                 patch("local_worker.server.probe_models", side_effect=WorkerError("server_native_model_load_failed", "synthetic-private-detail", "needs_setup")), \
                 patch("local_worker.runner.Worker") as worker, patch("sys.stderr", new_callable=io.StringIO) as stderr:
                self.assertEqual(server.main(), 2)
                worker.assert_not_called()
                self.assertNotIn("synthetic-private-detail", stderr.getvalue())

    def test_api_probe_requires_diarization_but_not_whisper_and_never_calls_api(self):
        from local_worker.models import SEGMENTATION_FLOAT32_SHA256, EMBEDDING_SHA256
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            segmentation, embedding = home / "segmentation.float32.onnx", home / "embedding.onnx"
            segmentation.write_bytes(b"synthetic")
            embedding.write_bytes(b"synthetic")
            config = self.config(directory, speech_mode="api", openai_api_key="synthetic-key", api_speech_model="gpt-transcribe",
                api_speech_price_per_minute=.0045, api_max_cost_usd=2, api_credit_only_ack=True,
                diarization_model=str(segmentation), embedding_model=str(embedding))
            sherpa, whisper = Mock(), Mock()
            sherpa.OfflineSpeakerDiarizationConfig.return_value.validate.return_value = True
            def digest(path):
                return SEGMENTATION_FLOAT32_SHA256 if path == segmentation else EMBEDDING_SHA256
            with patch("local_worker.server.sha256", side_effect=digest), patch.dict("sys.modules", {"av": SimpleNamespace(), "numpy": SimpleNamespace(), "faster_whisper": SimpleNamespace(WhisperModel=whisper), "sherpa_onnx": sherpa}), \
                 patch("local_worker.api_speech._post") as post:
                server.probe_models(config)
            post.assert_not_called()
            whisper.assert_not_called()
            sherpa.OfflineSpeakerDiarization.assert_called_once()


if __name__ == "__main__":
    unittest.main()
