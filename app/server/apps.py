"""The app records, the deploy, the checks, and prune. The yard record is ensured at boot."""

import asyncio
import hashlib
import logging
import re
import shutil
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from app.server import binds, compose, detect, git, journal, podman, registry
from app.server.forges import Forge
from app.server.store import App, Config
from app.server.yard import Yard
from app.shared import YardError, atomic_write, clone_url, ensure_file, now

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


def create(
    yard: Yard,
    name: str,
    repo: str,
    ref: str = "",
    trigger: str = "",
    containerfile: str = "",
    compose: str = "",
    image: str = "",
    recipe: str = "",
    *,
    actor: str = "",
) -> App:
    app = yard.store.create_app(name, repo, ref, trigger, containerfile, compose, image, recipe)
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
    containerfile: str | None = None,
    compose: str | None = None,
    actor: str = "",
) -> App:
    app = yard.store.update_app(name, repo=repo, ref=ref, trigger=trigger, containerfile=containerfile, compose=compose)
    journal.send(
        yard.config,
        "app.update",
        f"app {app.name} updated",
        app=app.name,
        repo=app.repo,
        ref=app.ref,
        trigger=app.trigger,
        containerfile=app.containerfile,
        compose=app.compose,
        image=app.image,
        recipe=app.recipe,
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
            if not (app.image or app.recipe):
                return Outcome(
                    "failed",
                    error=f"app {name} has no repo",
                    log="Set the repo: yard app set --repo https://<host>/<owner>/<name>\n",
                )
            if sha:
                return Outcome("failed", error=_rollback_help(app), log=f"{_rollback_help(app)}\n")
            if yard.skip_deploy:
                return Outcome("skipped", log="YARD_SKIP_DEPLOY is set. Deploy did not run.\n")
            return await _deploy_source(yard, app, cause=cause, actor=actor)
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

    # What the app runs: the record decides, or the first deploy resolves and saves it.
    try:
        app, runs = _runs(yard, app, sha)
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
    log = runs + log
    if app.name == "yard":
        # Upgrading yard replaces this process, so that apply run is detached.
        return Outcome("pending", image, log, previous=previous)
    if code == 3:
        return Outcome("reverted", image, log, f"unhealthy; reverted to {previous}", previous)
    if code:
        return Outcome("failed", image, log, f"apply exited {code}", previous)
    return Outcome("ok", image, log, previous=previous)


def app_file(config: Config, app: App, which: str) -> str:
    deploy = f"Yard decides on the first deploy: yard app deploy --app {app.name}"
    if which == "compose":
        if app.compose:
            return _clone_file(config.clone(app.name), app.compose, app.name)
        if app.image:
            path = Path(compose.APPLY_DIR) / "image.compose.yml"
            if path.is_file():
                return path.read_text()
            raise YardError(404, "no_file", "Yard's image compose file is missing on this host", help_text=deploy)
        if app.recipe:
            path = _recipe_file(config, app.name)
            if path is not None:
                return path.read_text()
            raise YardError(404, "no_file", f"{app.name}'s recipe is not fetched yet", help_text=deploy)
        if app.containerfile:
            path = Path(compose.APPLY_DIR) / "default.compose.yml"
            if path.is_file():
                return path.read_text()
            raise YardError(404, "no_file", "Yard's compose file is missing on this host", help_text=deploy)
        raise YardError(404, "no_file", f"{app.name} has no compose file yet", help_text=deploy)
    if which == "containerfile":
        if app.image or app.recipe:
            kind = "an image" if app.image else "a recipe"
            raise YardError(404, "no_file", f"{app.name} runs {kind}; there is no Containerfile")
        if app.containerfile == "yard":
            path = config.app_dir(app.name) / "Containerfile"
            if path.is_file():
                return path.read_text()
            raise YardError(
                404,
                "no_file",
                f"{app.name}'s Containerfile is missing",
                help_text="Run yard app detect to write it again.",
            )
        if app.containerfile:
            return _clone_file(config.clone(app.name), app.containerfile, app.name)
        if app.compose:
            raise YardError(
                404,
                "no_file",
                f"{app.name} runs its own compose file; that file names its build",
                help_text="yard app cat compose",
            )
        raise YardError(404, "no_file", f"{app.name} has no Containerfile yet", help_text=deploy)
    raise YardError(404, "no_file", f"no file {which}", help_text="The files are compose and containerfile.")


def _clone_file(clone: Path, rel: str, name: str) -> str:
    root = clone.resolve()
    path = (root / rel).resolve()
    if path.parent != root and root not in path.parents:
        raise YardError(404, "no_file", f"{rel} is outside the clone")
    if not path.is_file():
        raise YardError(
            404,
            "no_file",
            f"{rel} is not in {name}'s clone",
            help_text=f"The clone is the deployed commit: yard app deploy --app {name}",
        )
    return path.read_text()


def _runs(yard: Yard, app: App, sha: str) -> tuple[App, str]:
    """What the app runs. Set fields are checked against the clone; an unset record is resolved and saved."""
    clone = yard.config.clone(app.name)
    if app.compose:
        if not (clone / app.compose).is_file():
            raise YardError(
                502,
                "compose",
                f"compose is set to {app.compose}; it is not in {sha[:12]}",
                help_text="Add the file, run yard app set --compose PATH, or run yard app detect.",
            )
        return app, ""
    if app.containerfile:
        if app.containerfile == "yard":
            if not (yard.config.app_dir(app.name) / "Containerfile").is_file():
                raise YardError(
                    502,
                    "compose",
                    f"containerfile is set to yard, but {app.name}'s Containerfile is missing",
                    help_text="Run yard app detect to write it again.",
                )
        elif not (clone / app.containerfile).is_file():
            raise YardError(
                502,
                "containerfile",
                f"containerfile is set to {app.containerfile}; it is not in {sha[:12]}",
                help_text="Add the file, run yard app set --containerfile PATH, or run yard app detect.",
            )
        return app, ""
    resolved = detect.resolve(clone, app.name)
    if resolved.generated:
        atomic_write(yard.config.app_dir(app.name) / "Containerfile", resolved.generated)
    app = update(yard, app.name, **{resolved.field: resolved.value})
    line = f"== runs {resolved.field} {resolved.value}"
    if resolved.generated:
        line += f" ({resolved.kind}: {resolved.reason})"
    return app, line + "\n"


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
    recipes: dict[str, bytes] = {}
    for app in yard.store.list_apps():
        if app.image or app.recipe:
            await _check_source(yard, app, recipes)


async def _check_source(yard: Yard, app: App, recipes: dict[str, bytes]) -> None:
    """The newest revision of an image tag or recipe URL. A failed fetch never deploys."""
    try:
        if app.image:
            revision = await registry.digest(app.image)
        else:
            body = recipes.get(app.recipe)
            if body is None:
                body = await fetch_recipe(app.recipe)
                recipes[app.recipe] = body
            revision = "sha256:" + hashlib.sha256(body).hexdigest()
    except Exception as exc:
        message = exc.message if isinstance(exc, YardError) else str(exc)
        yard.source_errors[app.name] = message
        log.warning("%s source: %s", app.name, message)
        return
    yard.source_errors.pop(app.name, None)
    yard.tips[app.name] = revision
    if app.trigger != "push":
        return
    deployed = deployed_revision(yard.config, app.name)
    if revision != deployed:
        journal.send(
            yard.config,
            "check.moved",
            f"{app.name} revision moved",
            app=app.name,
            head=deployed,
            tip=revision,
        )
        await deploy(yard, app.name, cause="check", actor="check")


def deployed_revision(config: Config, name: str) -> str:
    """The revision the last ok deploy ran: a manifest digest or the recipe file sha256."""
    file = config.app_dir(name) / "revision"
    return file.read_text().strip() if file.is_file() else ""


def _rollback_help(app: App) -> str:
    if app.image:
        return f"an image app rolls back by tag: yard app set --app {app.name} --image REF:OLDER"
    return "a recipe app rolls back by URL: point --recipe at an older commit's raw URL"


async def _deploy_source(yard: Yard, app: App, *, cause: str, actor: str) -> Outcome:
    """A repo-less deploy: pin an image by digest, or fetch a recipe compose file."""
    try:
        if app.image:
            revision = await registry.digest(app.image)
            image = f"{app.image.split('@', 1)[0]}@{revision}"
            project = _source_project(yard.config, app, compose.IMAGE_COMPOSE, image, revision)
            project.env["SKIP_BUILD"] = "1"
        else:
            body = await fetch_recipe(app.recipe)
            revision = "sha256:" + hashlib.sha256(body).hexdigest()
            image = compose.image_tag(app.name, revision[7:19])
            recipe_file = _write_recipe(yard.config, app.name, revision, body)
            project = _source_project(yard.config, app, str(recipe_file), image, revision)
            previous_recipe = _recipe_file(yard.config, app.name)
            if previous_recipe is not None:
                project.env["PREVIOUS_COMPOSE_FILE"] = str(previous_recipe)
    except YardError as exc:
        return Outcome("failed", error=str(exc), log=f"{exc}\n")
    journal.send(
        yard.config,
        "deploy.start",
        f"deploy {app.name} {revision[:12]}",
        app=app.name,
        sha=revision,
        image=image,
        cause=cause,
        actor=actor,
        requested="",
    )
    previous = await _running_image(yard, app.name)
    if previous and app.image and previous != image:
        project.env["PREVIOUS_IMAGE"] = previous
    code, text = await compose.apply(yard.config, app.name, "up", project)
    log = text if text.endswith("\n") or not text else text + "\n"
    if code == 3:
        outcome = Outcome("reverted", image, log, f"unhealthy; reverted to {previous}", previous)
    elif code:
        outcome = Outcome("failed", image, log, f"apply exited {code}", previous)
    else:
        outcome = Outcome("ok", image, log, previous=previous)
    _deploy_end(yard, app, revision, outcome)
    if outcome.status == "ok":
        atomic_write(yard.config.app_dir(app.name) / "revision", revision + "\n")
        if app.recipe:
            _prune_recipes(yard.config, app.name)
    return outcome


def _source_project(config: Config, app: App, file: str, image: str, revision: str) -> compose.Project:
    env = compose.run_env(config, app, config.app_dir(app.name), image, revision)
    compose.ensure_env(config.env(app.name))
    return compose.Project(app.name, [file], env)


_RECIPE_MAX = 256 * 1024
_RECIPE_PORT = re.compile(r"^#\s*port:\s*(\d{1,5})\s*$", re.M)


def recipe_port(body: bytes) -> int:
    """A recipe may declare `# port: 3000`; --public binds it."""
    found = _RECIPE_PORT.search(body.decode("utf-8", "replace"))
    return int(found.group(1)) if found else 0


def _recipe_dir(config: Config, name: str) -> Path:
    directory = config.app_dir(name) / "recipe"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _recipe_file(config: Config, name: str) -> Path | None:
    """The compose file of the last ok recipe deploy, for the revert path."""
    revision = deployed_revision(config, name)
    if not revision.startswith("sha256:"):
        return None
    path = config.app_dir(name) / "recipe" / f"{revision[7:19]}.yml"
    return path if path.is_file() else None


def _write_recipe(config: Config, name: str, revision: str, body: bytes) -> Path:
    path = _recipe_dir(config, name) / f"{revision[7:19]}.yml"
    if not path.is_file():
        atomic_write(path, body.decode("utf-8", "replace"))
    return path


def _prune_recipes(config: Config, name: str) -> None:
    """Keep the current and the previous recipe file; drop older ones."""
    keep = {deployed_revision(config, name)[7:19]}
    files = sorted(_recipe_dir(config, name).glob("*.yml"), key=lambda p: p.stat().st_mtime, reverse=True)
    keep.add(files[1].stem if len(files) > 1 else "")
    for path in files[2:]:
        path.unlink()


def _get(url: str) -> tuple[int, dict[str, str], bytes]:
    """One recipe request; redirects surface so they stay on https."""
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return res.status, dict(res.headers), res.read(_RECIPE_MAX + 1)
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()
    except urllib.error.URLError as exc:
        raise YardError(502, "recipe", f"{url}: {exc.reason}") from exc


async def fetch_recipe(url: str) -> bytes:
    """The recipe body: https only, ≤ 256 KiB, redirects stay on https."""
    return await asyncio.to_thread(_fetch_recipe, url)


def _fetch_recipe(url: str) -> bytes:
    for _ in range(6):
        code, headers, body = _get(url)
        if code in (301, 302, 303, 307, 308):
            location = headers.get("Location") or headers.get("location") or ""
            if not location.startswith("https://"):
                raise YardError(502, "recipe", f"{url}: redirect to {location or 'nowhere'} is not https")
            url = location
            continue
        if code >= 300:
            raise YardError(502, "recipe", f"{url}: HTTP {code}")
        if len(body) > _RECIPE_MAX:
            raise YardError(502, "recipe", "recipe is larger than 256 KiB")
        return body
    raise YardError(502, "recipe", f"{url}: too many redirects")


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
