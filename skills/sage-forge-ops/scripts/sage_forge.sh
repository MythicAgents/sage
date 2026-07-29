#!/usr/bin/env bash
# sage_forge.sh — dispatch codex for SAGE code with the cyber-preview model HARDCODED.
#
# Why this exists: Sage's owner account is cyber-flagged, so offensive-security code must run on
# `gpt-5.5-cyber-preview`. Routing that choice through the Forge *agent* is fragile — the agent has
# its own "pinned model" judgment and once refused the override as a jailbreak (2026-06-04), running
# plain gpt-5.5 instead. This wrapper removes the agent's discretion: the model is a hardcoded flag,
# not an instruction anything can reinterpret. Probe-confirmed real: `--model gpt-5.5-cyber-preview`
# ran and returned (codex v0.135.0) on 2026-06-04.
#
# Usage:
#   skills/sage-forge-ops/scripts/sage_forge.sh "<task spec>"            # inline spec
#   skills/sage-forge-ops/scripts/sage_forge.sh -f spec.md               # spec from a file
#   skills/sage-forge-ops/scripts/sage_forge.sh --cd <dir> "<spec>"      # override working dir (default: repo root)
set -euo pipefail

MODEL="gpt-5.5-cyber-preview"   # HARDCODED — do not parameterize. The whole point is no override.
EFFORT="high"
CD="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"

SPEC=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -f) SPEC="$(cat "$2")"; shift 2;;
    --cd) CD="$2"; shift 2;;
    *) SPEC="$1"; shift;;
  esac
done

if [[ -z "$SPEC" ]]; then
  echo "usage: sage_forge.sh [-f spec.md | \"<spec>\"] [--cd <dir>]" >&2
  exit 2
fi

CODEX="$(command -v codex || echo "$HOME/.local/bin/codex")"
[[ -x "$CODEX" ]] || { echo "codex not found on PATH or ~/.local/bin" >&2; exit 1; }

echo "▶ sage_forge: model=$MODEL effort=$EFFORT cd=$CD" >&2
exec "$CODEX" exec \
  --model "$MODEL" \
  -c model_reasoning_effort="$EFFORT" \
  --sandbox workspace-write \
  --skip-git-repo-check \
  --cd "$CD" \
  "$SPEC"
