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

```sh
yard init --every 5m
```

That slot is a job. Yard keeps one run at a time.

## On the machine

Yard keeps its files in `/srv/yard`. A slot keeps its files in `/srv/yard/data/<name>`.

Caddy listens on 8443. The host forwards 443 to that port.

Install on the VM, as root:

```sh
./install.sh --domain yard.example.com
```

The script prints an admin token. Use it with `yard login`. The image is `ghcr.io/bobowski/yard:latest`.

## Day to day

```sh
yard slot list
yard slot logs <name>
yard slot edit <name>
yard update
```

`yard slot list` shows what is up, how long it has been up, and the revision it runs. A revision that is behind the branch shows both. Color marks a line that needs attention. A pipe, `--json`, `NO_COLOR`, and `--no-color` leave the color off.

`yard update` pulls the image and replaces this Yard. The current container stays until the new one answers. `yard rollback` returns to the previous image.

To build Yard from a repo on the same host:

```sh
yard settings --self-repo yard
yard update
```

`yard settings --self-image` returns to `ghcr.io/bobowski/yard:latest`.

`yard` prints the short command list. A terminal prints text. A pipe prints JSON.

## Develop

```sh
uv run pytest
STARIO_LOOP=uvloop uv run python -m app.main
```
