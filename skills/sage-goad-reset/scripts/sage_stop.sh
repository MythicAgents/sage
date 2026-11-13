#!/usr/bin/env bash
# Gracefully stop the local Sage dev process in the existing `sage` tmux session.
set -euo pipefail

SESSION="${SAGE_TMUX_SESSION:-sage}"
INITIAL_WAIT_SECONDS="${SAGE_STOP_INITIAL_WAIT_SECONDS:-60}"
RETRY_WAIT_SECONDS="${SAGE_STOP_RETRY_WAIT_SECONDS:-20}"
FINAL_WAIT_SECONDS="${SAGE_STOP_FINAL_WAIT_SECONDS:-20}"

for WAIT_SECONDS in "$INITIAL_WAIT_SECONDS" "$RETRY_WAIT_SECONDS" "$FINAL_WAIT_SECONDS"; do
  [[ "$WAIT_SECONDS" =~ ^[1-9][0-9]*$ ]] || {
    echo "ERR: Sage stop wait values must be positive integers" >&2
    exit 2
  }
done

if ! tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "Local Sage is already stopped; tmux session '$SESSION' is absent."
  exit 0
fi

PANE_PID="$(tmux list-panes -t "$SESSION" -F '#{pane_pid}' | head -1)"
PID="$(pgrep -P "$PANE_PID" -f 'main.py' | head -1 || true)"
if [[ -z "$PID" ]]; then
  echo "Local Sage is already stopped in tmux '$SESSION'."
  exit 0
fi

tmux send-keys -t "$SESSION" C-c
process_exited() {
  local state
  if ! kill -0 "$PID" 2>/dev/null; then
    return 0
  fi
  state="$(ps -o stat= -p "$PID" 2>/dev/null || true)"
  [[ "$state" == Z* ]]
}

# Check once more after the last sleep, so an exit in the final second of a wait still counts.
wait_for_exit() {
  local seconds="$1"
  process_exited && return 0
  for _ in $(seq 1 "$seconds"); do
    sleep 1
    process_exited && return 0
  done
  return 1
}

if wait_for_exit "$INITIAL_WAIT_SECONDS"; then
  echo "Stopped local Sage pid $PID in tmux '$SESSION'."
  exit 0
fi

tmux send-keys -t "$SESSION" C-c
if wait_for_exit "$RETRY_WAIT_SECONDS"; then
  echo "Stopped local Sage pid $PID in tmux '$SESSION'."
  exit 0
fi

# Python can reach interpreter finalization yet hang joining a background thread. A further
# terminal interrupt releases that join without escalating to TERM/KILL or hiding the failure.
tmux send-keys -t "$SESSION" C-c
if ! wait_for_exit "$FINAL_WAIT_SECONDS"; then
  echo "ERR: local Sage pid $PID is still running" >&2
  exit 1
fi
echo "Stopped local Sage pid $PID in tmux '$SESSION'."
