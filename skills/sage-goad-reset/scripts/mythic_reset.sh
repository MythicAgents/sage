#!/usr/bin/env bash
# Reset the local Docker-backed Mythic deployment through mythic-cli.
set -euo pipefail

if [[ "${1:-}" != "--yes" ]]; then
  echo "ERR: Mythic database reset requires --yes" >&2
  exit 2
fi

MYTHIC_CLI="${MYTHIC_CLI:-/home/john/dev/mythic_v4/mythic-cli}"
[[ -x "$MYTHIC_CLI" ]] || {
  echo "ERR: Mythic CLI not executable: $MYTHIC_CLI" >&2
  exit 1
}

MYTHIC_DIR="$(cd "$(dirname "$MYTHIC_CLI")" && pwd)"
cd "$MYTHIC_DIR"

"$MYTHIC_CLI" stop
if [[ -d "$MYTHIC_DIR/postgres-docker/database" ]]; then
  docker run --rm \
    -v "$MYTHIC_DIR/postgres-docker/database:/db" \
    alpine sh -c 'rm -rf /db/* /db/.[!.]* /db/..?*'
else
  "$MYTHIC_CLI" database reset -f
fi
"$MYTHIC_CLI" start
