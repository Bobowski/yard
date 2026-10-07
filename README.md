# Yard

Yard runs each app in its own container. You push a repo. Yard builds the Containerfile and starts the container. Caddy sends public traffic to that container.

## Slot

A slot is one app. The container name is the slot name.

A slot points at one repo and one branch. The repo must already contain one of these files:

- `Containerfile`
- `Dockerfile`
- `ops/Containerfile`
- `ops/Dockerfile`

A push to that branch builds the image and starts the container. Yard removes the slot when that branch is merged or deleted.

`--every 5m` makes a job. Yard keeps one run at a time. The next run uses the same container name.

## Host

Yard keeps its files under `/srv/yard`. Each slot keeps its files under `/srv/yard/data/<name>`.

Caddy listens on port 8443. The host forwards port 443 to that port. Yard posts the Caddy config to a Unix socket.

## CLI

The command is `yard`. The package name is `staryard`. PyPI already has a package named `yard`.

```sh
uv tool install staryard
yard --version
```

Python 3.14 is required. `yard login` stores the host and the token on this computer.

`staryard[server]` installs the server libraries. The host still runs the container image.

## Install

Run the script on the VM as root. A second run repairs a partial install. A healthy Yard container stays in place.

```sh
./install.sh --domain yard.example.com
```

The script installs `ghcr.io/bobowski/yard:latest`. A finished run has checked the health page, the container route, and the path to Let's Encrypt. With `--domain` it also checks the certificate and `https://<domain>/health`.

The script prints an admin token. Log in from your computer:

```sh
yard login --url https://yard.example.com --token <token>
```

The package must be public. A private package needs `podman login ghcr.io` on the VM before the script.

GitHub Actions builds the image when this directory is the root of the repo. The workflow publishes `ghcr.io/bobowski/yard:latest` for `linux/amd64`.

## Update Yard

`yard update` pulls `ghcr.io/bobowski/yard:latest` and swaps the Yard container. `yard rollback` starts the previous image.

A repo on this host replaces that image:

```sh
yard settings --self-repo yard
yard update
```

Clear the repo to return to the image:

```sh
yard settings --self-image
yard update
```

## Add an app

Run this in the app directory. The directory name is the repo name and the slot name.

```sh
yard init --domain heldso.example.com
git push -u yard main
```

`yard init` makes a git repo when the directory has none. It makes the Yard repo and the slot. It adds a git remote named `yard`. It does not push.

Pass `--domain` for a public host. Pass `--every 5m` for a job. One of them is required. `--name` sets the repo name and the slot name. `--repo` and `--slot` set one each.

`yard repo new`, `yard repo link`, and `yard slot new` do those steps one at a time.

## Commands

Run `yard` to see every command. These are the common ones:

- `yard slot list` shows each slot, its data size, and how long the last build took. The last row is the host total.
- `yard slot edit <name>` changes the slot file.
- `yard slot logs <name>` reads the container log.
- `yard settings` sets the public host, the Caddy email, and the Yard image.

A terminal prints text. Any other stdout prints JSON.

## Develop

The server command is `python -m app.main`.

```sh
uv run pytest
STARIO_LOOP=uvloop uv run python -m app.main
```
