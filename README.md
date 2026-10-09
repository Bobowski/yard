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

To start from a GitHub address, pass it to the host. The laptop does not need a checkout.

```sh
yard init --remote https://github.com/org/heldso --domain heldso.example.com
```

Yard clones that address and follows `origin/main`. The first deploy builds that revision. `--ref` selects another branch.

```sh
yard init --every 5m
```

That slot is a job. Yard keeps one run at a time.

## On the machine

Yard keeps its files in `/srv/yard`. A slot keeps its files in `/srv/yard/data/<name>`. The container sees that directory at `/data`.

Caddy listens on 8443. The host forwards 443 to that port.

Install on the VM, as root:

```sh
./install.sh --domain yard.example.com
```

The script prints an admin token. Use it with `yard login`. The script clones the public repo and builds Yard from `origin/main`.

## Day to day

```sh
yard slot list
yard logs --slot <name>
yard deploys <name>
yard deploy <name>
yard slot edit <name>
yard git <name> fetch
```

`yard slot list` shows what is up, how long it has been up, and the revision it runs. A revision that is behind the ref shows both. Color marks a line that needs attention. A failed build points at `yard deploys`. A down process points at `yard logs`. A pipe, `--json`, `NO_COLOR`, and `--no-color` leave the color off.

`yard logs --slot <name>` reads containers whose role is app. `--role` selects app, deploy, revert, or job. `--service` and `--sha` narrow that set. `yard podman logs <container>` reads one container by its Podman name.

`yard deploys <name>` shows the build time, the start time, and the total. `yard deploy <name>` builds the ref the slot follows. An edit of the ref, the directory, or an env value runs that deploy.

`yard git <name> fetch` fetches origin. A slot whose ref is `origin/main` deploys when that tip moves. Yard is that kind of slot.

`yard git <name> log -1` reads that bare repo. The command list is log, show, rev-parse, ls-tree, cat-file, for-each-ref, branch, remote, fetch, and gc.

`yard` prints the short command list. A terminal prints text. A pipe prints JSON.

## Develop

```sh
uv run pytest
STARIO_LOOP=uvloop uv run python -m app.main
```
