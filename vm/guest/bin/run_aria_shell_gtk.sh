#!/usr/bin/env bash
set -euo pipefail
CLIENT_DIR="${ARIA_CLIENT_DIR:-/opt/aria-client}"
PY_APP_DIR="${ARIA_CLIENT_PY_DIR:-$CLIENT_DIR/lib/.bundle.dat}"
RUNTIME_ENV_FILE="${ARIA_CLIENT_RUNTIME_ENV:-$CLIENT_DIR/share/AriaApp/runtime.env}"
USER_RUNTIME_ENV_FILE="${ARIA_CLIENT_USER_RUNTIME_ENV:-$HOME/.ariaos/runtime.env}"
export ARIA_CLIENT_DIR="$CLIENT_DIR"
export ARIA_CLIENT_WORKSPACE_DIR="${ARIA_CLIENT_WORKSPACE_DIR:-$HOME/AriaWorkspace}"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/ariaos"
REQUESTED_VIEW_PATH="$STATE_DIR/requested_start_view"
ONBOARDING_STATE_PATH="$STATE_DIR/onboarding_state.json"
LOCAL_SECRETS_FILE="${HOME}/.config/ariaos/local_agent_secrets.json"
PYTHON_BIN="${ARIA_SYSTEM_PYTHON:-/usr/bin/python3}"
if [[ ! -x "$PYTHON_BIN" ]]; then
    PYTHON_BIN="$(command -v python3)"
fi

mkdir -p "$STATE_DIR"
load_runtime_env() {
    local env_file="$1"
    if [[ -f "$env_file" ]]; then
        set -a
        # shellcheck disable=SC1090
        source "$env_file"
        set +a
    fi
}

load_runtime_env "$RUNTIME_ENV_FILE"
if [[ "$USER_RUNTIME_ENV_FILE" != "$RUNTIME_ENV_FILE" ]]; then
    load_runtime_env "$USER_RUNTIME_ENV_FILE"
fi
if [[ -z "${ARIA_SHELL_START_VIEW:-}" ]]; then
    if "$PYTHON_BIN" - "$ONBOARDING_STATE_PATH" "$LOCAL_SECRETS_FILE" <<'PY' >/dev/null 2>&1
import json
import sys
from pathlib import Path

onboarding_path = Path(sys.argv[1])
secrets_path = Path(sys.argv[2])

try:
    onboarding = json.loads(onboarding_path.read_text(encoding="utf-8"))
except Exception:
    onboarding = {}

try:
    secrets = json.loads(secrets_path.read_text(encoding="utf-8"))
except Exception:
    secrets = {}

done = bool(onboarding.get("done"))
api_key_ready = bool(str(secrets.get("openai_api_key") or "").strip())
sys.exit(0 if (not done and not api_key_ready) else 1)
PY
    then
        export ARIA_SHELL_START_VIEW="intro"
    fi
fi
if [[ -n "${ARIA_SHELL_START_VIEW:-}" ]]; then
    printf '%s\n' "$ARIA_SHELL_START_VIEW" >"$REQUESTED_VIEW_PATH"
fi

if "$PYTHON_BIN" - <<'PY' >/dev/null 2>&1
import importlib.util
import sys
sys.exit(0 if importlib.util.find_spec('gi') else 1)
PY
then
    export PYTHONPATH="$PY_APP_DIR${PYTHONPATH:+:$PYTHONPATH}"
    exec "$PYTHON_BIN" -m aria_shell_gtk
fi

echo "GTK4/libadwaita runtime is required to launch Aria." >&2
exit 1
