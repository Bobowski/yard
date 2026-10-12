# Yard

Push a repo. Yard builds the Containerfile and serves the app.

```sh
uv tool install staryard
yard login --url https://yard.example.com --token <token>
yard init --domain heldso.example.com
git push -u yard main
```

The command is `yard`. The PyPI name is `staryard`, because `yard` is already taken. Python 3.14 is required.

## One app

A slot is one app. The container has the same name.

The repo needs a `Containerfile` or a `Dockerfile`, at the root or in `ops/`.

`yard init` creates the git repo, the Yard repo, and the slot. It does not push. The next push builds the image and starts the container. Yard removes the slot when that branch is merged or deleted.

The slot env is the file `/srv/yard/env/<name>.env` on the host; `yard slot edit` writes it.

To start from a GitHub address, pass it to the host. The laptop does not need a checkout.

```sh
yard init --remote https://github.com/org/heldso --domain heldso.example.com
```

Yard clones that address and follows `origin/main`. The first deploy builds that revision. `--ref` selects another branch.

```sh
yard init --trigger "every 5m"
```

`TRIGGER` decides when a slot runs. Empty is a service that stays up. `push` runs the job after each push builds it. `manual` runs only on `yard run <name>`. `every 5m` runs on that schedule. `yard run <name>` runs any slot now; a second run while one is running says so.

## Your own compose file

A repo with a `compose.yml` (or `compose.yaml`, at the root or in `ops/`) runs as it is. Yard sets the values and runs the file. The contract:

- `image: ${IMAGE}` and `build.context: ${CHECKOUT}` on the service Yard builds.
- The web service joins network `yard` with alias `${SLOT_NAME}`. Yard checks this and fails the deploy otherwise.
- `env_file: ${APP_ENV}` for the slot env.
- `${DATA_DIR}:/data` for the slot's data.

Yard also sets `SLOT_NAME SLOT_DATA_DIR SHA PORT` in the container.

## On the machine

Yard keeps its files in `/srv/yard`:

- `repos/` — the bare git repositories, one per repo.
- `checkouts/` — one extracted revision per deploy, the build context.
- `data/` — per-slot data; the container sees it at `/data`.
- `env/` — the slot env files, mode 0600.
- `run/` — the Caddy admin socket.

Caddy listens on 8443. The host forwards 443 to that port.

Install on the VM, as root:

```sh
./install.sh --domain yard.example.com
```

`install.sh` prepares the host, clones the bare repo, builds the apply image, and runs the first apply. Yard records that deploy when it boots. A second run repairs.

The script prints an admin token. Use it with `yard login`. The script clones the public repo and builds Yard from `origin/main`.

## Day to day

```sh
yard
yard logs --slot <name>
yard deploys <name>
yard deploy <name>
yard slot show <name>
yard git <name> fetch
```

`yard` shows the status table: what is up, how long it has been up, and the revision it runs. A revision that is behind the ref shows both. Color marks a line that needs attention. A failed build points at `yard deploys`. A down process points at `yard logs`. A pipe, `--json`, `NO_COLOR`, and `--no-color` leave the color off.

`yard status <name>` opens one slot: its route, revision, containers, recent deploys, and a log tail.

A running app gets a short HTTP probe against its name on the yard network. `no answer` in the state column means the container runs but nothing answered; `N restarts` counts Podman's restarts. On a laptop without the host network the probe is skipped.

`yard doctor` checks the host: podman, the yard network, the apply image, caddy and its routes, DNS and certificates for the routed hosts, disk, env files, and the hook token. It exits 1 when a check fails.

`yard prune` frees the disk the history does not need: checkouts that are not a slot's live one, crashed `*.tmp` extracts, and `localhost/` images that are neither a live image nor a revert target. The default keeps anything newer than 7 days; `--days` changes that and `--dry-run` reports without removing. Deploy rows stay — they are the history.

`yard logs --slot <name>` reads containers whose role is app. `--role` selects app, deploy, or job. `--service` narrows that set. `yard podman logs <container>` reads one container by its Podman name.

`yard deploys <name>` shows the build time, the start time, and the total. `yard deploys <name> <id>` prints the build log of that deploy. `yard deploy <name>` builds the ref the slot follows. An edit of the ref, the directory, or an env value runs that deploy.

`yard slot show <name>` prints the slot file. `yard slot edit <name>` opens it in `$EDITOR`. The file sets REF, DIR, DOMAIN, and TRIGGER. The lines under `# env` are what the container receives.

`yard token new <name>` prints a token once. `yard token rm <name>` removes it.

`yard git <name> fetch` fetches origin. A slot whose ref is `origin/main` deploys when that tip moves. Yard is that kind of slot.

`yard git <name> log -1` reads that bare repo. The command list is log, show, rev-parse, ls-tree, cat-file, for-each-ref, branch, remote, fetch, and gc.

`yard --help` prints the command list. A terminal prints text. A pipe prints JSON.

## API

Every `/api/v1/` call needs `Authorization: Bearer <token>`. Git over HTTP uses Basic auth with the token as the password. An error is one JSON document:

```json
{"error": {"code": "not_found", "message": "...", "help": "...", "example": "...", "context": {"name": "..."}}}
```

`context`, `help`, and `example` are optional. `POST /api/v1/hooks/push` answers plain text — that is what `git push` prints as `remote:` lines.

```text
GET /health                              — 200, empty
GET|POST /git/{path…}                    Basic auth, token as password — git smart HTTP
GET /api/v1/status                       ?running=1 — {host, rows}
GET /api/v1/slots                        — [slot]  (no env)
POST /api/v1/slots                       {name, repo, ref?, dir?, domain?, trigger?, env?} — {slot (with env), deploy}
GET /api/v1/slots/{name}                 — slot with env
PATCH /api/v1/slots/{name}               any of {ref, dir, domain, trigger, env} — {slot (with env), deploy|null}
DELETE /api/v1/slots/{name}              — {name}
GET /api/v1/slots/{name}/status          — {slot, env_file, rows, route, deploys, log}
POST /api/v1/slots/{name}/deploy         {sha?} — {slot (with env), deploy}
POST /api/v1/slots/{name}/run            — {output}; 409 running | no_image
GET /api/v1/slots/{name}/deploys         ?limit=N — [deploy row]
GET /api/v1/slots/{name}/deploys/{id}    — deploy row with log
GET /api/v1/logs                         ?slot&role&service&tail&cursor&follow=1 — {log, cursor, more}; follow=1 streams text
GET /api/v1/containers/{name}            — podman inspect JSON
GET /api/v1/containers/{name}/logs       ?tail&cursor&follow=1 — as /logs
GET /api/v1/repos                        — [repo]
POST /api/v1/repos                       {name, force?, origin?} — repo; 409 exists
DELETE /api/v1/repos/{name}              — {name}
POST /api/v1/repos/{name}/git            {args: [...]} — {output}
GET /api/v1/doctor                       — [{name, ok, detail}]
POST /api/v1/prune                       {days?: 7, dry?: false} — {checkouts, images, freed, days, dry}
GET /api/v1/tokens                       — [token]
POST /api/v1/tokens                      {name} — {name, token}
DELETE /api/v1/tokens/{name}             — {name}
POST /api/v1/hooks/push                  {repo, new, ref}, localhost only — text
```

A slot is `{name, repo, ref, dir, domain, trigger, image, sha, created}` plus `env` on the routes that show it.

A status row is `{slot, container, service, state, running, health, answers, restarts, exit_code, started, exited, image, sha, tip, domain, trigger, cpu, mem, data, deploy}`. `answers` is true, false, or null — the HTTP probe ran, refused, or did not apply.

A deploy row is `{id, slot, sha, image, status, started, built, ended, log}`; `status` is `running`, `ok`, `failed`, or `interrupted` (the process died mid-deploy).

A deploy outcome — returned by the create, patch, and deploy routes — has `status` one of:

- `ok` — the deploy finished; the image is live.
- `failed` — the deploy failed; the log is the apply log.
- `pending` — the detached yard self-upgrade; the next boot claims it.
- `current` — that revision is already live; no deploy ran.
- `superseded` — a newer revision landed first; its deploy builds it.
- `waiting` — the ref has no revision yet; the next push deploys.
- `skipped` — `YARD_SKIP_DEPLOY` is set; nothing ran.

## Develop

```sh
uv run pytest
STARIO_LOOP=uvloop uv run python -m app.main
```
