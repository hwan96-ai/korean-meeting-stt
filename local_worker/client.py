from __future__ import annotations

import hashlib
import http.client
import json
from pathlib import Path
import time
from urllib import request, error, parse

from .core import WorkerError, sha256


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class SiteClient:
    def __init__(self, config: dict):
        self.config = config
        self.opener = request.build_opener(NoRedirect())

    def headers(self) -> dict:
        headers = {"Accept": "application/json", "User-Agent": "CodexMeetingLocalWorker/1.0",
                   "Authorization": "Bearer " + self.config["device_token"]}
        token = self.config.get("sites_dispatch_token")
        if token:
            headers["OAI-Sites-Authorization"] = token
        return headers

    def call(self, method: str, path: str, body=None, *, retry_safe=False):
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {**self.headers(), "Content-Type": "application/json"}
        for attempt in range(3 if retry_safe else 1):
            try:
                with self.opener.open(request.Request(self.config["base_url"] + path, data, headers, method=method), timeout=45) as response:
                    return json.load(response)
            except error.HTTPError as exc:
                status = exc.code
                if status in {401, 403}:
                    raise WorkerError("device_auth", "PC 연결 또는 사이트 접근 인증을 다시 설정하세요.", "needs_setup") from None
                if status in {409, 410, 404}:
                    raise WorkerError("lease_lost", "작업이 변경·취소·삭제되었거나 임대가 만료되었습니다.", "cancelled") from None
                if not retry_safe or status not in {429, 502, 503, 504} or attempt == 2:
                    raise WorkerError("site_http_" + str(status), "사이트 요청에 실패했습니다. PC 연결 상태를 확인하세요.", "failed", True) from None
            except (error.URLError, TimeoutError, OSError):
                if not retry_safe or attempt == 2:
                    raise WorkerError("site_connection", "사이트 응답을 확인할 수 없습니다. 로컬 결과를 보존했습니다.", "failed", True) from None
            except (ValueError, http.client.HTTPException):
                raise WorkerError("site_response", "사이트 응답이 완전하지 않습니다. 로컬 결과를 보존했습니다.", "failed", True) from None
            time.sleep(2 ** attempt)

    def result(self, job: dict, **values):
        result = self.call("POST", f"/api/worker/jobs/{job['id']}/result", {
            "lease_id": job["lease_id"], "generation": job["generation"],
            "input_revision": job["transcript_revision"], **values,
        })
        job["transcript_revision"] = result["transcript_revision"]
        return result

    def download(self, job: dict, destination: Path) -> None:
        expected = int(job["byte_size"])
        if expected <= 0 or expected > 512 * 1024 * 1024:
            raise WorkerError("audio_size", "파일 크기가 허용 범위를 벗어났습니다.")
        if destination.is_file() and destination.stat().st_size == expected and sha256(destination) == job.get("sha256"):
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_suffix(".partial")
        if partial.exists() and partial.stat().st_size > expected:
            partial.unlink()
        start = partial.stat().st_size if partial.exists() else 0
        if start == expected:
            if sha256(partial) != job.get("sha256"):
                partial.unlink()
                raise WorkerError("checksum", "다운로드 체크섬이 일치하지 않습니다.", retryable=True)
            partial.replace(destination)
            return
        query = parse.urlencode({"lease_id": job["lease_id"], "generation": job["generation"]})
        headers = self.headers()
        if start:
            headers["Range"] = f"bytes={start}-"
        try:
            req = request.Request(self.config["base_url"] + f"/api/worker/jobs/{job['id']}/audio?{query}", headers=headers)
            with self.opener.open(req, timeout=60) as response:
                if start and response.status != 206:
                    start = 0
                if start and not response.headers.get("Content-Range", "").startswith(f"bytes {start}-"):
                    raise WorkerError("invalid_range", "서버가 다른 다운로드 구간을 반환했습니다.")
                with partial.open("ab" if start else "wb") as file:
                    for data in iter(lambda: response.read(1024 * 1024), b""):
                        if file.tell() + len(data) > expected:
                            raise WorkerError("audio_size", "다운로드 크기가 원본보다 큽니다.")
                        file.write(data)
        except (error.HTTPError, error.URLError, TimeoutError, OSError, http.client.HTTPException):
            raise WorkerError("download_failed", "다운로드가 중단되었습니다. 다음 실행에서 이어받습니다.", retryable=True) from None
        if partial.stat().st_size != expected or sha256(partial) != job.get("sha256"):
            partial.unlink(missing_ok=True)
            raise WorkerError("checksum", "다운로드 크기 또는 체크섬이 일치하지 않습니다.", retryable=True)
        partial.replace(destination)
