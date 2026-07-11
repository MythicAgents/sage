#!/usr/bin/env bash
# Gracefully stop the local Sage dev process in the existing `sage` tmux session.
set -euo pipefail

SESSION="${SAGE_TMUX_SESSION:-sage}"

tmux has-session -t "$SESSION" 2>/dev/null || {
  echo "ERR: tmux session '$SESSION' not found" >&2
  exit 1
}

PANE_PID="$(tmux list-panes -t "$SESSION" -F '#{pane_pid}' | head -1)"
PID="$(pgrep -P "$PANE_PID" -f 'main.py' | head -1 || true)"
if [[ -z "$PID" ]]; then
  echo "Local Sage is already stopped in tmux '$SESSION'."
  exit 0
fi

tmux send-keys -t "$SESSION" C-c
wait_for_exit() {
  local seconds="$1"
  local state
  for _ in $(seq 1 "$seconds"); do
    if ! kill -0 "$PID" 2>/dev/null; then
      return 0
    fi
    state="$(ps -o stat= -p "$PID" 2>/dev/null || true)"
    [[ "$state" == Z* ]] && return 0
    sleep 1
  done
  return 1
}

if wait_for_exit 60; then
  echo "Stopped local Sage pid $PID in tmux '$SESSION'."
  exit 0
fi

tmux send-keys -t "$SESSION" C-c
if ! wait_for_exit 20; then
  echo "ERR: local Sage pid $PID is still running" >&2
  exit 1
fi
echo "Stopped local Sage pid $PID in tmux '$SESSION'."
