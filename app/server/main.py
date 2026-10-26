"""Yard. `python -m app.server.main` serves; `yard login --token <token>` talks to it."""

import asyncio
import logging

import orjson
import uvloop
from stario import App, Span, json, serve

from app.server import api, apps, caddy, compose
from app.server.store import Config, Store, ensure_admin
from app.server.yard import Yard
from app.shared import YardError

log = logging.getLogger("yard")


async def bootstrap(app: App, span: Span):
    json.set_codec(orjson.dumps, orjson.loads)
    config = Config.from_env()
    span.attrs({"yard.root": str(config.root), "yard.boot": config.boot})
    store = Store.open(config.root)
    token = ensure_admin(store)
    if token:
        span.event("yard.admin_token", {"path": str(config.token_file)})
    yard = Yard(config, store)
    yard.attach(app)
    if config.boot:
        await boot(yard, app)
    api.register(app, yard)

    try:
        yield
    finally:
        await close(yard)


async def boot(yard: Yard, app: App) -> None:
    """Directories, the yard record, the network, the two background tasks."""
    config = yard.config
    (config.root / "apps").mkdir(parents=True, exist_ok=True)
    (config.root / "tokens").mkdir(mode=0o700, parents=True, exist_ok=True)
    caddy_root = config.app_dir("yard") / "caddy"
    for part in ("config", "data", "run"):
        (caddy_root / part).mkdir(parents=True, exist_ok=True)
    for forge in yard.forges.values():
        forge.dir.mkdir(parents=True, exist_ok=True)
    try:
        apps.migrate(yard)
        apps.ensure_host(yard)
    except YardError as exc:
        log.warning("host app: %s", exc)

    await _network(yard)
    # The apply image build pulls a base image. The server answers while that build runs.
    yard.tasks.append(app.create_task(_apply_image(yard), name="apply-image"))
    yard.tasks.append(app.create_task(apps.loop(yard), name="checks"))
    yard.tasks.append(app.create_task(_caddy_server(yard), name="caddy-server"))
    yard.tasks.append(app.create_task(_boot_report(yard), name="boot-report"))
    if config.prune_days > 0:
        yard.tasks.append(app.create_task(apps.prune_loop(yard), name="prune"))


async def _boot_report(yard: Yard) -> None:
    """This process is the last yard deploy's commit; mark it live on the forge."""
    for forge in yard.forges.values():
        # Older hosts never saved the installation account; learn it once so
        # forge.owns() can keep statuses off repos the installation does not own.
        if forge.reports and forge.connected() and not forge.account:
            try:
                await forge.find_installation()
            except YardError as exc:
                log.warning("%s installation: %s", forge.name, exc)
    sha = yard.config.sha
    if not sha:
        return
    try:
        repo = yard.store.app("yard").repo
        forge = yard.forge(repo)
    except YardError:
        return
    if not repo or not forge.connected():
        return
    await apps.report(forge, repo, sha, "success", "live")


async def _caddy_server(yard: Yard) -> None:
    """The server block is ours too: the seed declares it, the autosave must match.

    An existing autosave still listens on :8443 with h1/h2. At boot the whole
    http app is read, converged to listen :443 with h1/h2/h3, and PATCHed back
    in one call when it differs.
    """
    socket = yard.config.caddy_socket
    for _ in range(40):
        if socket.exists():
            break
        await asyncio.sleep(0.5)
    else:
        log.warning("caddy server: admin socket never appeared")
        return
    try:
        _, raw = await yard.caddy.request("GET", caddy.HTTP_PATH)
        current = json.loads(raw)
        if not isinstance(current, dict):
            raise YardError(502, "caddy", f"caddy GET {caddy.HTTP_PATH}: not an object")
        converged = caddy.converge_http(current)
        if converged != current:
            await yard.caddy.request("PATCH", caddy.HTTP_PATH, json.dumps_bytes(converged))
    except (YardError, ValueError) as exc:
        log.warning("caddy server: %s", exc)


async def _network(yard: Yard) -> None:
    """The `yard` bridge: apply containers and apps join it, Caddy resolves them on it."""
    try:
        await yard.podman.run("network", "exists", "yard")
    except YardError:
        try:
            await yard.podman.run("network", "create", "yard")
        except YardError as exc:
            log.warning("network: %s", exc)


async def _apply_image(yard: Yard) -> None:
    try:
        code, text = await compose.build_apply(yard.config)
        if code:
            log.warning("apply image: %s", text.strip().splitlines()[-1] if text.strip() else code)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("apply image: %s", exc)


async def close(yard: Yard) -> None:
    for task in yard.tasks:
        task.cancel()
    if yard.tasks:
        await asyncio.gather(*yard.tasks, return_exceptions=True)


def main() -> None:
    uvloop.run(serve(bootstrap))


if __name__ == "__main__":
    main()
