"""NAME=value slot files and experiment documents. The API and the CLI share these."""

import ast
from dataclasses import dataclass, field

from app.errors import YardError
from app.store import Slot

ORDER = [
    "SLOT_NAME",
    "SLOT_REPO",
    "SLOT_DATA_DIR",
    "SHA",
    "IMAGE",
    "REF",
    "SLOT_DIRECTORY",
    "DOMAIN",
    "SLOT_INTERVAL",
    "PORT",
    "PYTHONUNBUFFERED",
    "STARIO_HOST",
    "STARIO_PORT",
    "STARIO_TRACER",
]
READONLY = {"SLOT_NAME", "SLOT_REPO", "SHA", "IMAGE", "SLOT_DATA_DIR"}
FIELDS = {"REF", "SLOT_DIRECTORY", "DOMAIN", "SLOT_INTERVAL"}
_BASE = (
    ("SLOT_DATA_DIR", "/data"),
    ("PORT", "8000"),
    ("PYTHONUNBUFFERED", "1"),
    ("STARIO_HOST", "0.0.0.0"),
    ("STARIO_PORT", "8000"),
    ("STARIO_TRACER", "json"),
)


def name_ok(name: str) -> bool:
    if not name or len(name) > 128:
        return False
    for index, char in enumerate(name):
        if char == "_" or "A" <= char <= "Z" or "a" <= char <= "z":
            continue
        if index and "0" <= char <= "9":
            continue
        return False
    return True


def image_sha(image: str) -> str:
    if ":" not in image:
        return ""
    head, _, tail = image.rpartition(":")
    return tail if head and tail else ""


def process_defaults(name: str) -> dict[str, str]:
    return {"SLOT_NAME": name, **dict(_BASE)}


def slot_values(slot: Slot) -> dict[str, str]:
    values = process_defaults(slot.name)
    values.update(
        {
            "SLOT_NAME": slot.name,
            "SLOT_REPO": slot.repo,
            "REF": slot.ref,
            "SLOT_DIRECTORY": slot.directory,
            "DOMAIN": slot.domain,
            "SLOT_INTERVAL": slot.every,
            "SHA": image_sha(slot.live_image),
            "IMAGE": slot.live_image,
        }
    )
    for key, value in slot.env.items():
        if key.startswith("SLOT_") or key in READONLY or key in FIELDS:
            continue
        values[key] = value
    return values


def format_values(values: dict[str, str]) -> str:
    """NAME=value lines. A # line says who sets that group. The container receives every name."""
    groups = (
        ("# Identity. Yard sets these.", ("SLOT_NAME", "SLOT_REPO", "SLOT_DATA_DIR")),
        ("# This deploy. Yard sets these.", ("SHA", "IMAGE")),
        (
            "# Configuration. Edit these. The slot follows REF.",
            ("REF", "SLOT_DIRECTORY", "DOMAIN", "SLOT_INTERVAL"),
        ),
    )
    lines: list[str] = []
    used: set[str] = set()
    for title, keys in groups:
        present = [key for key in keys if key in values]
        if not present:
            continue
        if lines:
            lines.append("")
        lines.append(title)
        for key in present:
            lines.append(f"{key}={values.get(key, '')}")
            used.add(key)
    rest = [key for key in ORDER if key in values and key not in used]
    rest.extend(key for key in sorted(set(values) - set(ORDER)) if key not in used)
    if rest:
        if not any(line.startswith("# Configuration") for line in lines):
            if lines:
                lines.append("")
            lines.append("# Configuration. Edit these. The container receives these names.")
        for key in rest:
            lines.append(f"{key}={values[key]}")
    return "\n".join(lines) + "\n"


def _env_error(message: str) -> YardError:
    return YardError(
        400,
        "bad_key",
        message,
        help_text="A line is NAME=value. A # line is a comment.",
        example="DOMAIN=heldso.example.com",
    )


def _note_error(message: str) -> YardError:
    return YardError(
        400,
        "bad_title",
        message,
        help_text="A note starts with # and a title. A label line is @ key: value.",
        example="# first try\n@ area: login",
    )


def parse_env(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for number, line in enumerate(text.replace("\r\n", "\n").split("\n"), 1):
        trimmed = line.strip()
        if not trimmed or trimmed.startswith("#"):
            continue
        trimmed = trimmed.removeprefix("export ")
        key, sep, value = trimmed.partition("=")
        key = key.strip()
        if not sep or not name_ok(key):
            raise _env_error(f"line {number} must be NAME=value")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            try:
                parsed = ast.literal_eval(value)
            except (SyntaxError, ValueError) as exc:
                raise _env_error(f"line {number} has a broken quoted value") from exc
            if not isinstance(parsed, str):
                raise _env_error(f"line {number} has a broken quoted value")
            value = parsed
        if "\r" in value or "\n" in value:
            raise _env_error(f"line {number}: a value stays on one line")
        if key in found:
            raise _env_error(f"line {number} repeats {key}")
        found[key] = value
    return found


@dataclass(slots=True)
class SlotEdit:
    ref: str | None = None
    domain: str | None = None
    directory: str | None = None
    every: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    warn: list[str] = field(default_factory=list)


def apply_slot_keys(slot: Slot, patch: dict[str, str]) -> SlotEdit:
    values = slot_values(slot)
    for key, value in patch.items():
        if not name_ok(key):
            raise _env_error(f"bad key {key}")
        if value == "":
            values.pop(key, None)
        else:
            values[key] = value
    return apply_values(slot, values)


def apply_values(slot: Slot, parsed: dict[str, str]) -> SlotEdit:
    current = slot_values(slot)
    edit = SlotEdit()
    for key in ORDER:
        if key not in READONLY:
            continue
        if key in parsed and parsed[key] != current.get(key, ""):
            edit.warn.append(f"{key} stays {current.get(key, '')}")

    edit.ref = _changed(parsed, "REF", slot.ref, keep_empty=True)
    edit.directory = _changed(parsed, "SLOT_DIRECTORY", slot.directory, keep_empty=False)
    edit.domain = _changed(parsed, "DOMAIN", slot.domain, keep_empty=False)
    edit.every = _changed(parsed, "SLOT_INTERVAL", slot.every, keep_empty=False)

    # Anything else is an environment value for the container.
    defaults = process_defaults(slot.name)
    unknown: list[str] = []
    for key, value in parsed.items():
        if key in READONLY or key in FIELDS or key.startswith("SLOT_"):
            if key.startswith("SLOT_") and key not in READONLY and key not in FIELDS:
                unknown.append(key)
            continue
        if defaults.get(key) == value:
            continue
        edit.env[key] = value
    for key in sorted(unknown):
        edit.warn.append(f"{key} is not a slot field")
    return edit


def _changed(parsed: dict[str, str], key: str, current: str, *, keep_empty: bool) -> str | None:
    if key not in parsed:
        if current == "" or keep_empty:
            return None
        return ""

    got = parsed[key]
    if (keep_empty and got == "") or got == current:
        return None
    return got


@dataclass(slots=True)
class ExpLabel:
    key: str
    value: str


@dataclass(slots=True)
class Block:
    title: str
    body: str = ""
    labels: list[ExpLabel] = field(default_factory=list)
    id: int = 0
    has_id: bool = False


def parse_blocks(text: str) -> list[Block]:
    text = text.replace("\r\n", "\n").removeprefix("\ufeff")
    if len(text) > 1 << 20:
        raise _note_error("the file is too large")
    if not text.strip():
        return []
    lines = text.removesuffix("\n").split("\n")
    index = 0
    while index < len(lines) and not lines[index].strip():
        index += 1
    blocks: list[Block] = []
    seen: set[int] = set()
    while index < len(lines):
        title, ok = _title(lines[index])
        if not ok:
            raise _note_error(f"line {index + 1}: a title line starts with # ")
        if not title:
            raise _note_error(f"line {index + 1}: a title is required")
        if len(title) > 200:
            raise _note_error(f"line {index + 1}: the title is too long")
        index += 1
        labels: list[ExpLabel] = []
        exp_id = 0
        has_id = False
        while index < len(lines) and lines[index].startswith("@ "):
            key, value, ok = _label(lines[index])
            if not ok:
                raise _note_error(f"line {index + 1}: a label line is @ key: value")
            if key == "id":
                if has_id:
                    raise _note_error(f"line {index + 1}: id is already set")
                if not value.isdigit() or int(value) <= 0:
                    raise _note_error(f"line {index + 1}: id must be a number")
                exp_id = int(value)
                if exp_id in seen:
                    raise _note_error(f"line {index + 1}: id {exp_id} is repeated")
                seen.add(exp_id)
                has_id = True
            else:
                if not value:
                    raise _note_error(f"line {index + 1}: a label needs a value")
                if len(value) > 500:
                    raise _note_error(f"line {index + 1}: the label value is too long")
                labels.append(ExpLabel(key, value))
            index += 1
        if index < len(lines) and not lines[index].strip():
            index += 1
        body: list[str] = []
        while index < len(lines) and not _title(lines[index])[1]:
            body.append(lines[index])
            index += 1
        blocks.append(Block(title, "\n".join(body).rstrip("\n"), labels, exp_id, has_id))
    return blocks


def format_blocks(blocks: list[Block]) -> str:
    chunks: list[str] = []
    for index, block in enumerate(blocks):
        if index:
            chunks.append("\n")
        chunks.append(f"# {block.title}\n")
        if block.has_id:
            chunks.append(f"@ id: {block.id}\n")
        for label in block.labels:
            chunks.append(f"@ {label.key}: {label.value}\n")
        chunks.append("\n")
        if block.body:
            chunks.append(block.body + "\n")
    return "".join(chunks)


def _title(line: str) -> tuple[str, bool]:
    if line == "#":
        return "", True
    if not line.startswith("# "):
        return "", False
    return line[2:].strip(), True


def _label(line: str) -> tuple[str, str, bool]:
    rest = line.removeprefix("@ ")
    key, sep, value = rest.partition(": ")
    if not sep or not _key_ok(key):
        return "", "", False
    return key, value.strip(), True


def _key_ok(key: str) -> bool:
    if not key or len(key) > 64 or not key[0].isalpha():
        return False
    return all(char.isalnum() or char in "_-" for char in key)
