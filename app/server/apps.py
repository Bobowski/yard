"""The app records, the deploy, the checks, and prune. The yard record is ensured at boot."""

import asyncio
import logging
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.server import binds, compose, git, journal, podman
from app.server.forges import Forge
from app.server.store import App
from app.server.yard import Yard
from app.shared import YardError, clone_url, ensure_file, now

log = logging.getLogger("yard")


def ensure_host(yard: Yard) -> None:
    """The yard app record. Its compose runs the app and Caddy."""
    ensure_file(yard.config.env("yard"))
    try:
        yard.store.app("yard")
    except YardError as exc:
        if exc.code != "not_found":
            raise
        yard.store.create_app("yard", "", "main", "manual")


def create(yard: Yard, name: str, repo: str, ref: str = "", trigger: str = "", *, actor: str = "") -> App:
    app = yard.store.create_app(name, repo, ref, trigger)
    ensure_file(yard.config.env(app.name))
    journal.send(
        yard.config,
        "app.create",
        f"app {app.name} created",
        app=app.name,
        repo=app.repo,
        ref=app.ref,
        trigger=app.trigger,
        actor=actor,
    )
    return app


def update(
    yard: Yard,
    name: str,
    *,
    repo: str | None = None,
    ref: str | None = None,
    trigger: str | None = None,
    actor: str = "",
) -> App:
    app = yard.store.update_app(name, repo=repo, ref=ref, trigger=trigger)
    journal.send(
        yard.config,
        "app.update",
        f"app {app.name} updated",
        app=app.name,
        repo=app.repo,
        ref=app.ref,
        trigger=app.trigger,
        actor=actor,
    )
    return app


@dataclass(slots=True)
class Removed:
    """What a delete did to the binds that dialed the app's containers."""

    name: str
    binds_removed: list[tuple[str, str]] = field(default_factory=list)
    binds_left: list[tuple[str, str]] = field(default_factory=list)
    binds_error: str = ""


async def delete(yard: Yard, name: str, *, with_binds: bool = False, actor: str = "") -> Removed:
    if name == "yard":
        raise YardError(
            400,
            "forbidden",
            "the yard app runs Yard itself",
            help_text="To stop Yard, run: sudo -u yard podman stop yard-app-1 on the host.",
        )
    app = yard.store.app(name)

    # A bind that dials one of the app's containers points at nothing after
    # this. Collect the names before the purge removes the containers.
    names = {compose.app_container(name)}
    try:
        listed = podman.parse_ps(await yard.podman.run("ps", "-a", "--format", "json"))
        names |= {item.name for item in listed.values() if item.app == name}
    except YardError:
        pass
    app_binds: list[binds.Bind] = []
    binds_error = ""
    try:
        app_binds = [bind for bind in await binds.read(yard) if bind.dial.split(":", 1)[0] in names]
    except YardError as exc:
        binds_error = exc.message

    project = compose.saved_project(yard.config, app)
    if project is not None:
        try:
            await compose.apply(yard.config, name, "purge", project)
        except Exception as exc:
            log.warning("purge %s: %s", name, exc)

    yard.store.delete_app(name)
    directory = yard.config.app_dir(name)
    if directory.exists():
        shutil.rmtree(directory)
    journal.send(yard.config, "app.delete", f"app {name} deleted", app=name, actor=actor)

    removed = Removed(name, binds_error=binds_error)
    if binds_error:
        return removed
    if not with_binds:
        removed.binds_left = [(bind.host, bind.dial) for bind in app_binds]
        return removed
    for bind in app_binds:
        await binds.delete_bind(yard, bind.host, actor=actor)
        removed.binds_removed.append((bind.host, bind.dial))
    return removed


@dataclass(slots=True)
class Outcome:
    status: str
    image: str = ""
    log: str = ""
    error: str = ""
    previous: str = ""


async def deploy(
    yard: Yard,
    name: str,
    *,
    sha: str = "",
    requested: str = "",
    cause: str = "manual",
    actor: str = "",
) -> Outcome:
    """Sync the clone and run compose up. One deploy per app at a time.

    sha is a commit to deploy; the app ref otherwise. requested is the sha as
    the user typed it, kept on the deploy.start event.
    """
    async with yard.lock(name):
        try:
            app = yard.store.app(name)
        except YardError as exc:
            return Outcome("failed", error=str(exc))
        if not app.repo:
            return Outcome(
                "failed",
                error=f"app {name} has no repo",
                log="Set the repo: yard app set --repo https://<host>/<owner>/<name>\n",
            )
        if yard.skip_deploy:
            return Outcome("skipped", log="YARD_SKIP_DEPLOY is set. Deploy did not run.\n")
        try:
            forge = yard.forge(app.repo)
        except YardError as exc:
            return Outcome("failed", error=str(exc), log=f"{exc}\n")
        if not forge.connected():
            return Outcome(
                "failed",
                error=f"{forge.name} is not connected. Run yard forge setup {forge.name}.",
            )
        head = ""
        pending: asyncio.Task[None] | None = None
        try:
            token = await forge.token()
            head = await git.sync(yard.config.clone(name), clone_url(app.repo), sha or app.ref, token)
            pending = yard.spawn(report(forge, app.repo, head, "pending", f"deploying {head[:12]}"))
            journal.send(
                yard.config,
                "deploy.start",
                f"deploy {name} {head[:12]}",
                app=name,
                repo=app.repo,
                ref=app.ref,
                sha=head,
                image=compose.image_tag(name, head),
                cause=cause,
                actor=actor,
                requested=requested,
            )
            outcome = await _apply(yard, app, head)
        except YardError as exc:
            # A sync failure never reaches apply; deploy.end alone is the record.
            outcome = Outcome("failed", error=str(exc), log=f"{exc}\n")
        _deploy_end(yard, app, head, outcome)
        yard.spawn(_report_outcome(yard, app, forge, head, outcome, pending))
        return outcome


async def report(forge: Forge, repo: str, sha: str, state: str, description: str, target_url: str = "") -> None:
    """Post a commit status. A failure logs and records; it never raises."""
    if not forge.reports or not forge.owns(repo):
        return
    try:
        await forge.report(repo, sha, state, description, target_url)
    except Exception as exc:
        log.warning("%s report: %s", forge.name, exc)
        forge.last_report_error = str(exc)
    else:
        forge.last_report = now()
        forge.last_report_error = ""


async def _report_outcome(
    yard: Yard,
    app: App,
    forge: Forge,
    sha: str,
    outcome: Outcome,
    pending: asyncio.Task[None] | None,
) -> None:
    """The last word on a deploy, after the pending status landed."""
    if not sha or not forge.reports or outcome.status in {"skipped", "pending"}:
        return
    if pending is not None:
        await asyncio.gather(pending, return_exceptions=True)
    if outcome.status == "ok":
        host = await _live_host(yard, app)
        await report(
            forge,
            app.repo,
            sha,
            "success",
            f"live on {host}" if host else "live",
            f"https://{host}" if host else "",
        )
    elif outcome.status in {"reverted", "failed"}:
        error = (outcome.error.splitlines() or [""])[0]
        await report(forge, app.repo, sha, "failure", f"{outcome.status}: {error}".rstrip(": "))


async def _live_host(yard: Yard, app: App) -> str:
    """The public host whose bind dials this app; '' when Caddy or the bind is absent."""
    try:
        found = await binds.read(yard)
    except YardError:
        return ""
    names = {app.name, compose.app_container(app.name)}
    for bind in found:
        if bind.dial.split(":", 1)[0] in names:
            return bind.host
    return ""


def _deploy_end(yard: Yard, app: App, sha: str, outcome: Outcome) -> None:
    journal.send(
        yard.config,
        "deploy.end",
        f"deploy {app.name} {outcome.status}",
        priority=3 if outcome.status in {"failed", "reverted"} else 6,
        app=app.name,
        sha=sha,
        image=outcome.image,
        status=outcome.status,
        previous=outcome.previous,
        container=f"deploy-{app.name}",
        error=(outcome.error.splitlines() or [""])[0],
    )


async def _apply(yard: Yard, app: App, sha: str) -> Outcome:
    """The compose check, then one apply container for the rest."""
    image = compose.image_tag(app.name, sha)

    # The compose check. Any failure here stops before a build.
    try:
        project = compose.prepare(yard.config, app, yard.config.clone(app.name), image, sha)
    except Exception as exc:
        return Outcome("failed", image, f"{exc}\n", str(exc))

    # A deploy of a sha whose image is still local skips the build.
    if await _image_exists(yard, image):
        project.env["SKIP_BUILD"] = "1"

    # A failed `up --wait` reverts to the running image.
    previous = await _running_image(yard, app.name)
    if previous and previous != image:
        project.env["PREVIOUS_IMAGE"] = previous

    # One apply container runs compose up. The deploy container is the record of this run.
    code, text = await compose.apply(
        yard.config,
        app.name,
        "up",
        project,
        detach=app.name == "yard",
    )
    log = text if text.endswith("\n") or not text else text + "\n"
    if app.name == "yard":
        # Upgrading yard replaces this process, so that apply run is detached.
        return Outcome("pending", image, log, previous=previous)
    if code == 3:
        return Outcome("reverted", image, log, f"unhealthy; reverted to {previous}", previous)
    if code:
        return Outcome("failed", image, log, f"apply exited {code}", previous)
    return Outcome("ok", image, log, previous=previous)


async def _image_exists(yard: Yard, image: str) -> bool:
    """True when podman already holds the tag. A podman failure means build."""
    try:
        await yard.podman.run("image", "exists", image)
    except YardError:
        return False
    return True


async def _running_image(yard: Yard, name: str) -> str:
    """The image behind the running app container; empty when there is none."""
    try:
        listed = podman.parse_ps(await yard.podman.run("ps", "--format", "json"))
    except YardError:
        return ""
    item = listed.get(compose.app_container(name))
    return item.image if item and item.running else ""


_DEBOUNCE = 10.0


async def check_all(yard: Yard) -> None:
    """Poll every push app once per forge. A remote tip newer than the clone HEAD deploys."""
    for forge in yard.forges.values():
        await _check_forge(yard, forge)


async def _check_forge(yard: Yard, forge: Forge) -> None:
    if not forge.connected():
        return
    try:
        token = await forge.token()
    except YardError as exc:
        log.warning("%s token: %s", forge.name, exc)
        forge.last_check = now()
        return
    prefix = f"https://{forge.host}/"
    for app in yard.store.list_apps():
        if app.trigger == "push" and app.repo.lower().startswith(prefix):
            await _check_app(yard, app, forge, token, cause="check", actor="check")
    forge.last_check = now()


async def check_repo(yard: Yard, repo: str) -> None:
    """The webhook path: check the apps on one repo. The first push runs now."""
    key = repo.lower()
    if _debounce(yard, key, lambda: _check_repo(yard, repo)):
        await _check_repo(yard, repo)


async def check_installation(yard: Yard, forge: Forge) -> None:
    """An installation event refreshes the saved installation. The push debounce applies."""
    if _debounce(yard, f"installation:{forge.host}", lambda: _refresh_installation(yard, forge)):
        await _refresh_installation(yard, forge)


def _debounce(yard: Yard, key: str, run: Callable[[], Awaitable[None]]) -> bool:
    """Leading plus trailing. True runs now; false queues one run at the end of the window."""
    if time.monotonic() - yard.checked.get(key, -_DEBOUNCE) < _DEBOUNCE:
        if key not in yard.queued:
            yard.queued[key] = run
            yard.spawn(_trailing(yard, key))
        return False
    yard.checked[key] = time.monotonic()
    return True


async def _trailing(yard: Yard, key: str) -> None:
    await asyncio.sleep(max(0.0, _DEBOUNCE - (time.monotonic() - yard.checked[key])))
    run = yard.queued.pop(key)
    yard.checked[key] = time.monotonic()
    await run()


async def _refresh_installation(yard: Yard, forge: Forge) -> None:
    try:
        installation = await forge.find_installation()
    except YardError as exc:
        log.warning("%s installation: %s", forge.name, exc)
        return
    if installation is not None:
        selection = forge.selection(installation)
        forge.save(selection=selection)
        journal.send(
            yard.config,
            "forge.installation",
            f"{forge.name} installation",
            forge=forge.name,
            installation_id=forge.installation_id,
            selection=selection,
        )


async def _check_repo(yard: Yard, repo: str) -> None:
    try:
        forge = yard.forge(repo)
    except YardError:
        return
    if not forge.connected():
        return
    try:
        token = await forge.token()
    except YardError as exc:
        log.warning("%s token: %s", forge.name, exc)
        return
    for app in yard.store.list_apps():
        if app.repo.lower() == repo.lower() and app.trigger == "push":
            await _check_app(yard, app, forge, token, cause="push", actor=f"webhook:{forge.name}")


async def _check_app(yard: Yard, app: App, forge: Forge, token: str, *, cause: str, actor: str) -> None:
    try:
        clone = yard.config.clone(app.name)
        tip = await git.remote_tip(clone, clone_url(app.repo), app.ref, token)
        yard.tips[app.name] = tip
        head = git.head(clone)
        if tip and tip != head:
            journal.send(
                yard.config,
                "check.moved",
                f"{app.name} tip moved",
                app=app.name,
                repo=app.repo,
                ref=app.ref,
                head=head,
                tip=tip,
            )
            await deploy(yard, app.name, cause=cause, actor=actor)
    except Exception as exc:
        log.warning("check %s: %s", app.name, exc)


async def loop(yard: Yard) -> None:
    """One loop that asks the forges for the watched refs on a timer."""
    try:
        while True:
            await asyncio.sleep(yard.config.check_seconds)
            try:
                await check_all(yard)
            except Exception as exc:
                log.warning("check: %s", exc)
    except asyncio.CancelledError:
        return


async def prune_loop(yard: Yard) -> None:
    """The daily prune. The first run waits ten minutes so the boot deploys settle."""
    try:
        await asyncio.sleep(600)
        while True:
            try:
                found = await prune(yard, yard.config.prune_days, False, actor="auto")
            except Exception as exc:
                log.warning("prune: %s", exc)
                yard.last_prune_error = str(exc)
            else:
                yard.last_prune = now()
                yard.last_prune_freed = found.freed
                yard.last_prune_error = ""
            await asyncio.sleep(24 * 3600)
    except asyncio.CancelledError:
        return


@dataclass(slots=True)
class Image:
    tag: str
    bytes: int
    kept: str = ""


@dataclass(slots=True)
class Report:
    images: list[Image] = field(default_factory=list)
    freed: int = 0
    days: int = 0
    dry: bool = False


async def prune(yard: Yard, days: int, dry: bool, *, actor: str = "") -> Report:
    """Remove localhost images no container uses."""
    listed = podman.parse_ps(await yard.podman.run("ps", "-a", "--format", "json"))
    keep_images = {item.image for item in listed.values() if item.image.startswith("localhost/")}

    cutoff = time.time() - days * 86400
    result = Report(days=days, dry=dry)
    raw = await yard.podman.run("images", "--format", "json")
    for image in podman.parse_images(raw):
        if not image.name.startswith("localhost/") or image.name == "localhost/yard-apply":
            continue
        if image.name in keep_images or (image.created and image.created.timestamp() >= cutoff):
            continue
        kept = ""
        if not dry:
            try:
                await yard.podman.run("rmi", image.name)
            except Exception as exc:
                # Podman refuses an image a container uses. The refusal is a kept reason.
                kept = str(exc).removeprefix(f"podman rmi {image.name}: ").splitlines()[-1].strip()
        if not kept:
            result.freed += image.size
        result.images.append(Image(image.name, image.size, kept))
    # podman image prune does not run: the build cache is worth keeping.
    if not dry:
        count = sum(1 for image in result.images if not image.kept)
        journal.send(
            yard.config,
            "prune",
            f"pruned {count} images",
            count=str(count),
            freed=str(result.freed),
            actor=actor,
        )
    return result
