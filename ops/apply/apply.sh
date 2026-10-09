#!/bin/sh
# One apply container. Podman names it.
# up builds, then replaces the running service. A failed start stays on this container.
# revert is a second container. It starts the previous image.
# job uses compose run. Compose names that container. The project name stays the slot name.
# Commands: up, revert, build, start, down, purge, job.
set -eu

cmd="${1:-}"
if [ -z "$cmd" ]; then
  echo "apply: missing command" >&2
  exit 2
fi

: "${DOCKER_HOST:?DOCKER_HOST is required}"
: "${COMPOSE_PROJECT_NAME:?COMPOSE_PROJECT_NAME is required}"
: "${COMPOSE_FILE:?COMPOSE_FILE is required}"

envfile="${COMPOSE_ENV_FILE:-}"
service="${SERVICE:-app}"
label_file=""
data_file=""

dc() {
  if [ -n "$data_file" ] && [ -f "$data_file" ]; then
    set -- -f "$data_file" "$@"
  fi
  if [ -n "$label_file" ] && [ -f "$label_file" ]; then
    set -- -f "$label_file" "$@"
  fi
  if [ -n "$envfile" ]; then
    set -- --env-file "$envfile" "$@"
  fi
  docker-compose -p "$COMPOSE_PROJECT_NAME" -f "$COMPOSE_FILE" "$@"
}

state_dir() {
  if [ -n "$envfile" ]; then
    dirname "$envfile"
  else
    printf '%s\n' /tmp
  fi
}

# The image exists at this time. The start time is everything after it.
note_built() {
  stamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  printf '%s\n' "$stamp" > "$(state_dir)/apply.built"
  printf 'built %s\n' "$stamp"
}

clear_built() {
  rm -f "$(state_dir)/apply.built"
}

# A service that already binds /data keeps that bind. Caddy uses /data for certificates.
service_mounts_data() {
  printf '%s\n' "$config_text" | awk -v svc="$1" '
    $0 == "  " svc ":" { on = 1; next }
    on && $0 ~ /^  [^ ]/ { on = 0 }
    on && $1 == "target:" && ($2 == "/data" || $2 == "\"/data\"" || $2 == "'\''/data'\''") { hit = 1 }
    on && $0 ~ /:\/data["'\'']?$/ { hit = 1 }
    END { exit hit ? 0 : 1 }
  '
}

# Repo compose files do not all mount the slot directory. Add the bind when it is missing.
write_data_mount() {
  data_file=""
  dir="${DATA_DIR:-}"
  [ -n "$dir" ] || return 0
  services=$(dc config --services)
  config_text=$(dc config)
  dest="$(state_dir)/data.yml"
  tmp="${dest}.tmp"
  wrote=0
  {
    printf 'services:\n'
    for name in $services; do
      [ -n "$name" ] || continue
      if service_mounts_data "$name"; then
        continue
      fi
      printf '  %s:\n' "$name"
      printf '    volumes:\n'
      printf '      - "%s:/data"\n' "$dir"
      wrote=1
    done
  } > "$tmp"
  if [ "$wrote" -eq 1 ]; then
    mv "$tmp" "$dest"
    data_file="$dest"
  else
    rm -f "$tmp" "$dest"
  fi
}

# The service containers carry the slot and the revision. Caddy has no revision.
stamp_labels() {
  [ -n "${YARD_SLOT:-}" ] || return 0
  dest="$(state_dir)/labels.yml"
  tmp="${dest}.tmp"
  services=$(dc config --services)
  {
    printf 'services:\n'
    for name in $services; do
      [ -n "$name" ] || continue
      printf '  %s:\n' "$name"
      printf '    labels:\n'
      printf '      yard.role: "app"\n'
      printf '      yard.slot: "%s"\n' "$YARD_SLOT"
      printf '      yard.service: "%s"\n' "$name"
      if [ -n "${YARD_SHA:-}" ] && [ "$name" != "caddy" ]; then
        printf '      yard.sha: "%s"\n' "$YARD_SHA"
      fi
    done
  } > "$tmp"
  mv "$tmp" "$dest"
  label_file="$dest"
}

# The default compose file already has the labels and the data mount.
prepare_project() {
  [ "${YARD_DEFAULT_COMPOSE:-}" = "1" ] && return 0
  write_data_mount
  stamp_labels
}

case "$cmd" in
  up)
    prepare_project
    clear_built
    dc build
    note_built
    dc up -d --wait
    ;;
  revert)
    # Python writes IMAGE and SHA before this command.
    prepare_project
    printf 'revert %s\n' "$IMAGE"
    dc up -d --wait
    ;;
  build)
    clear_built
    dc build
    note_built
    ;;
  start)
    prepare_project
    dc up -d --wait
    ;;
  down)
    dc down
    ;;
  purge)
    dc down -v
    ;;
  job)
    if [ "${YARD_DEFAULT_COMPOSE:-}" != "1" ]; then
      write_data_mount
    fi
    # Compose names the container. --rm stays off, so the previous run stays.
    set -- run -T --no-deps --rm=false
    if [ -n "${YARD_SLOT:-}" ]; then
      set -- "$@" --label yard.role=job --label "yard.slot=${YARD_SLOT}" --label "yard.service=${service}"
    fi
    if [ -n "${YARD_SHA:-}" ]; then
      set -- "$@" --label "yard.sha=${YARD_SHA}"
    fi
    dc "$@" "$service"
    ;;
  *)
    echo "apply: unknown command $cmd" >&2
    exit 2
    ;;
esac
