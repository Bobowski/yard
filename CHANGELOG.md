# Changelog

## 0.4.0

The store is files. One project is one directory. Move the host files once, then rerun `install.sh`.

- `projects/<name>/project` holds repo, ref, dir, trigger, and created. Env is `projects/<name>/env`. Data is `projects/<name>/data`. The checkout is `projects/<name>/worktree`.
- The command and the API say project. A container gets `PROJECT_NAME` and `PROJECT_DATA_DIR`.
- A project write saves the file. `yard up` builds it. `yard clone --ref` adds a feature project, and a merge removes it.
- Binds are `/api/v1/binds`. Caddy keeps its autosave under the yard project.
- `yard prune` removes unused localhost images.

## 0.3.1

Formatting only. `app/api.py` and `app/git.py` are reformatted under the locked ruff 0.17 so CI's `ruff format --check` agrees with the workspace's local run. No behavior change.

## 0.3.0

Stario 5.0 and a round of hardening after the 0.2 cutover. Same install.sh path.

- The table names the container (`engratia-app-1`); an idle row keeps the slot name. CPU and memory read the keys Podman actually prints.
- Image tags use twelve chars of sha. Yard's own containers follow the compose convention: `yard-app-1`, `yard-caddy-1`.
- Caddy no longer recreates on a Yard self-deploy. Its seed file mounts from a stable path under `run/` and the admin socket is fixed.
- The recurring container healthcheck is gone. The apply probe is the only readiness gate; the healthcheck alone was about half of Yard's idle CPU.
- The apply image builds with podman-remote, not the Compose API path.
- `YardError` renders on every route through `catch_errors`; anything else gets the framework's 500. A log follow drains its writer and stops on disconnect.
- install.sh fails early without root, installs `sudo`, and creates `checkouts/` itself. `saved_project` ensures the slot env file.
- `staryard` is a virtual package (`package = false`). The image ships `app/` as source; `uv build` still publishes the PyPI wheel.

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
