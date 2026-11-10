#!/usr/bin/env bash
# Harness-agnostic ONE-COMMAND Apollo foothold launch — works identically for Codex, Claude Code's
# Bash tool, and unattended/cron runs. It opens the interactive Samwell RDP session under a
# self-allocated PTY (so it does NOT depend on the caller's shell having a controlling TTY), then
# starts the pre-staged Apollo via `launch-existing` and waits for the callback.
#
# Why this exists: `xfreerdp3`'s NTLM/NLA path dies pre-auth (exit 144) when it has no controlling
# terminal. Codex supplies one via tty:true; Claude Code / cron do not. `open_rdp_session.py` wraps
# xfreerdp3 in pty.fork() to give it its own controlling terminal regardless of the caller.
#
# The run-as password resolves durably (env -> ~/.config/sage/runas.env -> mythic .env) so a fresh,
# env-less shell still authenticates without an operator export.
#
# Usage:  launch_apollo_foothold.sh [target-ip] [run-as-user] [-- extra launch-existing args...]
#   e.g.  launch_apollo_foothold.sh 10.4.10.22 'NORTH\samwell.tarly'
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
cd "$REPO"

TARGET_IP="${1:-10.4.10.22}"
RUN_AS="${2:-NORTH\\samwell.tarly}"
shift $(( $# >= 2 ? 2 : $# )) || true
[ "${1:-}" = "--" ] && shift || true

# The canonical launcher relies on Mythic's operator configuration later in launch-existing.
# Fail locally before any target-facing helper when that explicit discovery binding is unusable.
if [ -z "${MYTHIC_ENV_PATH:-}" ]; then
  echo "launch_apollo_foothold: MYTHIC_ENV_PATH must name a readable regular file" >&2
  exit 2
fi
if [ ! -f "$MYTHIC_ENV_PATH" ] || [ ! -r "$MYTHIC_ENV_PATH" ]; then
  echo "launch_apollo_foothold: MYTHIC_ENV_PATH must name a readable regular file" >&2
  exit 2
fi

# The retained-launch arguments also carry the target host selector. Reuse it for the
# pre-RDP localuser logoff instead of letting that helper fall back to CASTELBLACK.
TARGET_HOST="CASTELBLACK"
EXTRA_ARGS=("$@")
for ((i = 0; i < ${#EXTRA_ARGS[@]}; i++)); do
  case "${EXTRA_ARGS[$i]}" in
    --target-host)
      if (( i + 1 >= ${#EXTRA_ARGS[@]} )); then
        echo "launch_apollo_foothold: --target-host requires a value" >&2
        exit 2
      fi
      TARGET_HOST="${EXTRA_ARGS[$((i + 1))]}"
      ;;
    --target-host=*)
      TARGET_HOST="${EXTRA_ARGS[$i]#*=}"
      ;;
    --ludus-range-id)
      if (( i + 1 >= ${#EXTRA_ARGS[@]} )); then
        echo "launch_apollo_foothold: --ludus-range-id requires a value" >&2
        exit 2
      fi
      export SAGE_LUDUS_RANGE_ID="${EXTRA_ARGS[$((i + 1))]}"
      ;;
    --ludus-range-id=*)
      export SAGE_LUDUS_RANGE_ID="${EXTRA_ARGS[$i]#*=}"
      ;;
  esac
done

# Prefer an explicitly configured display. Otherwise select a live local display instead of assuming
# the historical :99 Xvfb still exists.
if [ -n "${SAGE_RDP_DISPLAY:-}" ]; then
  DISPLAY="$SAGE_RDP_DISPLAY"
elif pgrep -f '/Xwayland :0 ' >/dev/null && [ -S /tmp/.X11-unix/X0 ]; then
  DISPLAY=:0
elif pgrep -f 'Xvfb :99 ' >/dev/null && [ -S /tmp/.X11-unix/X99 ]; then
  DISPLAY=:99
else
  echo "launch_apollo_foothold: no live Xwayland :0 or Xvfb :99 display found" >&2
  exit 2
fi
export DISPLAY

if [ "$DISPLAY" = ":0" ] && [ -z "${XAUTHORITY:-}" ]; then
  XAUTHORITY="$(
    ps -eo args= | awk '
      $1 ~ /Xwayland$/ && $2 == ":0" {
        for (i = 1; i <= NF; i++) if ($i == "-auth" && i < NF) { print $(i + 1); exit }
      }
    '
  )"
  [ -n "$XAUTHORITY" ] && [ -f "$XAUTHORITY" ] || {
    echo "launch_apollo_foothold: could not resolve XAUTHORITY for Xwayland :0" >&2
    exit 2
  }
  export XAUTHORITY
fi

# Resolve the run-as password durably and export it so BOTH the RDP opener and launch-existing see it.
if [ -z "${SAGE_RUN_AS_PASSWORD:-}" ]; then
  for f in \
    "$REPO/skills/sage-foothold-launch/.env" \
    "${SAGE_RUNAS_FILE:-}" \
    "$HOME/.config/sage/runas.env" \
    "$REPO/Payload_Type/sage/.env" \
    "${MYTHIC_ENV_PATH:-}"
  do
    [ -n "$f" ] && [ -f "$f" ] || continue
    v="$(grep -E '^SAGE_RUN_AS_PASSWORD=' "$f" | head -1 | cut -d= -f2- | tr -d "\"'" || true)"
    [ -n "$v" ] && { export SAGE_RUN_AS_PASSWORD="$v"; break; }
  done
fi
if [ -z "${SAGE_RUN_AS_PASSWORD:-}" ]; then
  echo "launch_apollo_foothold: no run-as password — export SAGE_RUN_AS_PASSWORD or set it in Sage's .env or ~/.config/sage/runas.env" >&2
  exit 2
fi

# 1) Clear the snapshot's local console user before opening Samwell's RDP session. Retry while WinRM
#    finishes booting, but never log off any other account.
LOGOFF_LOG=/tmp/sage_localuser_logoff.log
LOGOFF_ARGS=()
# CASTELBLACK autologs localuser on after WinRM first responds. An empty
# census during boot is not proof that its interactive slot was freed.
if [ "$TARGET_HOST" != "CASTELBLACK" ]; then
  LOGOFF_ARGS+=(--allow-already-absent)
fi
for attempt in $(seq 1 30); do
  if "$REPO/.venv/bin/python" "$HERE/deploy_payload_via_ludus.py" logoff-user \
      --target-host "$TARGET_HOST" --target-ip "$TARGET_IP" --username localuser \
      "${LOGOFF_ARGS[@]}" >"$LOGOFF_LOG" 2>&1; then
    echo "Apollo foothold: localuser session cleared"
    break
  fi
  if [ "$attempt" -eq 30 ]; then
    cat "$LOGOFF_LOG" >&2
    echo "launch_apollo_foothold: localuser logoff failed after 300s" >&2
    exit 2
  fi
  sleep 10
done

# 2) Open the interactive RDP session under a PTY. A VM can report powered-on before its domain
#    controller can service NLA, so retry authentication instead of treating STATUS_NO_LOGON_SERVERS
#    as a terminal reset failure.
rdp_retry() {
  local attempt
  for attempt in $(seq 1 60); do
    if python3 "$HERE/open_rdp_session.py" --target-ip "$TARGET_IP" --run-as-user "$RUN_AS" \
        --display "$DISPLAY" --log /tmp/open_rdp_session.log; then
      return 0
    fi
    sleep 10
  done
  echo "launch_apollo_foothold: RDP authentication did not succeed within 600s" >&2
  return 2
}
rdp_retry &
RDP_PID=$!

# Local cleanup runs on every exit. Killing the retry subshell alone leaves the opener (and its xfreerdp3) running,
# so: freeze the retry loop (no new opener can start), collect its children, end the loop, and ask each opener to
# exit (it closes its own RDP client on SIGTERM). Survivors past the grace period are killed and the cleanup is
# reported as failed. The launch result is kept: a failed launch stays failed, and a successful launch whose
# local cleanup failed exits non-zero and says both.
cleanup() {
  local rc=$? status=ok pids="" pid alive deadline
  trap - EXIT
  if [ -n "${RDP_PID:-}" ] && kill -0 "$RDP_PID" 2>/dev/null; then
    kill -STOP "$RDP_PID" 2>/dev/null || true
    pids="$(pgrep -P "$RDP_PID" 2>/dev/null | tr '\n' ' ' || true)"
    kill -KILL "$RDP_PID" 2>/dev/null || true
    wait "$RDP_PID" 2>/dev/null || true
    for pid in $pids; do kill -TERM "$pid" 2>/dev/null || true; done
    deadline=$(( $(date +%s) + ${SAGE_RDP_CLEANUP_SECONDS:-15} ))
    while :; do
      alive=""
      for pid in $pids; do kill -0 "$pid" 2>/dev/null && alive="$alive $pid"; done
      [ -z "$alive" ] && break
      if [ "$(date +%s)" -ge "$deadline" ]; then
        for pid in $alive; do kill -KILL "$pid" 2>/dev/null || true; done
        status="failed (opener did not exit on SIGTERM:$alive; killed, its RDP client may remain)"
        break
      fi
      sleep 0.1
    done
  fi
  echo "launch_apollo_foothold: local RDP client cleanup: $status" >&2
  if [ "$status" != ok ] && [ "$rc" -eq 0 ]; then
    echo "launch_apollo_foothold: the launch succeeded, but its local cleanup failed" >&2
    rc=2
  fi
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP

# 3) Start the pre-staged Apollo and wait for the callback (launch-existing polls for the session,
#    starts SageApolloBootstrap, waits for check-in, then tsdiscon's the RDP on every exit path).
"$REPO/.venv/bin/python" "$HERE/deploy_payload_via_ludus.py" launch-existing \
  --target-ip "$TARGET_IP" --run-as-user "$RUN_AS" \
  --wait-interactive-session-seconds 600 "$@"
