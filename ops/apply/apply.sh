#!/bin/sh
# One apply container runs a whole deploy: build, start, the health probe,
# and the revert when a step fails. The env is the only input. Every step
# prints `==` so `podman logs yard-apply-<slot>` reads as the full story.
set -eu

: "${COMPOSE_PROJECT_NAME:?}" "${COMPOSE_FILE:?}"

dc() {
  docker-compose "$@"
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

revert() {
  # Process env beats compose.env in interpolation, so no file edit is needed.
  if [ -n "${PREVIOUS:-}" ] && [ "$PREVIOUS" != "$IMAGE" ]; then
    echo "== revert $PREVIOUS"
    if IMAGE="$PREVIOUS" SHA="${PREVIOUS##*:}" dc up -d --wait --no-build; then
      echo "== kept $PREVIOUS"
    else
      echo "== revert $PREVIOUS failed"
    fi
  fi
  exit 1
}

up() {
  build

  # Caddy's seed file lives on a stable path so compose leaves caddy alone;
  # the running config comes from the admin socket and autosave anyway.
  if [ -n "${YARD_ROOT:-}" ] && [ -f "$CHECKOUT/ops/caddy.json" ]; then
    mkdir -p "$YARD_ROOT/run"
    cp "$CHECKOUT/ops/caddy.json" "$YARD_ROOT/run/caddy.json"
  fi

  echo "== start $IMAGE"
  dc up -d --wait --remove-orphans --no-build || revert

  url="${HEALTH_URL:-http://${SLOT_NAME:?}:8000/}"
  echo "== wait $url"
  try=0
  while [ "$try" -lt "${WAIT_SECONDS:-30}" ]; do
    code=0
    wget -q -O /dev/null -T 3 "$url" || code=$?
    # Exit 8 means the server answered with an error status: it is up.
    if [ "$code" -eq 0 ] || [ "$code" -eq 8 ]; then
      echo "== ready $IMAGE"
      exit 0
    fi
    try=$((try + 1))
    sleep 1
  done
  revert
}

case "${1:-}" in
  build) build ;;
  up) up ;;
  job)
    echo "== run $IMAGE"
    dc run --rm -T --no-deps app
    ;;
  start)
    echo "== start $IMAGE"
    dc up -d --wait --no-build
    ;;
  down) dc down ;;
  purge) dc down -v ;;
  *)
    echo "usage: apply {build|up|job|start|down|purge}" >&2
    exit 2
    ;;
esac
