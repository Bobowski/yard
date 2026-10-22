# Yard

Push a repo to GitHub or Cursor Origin. Yard builds the container and serves the app over HTTPS.

Yard runs on one Ubuntu VM. It watches the forge, builds each push with Podman, and points Caddy at the new container. A `git push` is the whole deploy. There is no pipeline file and no dashboard.

**Docs: [stario.dev/yard](https://stario.dev/yard)**

## Quick start

On a fresh Ubuntu 26.04 LTS VM, as root, from a clone of this repo:

```sh
./install.sh --domain yard.example.com
```

The script prints an admin token. Then, on your laptop:

```sh
uv tool install staryard
yard login --url https://yard.example.com --token <token>
yard forge setup github          # or: yard forge setup origin

cd my-app
yard app new --public app.example.com
git push
```

The command is `yard`. The PyPI name is `staryard`, because `yard` is taken. Python 3.14 or newer is required.

## What an app needs

- A `Containerfile` or `Dockerfile` at the repo root or in `ops/`. Or a `compose.yml` with a service `app` on the network `yard`.
- A web process on port 8000.
- Settings from the environment. `yard env set KEY=VALUE` writes them.
- Persistent files in `/data`. That directory survives every deploy.

Yard is built for [Stario](https://stario.dev) apps, but it never looks inside the image. Any app in a container works.

## Docs

The docs at [stario.dev/yard](https://stario.dev/yard) read as a short book:

- [The whole idea](https://stario.dev/yard/the-idea) — Yard in one page: what each tool does and what Yard leaves out.
- [One push, start to finish](https://stario.dev/yard/one-push) — follow a single commit from your laptop to a live container.
- [Set up a host](https://stario.dev/yard/install) — a fresh VM to a working Yard in one script and a few commands.
- [Your first app](https://stario.dev/yard/first-app) — the four rules an app follows, then one push.
- [Day to day](https://stario.dev/yard/day-to-day) — logs, settings, domains, rollbacks, and cleanup.
- [Your own compose file](https://stario.dev/yard/compose) — workers, sidecars, healthchecks, and apps in any language.
- [How it works](https://stario.dev/yard/how-it-works) — the internals: files, the check loop, the deploy container, Caddy, journald.
- [CLI](https://stario.dev/yard/cli-reference), [API](https://stario.dev/yard/api-reference), [Glossary](https://stario.dev/yard/glossary) — the lookup pages.

## Develop

```sh
uv run pytest
uv run python -m app.server.main
just check                       # ruff, format, pyright, pytest
```

The code layout is in [`AGENTS.md`](AGENTS.md). Changes are in [`CHANGELOG.md`](CHANGELOG.md).
