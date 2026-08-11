#!/usr/bin/env bash
# sage_restart.sh — kill + relaunch the local Sage dev process inside the existing `sage` tmux session.
#
# The launcher snapshots the current process env when Sage is already running, or builds one from the
# repo-local env files on a fresh start, then relaunches `python -u main.py` through _sage_relaunch.py.
set -euo pipefail

SESSION="sage"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../../.." && pwd)"
SNAP="/tmp/sage_env.snapshot"
VENV_PY="${SAGE_VENV_PY:-$REPO_ROOT/.venv/bin/python}"
DEFAULT_CWD="${SAGE_CWD:-$REPO_ROOT/Payload_Type/sage}"
# No checkout-name default: set MYTHIC_ENV_PATH to your install (see .env.example).
MYTHIC_ENV_PATH="${MYTHIC_ENV_PATH:-}"
SAGE_RUNTIME_ENV_PATH="${SAGE_RUNTIME_ENV_PATH:-$REPO_ROOT/Payload_Type/sage/.env}"
STARTUP_DISCOVERY_SECONDS="${SAGE_STARTUP_DISCOVERY_SECONDS:-10}"
STARTUP_STABILITY_SECONDS="${SAGE_STARTUP_STABILITY_SECONDS:-3}"

snapshot_last_value() {
  local key="$1"
  tr '\0' '\n' < "$SNAP" \
    | awk -F= -v key="$key" '$1 == key { sub(/^[^=]*=/, ""); value=$0 } END { print value }'
}

append_snapshot_override() {
  local key="$1"
  local value="$2"
  printf '%s=%s\0' "$key" "$value" >> "$SNAP"
  echo "▶ env override: $key=<set>" >&2
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

require_bloodhound_dir() {
  # SAGE_BLOODHOUND_MCP_DIR is Sage RUNTIME config — the process reads it to auto-connect the
  # baked-in BloodHound MCP (ai/bloodhound_config.py). Its home is therefore the runtime env file
  # ($SAGE_RUNTIME_ENV_PATH, i.e. Payload_Type/sage/.env), mirroring the container's own
  # `ENV SAGE_BLOODHOUND_MCP_DIR=/opt/bloodhound_mcp`.
  #
  # A fresh start already picks it up: that branch sources the runtime env with `set -a`. This
  # covers restarting an ALREADY-RUNNING Sage, where the snapshot comes from /proc/<pid>/environ and
  # the env file is never read — so a Sage first started without the variable would otherwise keep
  # inheriting its absence forever.
  #
  # No sibling-checkout guess. readiness_contract.py lists this in REQUIRED_STARTUP_ENV, so a wrong
  # silent default yields a Sage that starts, passes readiness, and is bound to a BloodHound
  # checkout the operator never chose.
  # Search the SAME files, in the same order, that Sage's own loader uses at startup
  # (dotenv_bootstrap.DOTENV_FILENAMES = `.env.local`, then `.env`). Reading only `.env` made this
  # launcher refuse to start a Sage whose variable was set in `.env.local` — the file the runtime
  # actually prefers — and then tell the operator to add it to the other file.
  #
  # Every pipeline below carries `|| true`, and the append is a full `if` rather than a
  # `[[ ... ]] && cmd` one-liner. Both are load-bearing under this script's `set -euo pipefail`: a
  # non-matching `grep` fails its pipeline, and a trailing `&&` list whose test is false returns 1,
  # and either one made the shell exit **silently, before reaching the error message below**. That
  # is exactly the failure the message exists to prevent, so the guard was mute in the one case it
  # was written for (found 2026-08-11: an operator sees only `exit 1` and no reason).
  local configured=""
  local env_file
  configured="$(snapshot_last_value SAGE_BLOODHOUND_MCP_DIR || true)"
  if [[ -z "$configured" ]]; then
    for env_file in "${SAGE_RUNTIME_ENV_PATH%/*}/.env.local" "$SAGE_RUNTIME_ENV_PATH"; do
      [[ -f "$env_file" ]] || continue
      configured="$(grep -E '^SAGE_BLOODHOUND_MCP_DIR=' "$env_file" | tail -1 | cut -d= -f2- | tr -d "\"'" || true)"
      if [[ -n "$configured" ]]; then
        append_snapshot_override "SAGE_BLOODHOUND_MCP_DIR" "$configured"
        break
      fi
    done
  fi
  if [[ -z "$configured" ]]; then
    echo "ERR: SAGE_BLOODHOUND_MCP_DIR is not set. Add it to ${SAGE_RUNTIME_ENV_PATH%/*}/.env.local" >&2
    echo "     or $SAGE_RUNTIME_ENV_PATH (Sage runtime env), or pass SAGE_BLOODHOUND_MCP_DIR=<dir>" >&2
    echo "     to this script. Sage cannot auto-connect BloodHound without it, and readiness" >&2
    echo "     requires it." >&2
    exit 1
  fi
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
  if [[ -f "$SAGE_RUNTIME_ENV_PATH" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$SAGE_RUNTIME_ENV_PATH"
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
  echo "▶ no running local Sage child under pane $PANE_PID; starting fresh from configured env files" >&2
fi

normalize_local_mythic_hosts
require_bloodhound_dir

# Optional env overrides: pass KEY=VAL args (e.g. `sage_restart.sh SAGE_ENGAGEMENT_GATE=1`).
# Appended after the snapshot so they WIN (dict-merge in _sage_relaunch.py = last value wins).
for kv in "$@"; do
  case "$kv" in
    *=*)
      printf '%s\0' "$kv" >> "$SNAP"
      echo "▶ env override: ${kv%%=*}=<set>" >&2
      ;;
  esac
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
NEW_PID=""
for _ in $(seq 1 "$STARTUP_DISCOVERY_SECONDS"); do
  NEW_PID="$(pgrep -P "$PANE_PID" -f 'main.py' | head -1 || true)"
  [[ -n "$NEW_PID" ]] && break
  sleep 1
done
[[ -n "$NEW_PID" ]] || {
  echo "ERR: Sage relaunch did not produce a main.py child under tmux pane $PANE_PID" >&2
  exit 1
}
for _ in $(seq 1 "$STARTUP_STABILITY_SECONDS"); do
  kill -0 "$NEW_PID" 2>/dev/null || {
    echo "ERR: Sage relaunch exited before the positive-start window completed" >&2
    exit 1
  }
  sleep 1
done
echo "▶ Sage relaunch verified in tmux '$SESSION'" >&2
