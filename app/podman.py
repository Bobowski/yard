"""Podman CLI and the JSON it prints. The socket stays on the host."""

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from app.errors import YardError


@dataclass(slots=True)
class Listed:
    id: str = ""
    name: str = ""
    slot: str = ""
    state: str = ""
    status: str = ""
    health: str = ""
    exit_code: int = 0
    running: bool = False
    created: datetime | None = None
    started: datetime | None = None
    exited: datetime | None = None


@dataclass(slots=True)
class Inspected:
    id: str
    running: bool
    started: datetime | None
    finished: datetime | None


@dataclass(slots=True)
class Sample:
    name: str
    cpu: str
    mem: str


@dataclass(slots=True)
class LogRow:
    at: str
    line: str
    raw: str


class Podman:
    def __init__(self, binary: str = "podman", *, remote: bool = False) -> None:
        self.binary = binary or "podman"
        self.remote = remote

    def _args(self, args: tuple[str, ...]) -> list[str]:
        head = [self.binary]
        if self.remote:
            head.append("--remote")
        head.extend(args)
        return head

    async def run(self, *args: str, timeout: float | None = 20) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._args(args),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise YardError(502, "podman", f"podman {' '.join(args)}: {exc}") from exc
        try:
            if timeout is None:
                out, err = await proc.communicate()
            else:
                out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError as exc:
            proc.kill()
            raise YardError(502, "podman", f"podman {' '.join(args)}: timed out") from exc
        text = ((out or b"") + (err or b"")).decode(errors="replace").strip()
        if proc.returncode:
            raise YardError(502, "podman", f"podman {' '.join(args)}: {text}", exit_code=proc.returncode)
        return text

    async def popen(self, *args: str) -> asyncio.subprocess.Process:
        try:
            return await asyncio.create_subprocess_exec(
                *self._args(args),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError as exc:
            raise YardError(502, "podman", f"podman {' '.join(args)}: {exc}") from exc

    async def ensure_network(self, name: str) -> None:
        try:
            await self.run("network", "exists", name, timeout=20)
        except YardError:
            await self.run("network", "create", name, timeout=20)

    async def build(self, tag: str, context: str, file: str) -> None:
        await self.run("build", "-t", tag, "-f", file, context, timeout=None)

    async def pull(self, image: str) -> None:
        await self.run("pull", image, timeout=None)

    async def image_exists(self, tag: str) -> bool:
        try:
            await self.run("image", "exists", tag, timeout=20)
        except YardError as exc:
            if exc.exit_code == 1:
                return False
            raise
        return True

    async def remove(self, name: str) -> None:
        await self.run("rm", "-f", name, timeout=20)


def ps_args() -> list[str]:
    return ["ps", "-a", "--format", "json"]


def stats_args(names: list[str] | None = None) -> list[str]:
    args = ["stats", "--no-stream", "--format", "json"]
    for name in names or []:
        if not ok_name(name):
            raise YardError(400, "bad_args", "bad container name")
        args.append(name)
    return args


def inspect_args(name: str) -> list[str]:
    if not ok_name(name):
        raise YardError(400, "bad_args", "bad container name")
    return ["inspect", name]


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


def parse_stats(raw: str) -> dict[str, Sample]:
    rows = _rows(raw)
    found: dict[str, Sample] = {}
    for row in rows:
        name = _str(row, "Name", "name")
        if not name:
            continue
        found[name] = Sample(name, _str(row, "CPUPerc", "CPU", "cpu_percent"), _str(row, "MemUsage", "mem_usage"))
    return found


def parse_ps(raw: str) -> dict[str, Listed]:
    found: dict[str, Listed] = {}
    for row in _rows(raw):
        names = _names(row)
        if not names:
            continue
        health = _str(row, "Health", "health")
        status = _str(row, "Status", "status")
        if not health:
            health = _health_text(status)
        item = Listed(
            id=_str(row, "Id", "ID", "id"),
            name=names[0],
            slot=_label(row, "yard.slot"),
            state=_str(row, "State", "state"),
            status=status,
            health=health,
            exit_code=_int(row, "ExitCode", "exitCode"),
            created=_time(row, "Created", "CreatedAt", "created"),
            started=_time(row, "StartedAt", "startedAt"),
            exited=_time(row, "ExitedAt", "exitedAt"),
        )
        item.running = item.state == "running"
        found[item.name] = item
    return found


def latest_for_slot(listed: dict[str, Listed], slot: str) -> Listed | None:
    best: Listed | None = None
    best_at = datetime.min.replace(tzinfo=UTC)
    for item in listed.values():
        if item.name != slot and item.slot != slot:
            continue
        created = item.created or datetime.min.replace(tzinfo=UTC)
        if best is None or created > best_at or (created == best_at and item.running):
            best = item
            best_at = created
    return best


def ids_for_slot(raw: str, slot: str) -> list[str]:
    listed = parse_ps(raw)
    labeled = [item.id for item in listed.values() if item.slot == slot and item.id]
    if labeled:
        return labeled
    return [item.id for item in listed.values() if item.id]


def parse_inspect(raw: str) -> list[Inspected]:
    rows = _rows(raw)
    found: list[Inspected] = []
    for row in rows:
        state = row.get("State")
        if not isinstance(state, dict):
            state = {}
        started = _stamp(state.get("StartedAt"))
        finished = _stamp(state.get("FinishedAt"))
        found.append(Inspected(str(row.get("Id") or ""), bool(state.get("Running")), started, finished))
    return found


def page_logs(raw: str, cursor: str, limit: int) -> tuple[str, str, bool]:
    rows, nxt, more = page_log_rows(raw, cursor, "", "", limit)
    return "\n".join(row.raw for row in rows), nxt, more


def page_log_rows(raw: str, cursor: str, since: str, until: str, limit: int) -> tuple[list[LogRow], str, bool]:
    if limit < 1:
        limit = 200
    current = _parsed_stamp(cursor)
    start = _parsed_stamp(since)
    stop = _parsed_stamp(until)
    nxt = cursor
    kept: list[LogRow] = []
    for line in raw.split("\n"):
        if not line:
            continue
        field, _, rest = line.partition(" ")
        stamp = _parsed_stamp(field)
        if stamp is not None:
            if current is not None and stamp <= current:
                continue
            if start is not None and stamp < start:
                continue
            if stop is not None and stamp >= stop:
                continue
        if len(kept) == limit:
            return kept, nxt, True
        row = LogRow(at="", line=line, raw=line)
        if stamp is not None and rest:
            row.at = field
            row.line = rest
            nxt = format_stamp(stamp)
        kept.append(row)
    return kept, nxt, False


def format_stamp(stamp: datetime) -> str:
    stamp = stamp.astimezone(UTC)
    base = stamp.strftime("%Y-%m-%dT%H:%M:%S")
    if stamp.microsecond == 0:
        return base + "Z"
    return base + "." + f"{stamp.microsecond:06d}".rstrip("0") + "Z"


def _rows(raw: str) -> list[dict]:
    raw = raw.strip()
    if raw in {"", "null", "[]"}:
        return []
    data = json.loads(raw)
    if not isinstance(data, list):
        raise YardError(502, "podman", "podman json is not a list")
    return [row for row in data if isinstance(row, dict)]


def _str(row: dict, *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if isinstance(value, str):
            return value
    return ""


def _int(row: dict, *keys: str) -> int:
    for key in keys:
        value = row.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return 0


def _names(row: dict) -> list[str]:
    for key in ("Names", "names"):
        value = row.get(key)
        if isinstance(value, str) and value:
            return [value]
        if isinstance(value, list):
            return [item for item in value if isinstance(item, str) and item]
    return []


def _label(row: dict, key: str) -> str:
    labels = row.get("Labels")
    if not isinstance(labels, dict):
        labels = row.get("labels")
    if not isinstance(labels, dict):
        return ""
    value = labels.get(key, "")
    return value if isinstance(value, str) else ""


def _time(row: dict, *keys: str) -> datetime | None:
    for key in keys:
        stamp = _stamp(row.get(key))
        if stamp is not None:
            return stamp
    return None


def _stamp(value: object) -> datetime | None:
    if isinstance(value, str) and value:
        return _parsed_stamp(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return datetime.fromtimestamp(int(value), UTC)
    return None


def _parsed_stamp(text: str) -> datetime | None:
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


def _health_text(status: str) -> str:
    if "(healthy)" in status:
        return "healthy"
    if "(unhealthy)" in status:
        return "unhealthy"
    if "(starting)" in status:
        return "starting"
    return ""
