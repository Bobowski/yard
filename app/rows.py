"""One table row per container. A slot with no container still gets a row."""

import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.podman import Listed, Sample, format_stamp
from app.store import Slot

_SYSTEM = {"caddy", "yard"}
_SIZE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)")
_SIZE_UNITS = {
    "B": 1,
    "kB": 10**3,
    "KB": 10**3,
    "MB": 10**6,
    "GB": 10**9,
    "TB": 10**12,
    "PB": 10**15,
    "KiB": 2**10,
    "MiB": 2**20,
    "GiB": 2**30,
    "TiB": 2**40,
}


@dataclass(slots=True)
class SlotRow:
    slot: str = ""
    container: str = ""
    state: str = ""
    exit_code: int = 0
    status: str = ""
    health: str = ""
    started: str = ""
    cpu: str = ""
    mem: str = ""
    data: str = ""
    sha: str = ""
    expected: str = ""
    build: str = ""


def slot_rows(
    slots: list[Slot],
    listed: dict[str, Listed],
    samples: dict[str, Sample],
    *,
    running_only: bool,
) -> list[SlotRow]:
    names = {slot.name for slot in slots}
    by_slot: dict[str, list[Listed]] = {}
    other: list[Listed] = []
    for item in listed.values():
        if item.slot:
            slot = item.slot
        elif item.name in names:
            slot = item.name
        else:
            slot = ""
        if not slot:
            other.append(item)
        else:
            by_slot.setdefault(slot, []).append(item)
    rows: list[SlotRow] = []
    seen: set[str] = set()
    for slot in slots:
        seen.add(slot.name)
        rows.extend(_rows_for(slot.name, slot.every, by_slot.get(slot.name, []), samples, running_only))
    for slot in sorted(set(by_slot) - seen):
        rows.extend(_rows_for(slot, "", by_slot[slot], samples, running_only))
    for item in sorted(other, key=lambda row: row.name):
        if running_only and not item.running:
            continue
        rows.append(_row_from(item, "", samples))
    return rows


def _rows_for(
    slot: str,
    every: str,
    items: list[Listed],
    samples: dict[str, Sample],
    running_only: bool,
) -> list[SlotRow]:
    if not items:
        if running_only:
            return []
        return [SlotRow(slot=slot, state="absent", health="idle" if every else "down")]
    items = sorted(items, key=lambda item: item.name)
    items.sort(key=lambda item: item.created or datetime.min.replace(tzinfo=UTC), reverse=True)
    rows: list[SlotRow] = []
    for item in items:
        if running_only and not item.running:
            continue
        rows.append(_row_from(item, slot, samples))
    return rows


def _row_from(item: Listed, slot: str, samples: dict[str, Sample]) -> SlotRow:
    sample = samples.get(item.name)
    if not slot and item.name in _SYSTEM:
        slot = item.name
    return SlotRow(
        slot=slot,
        container=item.name,
        state=item.state,
        exit_code=item.exit_code,
        status=item.status,
        health=_health(item),
        started=format_stamp(item.started) if item.started else "",
        cpu=sample.cpu if sample else "",
        mem=sample.mem if sample else "",
    )


def slot_label(slot: str, container: str) -> str:
    if slot == "total":
        return "total"
    if slot and container and slot != container:
        return f"{slot} ({container})"
    return slot or container


def mem_used(text: str) -> str:
    used = text.split("/", 1)[0].strip()
    return used


def build_label(status: str, started: str, ended: str) -> str:
    span = format_span(started, ended)
    if not status:
        return span
    if not span:
        return status
    return f"{status} {span}"


def format_span(started: str, ended: str) -> str:
    start = parse_time(started)
    stop = parse_time(ended)
    if start is None or stop is None or stop <= start:
        return ""
    return format_duration(stop - start)


def parse_time(text: str) -> datetime | None:
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def format_duration(delta: timedelta) -> str:
    ms = int(round(delta.total_seconds() * 1000))
    if ms < 1000:
        return f"{ms}ms"
    seconds, frac = divmod(ms, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes}m{seconds}s"
    if minutes:
        return f"{minutes}m{seconds}s"
    if frac:
        return f"{seconds}.{frac:03d}".rstrip("0") + "s"
    return f"{seconds}s"


def parse_percent(text: str) -> float | None:
    token = text.strip().rstrip("%")
    if not token:
        return None
    try:
        return float(token)
    except ValueError:
        return None


def parse_size(text: str) -> int | None:
    match = _SIZE.search(text.strip())
    if not match:
        return None
    unit = _SIZE_UNITS.get(match.group(2))
    if unit is None:
        return None
    return int(float(match.group(1)) * unit)


def format_bytes(size: int) -> str:
    value = float(max(size, 0))
    units = ["B", "kB", "MB", "GB", "TB", "PB"]
    unit = units[0]
    for name in units:
        unit = name
        if value < 1000 or name == units[-1]:
            break
        value /= 1000
    if unit == "B":
        return f"{int(value)}B"
    return f"{value:.4g}{unit}"


def format_percent(value: float) -> str:
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{text}%"


def total_row(rows: list[SlotRow], cpus: int, mem_limit: int, disk_limit: int) -> SlotRow:
    cpu = 0.0
    cpu_seen = False
    mem = 0
    mem_seen = False
    data = 0
    data_seen = False
    seen: set[str] = set()
    for row in rows:
        if row.slot == "total":
            continue
        percent = parse_percent(row.cpu)
        if percent is not None:
            cpu += percent
            cpu_seen = True
        used = parse_size(mem_used(row.mem)) if row.mem else None
        if used is not None:
            mem += used
            mem_seen = True
        key = row.slot or row.container
        if not key or key in seen:
            continue
        seen.add(key)
        size = parse_size(row.data) if row.data else None
        if size is not None:
            data += size
            data_seen = True
    cpu_text = format_percent(cpu) if cpu_seen else ""
    if cpu_text and cpus > 0:
        cpu_text = f"{cpu_text} / {format_percent(cpus * 100)}"
    mem_text = format_bytes(mem) if mem_seen else ""
    if mem_text and mem_limit > 0:
        mem_text = f"{mem_text} / {format_bytes(mem_limit)}"
    data_text = format_bytes(data) if data_seen else ""
    if data_text and disk_limit > 0:
        data_text = f"{data_text} / {format_bytes(disk_limit)}"
    return SlotRow(slot="total", cpu=cpu_text, mem=mem_text, data=data_text)


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


def _health(item: Listed) -> str:
    if item.health in {"healthy", "unhealthy", "starting"}:
        return item.health
    if item.state == "dead" or (item.state == "exited" and item.exit_code != 0):
        return "failed"
    return ""
