"""Fixed local child entry point: no credentials, network clients, or arbitrary commands."""
from pathlib import Path
import json
import sys

from .core import WorkerError, atomic_json
from .speech import diarize_native


def main() -> int:
    task = json.load(sys.stdin)
    output = Path(task["output"])
    error_path = output.with_suffix(".error.json")
    error_path.unlink(missing_ok=True)
    try:
        diarize_native(Path(task["audio"]), output, task["config"])
        return 0
    except WorkerError as exc:
        atomic_json(error_path, {"code": exc.code, "message": str(exc), "state": exc.state, "retryable": exc.retryable})
        return 2
    except Exception:
        atomic_json(error_path, {"code": "diarization_native_error", "message": "로컬 화자 분석 중 오류가 발생했습니다. 모델과 메모리를 확인하세요.", "state": "failed", "retryable": True})
        return 2


if __name__ == "__main__":
    sys.exit(main())
