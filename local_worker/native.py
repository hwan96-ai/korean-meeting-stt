"""Run trusted native work outside the network/lease process's Python GIL."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

from .core import WorkerError


SECRET_ENVIRONMENT = {"OPENAI_API_KEY", "CODEX_API_KEY", "NOTION_TOKEN", "HF_TOKEN", "MEETING_NOTION_TOKEN_FILE",
                      "OAI_SITES_AUTHORIZATION", "SITES_DISPATCH_TOKEN", "DEVICE_TOKEN", "CREDENTIALS_DIRECTORY",
                      "MEETING_WORKER_GATEWAY_TOKEN", "MEETING_SERVER_DEVICE_TOKEN", "MEETING_WORKER_MODE"}


def start_isolated(command: list[str], payload: dict) -> subprocess.Popen:
    """Start the child without credentials; the caller polls, and must call `stop` when done."""
    env = {key: value for key, value in os.environ.items() if key.upper() not in SECRET_ENVIRONMENT}
    child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             cwd=Path(__file__).resolve().parents[1], env=env,
                             creationflags=0x08000000 if os.name == "nt" else 0)
    try:
        child.stdin.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        child.stdin.close()
    except BaseException:
        stop(child)
        raise
    return child


def stop(child: subprocess.Popen) -> None:
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=10)


def run_isolated(command: list[str], payload: dict, check=lambda: None, timeout: float = 7200) -> int:
    child = start_isolated(command, payload)
    started = time.monotonic()
    try:
        while child.poll() is None:
            check()
            if time.monotonic() - started > timeout:
                raise WorkerError("native_timeout", "로컬 화자 분석의 제한 시간을 초과했습니다. 체크포인트는 보존했습니다.", retryable=True)
            time.sleep(0.1)
        check()  # A late cancellation/lease loss must win over a completed child.
        return child.returncode
    finally:
        stop(child)


DIARIZATION_COMMAND = [sys.executable, "-m", "local_worker.diarization_task"]


def diarization_child(task: dict, check=lambda: None, timeout: float = 7200) -> int:
    return run_isolated(DIARIZATION_COMMAND, task, check, timeout)
