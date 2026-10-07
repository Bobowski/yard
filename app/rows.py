"""One table row per container. A slot with no container still gets a row."""

from dataclasses import dataclass
from datetime import UTC, datetime

from app.podman import Listed, Sample, format_stamp
from app.store import Slot


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


def _health(item: Listed) -> str:
    if item.health in {"healthy", "unhealthy", "starting"}:
        return item.health
    if item.state == "dead" or (item.state == "exited" and item.exit_code != 0):
        return "failed"
    return ""
