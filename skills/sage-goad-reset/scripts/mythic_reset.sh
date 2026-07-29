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
