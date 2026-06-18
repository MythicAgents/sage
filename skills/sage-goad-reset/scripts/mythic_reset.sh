#!/usr/bin/env bash
# Reset the local Docker-backed Mythic deployment through mythic-cli.
set -euo pipefail

if [[ "${1:-}" != "--yes" ]]; then
  echo "ERR: Mythic database reset requires --yes" >&2
  exit 2
fi

MYTHIC_CLI="${MYTHIC_CLI:-/home/john/dev/mythic/mythic-cli}"
[[ -x "$MYTHIC_CLI" ]] || {
  echo "ERR: Mythic CLI not executable: $MYTHIC_CLI" >&2
  exit 1
}

"$MYTHIC_CLI" stop
"$MYTHIC_CLI" database reset -f
"$MYTHIC_CLI" start
