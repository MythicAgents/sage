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
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
cd "$REPO"

# Force the workflow's Xvfb (:99), NOT the caller's ambient DISPLAY (often :0/dead in an agent shell).
DISPLAY="${SAGE_RDP_DISPLAY:-:99}"; export DISPLAY
TARGET_IP="${1:-10.4.10.22}"
RUN_AS="${2:-NORTH\\samwell.tarly}"
shift $(( $# >= 2 ? 2 : $# )) || true
[ "${1:-}" = "--" ] && shift || true

# Resolve the run-as password durably and export it so BOTH the RDP opener and launch-existing see it.
if [ -z "${SAGE_RUN_AS_PASSWORD:-}" ]; then
  for f in "${SAGE_RUNAS_FILE:-}" "$HOME/.config/sage/runas.env" "${MYTHIC_ENV_PATH:-$HOME/dev/mythic_v4/.env}"; do
    [ -n "$f" ] && [ -f "$f" ] || continue
    v="$(grep -E '^SAGE_RUN_AS_PASSWORD=' "$f" | head -1 | cut -d= -f2- | tr -d "\"'")"
    [ -n "$v" ] && { export SAGE_RUN_AS_PASSWORD="$v"; break; }
  done
fi
if [ -z "${SAGE_RUN_AS_PASSWORD:-}" ]; then
  echo "launch_apollo_foothold: no run-as password — export SAGE_RUN_AS_PASSWORD or set it in ~/.config/sage/runas.env or the mythic .env" >&2
  exit 2
fi

# 1) Open the interactive RDP session under a PTY (backgrounded within THIS foreground process, so it
#    keeps its own controlling terminal and is not a job of the tool's background wrapper).
python3 "$HERE/open_rdp_session.py" --target-ip "$TARGET_IP" --run-as-user "$RUN_AS" \
  --display "$DISPLAY" --log /tmp/open_rdp_session.log &
RDP_PID=$!
cleanup(){ kill "$RDP_PID" 2>/dev/null || true; }
trap cleanup EXIT

# 2) Start the pre-staged Apollo and wait for the callback (launch-existing polls for the session,
#    starts SageApolloBootstrap, waits for check-in, then tsdiscon's the RDP).
"$REPO/.venv/bin/python" "$HERE/deploy_payload_via_ludus.py" launch-existing "$@"
