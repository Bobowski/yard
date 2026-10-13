"""What the host shows: the slot table, one slot's page, and container logs."""

import asyncio
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from stario.json import JsonValue

from app import caddy, git, podman
from app.compose import APP_PORT
from app.config import Config
from app.errors import YardError
from app.podman import Container, Sample, format_stamp
from app.store import Deploy, Slot
from app.values import image_sha

if TYPE_CHECKING:
    from app.yard import Yard


@dataclass(slots=True)
class Row:
    slot: str = ""
    container: str = ""
    service: str = ""
    state: str = ""
    running: bool = False
    health: str = ""
    answers: bool | None = None
    restarts: int = 0
    exit_code: int = 0
    started: str = ""
    exited: str = ""
    image: str = ""
    sha: str = ""
    tip: str = ""
    domain: str = ""
    trigger: str = ""
    cpu: str = ""
    mem: str = ""
    data: int = 0
    deploy: dict[str, Any] | None = None


async def report(yard: Yard, running_only: bool = False) -> dict[str, Any]:
    config, store, client = yard.config, yard.store, yard.podman
    slots = store.list_slots()
    listed = podman.parse_ps(await client.run("ps", "-a", "--format", "json"))
    try:
        samples = podman.parse_stats(await client.run("stats", "--no-stream", "--format", "json"))
    except YardError:
        samples = {}
    host = podman.parse_info(await client.run("info", "--format", "json"))
    if not host.disk:
        host.disk = disk_capacity(config.root)

    tips: dict[str, dict[str, str]] = {}
    for slot in slots:
        if slot.repo not in tips:
            tips[slot.repo] = await git.tips(config.repo(slot.repo))

    # Apply containers in a deploy step are noise. Everything else shows.
    grouped: dict[str, list[Container]] = {}
    other: list[Container] = []
    for item in listed.values():
        if item.role == "deploy":
            continue
        if item.slot:
            grouped.setdefault(item.slot, []).append(item)
        else:
            other.append(item)

    rows: list[Row] = []
    for slot in slots:
        items = [item for item in grouped.pop(slot.name, []) if _shows(item, slot)]
        tip = tips.get(slot.repo, {}).get(slot.ref, "")
        rows += _slot_rows(config, slot, items, samples, tip, store.last_deploy(slot.name), running_only)
    for name in sorted(grouped):
        for item in sorted(grouped[name], key=_age):
            if running_only and not item.running:
                continue
            rows.append(_row(config, item, None, samples.get(item.name), "", None))
    for item in sorted(other, key=lambda one: one.name):
        if running_only and not item.running:
            continue
        rows.append(_row(config, item, None, samples.get(item.name), "", None))
    if config.caddy_sock is not None:
        await _probe_rows(rows)
    shown = [samples.get(row.container) for row in rows]
    return {
        "host": {
            "cpus": host.cpus,
            "mem": host.mem,
            "disk": host.disk,
            "cpu_used": sum(sample.cpu_percent for sample in shown if sample),
            "mem_used": sum(sample.mem_bytes for sample in shown if sample),
            "data_used": sum(row.data for row in rows),
        },
        "rows": [asdict(row) for row in rows],
    }


def _shows(item: Container, slot: Slot) -> bool:
    """The containers that count for a slot: its app services, and a job slot's last run."""
    return item.role == "app" or (slot.job and item.name == f"yard-apply-{slot.name}-job" and item.role == "job")


def _slot_rows(
    config: Config,
    slot: Slot,
    items: list[Container],
    samples: dict[str, Sample],
    tip: str,
    deploy: Deploy | None,
    running_only: bool,
) -> list[Row]:
    rows: list[Row] = []
    for item in sorted(items, key=_age):
        if running_only and not item.running:
            continue
        rows.append(_row(config, item, slot, samples.get(item.name), tip, deploy))
    if not items and not running_only:
        rows.append(_row(config, None, slot, None, tip, deploy))
    return rows


async def slot_report(yard: Yard, name: str) -> dict[str, Any]:
    """The one-slot page: its rows, its route, its recent deploys, its log tail."""
    config, store, client = yard.config, yard.store, yard.podman
    slot = store.slot(name)
    listed = podman.parse_ps(await client.run("ps", "-a", "--format", "json"))
    try:
        samples = podman.parse_stats(await client.run("stats", "--no-stream", "--format", "json"))
    except YardError:
        samples = {}
    tip = (await git.tips(config.repo(slot.repo))).get(slot.ref, "")
    items = [item for item in listed.values() if item.slot == name and _shows(item, slot)]
    rows = _slot_rows(config, slot, items, samples, tip, store.last_deploy(name), False)
    if config.caddy_sock is not None:
        await _probe_rows(rows)
    route = next(iter(caddy.routes([slot], config.domain)), None)
    page = await log_page(yard, name, "app", "", 20, "")
    return {
        "slot": slot_json(slot),
        "env_file": str(config.env(name)),
        "rows": [asdict(row) for row in rows],
        "route": asdict(route) if route else None,
        "deploys": [asdict(row) for row in store.list_deploys(name, 5)],
        "log": page["log"] if isinstance(page, dict) else "",
    }


def slot_json(slot: Slot) -> dict[str, Any]:
    return {
        "name": slot.name,
        "repo": slot.repo,
        "ref": slot.ref,
        "dir": slot.directory,
        "domain": slot.domain,
        "trigger": slot.trigger,
        "image": slot.live_image,
        "sha": image_sha(slot.live_image),
        "created": slot.created,
    }


async def _probe_rows(rows: list[Row]) -> None:
    targets = [row for row in rows if row.container and row.running and row.slot and not row.trigger]
    found = await asyncio.gather(*(probe(row.slot, APP_PORT) for row in targets))
    for row, up in zip(targets, found, strict=True):
        row.answers = up


async def probe(host: str, port: int, timeout: float = 1.0) -> bool:
    """Any HTTP status line means the app answers; the same rule apply.sh uses."""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        writer.write(f"GET / HTTP/1.0\r\nHost: {host}\r\n\r\n".encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout)
        writer.close()
        return line.startswith(b"HTTP/")
    except OSError, TimeoutError:
        return False


def _age(item: Container) -> tuple[float, str]:
    return (-(item.created.timestamp() if item.created else 0), item.name)


def _health(item: Container) -> str:
    if item.health in {"healthy", "unhealthy", "starting"}:
        return item.health
    if item.state == "dead" or (item.state == "exited" and item.exit_code != 0):
        return "failed"
    return ""


def _row(
    config: Config,
    item: Container | None,
    slot: Slot | None,
    sample: Sample | None,
    tip: str,
    deploy: Deploy | None,
) -> Row:
    """One rendered row. slot None means the container has no slot row behind it."""
    image = (item.image if item and item.image else (slot.live_image if slot else "")) or ""
    if item:
        health = _health(item)
    elif slot and slot.job:
        health = "idle"
    else:
        health = "down"
    key = slot.name if slot else ((item.slot or item.name) if item else "")
    return Row(
        slot=slot.name if slot else (item.slot if item else ""),
        container=item.name if item else "",
        service=item.service if item else "",
        state=item.state if item else "absent",
        running=item.running if item else False,
        restarts=item.restarts if item else 0,
        health=health,
        exit_code=item.exit_code if item else 0,
        started=format_stamp(item.started) if item and item.started else "",
        exited=format_stamp(item.exited) if item and item.exited else "",
        image=image,
        sha=image_sha(image),
        tip=tip,
        domain=slot.domain if slot else "",
        trigger=slot.trigger if slot else "",
        cpu=sample.cpu if sample else "",
        mem=sample.mem if sample else "",
        data=dir_size(config.data(key)),
        deploy={
            "id": deploy.id,
            "status": deploy.status,
            "sha": deploy.sha,
            "started": deploy.started,
            "built": deploy.built,
            "ended": deploy.ended,
        }
        if deploy
        else None,
    )


def dir_size(path: Path) -> int:
    if not path.is_dir():
        return 0
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            file = Path(root) / name
            try:
                total += file.stat().st_size
            except OSError:
                continue
    return total


def disk_capacity(path: Path) -> int:
    current = path
    while True:
        try:
            stats = os.statvfs(current)
        except OSError:
            if current == current.parent:
                return 0
            current = current.parent
            continue
        return int(stats.f_blocks) * int(stats.f_frsize)


async def log_page(yard: Yard, slot: str, role: str, service: str, tail: int, cursor: str) -> JsonValue:
    targets = await log_targets(yard, slot, role, service)
    return await log_text(yard, targets, tail, cursor)


async def log_targets(yard: Yard, slot: str, role: str, service: str) -> list[tuple[str, str]]:
    if slot:
        yard.store.slot(slot)
    listed = podman.parse_ps(await yard.podman.run("ps", "-a", "--format", "json"))
    found: list[tuple[str, str]] = []
    for item in listed.values():
        if item.role != (role or "app"):
            continue
        if slot and item.slot != slot and item.name != slot:
            continue
        label = item.service or item.name
        if service and label != service:
            continue
        shown = label
        if not slot and item.slot and shown and item.slot != shown:
            shown = f"{item.slot}/{shown}"
        found.append((shown or item.name, item.name))
    found.sort()
    if service and not found:
        where = slot or "this host"
        raise YardError(
            404,
            "no_service",
            f"service {service} is not in {where}",
            help_text="The service name is the name in the compose file.",
            example="yard logs --slot heldso --service app",
        )
    return found


async def log_text(yard: Yard, targets: list[tuple[str, str]], tail: int, cursor: str) -> JsonValue:
    if not targets:
        return {"log": "", "cursor": "", "more": False}
    chunks: list[str] = []
    nxt = ""
    more = False
    for label, container in targets:
        args = ["logs", "--timestamps"]
        if cursor and len(targets) == 1:
            args.extend(("--since", cursor))
        else:
            args.extend(("--tail", str(tail)))
        args.append(container)
        text, one_next, one_more = podman.page_logs(await yard.podman.run(*args), cursor, tail)
        if len(targets) > 1 and text:
            text = "".join(f"{label}  {line}\n" for line in text.splitlines() if line)
        chunks.append(text)
        if len(targets) == 1:
            nxt, more = one_next, one_more
    return {"log": "".join(chunks), "cursor": nxt, "more": more}
