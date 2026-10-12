"""One deploy: checkout, the compose check, and one apply container for the rest."""

import json
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
        _keep_legacy_env(yard, slot.name)
        checkout = await git.checkout(config, slot.repo, slot.directory, sha)
        project, lines = await compose.prepare(config, slot, checkout, image, sha)
    except Exception as exc:
        return _finish(store, deploy_id, "failed", image, [str(exc)], str(exc))

    # A service builds while the old container still answers. The 0.1 container
    # is removed next, so the health probe hits the new container.
    if not slot.job and slot.name != "yard":
        code, text = await compose.apply(config, slot.name, "build", project)
        lines.append(text)
        built = compose.built_stamp(text)
        if built:
            store.mark_built(deploy_id, built)
        if code:
            return _finish(store, deploy_id, "failed", image, lines, f"apply exited {code}")
        try:
            note = await retire_legacy(yard, slot.name)
        except Exception as exc:
            lines.append(str(exc))
            return _finish(store, deploy_id, "failed", image, lines, str(exc))
        if note:
            lines.append(note.rstrip("\n"))

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


def _keep_legacy_env(yard: Yard, name: str) -> None:
    """Copy a 0.1 env_json into the env file when that file is still empty."""
    if yard.read_env(name):
        return
    legacy = yard.store.legacy_env(name)
    if legacy:
        yard.write_env(name, legacy)


async def retire_legacy(yard: Yard, name: str) -> str:
    """Remove the 0.1 container named after the slot. A compose-owned container stays."""
    try:
        raw = await yard.podman.run("inspect", "--format", "{{json .Config.Labels}}", name, timeout=15)
    except Exception:
        return ""
    try:
        labels = json.loads(raw)
    except json.JSONDecodeError:
        labels = None
    if isinstance(labels, dict) and labels.get("com.docker.compose.project"):
        return ""
    try:
        await yard.podman.run("rm", "-f", "-t", "10", name, timeout=40)
    except Exception as exc:
        raise RuntimeError(f"remove {name} failed: {exc}") from exc
    return f"removed {name}\n"


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
