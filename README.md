# Yard

Push a repo to Cursor Origin or GitHub. Yard builds the Containerfile and serves the app.

A forge — Origin or GitHub — is the git host. Yard watches it and deploys. It does not host git itself.

```sh
uv tool install staryard
yard login --url https://yard.example.com --token <token>
yard app deploy
```

The command is `yard`. The PyPI name is `staryard`, because `yard` is already taken. Python 3.14 is required.

## One app

An app is one name. The container uses that name.

The repo needs a `Containerfile` or a `Dockerfile`, at the root or in `ops/`.

The app record is four fields: `repo` (a full `https://<host>/<owner>/<name>` URL — the host is `origin.cursor.com` or `github.com`), `ref` (a branch, `main` by default), `trigger` (`push` by default, or `manual`), and `created`. A checkout remote in any spelling — `https`, `ssh://`, `git@host:` — becomes that URL.

`yard app new` creates the app. Run it inside a checkout and the repo, branch, and name come from the checkout. `--repo`, `--ref`, and `--app` override them. `--public` adds a bind for the app. The dial is the app container, `<name>-app-1`.

A push to the repo deploys the app. `yard app deploy` deploys now. A `manual` app deploys only on `yard app deploy`.

The app env is the file `/srv/yard/apps/<name>/env` on the host. `yard env set`, `yard env unset`, and `yard env edit` write it. A deploy applies the file.

A bind holds the host. `yard bind set` publishes it.

## Your own compose file

A repo with a `compose.yml` (or `compose.yaml`, at the root or in `ops/`) runs as it is. Yard sets the values and runs the file. The contract:

- `image: ${IMAGE}` and `build.context: ${CHECKOUT}` on the service Yard builds.
- The web service is named `app` and joins network `yard`. Yard checks this and fails the deploy otherwise. Compose names that container `<name>-app-1`. That name is the dial.
- `env_file: ${APP_ENV}` for the app env.
- `${DATA_DIR}:/data` for the app data.

Yard also sets `PROJECT_NAME PROJECT_DATA_DIR SHA PORT` in the container. The default compose sets `STARIO_TRACER=json`, so spans go to the container log.

## Forges

Yard connects to each forge as an app — an Origin app on Cursor, a GitHub App on GitHub. Setup runs on the laptop, through the CLI:

```sh
yard forge setup origin
yard forge setup github [--key PATH] [--org ORG] [--new]
```

`yard forge setup origin` prints a public key, the webhook URL `https://<domain>/hooks/origin`, the event `repository.pushed`, and the scope `repository:contents:read`. Create the app at `https://cursor.com/codebase/settings/apps` with those values. Then install the app on the owner and select all repositories.

`yard forge setup github` mints a webhook secret, opens a prefilled GitHub App form, and asks for the app ID and a private key file. Then it opens the install page and waits for the install. A stopped setup continues on the next run with the same webhook secret, so no second GitHub App is needed; `--new` starts over.

Each forge stores its app ID, installation ID, and secrets in `apps/yard/data/<forge>/state.json`, mode 0600. The private key is `key.pem` in that directory. The files are the truth; the server re-reads them on each call, so no restart is needed. Keys and secrets never appear in an API response.

`yard forge ls` shows which forges are connected.

## Checks and deploys

A push event on `/hooks/origin` or `/hooks/github` schedules a check for that repo. The first push runs a check now. More pushes inside ten seconds run one more check at the end of the window. Every five minutes (`YARD_CHECK_SECONDS`) a full check runs. A check fetches the remote tip of each push app's ref. A tip that differs from the clone's HEAD deploys the app.

A deploy syncs the clone at `apps/<name>/repo`, builds `localhost/<name>:<sha12>`, and runs compose up. The fetch token comes from the repo's forge and never lands in `.git/config`. A manual deploy always applies the env file.

After the build, Yard runs `compose up --wait`. When the app's compose file defines a healthcheck, the deploy waits until it reports healthy, at most 120 seconds. An unhealthy or exited container reverts to the previous image and the deploy ends `reverted`; with no previous image the deploy ends `failed`. Without a healthcheck the deploy ends once the container runs.

`podman build` drops a `HEALTHCHECK` from a Containerfile when it builds the OCI format, so declare the healthcheck in the app's own `compose.yml`: service `app`, `healthcheck:` with `start_interval` for a fast start and a long `interval`.

## Deploy an older revision

`yard app deploy --sha SHA` deploys one commit instead of the ref tip. A full 40-character SHA deploys as is; a prefix resolves against the deploy journal — one match deploys, none or several is an error. The clone fetches that commit, and when the host still holds `localhost/<name>:<sha12>` the build is skipped.

The trigger rule: an app with trigger `push` redeploys the ref tip on the next check, so an older revision holds only until the next push or five-minute check. Set `yard app set --trigger manual` to keep it.

## What happened

Everything observable lands in journald on the host: container logs, Podman's container events, and Yard's own events — hooks, checks, deploys, app and env changes, tokens, binds, forges, prunes. `install.sh` turns that on: persistent storage in `/etc/systemd/journald.conf.d/yard.conf` (2 GB cap), and `log_driver` plus `events_logger` set to `journald` in yard's `containers.conf`.

- `yard logs CONTAINER` reads the journal by container name or id — a 12-char or full id works, even for a container that is gone. `--tail N`, `--since -15m` (or `1h`, `2d`, `2025-01-31 14:00`), `--until`, `-f` to stream.
- `yard app logs` is the same read for `<name>-app-1`; `--deploy` picks `deploy-<name>`, `--container` another of the app's containers.
- `yard events` is the merged timeline of Podman and Yard events: `--app`, `--container`, `--source podman|yard`, `--event PREFIX`, `--tail N`, `-f` for NDJSON. `yard events --app <name> --event deploy.` shows an app's deploy history.

A container that predates journald starts logging there on its next deploy. `yard podman logs` still works for the containers that keep file logs.

A Yard event's fields never carry secrets: an env change logs key names only, and tokens, keys, and webhook secrets never appear.

## On the machine

Yard keeps its files in `/srv/yard`:

- `apps/<name>/app` — repo, ref, trigger, and created.
- `apps/<name>/env` — the app env, mode 0600.
- `apps/<name>/data/` — the container sees this at `/data`.
- `apps/<name>/repo/` — the clone. A deploy resets it to the remote tip.
- `apps/yard/data/origin/` and `apps/yard/data/github/` — each forge's key and state.
- `apps/yard/caddy/config/` — Caddy autosave. Caddy writes it for `--resume`.
- `apps/yard/caddy/data/` — certificates.
- `apps/yard/caddy/run/` — the admin socket. Yard talks to Caddy there.
- `tokens/<name>` — a token hash, mode 0600. `tokens/admin.token` is the plaintext admin token.

Caddy listens on 443 inside its container. The host publishes 8443 for TCP and UDP, and nft redirects public 443 — TCP and UDP — to 8443, so HTTP/3 reaches Caddy. The admin API is a unix socket at `apps/yard/caddy/run/admin.sock`. Binds and doctor use it; there is no admin port. A firewall must allow `8443/tcp` and `8443/udp`; install.sh adds both rules to ufw.

Install on the VM, as root:

```sh
./install.sh --domain yard.example.com
```

`install.sh` prepares the host, configures journald and Podman's journald settings, clones the Yard repo, builds the apply image, and runs the first apply. The `deploy-yard` container is that deploy. A second run repairs. It never asks questions; at the end it prints the next steps.

The script prints an admin token. Use it with `yard login`, then run `yard forge setup origin` or `yard forge setup github`, and point the yard app at its repo with `yard app set` so pushes to Yard deploy Yard itself.

## Commands

The command finds the app from the checkout. The remote must be an Origin or GitHub URL and the branch must match an app's ref. `--app` overrides that lookup everywhere.

```sh
yard status [--running] [--full]
yard prune [--days N] [--dry-run]
yard logs CONTAINER [-f] [--tail N] [--since S] [--until S]
yard events [--app NAME] [--container X] [--source podman|yard] [--event PREFIX] [--tail N] [-f]
yard app ls
yard app new [--repo URL] [--ref BRANCH] [--trigger push|manual] [--public HOST] [--try DURATION] [--app NAME]
yard app set [--repo URL] [--ref BRANCH] [--trigger push|manual] [--app NAME]
yard app status [--app NAME]
yard app logs [-f] [--tail N] [--since S] [--until S] [--deploy] [--container NAME] [--app NAME]
yard app deploy [--sha SHA] [--app NAME]
yard app rm [--app NAME]
yard env show [--app NAME]
yard env set KEY=VALUE ... [--app NAME]
yard env unset KEY ... [--app NAME]
yard env edit [--app NAME]
yard forge ls
yard forge setup origin [--no-browser]
yard forge setup github [--key PATH] [--org ORG] [--no-browser]
yard bind ls
yard bind set HOST [CONTAINER[:PORT]] [--try DURATION] [--app NAME]
yard bind rm HOST
yard token ls | new NAME | rm NAME
yard podman ARGS...
```

`yard` prints help. `yard status` shows what is running: how long it has been up, the revision, and the app config. A revision behind the remote tip shows both. A failed deploy points at `yard app logs --deploy --app <name>`; a down container at `yard logs <container>`. When no forge is connected, `status` and `login` print a hint on stderr.

`yard status --full` adds the host checks: podman, the yard network, the apply image, caddy, the admin socket and the binds, DNS and certificates, disk, Podman's journald settings, the journal, the container log drivers, the events socket, and each forge in use. It exits 1 when a check fails.

`yard prune` removes `localhost/` images that no container uses. An app directory stays. The default keeps an image newer than 7 days. `--dry-run` reports without removing.

`yard podman` passes read-only commands to Podman on the host: `ps`, `logs`, `inspect`, `stats`, `top`, `images`, `port`. `yard podman logs -f <container>` streams the log.

A terminal prints text. A pipe, `--json`, `NO_COLOR`, and `--no-color` leave the color off.

## API

Every `/api/v1/` call needs `Authorization: Bearer <token>`. `/hooks/origin` and `/hooks/github` have no auth; the GitHub hook verifies `X-Hub-Signature-256` and a bad signature is a 401. A delivery only schedules a check; Yard reads the tip from the forge and never trusts the body. An error is one JSON document:

```json
{"error": {"code": "not_found", "message": "...", "help": "...", "example": "...", "context": {"name": "..."}}}
```

`context`, `help`, and `example` are optional.

```text
GET /health                              — 200, empty
POST /hooks/origin                       — Origin events; 204 always
POST /hooks/github                       — GitHub events; 401 on a bad signature, else 204
GET /api/v1/status                       ?running=1 — {host, rows}
GET /api/v1/logs                         ?container&lines&since&until&follow — text/plain
GET /api/v1/events                       ?app&container&source&event&lines&since&until&follow — JSON or NDJSON
GET /api/v1/doctor                       — [{name, ok, detail}]
POST /api/v1/prune                       {days?: 7, dry?: false} — {images, freed, days, dry}
GET /api/v1/apps                         — [app]
POST /api/v1/apps                        {name, repo, ref?, trigger?} — {app}
GET /api/v1/apps/{name}                  — {app, env_file, rows, binds, deploys, log}
PATCH /api/v1/apps/{name}                any of {repo, ref, trigger} — {app}
DELETE /api/v1/apps/{name}               — {name}
POST /api/v1/apps/{name}/deploy          {sha?: sha} — {app, deploy}
GET /api/v1/apps/{name}/env              — {KEY: VALUE}
PATCH /api/v1/apps/{name}/env            {KEY: VALUE|null} — {keys}
PUT /api/v1/apps/{name}/env              {KEY: VALUE} — {keys}
GET /api/v1/forges                       — [{name, host, connected, app_id, installation_id, selection, last_delivery, last_check, public_key?, slug?, error?}]
PUT /api/v1/forges/origin                {app_id} — {forge, app}
PUT /api/v1/forges/github                {app_id, private_key, webhook_secret} — {forge, app}
GET /api/v1/binds                        — [{host, dial, id, state, try}]
PUT /api/v1/binds/{host}                 {dial, try?} — {host, dial, id, state, try}
DELETE /api/v1/binds/{host}              — {host}
POST /api/v1/podman                      {args: [...]} — text; -f or --follow streams
GET /api/v1/tokens                       — [token]
POST /api/v1/tokens                      {name} — {name, token}
DELETE /api/v1/tokens/{name}             — {name}
```

An app is `{name, repo, ref, trigger, created}`. `GET /api/v1/apps/{name}` returns the full app page: `app` with `image` and `sha` from the running container, `env_file`, `rows`, `binds`, `deploys`, and `log`. The env never appears in an app response; it has its own routes.

`GET /api/v1/logs` takes a container name, a 12-char id, or a full id as `container` — anything else is a 400. `follow=1` streams. `GET /api/v1/events` merges Podman and Yard events into one list; `follow=1` streams NDJSON. An event row is `{time, source, event, app, container, container_id, image, exit_code, oom, actor, sha, status, message, fields}` with empty keys omitted; `fields` holds the remaining Yard event fields.

A status row is `{app, container, service, state, running, health, answers, restarts, exit_code, started, exited, id, oom, error, image, sha, tip, binds, trigger, cpu, mem, data, deploy}`. `binds` is a list of `{host, dial}`. `tip` is the last remote tip a check saw. `answers` is true, false, or null — the HTTP probe ran, refused, or did not apply. `id` is the 12-char container id; `oom` and `error` come from the inspect of a stopped container.

A deploy row is the `deploy-<name>` container: `{app, sha, image, status, started, ended}`. `status` is `running`, `ok`, or `failed`. A leftover `up-<name>` container from an older host reads the same way.

A deploy outcome — returned by `POST /api/v1/apps/{name}/deploy` — has `status` one of:

- `ok` — the deploy finished; the image is live.
- `reverted` — the new container never got healthy; the previous image runs.
- `failed` — the deploy failed; the log is the apply log.
- `pending` — the yard upgrade runs detached. This process does not wait for it.
- `skipped` — `YARD_SKIP_DEPLOY` is set; nothing ran.

## Develop

```sh
uv run pytest
uv run python -m app.server.main
```
