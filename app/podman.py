"""Podman CLI and the JSON it prints. The socket stays on the host."""

import asyncio
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.errors import YardError


@dataclass(slots=True)
class Container:
    id: str = ""
    name: str = ""
    image: str = ""
    slot: str = ""
    service: str = ""
    role: str = ""
    state: str = ""
    running: bool = False
    restarts: int = 0
    exit_code: int = 0
    health: str = ""
    created: datetime | None = None
    started: datetime | None = None
    exited: datetime | None = None


@dataclass(slots=True)
class Sample:
    name: str
    cpu: str
    mem: str
    cpu_percent: float = 0.0
    mem_bytes: int = 0


@dataclass(slots=True)
class Host:
    cpus: int = 0
    mem: int = 0
    disk: int = 0
    version: str = ""


@dataclass(slots=True)
class Image:
    name: str
    created: datetime | None = None
    size: int = 0


class Podman:
    def __init__(self, binary: str = "podman", remote: bool = False) -> None:
        self.binary = binary or "podman"
        self.remote = remote

    def _args(self, args: tuple[str, ...]) -> list[str]:
        head = [self.binary]
        if self.remote:
            head.append("--remote")
        head.extend(args)
        return head

    async def run(self, *args: str, timeout: float | None = 20, env: dict[str, str] | None = None) -> str:
        merged = os.environ.copy()
        if env:
            merged.update(env)
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._args(args),
                env=merged if env else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise YardError(
                502,
                "podman",
                f"podman {' '.join(args)}: {exc}",
                help_text="The podman command has to be on PATH.",
            ) from exc
        try:
            if timeout is None:
                out, err = await proc.communicate()
            else:
                out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError as exc:
            proc.kill()
            raise YardError(
                502,
                "podman",
                f"podman {' '.join(args)}: timed out",
                help_text="The command did not finish. The container may still be starting.",
            ) from exc
        text = ((out or b"") + (err or b"")).decode(errors="replace").strip()
        if proc.returncode:
            raise YardError(
                502,
                "podman",
                f"podman {' '.join(args)}: {text}",
                exit_code=proc.returncode,
                help_text="Yard talks to Podman through the socket on this host.",
            )
        return text

    async def popen(self, *args: str) -> asyncio.subprocess.Process:
        try:
            return await asyncio.create_subprocess_exec(
                *self._args(args),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError as exc:
            raise YardError(
                502,
                "podman",
                f"podman {' '.join(args)}: {exc}",
                help_text="The podman command has to be on PATH.",
            ) from exc


def ok_name(text: str) -> bool:
    if not text or len(text) > 128:
        return False
    for index, char in enumerate(text):
        if "a" <= char <= "z" or "A" <= char <= "Z" or "0" <= char <= "9":
            continue
        if index and char in "_.-":
            continue
        return False
    return True


def check_name(name: str) -> None:
    if ok_name(name):
        return
    raise YardError(
        400,
        "bad_args",
        "bad container name",
        context={"name": name},
        help_text="Start with a letter or digit. Then use letters, digits, and . _ -",
        example="heldso",
    )


def parse_info(raw: str) -> Host:
    try:
        info = json.loads(raw)
    except json.JSONDecodeError:
        return Host()
    if not isinstance(info, dict):
        return Host()
    host = info.get("host")
    store = info.get("store")
    cpus = mem = disk = 0
    if isinstance(host, dict):
        cpus = _int(host.get("cpus"))
        mem = _int(host.get("memTotal"))
    if isinstance(store, dict):
        disk = _int(store.get("graphRootAllocated"))
    version = info.get("version")
    return Host(cpus, mem, disk, str(version.get("Version") or "") if isinstance(version, dict) else "")


# Podman --format json prints snake_case keys; the table header spellings are a fallback.
def parse_stats(raw: str) -> dict[str, Sample]:
    found: dict[str, Sample] = {}
    for row in _rows(raw):
        name = str(row.get("name") or row.get("Name") or "")
        if not name:
            continue
        cpu = str(row.get("cpu_percent") or row.get("CPUPerc") or "")
        mem = str(row.get("mem_usage") or row.get("MemUsage") or "")
        found[name] = Sample(name, cpu, mem, _percent(cpu), _size(mem.split("/", 1)[0]))
    return found


def parse_images(raw: str) -> list[Image]:
    """One row per tag. Podman 4.9 gives Names, unix Created, and Size in bytes."""
    found: list[Image] = []
    for row in _rows(raw):
        names = row.get("Names")
        if not isinstance(names, list):
            continue
        created = _stamp(row.get("Created"))
        size = _int(row.get("Size"))
        for name in names:
            if isinstance(name, str) and name:
                found.append(Image(name, created, size))
    return found


def parse_ps(raw: str) -> dict[str, Container]:
    found: dict[str, Container] = {}
    for row in _rows(raw):
        names = row.get("Names")
        if not isinstance(names, list):
            names = []
        names = [str(name) for name in names if name]
        if not names:
            continue
        health = str(row.get("Health") or "")
        if not health:
            health = _health_text(str(row.get("Status") or ""))

        # Yard names its apply containers; compose labels every app container with its project and service.
        name = names[0]
        if name.startswith("yard-apply-"):
            rest = name.removeprefix("yard-apply-")
            role = "job" if rest.endswith("-job") else "deploy"
            slot = rest.removesuffix("-job")
            service = ""
        else:
            role = "app"
            slot = _label(row, "com.docker.compose.project")
            service = _label(row, "com.docker.compose.service")

        item = Container(
            id=str(row.get("Id") or ""),
            name=name,
            image=str(row.get("Image") or ""),
            slot=slot,
            service=service,
            role=role,
            state=str(row.get("State") or ""),
            restarts=_int(row.get("Restarts")),  # podman >= 4.3 exposes Restarts in ps --format json
            exit_code=_int(row.get("ExitCode")),
            health=health,
            created=_stamp(row.get("Created")),
            started=_stamp(row.get("StartedAt")),
            exited=_stamp(row.get("ExitedAt")),
        )
        item.running = item.state == "running"
        found[item.name] = item
    return found


def page_logs(raw: str, cursor: str, limit: int) -> tuple[str, str, bool]:
    if limit < 1:
        limit = 200
    current = parse_stamp(cursor)
    nxt = cursor
    kept: list[str] = []

    for line in raw.split("\n"):
        if not line:
            continue
        field, _, rest = line.partition(" ")
        stamp = parse_stamp(field)
        if stamp is not None and current is not None and stamp <= current:
            continue

        if len(kept) == limit:
            return "\n".join(kept), nxt, True
        if stamp is not None and rest:
            nxt = format_stamp(stamp)
        kept.append(line)
    return "\n".join(kept), nxt, False


def format_stamp(stamp: datetime) -> str:
    stamp = stamp.astimezone(UTC)
    base = stamp.strftime("%Y-%m-%dT%H:%M:%S")
    if stamp.microsecond == 0:
        return base + "Z"
    return base + "." + f"{stamp.microsecond:06d}".rstrip("0") + "Z"


def parse_stamp(text: str) -> datetime | None:
    if not text:
        return None
    head = text.split("|", 1)[0]
    if head.endswith("Z"):
        head = head[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(head)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp


def _rows(raw: str) -> list[dict[str, Any]]:
    raw = raw.strip()
    if raw in {"", "null", "[]"}:
        return []
    data = json.loads(raw)
    if not isinstance(data, list):
        raise YardError(
            502,
            "podman",
            "podman json is not a list",
            help_text="Podman --format json has to return a list of objects.",
        )
    return [row for row in data if isinstance(row, dict)]


_SIZE = re.compile(r"([0-9.]+)\s*([kmgtp]?i?b?|bytes?)", re.IGNORECASE)
_SIZE_UNITS = {"": 1, "b": 1, "byte": 1, "bytes": 1}
for _index, _prefix in enumerate("kmgtp", 1):
    _SIZE_UNITS[_prefix] = 1000**_index
    _SIZE_UNITS[f"{_prefix}b"] = 1000**_index
    _SIZE_UNITS[f"{_prefix}ib"] = 1024**_index


def _percent(text: str) -> float:
    try:
        return float(text.strip().rstrip("%"))
    except ValueError:
        return 0.0


def _size(text: str) -> int:
    match = _SIZE.search(text.strip())
    if not match:
        return 0
    unit = _SIZE_UNITS.get(match.group(2).lower())
    if unit is None:
        return 0
    return int(float(match.group(1)) * unit)


def _int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def _label(row: dict[str, Any], key: str) -> str:
    labels = row.get("Labels")
    if not isinstance(labels, dict):
        return ""
    value = labels.get(key, "")
    return value if isinstance(value, str) else ""


def _stamp(value: Any) -> datetime | None:
    if isinstance(value, str) and value:
        return parse_stamp(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return datetime.fromtimestamp(int(value), UTC)
    return None


def _health_text(status: str) -> str:
    if "(healthy)" in status:
        return "healthy"
    if "(unhealthy)" in status:
        return "unhealthy"
    if "(starting)" in status:
        return "starting"
    return ""
