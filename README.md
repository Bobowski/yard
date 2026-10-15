# Yard

Push a repo. Yard builds the Containerfile and serves the app.

```sh
uv tool install staryard
yard login --url https://yard.example.com --token <token>
yard init
git push -u yard main
```

The command is `yard`. The PyPI name is `staryard`, because `yard` is already taken. Python 3.14 is required.

## One app

A project is one app. The container has the same name.

The repo needs a `Containerfile` or a `Dockerfile`, at the root or in `ops/`.

`yard init` creates the repo and the project. `--repo` and `--project` set those names. The directory name is the default for both. `--public` adds a bind for the project. The dial is the project name. Init does not push. The next push builds the image and starts the container. Yard removes the project when that branch is merged or deleted.

The project env is the file `/srv/yard/projects/<name>/env` on the host. `yard project edit` writes it.

A bind holds the host. `yard bind new --public` publishes it.

To start from a GitHub address, pass it to the host. The laptop does not need a checkout.

```sh
yard init --remote https://github.com/org/heldso --public heldso.example.com
```

Yard clones that address and follows `origin/main`. `yard up` builds that revision. `--ref` selects another branch.

```sh
yard init --trigger "every 5m"
```

`TRIGGER` decides when a project runs. Empty is a service that stays up. `push` runs the job after each push builds it. `manual` runs only on `yard up <name>`. `every 5m` runs on that schedule. `yard up <name>` builds a service and starts it. On a job it runs that job now. A second run while one is running says so.

## Your own compose file

A repo with a `compose.yml` (or `compose.yaml`, at the root or in `ops/`) runs as it is. Yard sets the values and runs the file. The contract:

- `image: ${IMAGE}` and `build.context: ${CHECKOUT}` on the service Yard builds.
- The web service joins network `yard` with alias `${PROJECT_NAME}`. Yard checks this and fails the deploy otherwise.
- `env_file: ${APP_ENV}` for the project env.
- `${DATA_DIR}:/data` for the project data.

Yard also sets `PROJECT_NAME PROJECT_DATA_DIR SHA PORT` in the container. The default compose sets `STARIO_TRACER=json`, so spans go to the container log.

## On the machine

Yard keeps its files in `/srv/yard`:

- `repos/<name>.git` — one bare git repository.
- `projects/<name>/project` — repo, ref, dir, trigger, and created.
- `projects/<name>/env` — the project env, mode 0600.
- `projects/<name>/data/` — the container sees this at `/data`.
- `projects/<name>/worktree/` — the checkout. A deploy moves that worktree to the SHA.
- `projects/yard/caddy/config/` — Caddy autosave. Yard reads it. Caddy writes it.
- `projects/yard/caddy/data/` — certificates.
- `tokens/<name>` — a token hash, mode 0600. `tokens/admin.token` is the plaintext admin token.

Caddy listens on 8443. The host forwards 443 to that port.

Install on the VM, as root:

```sh
./install.sh --domain yard.example.com
```

`install.sh` prepares the host, clones the bare repo, builds the apply image, and runs the first apply. The up container is that deploy. A second run repairs.

The script prints an admin token. Use it with `yard login`. The script clones the public repo and builds Yard from `origin/main`.

## Commands

`status`, `up`, `init`, and `clone` combine records. `init` creates a repo and a project. `--public` adds a bind. `clone` copies a repo and sets the remote. `--ref` adds a project for that branch.

```sh
yard clone heldso --ref login --public login.heldso.example.com
```

`yard clone heldso --ref login` checks out `login` and creates project `heldso-login`. `--project` sets another name. Push with `git push -u yard login`. Yard removes the project when that branch merges.

`project`, `repo`, `bind`, and `token` are the records. Each record uses `ls`, `new`, and `rm`. `project` also has `show` and `edit`. `repo` also has `link`. A record command changes that record only.

`yard project ls` prints the projects. `yard status` prints the running containers.

## Day to day

```sh
yard
yard status
yard status --full
yard up <name>
yard podman logs up-<name>
yard project show <name>
yard git <name> fetch
```

`yard` prints help. `yard status` shows what is running: how long it has been up, the revision, and the project config. A revision that is behind the ref shows both. Color marks a line that needs attention. A failed up points at `yard podman logs up-<name>`. A down process points at `yard podman logs <container>`. A pipe, `--json`, `NO_COLOR`, and `--no-color` leave the color off.

`yard status <name>` opens one project: its route, revision, containers, recent ups, and a log tail.

A running app gets a short HTTP probe against its name on the yard network. `no answer` in the state column means the container runs but nothing answered; `N restarts` counts Podman's restarts. On a laptop without the host network the probe is skipped.

`yard status --full` adds the host checks: podman, the yard network, the apply image, caddy and the binds from `yard bind ls`, DNS and certificates for those hosts, disk, env files, and the hook token. It exits 1 when a check fails.

`yard prune` removes `localhost/` images that no container uses. A project directory stays. The default keeps an image newer than 7 days. `--days` changes that. `--dry-run` reports without removing. An exited container is the history. Prune keeps an image that a container still uses.

`yard podman logs <container>` reads one container. The up container is `up-<name>`. The app container is `<name>-<service>-1`, for example `heldso-app-1`. `yard podman logs -f <container>` follows the log.

`yard up <name>` builds the ref the project follows and starts a service. On a job it runs the job. An optional SHA selects that revision. `yard project edit` saves the file. `yard up` runs the build.

`yard project show <name>` prints the project file. `yard project edit <name>` opens it in `$EDITOR`. The file sets REF, DIR, and TRIGGER. The lines under `# env` are what the container receives.

`yard token new <name>` prints a token once. `yard token rm <name>` removes it.

`yard git <name> fetch` fetches origin. A project whose ref is `origin/main` deploys when that tip moves. The yard project follows `main`. Fetch fast-forwards `main` from `origin/main`. A push to the host's `main` deploys it too.

`yard git <name>` runs git in that bare repo. The words after the name are the git command.

`yard --help` prints the command list. A terminal prints text. A pipe prints JSON.

## Binds

`yard bind new --public` publishes one host for one container. A second bind for that container replaces the record. The bind stays when the container restarts. `yard bind rm` removes it. `yard bind ls` prints `up`, `down`, or `absent` from `podman ps`.

```sh
yard bind new --public heldso.example.com heldso-app
yard bind new --public heldso.example.com heldso-app:9000 --try 30s
yard bind ls
yard bind rm heldso-app
```

`yard bind new --internal` is the private side. That side is not available yet.

The last word is a container name on the yard network. A bare name uses port 8000. Caddy waits 10 seconds for the upstream, then answers 502. `--try` changes that wait.

`yard podman`, `yard git`, and `yard caddy` pass their words to that program. Podman runs on the host. Git runs in the bare repo. Caddy runs in the caddy container.

```sh
yard podman ps
yard podman logs heldso-app-1
yard git heldso log -1
yard caddy version
```

Put a yard flag before those words. `yard podman logs -f <container>` streams the log.

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
GET /api/v1/projects                        — [project]  (no env)
POST /api/v1/projects                       {name, repo, ref?, dir?, trigger?, env?} — {project (with env)}
GET /api/v1/projects/{name}                 — project with env
PATCH /api/v1/projects/{name}               any of {ref, dir, trigger, env} — {project (with env)}
DELETE /api/v1/projects/{name}              — {name}
GET /api/v1/projects/{name}/status          — {project, env_file, rows, route, deploys, log}
POST /api/v1/projects/{name}/deploy         {sha?} — {project (with env), deploy}
POST /api/v1/projects/{name}/run            — {output}; 409 running | no_image
GET /api/v1/projects/{name}/deploys         ?limit=N — [deploy row]
GET /api/v1/projects/{name}/deploys/{id}    — deploy row with log
GET /api/v1/logs                            ?project&role&service&tail&cursor&follow=1 — {log, cursor, more}; follow=1 streams text
GET /api/v1/containers/{name}            — podman inspect JSON
GET /api/v1/containers/{name}/logs       ?tail&cursor&follow=1 — as /logs
GET /api/v1/binds                        — [{host, dial, id, state}]
PUT /api/v1/binds                        {host, dial, try?} — {host, dial, id, state}
DELETE /api/v1/binds/{name}              — {host, dial}
POST /api/v1/caddy                       {args: [...]} — {output}
POST /api/v1/podman                      {args: [...]} — {output}
GET /api/v1/repos                        — [repo]
POST /api/v1/repos                       {name, force?, origin?} — repo; 409 exists
DELETE /api/v1/repos/{name}              — {name}
POST /api/v1/repos/{name}/git            {args: [...]} — {output}
GET /api/v1/doctor                       — [{name, ok, detail}]
POST /api/v1/prune                       {days?: 7, dry?: false} — {images, freed, days, dry}
GET /api/v1/tokens                       — [token]
POST /api/v1/tokens                      {name} — {name, token}
DELETE /api/v1/tokens/{name}             — {name}
POST /api/v1/hooks/push                  {repo, new, ref}, localhost only — text
```

A project is `{name, repo, ref, dir, trigger, created}` plus `env` on the routes that show it. The project page copies `image` and `sha` from the running container.

A status row is `{project, container, service, state, running, health, answers, restarts, exit_code, started, exited, image, sha, tip, domain, trigger, cpu, mem, data, deploy}`. `answers` is true, false, or null — the HTTP probe ran, refused, or did not apply.

A deploy row is the up container: `{id, project, sha, image, status, started, built, ended, log}`. `id` is 1. `status` is `running`, `ok`, or `failed`. `GET /deploys` returns that one row, or `[]`.

A deploy outcome — returned by `POST /api/v1/projects/{name}/deploy` — has `status` one of:

- `ok` — the deploy finished; the image is live.
- `failed` — the deploy failed; the log is the apply log.
- `pending` — the yard upgrade runs detached. This process does not wait for it.
- `current` — a container for the project already has that image. No deploy ran.
- `superseded` — a newer revision landed first; its deploy builds it.
- `waiting` — the ref has no revision yet; the next push deploys.
- `skipped` — `YARD_SKIP_DEPLOY` is set; nothing ran.

## Develop

```sh
uv run pytest
uv run python -m app.main
```
