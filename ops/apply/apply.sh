#!/bin/sh
# One apply container runs a deploy: build (unless SKIP_BUILD), then compose up.
# The env is the only input. Every step prints `==` so `podman logs deploy-<name>` reads as the full story.
set -eu

: "${COMPOSE_PROJECT_NAME:?}" "${COMPOSE_FILE:?}"

dc() {
  docker-compose "$@"
}

# The repo compose has to put service app on network yard. A missing env file or
# data mount is a warning. The default compose already meets the contract.
check() {
  case "$COMPOSE_FILE" in
    */default.compose.yml) return 0 ;;
  esac
  oldifs=$IFS
  IFS=:
  for file in $COMPOSE_FILE; do
    [ -f "$file" ] || continue
    grep -q '\${APP_ENV}' "$file" || echo "warning: compose: no service attaches env_file \${APP_ENV}; the project env is not passed"
    grep -q '\${DATA_DIR}' "$file" || echo "warning: compose: no service mounts \${DATA_DIR}; the project has no /data"
  done
  IFS=$oldifs
  case "$1" in
    up|start) ;;
    *) return 0 ;;
  esac
  cfg=$(dc config) || exit 1
  printf '%s\n' "$cfg" | awk '
    /^  app:/ { inapp = 1; next }
    inapp && /^  [^ ]/ { inapp = 0 }
    inapp && /^    networks:/ { innet = 1; next }
    inapp && innet && /^    [^ ]/ { innet = 0 }
    inapp && innet && (($1 == "-" && $2 == "yard") || $1 == "yard:") { found = 1 }
    END { exit found ? 0 : 1 }
  ' || {
    echo "compose: service app is not on network yard; the dial is ${COMPOSE_PROJECT_NAME}-app-1:8000" >&2
    exit 1
  }
}

build() {
  echo "== build $IMAGE"
  case "$COMPOSE_FILE" in
    # The default contract is one image from CHECKOUT; podman-remote builds it
    # in ~2s where dc build needs ~18s on the same socket.
    */default.compose.yml)
      podman-remote build -t "$IMAGE" -f "$CHECKOUT/${DOCKERFILE:-Containerfile}" "$CHECKOUT" || exit 1
      ;;
    *)
      dc build || exit 1
      ;;
  esac
  echo "built $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
}

# The deploy gate: the app container must stay running for SETTLE_SECONDS.
# A crash loop or a stop inside the window is unhealthy, and the deploy reverts.
settle() {
  limit="${SETTLE_SECONDS:-20}"
  app_container="${COMPOSE_PROJECT_NAME}-app-1"
  restarts=-1
  i=0
  while [ "$i" -lt "$limit" ]; do
    state="$(podman-remote inspect -f '{{.State.Running}} {{.RestartCount}}' "$app_container" 2>/dev/null)" || {
      echo "== unhealthy: $app_container did not stay up"
      return 1
    }
    running="$(printf '%s' "$state" | awk '{print $1}')"
    count="$(printf '%s' "$state" | awk '{print $2}')"
    if [ "$running" != "true" ]; then
      echo "== unhealthy: $app_container is not running"
      return 1
    fi
    if [ "$restarts" -ge 0 ] && [ "$count" -gt "$restarts" ]; then
      echo "== unhealthy: $app_container restarted"
      return 1
    fi
    restarts="$count"
    i=$((i + 2))
    if [ "$i" -lt "$limit" ]; then
      sleep 2
    fi
  done
  echo "== settled"
}

revert() {
  if [ -n "${PREVIOUS_IMAGE:-}" ]; then
    echo "== revert $PREVIOUS_IMAGE"
    if IMAGE="$PREVIOUS_IMAGE" dc up -d --no-build; then
      echo "== reverted $PREVIOUS_IMAGE"
      exit 3
    fi
    echo "== revert failed"
    exit 1
  fi
  exit 1
}

up() {
  if [ "${SKIP_BUILD:-}" = "1" ]; then
    echo "== reuse $IMAGE"
  else
    build
  fi
  echo "== start $IMAGE"
  dc up -d --wait --remove-orphans --no-build || {
    echo "== unhealthy: up failed"
    revert
  }
  settle || revert
  echo "== started $IMAGE"
}

case "${1:-}" in
  build|up|start) check "$1" ;;
esac

case "${1:-}" in
  build) build ;;
  up) up ;;
  start)
    echo "== start $IMAGE"
    dc up -d --wait --no-build
    ;;
  down) dc down ;;
  purge) dc down -v ;;
  *)
    echo "usage: apply {build|up|start|down|purge}" >&2
    exit 2
    ;;
esac
