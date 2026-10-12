# Changelog

## 0.2.0

A rewrite of the server and the command. Same host design: bare git repos, rootless Podman, Compose, Caddy. Backwards incompatible; rerun `install.sh` on the host and `uv tool upgrade staryard` on the laptop.

- One apply container does the whole deploy: build, start, probe, revert. `yard deploys <slot> <id>` is that one log, and a Yard self-upgrade reverts on its own.
- A repo's compose file runs as it is. No generated override. The contract: `image: ${IMAGE}`, `build.context: ${CHECKOUT}`, network `yard` with alias `${SLOT_NAME}`, `env_file: ${APP_ENV}`, `${DATA_DIR}:/data`. Yard checks it before the build.
- Slot env is a file, `/srv/yard/env/<slot>.env`, mode 600. Not in SQLite.
- `TRIGGER` replaces `--every`: empty is a service; `push`, `manual`, or `every 1h` make a job. `yard run <slot>` runs any slot now.
- New: `yard status <slot>`, `yard doctor`, `yard prune`. The table says `no answer` and counts restarts. `yard` alone prints the table.
- Gone: experiments, the token tree, `yard.*` labels, `yard logs --sha`, the `revert` role, the `STARIO_*` defaults Yard injected. `token revoke` is `token rm`.
- Host: `build/` is `checkouts/`; Caddy's first config is `ops/caddy.json`; the apply container is started with `podman run`, no apply compose file.

Migration: slots with `every` become `TRIGGER=every <interval>`; slot env values are not carried over, enter them again with `yard slot edit`; revoked tokens are deleted.

## 0.1.1

- An update keeps the running container until the new image answers `/health`.
- The swap no longer passes `--group-add keep-groups` to remote Podman.

## 0.1.0

- First release of the `yard` command.
- The PyPI package name is `staryard`.
- `yard --version` prints this version.
