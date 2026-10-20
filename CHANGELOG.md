# Changelog

## 0.5.0

Yard no longer hosts git. Cursor Origin is the git host. There is no migration path; rebuild the host and reconnect it by hand.

- `repos/` and `worktree/` are gone. `apps/<name>/repo` is a plain clone that a deploy resets to the remote tip of the ref.
- The app record is exactly `repo`, `ref`, `trigger`, and `created`. `dir` is gone. A trigger is `push` or `manual`; `every` is gone.
- Two forges: Cursor Origin and GitHub, both as Apps. An app's `repo` is the full `https://<host>/<owner>/<name>` URL; the host picks the forge.
- Everything observable goes through journald: container logs, Podman container events, and Yard's own events (`hook.received`, `check.moved`, `deploy.*`, `app.*`, `env.set`, `bind.*`, `token.*`, `forge.*`, `prune`). Yard writes its events to the native journald socket, `SYSLOG_IDENTIFIER=yard` with `YARD_*` fields; a missing socket falls back to one JSON line on stdout. No secret ever becomes a field.
- `GET /api/v1/logs` reads any container's journal log by name or id; `GET /api/v1/events` is the merged Podman+Yard timeline (JSON, NDJSON on `follow=1`). `GET /api/v1/apps/{name}/logs` is gone — `yard app logs` and `yard logs` use the new routes, and `yard events` shows the timeline.
- A deploy ends with a settle gate: the app container must stay up and never restart for `YARD_SETTLE_SECONDS`. A container that fails reverts to the previous image and the outcome is `reverted`.
- One deploy path: `POST /api/v1/apps/{name}/deploy` takes an optional `sha` (`yard app deploy --sha SHA`). A prefix resolves against the deploy journal; an image the host still holds skips the build. Rollback is gone — an older revision is a deploy of that sha.
- The apply container is `deploy-<name>` (was `up-<name>`); a leftover `up-` container still reads as the deploy record.
- The API contracts are trimmed: `GET /api/v1/apps/{name}` is the full app page (the `/status` route is gone), binds are `PUT|DELETE /api/v1/binds/{host}`, and the forge key/installation routes are gone — `GET /api/v1/forges` reports the Origin public key, discovers the installation, and carries `selection` and `error`.
- The CLI loses `app show`, `app history`, `app rollback`, and `bind new`. `app status` reads the app page, `bind set` takes the host as an argument, and `forge setup` polls `GET /api/v1/forges` for the installation.
- Status rows carry the container `id` plus `oom` and `error` for a stopped container.
- `yard forge setup origin` and `yard forge setup github` connect the host from the laptop. Each prints the app values, opens the create and install links, and waits for the install. `install.sh` no longer asks for a repo or runs setup; it prints the next steps.
- `/hooks/origin` takes Origin events; `/hooks/github` takes GitHub events and verifies `X-Hub-Signature-256` before anything else. A push schedules a repo check; a burst folds into one check now and one at the end of the window. A full check runs every `YARD_CHECK_SECONDS` (five minutes).
- A deploy syncs the clone through the repo's forge — the ref tip, or one commit with `--sha` — builds `localhost/<name>:<sha12>`, and runs compose up. The token never lands in `.git/config`.
- Containers log to journald. `GET /api/v1/logs`, `yard logs`, and `yard app logs` read them back — the log survives the container. `install.sh` writes the journald and Podman config.
- The yard app still deploys itself: push to its Origin repo, detached apply, `pending` outcome.
- Removed: smart-HTTP git hosting, `/api/v1/repos*`, `repo`/`git`/`init`/`clone`/`up`/`caddy` CLI commands, `app edit` and `app run`, deploy outcomes `superseded`, `waiting`, `current`, the merge/delete app cleanup, and the projects→apps layout migration.
- New env API: `GET|PATCH|PUT /api/v1/apps/{name}/env`. App responses never carry env.
- Doctor gains five rows per connected forge: app, installation, token, webhook, check.
- The code is three layers in about 20 modules: `app.shared`, `app.cli`, and `app.server` (api, services, adapters). The server is `python -m app.server.main`; the CLI script is unchanged.
- Push and installation events debounce with a leading plus trailing check, so a burst never waits five minutes.
- Every disk write is atomic. The git adapter raises `YardError` like the others. The CLI times calls out at 30 seconds (deploy at 30 minutes; streams never).
- Caddy's admin API is a unix socket at `apps/yard/caddy/run/admin.sock`. The curl-to-admin path and the autosave reads are gone.
- HTTP/3 is on. Caddy listens on 443 inside its container, the host publishes 8443 for TCP and UDP, and nft redirects public 443 for both protocols, so Alt-Svc's `h3=":443"` is a port clients can reach. An existing host must rerun `install.sh` — or add `udp dport 443 redirect to :8443` to the yard nft table and allow `8443/udp` in ufw — for UDP to reach Caddy.
- `yard forge setup github` recovers: a bad App ID or an unreadable key path re-prompts instead of exiting (quotes and `\ ` escapes on a pasted path come off), a 4xx on the forge PUT asks again, and a stopped run continues on the next `yard forge setup github` with the same webhook secret — `--new` starts over. A run against a host that already holds the app id skips to the install step; a connected forge exits early.

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
