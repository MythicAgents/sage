#!/usr/bin/env bash
# sage_restart.sh — kill + relaunch the local Sage dev process INSIDE the existing `sage` tmux session,
# so it stays remotely reachable (`tmux attach -t sage`) and Luminara can reload code changes unattended.
#
# Approach: find the Sage process as the python child of the `sage` tmux pane, PRE-FLIGHT the relaunch
# interpreter, snapshot the running process's exact env from /proc, C-c the pane, then relaunch
# `python -u main.py` via _sage_relaunch.py with byte-identical env parity.
#
# Two bugs found + fixed on the first live round-trip (2026-06-05):
#   1) the real cmdline is `python main.py` (NOT `python3 -u main.py`) — pgrep pattern missed it.
#      Fix: locate the PID as the main.py child of the tmux pane, not by a cmdline pattern.
#   2) /proc/$PID/exe resolves the venv-python SYMLINK to /usr/bin/python3.13, which lacks
#      `mythic_container` -> relaunch crashed with ModuleNotFoundError and left Sage down.
#      Fix: relaunch with the VENV python and PROVE it imports the SDK BEFORE killing anything.
set -euo pipefail

SESSION="sage"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SNAP="/tmp/sage_env.snapshot"
VENV_PY="${SAGE_VENV_PY:-/home/john/dev/sage/.venv/bin/python}"
DEFAULT_CWD="${SAGE_CWD:-/home/john/dev/sage/Payload_Type/sage}"
MYTHIC_ENV_PATH="${MYTHIC_ENV_PATH:-/home/john/dev/mythic_v4/.env}"

snapshot_last_value() {
  local key="$1"
  tr '\0' '\n' < "$SNAP" \
    | awk -F= -v key="$key" '$1 == key { sub(/^[^=]*=/, ""); value=$0 } END { print value }'
}

append_snapshot_override() {
  local key="$1"
  local value="$2"
  printf '%s=%s\0' "$key" "$value" >> "$SNAP"
  echo "▶ env override: $key=$value" >&2
}

normalize_local_mythic_hosts() {
  [[ "${SAGE_LOCAL_MYTHIC_HOSTS:-1}" == "1" ]] || return

  local rabbit_host mythic_host nginx_host
  rabbit_host="$(snapshot_last_value RABBITMQ_HOST)"
  mythic_host="$(snapshot_last_value MYTHIC_SERVER_HOST)"
  nginx_host="$(snapshot_last_value NGINX_HOST)"

  case "$rabbit_host" in
    ""|rabbitmq|mythic_rabbitmq) append_snapshot_override "RABBITMQ_HOST" "127.0.0.1" ;;
  esac
  case "$mythic_host" in
    ""|mythic_server) append_snapshot_override "MYTHIC_SERVER_HOST" "127.0.0.1" ;;
  esac
  case "$nginx_host" in
    ""|mythic_nginx) append_snapshot_override "NGINX_HOST" "127.0.0.1" ;;
  esac
}

# 1. Locate Sage as the main.py child of the `sage` tmux pane (robust to the cmdline form).
tmux has-session -t "$SESSION" 2>/dev/null || { echo "ERR: tmux session '$SESSION' not found" >&2; exit 1; }
PANE_PID="$(tmux list-panes -t "$SESSION" -F '#{pane_pid}' | head -1)"
PID="$(pgrep -P "$PANE_PID" -f 'main.py' | head -1 || true)"

# 2. PRE-FLIGHT the relaunch interpreter BEFORE killing anything (never leave Sage down on a bad py).
[[ -x "$VENV_PY" ]] || { echo "ERR: venv python $VENV_PY not executable" >&2; exit 1; }
"$VENV_PY" -c "import mythic_container" 2>/dev/null \
  || { echo "ERR: $VENV_PY cannot import mythic_container — aborting, Sage left RUNNING" >&2; exit 1; }

if [[ -n "$PID" ]]; then
  CWD="$(readlink "/proc/$PID/cwd")"
  cp "/proc/$PID/environ" "$SNAP"; chmod 600 "$SNAP"
else
  CWD="$DEFAULT_CWD"
  [[ -d "$CWD" ]] || { echo "ERR: Sage cwd $CWD not found" >&2; exit 1; }
  if [[ -f "$MYTHIC_ENV_PATH" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$MYTHIC_ENV_PATH"
    set +a
  fi
  export DEBUG_LEVEL="${DEBUG_LEVEL:-debug}"
  export MYTHIC_SERVER_HOST="${MYTHIC_SERVER_HOST:-127.0.0.1}"
  export RABBITMQ_HOST="${RABBITMQ_HOST:-127.0.0.1}"
  [[ -n "${RABBITMQ_PASSWORD:-}" ]] || {
    echo "ERR: RABBITMQ_PASSWORD not set and not found in $MYTHIC_ENV_PATH" >&2
    exit 1
  }
  env -0 > "$SNAP"; chmod 600 "$SNAP"
  echo "▶ no running local Sage child under pane $PANE_PID; starting fresh with env from $MYTHIC_ENV_PATH" >&2
fi

normalize_local_mythic_hosts

# Optional env overrides: pass KEY=VAL args (e.g. `sage_restart.sh SAGE_ENGAGEMENT_GATE=1`).
# Appended after the snapshot so they WIN (dict-merge in _sage_relaunch.py = last value wins).
for kv in "$@"; do
  case "$kv" in *=*) printf '%s\0' "$kv" >> "$SNAP"; echo "▶ env override: $kv" >&2 ;; esac
done
echo "▶ snapshot: pid=$PID cwd=$CWD venv_py=$VENV_PY env=$(tr '\0' '\n' < "$SNAP" | grep -c =) vars" >&2

# 3. Stop: Ctrl-C the pane, wait for exit.
if [[ -n "$PID" ]]; then
  tmux send-keys -t "$SESSION" C-c
  for _ in $(seq 1 25); do kill -0 "$PID" 2>/dev/null || break; sleep 1; done
  if kill -0 "$PID" 2>/dev/null; then
    echo "WARN: pid $PID still alive after C-c; sending another C-c" >&2
    tmux send-keys -t "$SESSION" C-c; sleep 3
  fi
else
  tmux send-keys -t "$SESSION" C-c
fi

# 4. Relaunch with the VENV python + parity env.
tmux send-keys -t "$SESSION" "$VENV_PY $HERE/_sage_relaunch.py '$CWD' '$VENV_PY' '$SNAP'" Enter
echo "▶ relaunched into tmux '$SESSION' with $VENV_PY. Verify: ps --ppid $PANE_PID ; tmux capture-pane -t $SESSION -p | tail" >&2
