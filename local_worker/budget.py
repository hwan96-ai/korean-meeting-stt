"""One durable API cost ceiling across speech, minutes, revisions and retries."""
from __future__ import annotations

import datetime as dt
import math
from pathlib import Path

from .core import WorkerError, SingleWorker, atomic_json, read_json


class JobBudget:
    def __init__(self, directory: Path, max_cost_usd: float):
        if not isinstance(max_cost_usd, (int, float)) or not math.isfinite(max_cost_usd) or max_cost_usd <= 0:
            raise WorkerError("api_policy_missing", "작업별 API 누적 비용 상한을 양수로 설정하세요.", "needs_setup")
        self.directory, self.cap = Path(directory), max_cost_usd
        self.path = self.directory / "api-budget.json"

    def reserve(self, attempt_id: str, reservation_usd: float, *, stage: str, model_id: str, details: dict | None = None) -> dict:
        if not isinstance(reservation_usd, (int, float)) or not math.isfinite(reservation_usd) or reservation_usd < 0:
            raise WorkerError("api_policy_missing", "API 요청의 보수적 비용을 계산할 수 없습니다.", "needs_setup")
        with SingleWorker(self.directory / "api-budget-lock"):
            ledger = read_json(self.path, {"attempts": []})
            previous = next((value for value in ledger["attempts"] if value["attempt_id"] == attempt_id), None)
            if previous:
                raise WorkerError("api_attempt_exists", "이미 예약된 API 요청입니다. 이전 결과를 확인하세요.", "unknown_outcome")
            spent = sum(float(value["charged_upper_bound_usd"]) for value in ledger["attempts"])
            if spent + reservation_usd > self.cap:
                raise WorkerError("api_cumulative_cost_limit", "이 작업의 이전 요청 비용과 새 요청 예약액이 누적 API 비용 상한을 초과합니다.", "needs_setup")
            entry = {"attempt_id": attempt_id, "started_at": dt.datetime.now(dt.timezone.utc).isoformat(), "state": "inflight",
                     "stage": stage, "model_id": model_id, "reserved_upper_bound_usd": reservation_usd,
                     "charged_upper_bound_usd": reservation_usd, "details": details or {}}
            ledger["attempts"].append(entry)
            atomic_json(self.path, ledger)
            return entry

    def settle(self, attempt_id: str, *, state: str = "responded", charged_upper_bound_usd: float | None = None,
               usage: dict | None = None, error_code: str | None = None) -> dict:
        if charged_upper_bound_usd is not None and (not math.isfinite(charged_upper_bound_usd) or charged_upper_bound_usd < 0):
            raise WorkerError("api_usage_invalid", "반환된 API 사용량 비용을 검증할 수 없습니다.", "needs_review")
        with SingleWorker(self.directory / "api-budget-lock"):
            ledger = read_json(self.path, {"attempts": []})
            entry = next((value for value in ledger["attempts"] if value["attempt_id"] == attempt_id), None)
            if entry is None:
                raise WorkerError("api_reservation_missing", "API 요청 예약 기록을 찾을 수 없습니다.", "unknown_outcome")
            entry["state"] = state
            if charged_upper_bound_usd is not None:
                entry["charged_upper_bound_usd"] = charged_upper_bound_usd
                entry["usage_based_upper_bound_usd"] = charged_upper_bound_usd
            if usage is not None:
                entry["usage"] = usage
            if error_code:
                entry["error_code"] = error_code
            atomic_json(self.path, ledger)
            return entry
