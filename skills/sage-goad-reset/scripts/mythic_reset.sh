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

SAGE_DEPLOYMENT_MODE="${SAGE_DEPLOYMENT_MODE:-local}"
case "$SAGE_DEPLOYMENT_MODE" in
  local|container) ;;
  *) echo "ERR: unknown SAGE_DEPLOYMENT_MODE: $SAGE_DEPLOYMENT_MODE" >&2; exit 1 ;;
esac

RESET_MUTATION_STARTED=0
enforce_deployment_on_exit() {
  local result=$?
  trap - EXIT
  # A partial local startup must not leave a second Sage registered, even when a later
  # service fails. Container mode keeps its existing successful-reset enforcement.
  if [[ "$RESET_MUTATION_STARTED" == "1" \
    && ( "$SAGE_DEPLOYMENT_MODE" == "local" || "$result" == "0" ) ]]; then
    if [[ "${SAGE_RESET_SKIP_DEPLOYMENT_ENFORCE:-0}" == "1" ]]; then
      echo "▶ deployment enforcement SKIPPED (SAGE_RESET_SKIP_DEPLOYMENT_ENFORCE=1) — tests only" >&2
    elif ! "$REPO_ROOT/.venv/bin/python" "$HERE/sage_deployment.py" enforce --conflict-only; then
      echo "ERR: Sage deployment conflict could not be resolved" >&2
      result=1
    fi
  fi
  exit "$result"
}
trap enforce_deployment_on_exit EXIT

# Sage's .env uses Docker service hostnames that are valid inside the chat container but poison
# mythic-cli when inherited by this host-side reset script.
mythic_cli() {
  env -u RABBITMQ_HOST -u MYTHIC_SERVER_HOST -u NGINX_HOST \
    -u COMPOSE_FILE -u COMPOSE_PROJECT_NAME "$MYTHIC_CLI" "$@"
}

mythic_docker() {
  timeout -k 5s 30s \
    env -u RABBITMQ_HOST -u MYTHIC_SERVER_HOST -u NGINX_HOST \
      -u COMPOSE_FILE -u COMPOSE_PROJECT_NAME docker "$@"
}

targeted_mythic_start() {
  timeout -k 5s 180s \
    env -u RABBITMQ_HOST -u MYTHIC_SERVER_HOST -u NGINX_HOST \
      -u COMPOSE_FILE -u COMPOSE_PROJECT_NAME "$MYTHIC_CLI" start "$@"
}

metered_docker_start() {
  timeout -k 5s 180s \
    env -u RABBITMQ_HOST -u MYTHIC_SERVER_HOST -u NGINX_HOST \
      -u COMPOSE_FILE -u COMPOSE_PROJECT_NAME docker "$@"
}

inspect_started_services() {
  local container_ids_output state_output row service running health extra
  local -a container_ids=()
  local -A seen=()
  FAILED_SERVICES=()

  container_ids_output="$(mythic_docker compose ps --all --quiet)" || {
    echo "ERR: unable to enumerate Mythic service containers" >&2
    return 2
  }
  mapfile -t container_ids <<< "$container_ids_output"
  if [[ ${#container_ids[@]} -eq 0 || -z "${container_ids[0]}" ]]; then
    FAILED_SERVICES=("${REQUIRED_SERVICES[@]}")
    return 1
  fi
  state_output="$(mythic_docker inspect --format '{{index .Config.Labels "com.docker.compose.service"}}|{{.State.Running}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${container_ids[@]}")" || {
    echo "ERR: unable to inspect Mythic service containers" >&2
    return 2
  }
  while IFS='|' read -r service running health extra; do
    if [[ -z "$service" || -n "$extra" || -n "${seen[$service]+set}" ]]; then
      echo "ERR: ambiguous Mythic service-container state" >&2
      return 2
    fi
    seen["$service"]="$running|$health"
  done <<< "$state_output"

  for service in "${REQUIRED_SERVICES[@]}"; do
    row="${seen[$service]-missing}"
    [[ "$row" == "true|none" || "$row" == "true|healthy" ]] || FAILED_SERVICES+=("$service")
  done
  ((${#FAILED_SERVICES[@]} == 0))
}

add_required_service() {
  local service="$1"
  [[ "$SAGE_DEPLOYMENT_MODE" == "local" && "$service" == "sage" ]] && return 0
  REQUIRED_SERVICES+=("$service")
}

if [[ "${SAGE_RESET_METERED_DOCKER:-0}" == "1" ]]; then
  [[ -n "${SAGE_RESOURCE_BUDGET_CONFIG:-}" && -f "$SAGE_RESOURCE_BUDGET_CONFIG" \
    && -n "${SAGE_RESOURCE_BUDGET_LEDGER:-}" && -f "$SAGE_RESOURCE_BUDGET_LEDGER" ]] || {
    echo "ERR: metered Docker reset requires the configured shared resource ledger" >&2
    exit 1
  }
  [[ "${DOCKER_HOST:-}" == unix:///* ]] || {
    echo "ERR: metered Docker reset requires an explicit Unix Docker proxy" >&2
    exit 1
  }
  docker_proxy_path="${DOCKER_HOST#unix://}"
  [[ "$docker_proxy_path" == /* && ! -L "$docker_proxy_path" && -S "$docker_proxy_path" ]] || {
    echo "ERR: metered Docker proxy is unavailable" >&2
    exit 1
  }
  compose_config_output="$(mythic_docker compose config --format json)" || {
    echo "ERR: unable to inspect configured Mythic services" >&2
    exit 1
  }
  service_images_output="$(printf '%s' "$compose_config_output" | "$REPO_ROOT/.venv/bin/python" -c '
import json,re,sys
try:
 data=json.load(sys.stdin); services=data["services"]
 if not isinstance(services,dict) or not services: raise ValueError
 rows=[]
 for name,value in services.items():
  image=value.get("image") if isinstance(value,dict) else None
  if not isinstance(name,str) or not re.fullmatch(r"[A-Za-z0-9._-]+",name): raise ValueError
  if not isinstance(image,str) or not image or any(c.isspace() for c in image): raise ValueError
  rows.append((name,image))
 for name,image in rows: print(name+"\t"+image)
except Exception:
 raise SystemExit(2)
')" || {
    echo "ERR: Mythic compose configuration is unsuitable for a no-build reset" >&2
    exit 1
  }
  REQUIRED_SERVICES=()
  while IFS=$'\t' read -r service image extra; do
    [[ -n "$service" && -n "$image" && -z "$extra" ]] || {
      echo "ERR: ambiguous Mythic compose service image" >&2
      exit 1
    }
    if [[ "$SAGE_DEPLOYMENT_MODE" == "local" && "$service" == "sage" ]]; then
      continue
    fi
    image_id="$(mythic_docker image inspect --format '{{.Id}}' "$image")" || {
      echo "ERR: a configured Mythic service image is not installed" >&2
      exit 1
    }
    [[ -n "$image_id" ]] || {
      echo "ERR: a configured Mythic service image has no installed ID" >&2
      exit 1
    }
    add_required_service "$service"
  done <<< "$service_images_output"
  [[ ${#REQUIRED_SERVICES[@]} -gt 0 ]] || {
    echo "ERR: Mythic compose configuration has no services" >&2
    exit 1
  }
  alpine_image_id="$(mythic_docker image inspect --format '{{.Id}}' alpine)" || {
    echo "ERR: reset wipe image is not installed" >&2
    exit 1
  }
  [[ -n "$alpine_image_id" ]] || {
    echo "ERR: reset wipe image has no installed ID" >&2
    exit 1
  }
  [[ -d "$MYTHIC_DIR/postgres-docker/database" ]] || {
    echo "ERR: Mythic postgres database directory is unavailable" >&2
    exit 1
  }
  RESET_MUTATION_STARTED=1
  mythic_docker compose stop
  mythic_docker run --rm --pull never \
    -v "$MYTHIC_DIR/postgres-docker/database:/db" \
    alpine sh -c 'rm -rf /db/* /db/.[!.]* /db/..?*'
  metered_docker_start compose up -d --no-build --pull never "${REQUIRED_SERVICES[@]}"
else
  required_services_output="$(mythic_docker compose config --services)" || {
    echo "ERR: unable to enumerate configured Mythic services" >&2
    exit 1
  }
  REQUIRED_SERVICES=()
  while IFS= read -r service; do
    [[ -n "$service" ]] && add_required_service "$service"
  done <<< "$required_services_output"
  [[ ${#REQUIRED_SERVICES[@]} -gt 0 ]] || {
    echo "ERR: Mythic compose configuration has no services for the selected deployment mode" >&2
    exit 1
  }
  RESET_MUTATION_STARTED=1
  mythic_cli stop
  if [[ -d "$MYTHIC_DIR/postgres-docker/database" ]]; then
    docker run --rm \
      -v "$MYTHIC_DIR/postgres-docker/database:/db" \
      alpine sh -c 'rm -rf /db/* /db/.[!.]* /db/..?*'
  else
    mythic_cli database reset -f
  fi
  if [[ "$SAGE_DEPLOYMENT_MODE" == "local" ]]; then
    mythic_cli start "${REQUIRED_SERVICES[@]}"
  else
    mythic_cli start
  fi
fi
if inspect_started_services; then
  :
else
  inspect_result=$?
  ((inspect_result == 1)) || exit "$inspect_result"
  printf 'WARN: retrying failed configured Mythic services:' >&2
  printf ' %s' "${FAILED_SERVICES[@]}" >&2
  printf '\n' >&2
  if [[ "${SAGE_RESET_METERED_DOCKER:-0}" == "1" ]]; then
    metered_docker_start compose up -d --no-build --pull never "${FAILED_SERVICES[@]}"
  else
    targeted_mythic_start "${FAILED_SERVICES[@]}"
  fi
  if ! inspect_started_services; then
    echo "ERR: configured Mythic services are not ready after targeted recovery" >&2
    exit 1
  fi
fi

# The exit trap checks deployment after a successful reset and after any partial local reset.
