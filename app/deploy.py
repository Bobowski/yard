"""One deploy: checkout, the compose check, and one apply container for the rest."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app import compose, git
from app.errors import YardError
from app.store import Slot, Store

if TYPE_CHECKING:
    from app.yard import Yard


@dataclass(slots=True)
class Outcome:
    status: str
    image: str = ""
    log: str = ""
    error: str = ""


async def deploy(yard: Yard, slot: Slot, sha: str) -> Outcome:
    config, store = yard.config, yard.store
    image = compose.image_tag(slot.repo, slot.directory, sha)
    deploy_id = store.begin_deploy(slot.name, sha, image)

    # The checkout and the compose check. Any failure here stops before a build.
    try:
        checkout = await git.checkout(config, slot.repo, slot.directory, sha)
        project, lines = await compose.prepare(config, slot, checkout, image, sha)
    except Exception as exc:
        return _finish(store, deploy_id, "failed", image, [str(exc)], str(exc))

    # One apply container runs the rest: build for a job slot, the whole
    # start/health/revert sequence otherwise.
    code, text = await compose.apply(
        config,
        slot.name,
        "build" if slot.job else "up",
        project,
        previous=slot.live_image,
        detach=slot.name == "yard",
    )
    lines.append(text)
    if slot.name == "yard":
        # Upgrading yard replaces this process, so that apply run is detached
        # and the row stays running. The next process claims it on boot.
        return Outcome("pending", image, "\n".join(lines))
    built = compose.built_stamp(text)
    if built:
        store.mark_built(deploy_id, built)
    if code:
        return _finish(store, deploy_id, "failed", image, lines, f"apply exited {code}")
    store.set_image(slot.name, image)
    if slot.trigger == "push":
        try:
            lines.append(await yard.run_slot_locked(slot.name))
        except YardError as exc:
            lines.append(str(exc))
    return _finish(store, deploy_id, "ok", image, lines)


def _finish(store: Store, deploy_id: int, status: str, image: str, lines: list[str], error: str = "") -> Outcome:
    log = "\n".join(lines) + "\n"
    store.finish_deploy(deploy_id, status, log)
    return Outcome(status, image, log, error)


async def start_service(yard: Yard, slot: Slot) -> str:
    """Start a down service from its saved project. Returns the apply output."""
    project = compose.saved_project(yard.config, slot)
    if project is None:
        raise RuntimeError(f"slot {slot.name} has no saved project")
    code, text = await compose.apply(
        yard.config,
        slot.name,
        "start",
        project,
    )
    if code:
        raise RuntimeError(text.strip() or f"start exited {code}")
    return text


async def run_job(yard: Yard, slot: Slot) -> str:
    project = compose.saved_project(yard.config, slot)
    if project is None:
        raise RuntimeError(f"slot {slot.name} has no saved project")
    code, text = await compose.apply(
        yard.config,
        slot.name,
        "job",
        project,
    )
    if code:
        raise RuntimeError(text.strip() or f"job exited {code}")
    return text
