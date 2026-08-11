#!/usr/bin/env bash
# Reset the local Docker-backed Mythic deployment through mythic-cli.
set -euo pipefail

if [[ "${1:-}" != "--yes" ]]; then
  echo "ERR: Mythic database reset requires --yes" >&2
  exit 2
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../../.." && pwd)"
WORKSPACE_ROOT="$(cd "$REPO_ROOT/.." && pwd)"
# No checkout-name default: set MYTHIC_CLI to your mythic-cli (see .env.example).
MYTHIC_CLI="${MYTHIC_CLI:-}"
[[ -x "$MYTHIC_CLI" ]] || {
  echo "ERR: Mythic CLI not executable: $MYTHIC_CLI" >&2
  exit 1
}

MYTHIC_DIR="$(cd "$(dirname "$MYTHIC_CLI")" && pwd)"
cd "$MYTHIC_DIR"

# Sage's .env uses Docker service hostnames that are valid inside the chat container but poison
# mythic-cli when inherited by this host-side reset script.
mythic_cli() {
  env -u RABBITMQ_HOST -u MYTHIC_SERVER_HOST -u NGINX_HOST "$MYTHIC_CLI" "$@"
}

mythic_cli stop
if [[ -d "$MYTHIC_DIR/postgres-docker/database" ]]; then
  docker run --rm \
    -v "$MYTHIC_DIR/postgres-docker/database:/db" \
    alpine sh -c 'rm -rf /db/* /db/.[!.]* /db/..?*'
else
  mythic_cli database reset -f
fi
mythic_cli start

# `mythic_cli start` above starts EVERY registered service, including the Sage container. Which
# Sage should actually serve Mythic is a deployment decision, not a side effect of a reset: this
# repo's workflow runs Sage locally in the `sage` tmux session, so the container must come back
# down. Left running, both register as `sage`, one wins the RabbitMQ queue, and requests are
# answered by whichever won — on 2026-08-01 that was the container, using its baked image instead
# of the working tree and with no BloodHound MCP directory.
#
# Override with SAGE_DEPLOYMENT_MODE=container to keep the container and stop the local process
# instead. Failing here is deliberate: a reset that leaves two Sages registered is worse than one
# that stops.
#
# `--conflict-only`: Sage itself is restarted later in the reset order, so only the unintended
# side must be down here, not the intended one up.
#
# SAGE_RESET_SKIP_DEPLOYMENT_ENFORCE exists for ONE caller: a test exercising this script's
# environment handling, which fakes `mythic-cli` but cannot fake `docker`. Without it such a test
# reads whatever Sage happens to be running on the developer's machine and fails or passes on that
# alone. Declaring the deployment check out of scope is not the same as declaring a mode: setting
# SAGE_DEPLOYMENT_MODE=container instead would make enforce stop the "unintended" LOCAL Sage by
# running sage_stop.sh for real, so a test run would kill a teammate's tmux session.
#
# NEVER set this for an operational reset. The split-brain guard is the entire point of the step:
# two Sages registered as `sage` means one silently wins the RabbitMQ queue.
if [[ "${SAGE_RESET_SKIP_DEPLOYMENT_ENFORCE:-0}" == "1" ]]; then
  echo "▶ deployment enforcement SKIPPED (SAGE_RESET_SKIP_DEPLOYMENT_ENFORCE=1) — tests only" >&2
else
  "$REPO_ROOT/.venv/bin/python" "$HERE/sage_deployment.py" enforce --conflict-only
fi
