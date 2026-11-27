#!/usr/bin/env bash
# Start the local web app or speech worker set up by scripts/local-setup.mjs, or print a one-time owner login address.
#   scripts/local-run.sh web | worker | login     (KMS_DIR overrides ~/.korean-meeting-stt, PYTHON the venv python)
set -euo pipefail
dir="${KMS_DIR:-$HOME/.korean-meeting-stt}"
repo="$(cd "$(dirname "$0")/.." && pwd)"
[ -f "$dir/env" ] || { echo "run first: node scripts/local-setup.mjs" >&2; exit 1; }

# Read KEY=VALUE lines as data (never executed as shell); keep '=' inside values; tolerate CRLF from Windows editors.
load_env() {
  local line key
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"; key="${line%%=*}"
    [[ "$line" == *=* && "$key" =~ ^[A-Z_][A-Z0-9_]*$ ]] || continue
    export "$key=${line#*=}"
  done < "$dir/env"
}
check_local() {
  [ "${MEETING_AUTH_MODE:-}" = local ] || { echo "MEETING_AUTH_MODE=local missing in $dir/env" >&2; exit 1; }
  [ "${HOSTNAME:-}" = 127.0.0.1 ] || { echo "HOSTNAME must be 127.0.0.1 in $dir/env" >&2; exit 1; }
  [[ "${MEETING_PUBLIC_ORIGIN:-}" =~ ^http://127\.0\.0\.1:([0-9]+)$ && "${BASH_REMATCH[1]}" = "${PORT:-}" ]] \
    || { echo "MEETING_PUBLIC_ORIGIN must be http://127.0.0.1:\$PORT in $dir/env" >&2; exit 1; }
}
# Local traffic must never go through an HTTP proxy (the worker sends its tokens to the web app).
export NO_PROXY="127.0.0.1,localhost,::1,[::1]${NO_PROXY:+,$NO_PROXY}" no_proxy="127.0.0.1,localhost,::1,[::1]${no_proxy:+,$no_proxy}"

case "${1:-}" in
  web)
    [ -f "$repo/.next/standalone/server.js" ] || { echo "run first: npm run build:oci" >&2; exit 1; }
    load_env; check_local
    echo "web: $MEETING_PUBLIC_ORIGIN  (new browser: scripts/local-run.sh login)"
    cd "$repo/.next/standalone"
    exec node server.js ;;
  worker)
    python="${PYTHON:-$repo/.venv-worker/bin/python}"
    export MEETING_WORKER_MODE=server MEETING_WORKER_HOME="$dir/worker" CREDENTIALS_DIRECTORY="$dir/credentials"
    cd "$repo"
    # Install missing models until the doctor passes (an interrupted first download is retried).
    if ! "$python" -m local_worker.server --doctor >/dev/null 2>&1; then
      model="$("$python" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("whisper_model", "base"))' "$dir/credentials/worker.json")"
      "$python" -m local_worker setup-models --whisper "$model" --diarization
      "$python" -m local_worker.server --doctor >/dev/null \
        || { echo "models still fail the check; if a download was cut off, move $dir/worker/models away and retry" >&2; exit 1; }
    fi
    exec "$python" -m local_worker.server ;;
  login)
    load_env; check_local
    # One-time address, valid 10 minutes. Open it in your browser on this computer; do not send it to anyone.
    exec node "$repo/scripts/local-login.mjs" ;;
  *) echo "usage: $0 web|worker|login" >&2; exit 2 ;;
esac
