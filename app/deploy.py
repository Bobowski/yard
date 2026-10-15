"""One deploy: checkout, the compose check, and one apply container for the rest."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app import compose, git, podman
from app.errors import YardError
from app.store import Slot

if TYPE_CHECKING:
    from app.yard import Yard


@dataclass(slots=True)
class Outcome:
    status: str
    image: str = ""
    log: str = ""
    error: str = ""


async def deploy(yard: Yard, slot: Slot, sha: str) -> Outcome:
    image = compose.image_tag(slot.repo, slot.directory, sha)
    previous = await _previous(yard, slot.name)

    # The checkout and the compose check. Any failure here stops before a build.
    try:
        checkout = await git.checkout(yard.config, slot.name, slot.repo, slot.directory, sha)
        project, lines = await compose.prepare(yard.config, slot, checkout, image, sha)
    except Exception as exc:
        return Outcome("failed", image, f"{exc}\n", str(exc))

    # One apply container runs the rest: build for a job slot, the whole
    # start sequence otherwise. The up container is the record of this run.
    code, text = await compose.apply(
        yard.config,
        slot.name,
        "build" if slot.job else "up",
        project,
        previous=previous,
        detach=slot.name == "yard",
    )
    lines.append(text)
    log = "\n".join(lines)
    if not log.endswith("\n"):
        log += "\n"
    if slot.name == "yard":
        # Upgrading yard replaces this process, so that apply run is detached.
        return Outcome("pending", image, log)
    if code:
        return Outcome("failed", image, log, f"apply exited {code}")
    if slot.trigger == "push":
        try:
            lines.append(await yard.run_slot_locked(slot.name))
            log = "\n".join(lines)
            if not log.endswith("\n"):
                log += "\n"
        except YardError as exc:
            log += f"{exc}\n"
    return Outcome("ok", image, log)


async def _previous(yard: Yard, name: str) -> str:
    """The image on the running app container. Apply reverts to it."""
    try:
        listed = podman.parse_ps(await yard.podman.run("ps", "-a", "--format", "json"))
    except Exception:
        return ""
    for item in listed.values():
        if item.slot == name and item.role == "app" and item.image:
            return item.image
    return ""


async def start_service(yard: Yard, slot: Slot) -> str:
    """Start a down service from its saved project. Returns the apply output."""
    project = compose.saved_project(yard.config, slot)
    if project is None:
        raise RuntimeError(f"project {slot.name} has no compose file")
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
        raise RuntimeError(f"project {slot.name} has no compose file")
    code, text = await compose.apply(
        yard.config,
        slot.name,
        "job",
        project,
    )
    if code:
        raise RuntimeError(text.strip() or f"job exited {code}")
    return text
