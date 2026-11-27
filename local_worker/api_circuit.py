"""Persistent server-wide API pause, independent of job, key and model identity."""
from __future__ import annotations

from contextlib import contextmanager
import datetime as dt
import os
from pathlib import Path
import uuid

from .core import SingleWorker, WorkerError, atomic_json, read_json

REASONS = {"credit_balance_exhausted", "insufficient_quota", "billing_limit", "ambiguous_429", "transient_429",
           "unknown_outcome", "provider_configuration", "response_invalid", "circuit_storage"}
_STORAGE_LATCHES = set()


def for_config(config: dict):
    if config.get("execution_location") == "server" and config.get("speech_mode") == "api":
        home = Path(config.get("worker_home", ""))
        if not home.is_absolute():
            raise WorkerError("api_circuit_storage", "API 전역 차단 저장소를 확인하세요.", "needs_setup")
        return ApiCircuit(home)
    return None


def for_text_config(config: dict):
    """Separate pause for the meeting-info text model. Never reported as `api_*`."""
    if config.get("execution_location") == "server":
        home = Path(config.get("worker_home", ""))
        if not home.is_absolute():
            raise WorkerError("api_circuit_storage", "API 전역 차단 저장소를 확인하세요.", "needs_setup")
        return ApiCircuit(home, "api-safety-text")
    return None


class ApiCircuit:
    def __init__(self, home: Path, name: str = "api-safety"):
        self.root = Path(home) / name
        self.path = self.root / "circuit.json"

    def _read(self):
        value = read_json(self.path, {"version": 1, "phase": "open"})
        if not isinstance(value, dict) or value.get("version") != 1 or value.get("phase") not in {"open", "inflight", "blocked"}:
            raise ValueError("invalid circuit")
        if set(value) - {"version", "phase", "reason", "block_id", "resolved_block_id", "attempt_id", "updated_at"}:
            raise ValueError("invalid fields")
        if "resolved_block_id" in value and not self._uuid(value["resolved_block_id"]):
            raise ValueError("invalid resolution")
        if value["phase"] == "blocked" and (value.get("reason") not in REASONS or not self._uuid(value.get("block_id"))):
            raise ValueError("invalid block")
        if value["phase"] == "inflight" and not self._uuid(value.get("attempt_id")):
            raise ValueError("invalid attempt")
        return value

    @staticmethod
    def _uuid(value):
        try:
            return str(uuid.UUID(value)) == value
        except (ValueError, TypeError, AttributeError):
            return False

    def _write(self, value):
        try:
            value["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            atomic_json(self.path, value)
            if os.name == "posix":
                # Persist both rename and a newly created api-safety directory.
                for path in (self.root, self.root.parent):
                    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
        except Exception:
            raise self._storage_error() from None

    def _storage_error(self):
        _STORAGE_LATCHES.add(str(self.root.resolve()))
        return WorkerError("api_circuit_storage", "API 전역 차단 기록을 검증하지 못해 추가 호출을 중단했습니다.", "needs_setup")

    def trip(self, reason):
        try:
            with SingleWorker(self.root / "state-lock"):
                previous = self._read()
                if previous["phase"] == "blocked":
                    return previous
                value = {"version": 1, "phase": "blocked", "reason": reason if reason in REASONS else "unknown_outcome", "block_id": str(uuid.uuid4())}
                self._write(value)
                return value
        except Exception:
            raise self._storage_error() from None

    def status(self):
        try:
            if str(self.root.resolve()) in _STORAGE_LATCHES:
                raise ValueError("storage paused")
            value = self._read()
            if value["phase"] == "inflight":
                # A live POST owns this lock. A crash releases it but leaves its
                # persisted intent, which becomes a durable manual-review pause.
                try:
                    with SingleWorker(self.root / "request-lock"):
                        # The request may have finished between our read and this lock; any intent
                        # still on disk now has no live owner (a live request holds this lock).
                        current = self._read()
                        value = self.trip("unknown_outcome") if current["phase"] == "inflight" else current
                except WorkerError as exc:
                    if exc.code != "worker_running":
                        raise
                    return {"api_blocked": False, "api_request_inflight": True}
            if value["phase"] == "blocked":
                return {"api_blocked": True, "api_block_reason": value["reason"], "api_block_id": value["block_id"]}
            result = {"api_blocked": False, "api_request_inflight": False}
            if self._uuid(value.get("resolved_block_id")):
                result["api_resolved_block_id"] = value["resolved_block_id"]
            return result
        except Exception:
            self._storage_error()
            return {"api_blocked": True, "api_block_reason": "circuit_storage"}

    def assert_open(self):
        status = self.status()
        if status["api_blocked"] or status.get("api_request_inflight"):
            raise WorkerError("api_safety_paused", "API 호출이 안전 중단됐습니다. 운영자가 결제 조건과 이전 요청을 확인해야 합니다.", "needs_setup")

    @contextmanager
    def request(self, attempt_id):
        try:
            lock = SingleWorker(self.root / "request-lock")
            lock.__enter__()
        except Exception:
            raise self._storage_error() from None
        begun = False
        try:
            if str(self.root.resolve()) in _STORAGE_LATCHES:
                raise self._storage_error()
            value = self._read()
            if value["phase"] == "inflight":
                self.trip("unknown_outcome")
                value = self._read()
            if value["phase"] != "open":
                raise WorkerError("api_safety_paused", "API 전역 차단을 운영자가 확인하세요.", "needs_setup")
            # Persist intent before yielding to any POST, including interrupted IO.
            self._write({**value, "phase": "inflight", "attempt_id": attempt_id})
            begun = True
            yield
            current = self._read()
            if current["phase"] == "inflight" and current.get("attempt_id") == attempt_id:
                self._write({key: val for key, val in {**current, "phase": "open"}.items() if key != "attempt_id"})
        except WorkerError:
            try:
                if begun and self._read().get("phase") == "inflight":
                    self.trip("unknown_outcome")
            except Exception:
                raise self._storage_error() from None
            raise
        except Exception:
            if begun:
                try:
                    self.trip("unknown_outcome")
                except WorkerError:
                    pass
            raise self._storage_error() from None
        finally:
            lock.__exit__(None, None, None)

    def resume(self):
        """Only explicit operator attestation invokes this; no key/model auto reset."""
        try:
            with SingleWorker(self.root / "request-lock"), SingleWorker(self.root / "state-lock"):
                value = self._read()
                if value["phase"] != "blocked":
                    raise WorkerError("api_resume_requires_block", "먼저 전역 차단 상태와 이전 요청을 확인하세요.", "needs_setup")
                self._write({"version": 1, "phase": "open", "resolved_block_id": value["block_id"]})
                _STORAGE_LATCHES.discard(str(self.root.resolve()))
                return {"api_blocked": False, "api_resolved_block_id": value["block_id"]}
        except WorkerError:
            raise
        except Exception:
            raise self._storage_error() from None
