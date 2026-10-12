"""Terminal text for the yard command. Pure functions; the tests render them with a fixed now."""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

from app.errors import YardError
from app.values import format_env, name_ok, parse_env

# --- slot file ------------------------------------------------------------


_YARD_ENV = "SLOT_NAME SLOT_DATA_DIR SHA PORT"


def format_slot(slot: dict) -> str:
    name = slot.get("name", "")
    env_file = slot.get("env_file") or f"/srv/yard/env/{name}.env"
    lines = [
        f"# slot {name}",
        f"# image {slot.get('image', '')}",
        f"# Yard sets {_YARD_ENV} in the container.",
        f"# The file is {env_file} on the host.",
        "# TRIGGER: empty is a service that stays up; push, manual, or every 5m make a job.",
        f"REPO={slot.get('repo', '')}",
        f"REF={slot.get('ref', '')}",
        f"DIR={slot.get('dir', '')}",
        f"DOMAIN={slot.get('domain', '')}",
        f"TRIGGER={slot.get('trigger', '')}",
        "",
        "# env. The container receives these lines.",
    ]
    env = slot.get("env")
    body = format_env(env) if isinstance(env, dict) else ""
    return "\n".join(lines) + "\n" + body


def parse_slot(text: str) -> tuple[dict[str, Any], dict[str, str]]:
    """The slot file parsed: the four PATCH fields plus REPO, and the env."""
    values = parse_env(text)
    fields = {
        "repo": values.pop("REPO", ""),
        "ref": values.pop("REF", ""),
        "dir": values.pop("DIR", ""),
        "domain": values.pop("DOMAIN", ""),
        "trigger": values.pop("TRIGGER", ""),
    }
    return fields, values


_FIELD_SETS = {"ref": "ref", "dir": "dir", "domain": "domain", "trigger": "trigger"}


def slot_edit_patch(current: dict, sets: list[str]) -> dict[str, Any]:
    """The PATCH body for `slot edit --set`. Field keys patch fields; other keys patch env."""
    patch: dict[str, Any] = {}
    env = dict(current.get("env") or {})
    env_touched = False
    for item in sets:
        key, _, value = item.partition("=")
        if not name_ok(key):
            raise YardError(400, "bad_key", f"bad key {key}", help_text="A key uses letters, digits, and _")
        field = _FIELD_SETS.get(key.lower())
        if field is not None:
            patch[field] = value
            continue
        env_touched = True
        if value:
            env[key] = value
        else:
            env.pop(key, None)
    if env_touched:
        patch["env"] = env
    return patch


# --- the tables ------------------------------------------------------------


def format_status(report: dict, now: datetime, *, color: bool = False) -> str:
    rows = [row for row in report.get("rows") or [] if isinstance(row, dict)]
    cells = [_status_cells(row, now) for row in rows]
    headers = ["name", "state", "cpu", "mem"]
    indexes = [0, 1, 2, 3]
    for index, header in ((4, "data"), (5, "rev"), (6, "domain"), (7, "deploy")):
        if any(cell[index] for cell in cells):
            headers.append(header)
            indexes.append(index)
    right = {i for i, header in enumerate(headers) if header in {"cpu", "mem", "data"}}
    deploy_index = headers.index("deploy") if "deploy" in headers else -1
    text = _render(
        headers,
        [[cell[i] for i in indexes] for cell in cells],
        right,
        color=color,
        paint=partial(_paint, state=1, deploy=deploy_index),
    )
    host = _host_line(report.get("host") or {})
    return text + host if host else text


def _status_cells(row: dict, now: datetime) -> list[str]:
    slot = str(row.get("slot") or "")
    container = str(row.get("container") or "")
    if slot and container and container != slot:
        name = f"{slot} ({container})"
    else:
        name = slot or container
    mem = str(row.get("mem") or "").split("/", 1)[0].strip()
    return [
        name,
        _state_text(row, now),
        str(row.get("cpu") or ""),
        mem,
        format_bytes(int(row.get("data") or 0)) if row.get("data") else "",
        _rev(str(row.get("sha") or ""), str(row.get("tip") or "")),
        str(row.get("domain") or ""),
        _deploy_text(row.get("deploy")),
    ]


def _state_text(row: dict, now: datetime) -> str:
    state = str(row.get("state") or "")
    health = str(row.get("health") or "")
    code = row.get("exit_code") or 0
    job = bool(row.get("trigger"))
    if state == "absent":
        return "idle" if job else "down"
    if state in {"exited", "dead"}:
        age = _ago(str(row.get("exited") or ""), now)
        if job and state == "exited" and not code:
            return f"ran {age}".rstrip()
        word = "down" if state == "exited" and not code else "failed"
        return f"{word} {age}".rstrip()
    if state == "running":
        age = _ago(str(row.get("started") or ""), now)
        word = health if health in {"starting", "unhealthy"} else "up"
        text = f"{word} {age}".rstrip()
        if row.get("answers") is False:
            text += " · no answer"
        restarts = int(row.get("restarts") or 0)
        if restarts:
            text += f" · {restarts} restart" + ("s" if restarts != 1 else "")
        return text
    return state


def _rev(sha: str, tip: str) -> str:
    short = sha[:7]
    want = tip[:7]
    if not want or short == want:
        return short
    if not short:
        return f"→ {want}"
    return f"{short} → {want}"


def _deploy_text(deploy: Any) -> str:
    if not isinstance(deploy, dict):
        return ""
    status = str(deploy.get("status") or "")
    took = format_span(str(deploy.get("started") or ""), str(deploy.get("ended") or ""))
    text = f"{status} {took}".rstrip()
    when = _when(str(deploy.get("started") or ""))
    return f"{text} · {when}" if when else text


def _host_line(host: dict) -> str:
    parts = []
    cpu = host.get("cpu_used") or 0
    cpus = host.get("cpus") or 0
    if cpu and cpus:
        parts.append(f"{format_percent(cpu)} of {cpus} cores")
    mem = host.get("mem_used") or 0
    total = host.get("mem") or 0
    if mem and total:
        parts.append(f"{format_bytes(mem)} of {format_bytes(total)}")
    data = host.get("data_used") or 0
    disk = host.get("disk") or 0
    if data and disk:
        parts.append(f"{format_bytes(data)} of {format_bytes(disk)}")
    return f"host: {' · '.join(parts)}\n" if parts else ""


def status_hint(report: dict) -> str:
    """The one command worth typing next. deploys beats logs."""
    hint = ""
    for row in report.get("rows") or []:
        if not isinstance(row, dict):
            continue
        slot = str(row.get("slot") or "")
        if not slot:
            continue
        deploy = row.get("deploy")
        if isinstance(deploy, dict) and deploy.get("status") == "failed":
            return f"yard deploys {slot}"
        # The state word without its age drives the hint.
        word = _state_text(row, datetime.now(UTC)).split(" ", 1)[0]
        if not hint and (word in {"failed", "down", "unhealthy"} or row.get("answers") is False):
            hint = f"yard logs --slot {slot}"
    return hint


def format_slot_status(report: dict, now: datetime, *, color: bool = False) -> str:
    """The one-slot page: header, containers, deploys, log."""
    slot_raw = report.get("slot")
    slot: dict = slot_raw if isinstance(slot_raw, dict) else {}
    name = str(slot.get("name") or "")
    lines = [name]
    route_raw = report.get("route")
    route: dict | None = route_raw if isinstance(route_raw, dict) else None
    if route is not None:
        lines[0] += f"  {route.get('host')} → {route.get('dial')}"
    rows = [row for row in report.get("rows") or [] if isinstance(row, dict)]

    sha = str(slot.get("sha") or "")
    tip = next((str(row.get("tip") or "") for row in rows if row.get("tip")), "")
    rev = f"rev {sha[:7]}" if sha else ""
    if tip and tip[:7] != sha[:7]:
        rev += f" · tip {tip[:7]}"
    image = str(slot.get("image") or "")
    if image:
        rev += f" · image {image}"
    trigger = str(slot.get("trigger") or "")
    if trigger:
        rev += f" · trigger {trigger}"
    if rev:
        lines.append(rev)
    env_raw = slot.get("env")
    env: dict = env_raw if isinstance(env_raw, dict) else {}
    data = max((int(row.get("data") or 0) for row in rows), default=0)
    keys = f"{len(env)} keys" if len(env) != 1 else "1 key"
    lines.append(f"data {format_bytes(data)} · env {report.get('env_file') or ''} ({keys})")

    containers = _align(
        [
            [
                str(row.get("container") or row.get("slot") or name),
                _state_text(row, now),
                str(row.get("cpu") or ""),
                str(row.get("mem") or "").split("/", 1)[0].strip(),
            ]
            for row in rows
        ],
        {2, 3},
    )
    lines += ["", "containers"] + [f"  {line}" for line in containers]

    deploys = [row for row in report.get("deploys") or [] if isinstance(row, dict)]
    lines += ["", "deploys"]
    if deploys:
        shown = _align(
            [
                [
                    str(row.get("id", "")),
                    str(row.get("status") or ""),
                    format_span(str(row.get("started") or ""), str(row.get("ended") or "")),
                    _ago(str(row.get("started") or ""), now),
                    str(row.get("sha") or "")[:7],
                ]
                for row in deploys
            ],
            {0, 2, 3},
        )
        lines += [f"  {line}" for line in shown]
    else:
        lines.append("  no deploys yet")

    log = str(report.get("log") or "").rstrip("\n")
    lines += ["", "log"]
    if log:
        lines += [f"  {line}" for line in log.splitlines()]
    else:
        lines.append("  no log")
    return "\n".join(lines) + "\n"


def _align(rows: list[list[str]], right: set[int]) -> list[str]:
    """Columns padded to the widest cell, like _render but without the header."""
    width = [0] * max((len(row) for row in rows), default=0)
    for row in rows:
        for index, cell in enumerate(row):
            width[index] = max(width[index], len(cell))
    return [
        "  ".join(
            cell.rjust(width[index]) if index in right else cell.ljust(width[index]) for index, cell in enumerate(row)
        ).rstrip()
        for row in rows
    ]


def format_deploys(rows: list, *, color: bool = False) -> str:
    body: list[list[str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        started = str(row.get("started") or "")
        built = str(row.get("built") or "")
        ended = str(row.get("ended") or "")
        body.append(
            [
                str(row.get("id", "")),
                str(row.get("status") or ""),
                str(row.get("sha") or "")[:7],
                format_span(started, built),
                format_span(built, ended),
                format_span(started, ended),
                _when(started),
            ]
        )
    return _render(
        ["id", "status", "rev", "build", "start", "took", "when"],
        body,
        {0, 3, 4, 5, 6},
        color=color,
        paint=partial(_paint, deploy=1),
    )


def deploy_line(row: dict, *, color: bool = False) -> str:
    sha = str(row.get("sha") or "")[:7]
    status = str(row.get("status") or "")
    if color:
        status = ansi(status, _deploy_attention(status))
    started = str(row.get("started") or "")
    built = str(row.get("built") or "")
    ended = str(row.get("ended") or "")
    parts = [f"deploy {row.get('id')}", str(row.get("slot") or ""), sha, status, _when(started)]
    build = format_span(started, built)
    start = format_span(built, ended)
    if build:
        parts.append(f"build {build}")
    if start:
        parts.append(f"start {start}")
    took = format_span(started, ended)
    if took:
        parts.append(took)
    return "  ".join(part for part in parts if part)


def format_repos(rows: list, *, color: bool = False) -> str:
    return _name_when("repo", rows, color=color)


def format_tokens(rows: list, *, color: bool = False) -> str:
    return _name_when("name", rows, color=color)


def _name_when(header: str, rows: list, *, color: bool) -> str:
    body = [[str(row.get("name") or ""), _when(str(row.get("created") or ""))] for row in rows if isinstance(row, dict)]
    return _render([header, "when"], body, {1}, color=color)


def format_inspect(body: str, *, color: bool = False) -> str:
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return body if body.endswith("\n") or not body else body + "\n"
    rows = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
    table: list[list[str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        state = row.get("State")
        state = state if isinstance(state, dict) else {}
        name = str(row.get("Name") or "").removeprefix("/") or str(row.get("Id") or "")[:12]
        status = str(state.get("Status") or "")
        when = _when(str(state.get("StartedAt") or ""))
        word = "up" if status == "running" else status
        table.append([name, f"{word} {when}".rstrip(), str(row.get("ImageName") or "")])
    if not table:
        return body if body.endswith("\n") or not body else body + "\n"
    return _render(["name", "state", "image"], table, color=color, paint=partial(_paint, state=1))


def format_record(body: str) -> str:
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return body if body.endswith("\n") or not body else body + "\n"
    if not isinstance(data, dict):
        return body if body.endswith("\n") else body + "\n"
    if "token" in data:
        name = str(data.get("name") or "")
        secret = str(data.get("token") or "")
        return f"{name}\n{secret}\n" if name else f"{secret}\n"
    if "name" in data and set(data) <= {"name", "created"}:
        return f"{data['name']}\n"
    lines = [f"{key}  {value}" for key, value in data.items() if value not in ("", None, [], {})]
    return ("\n".join(lines) + "\n") if lines else ""


# --- render and time -------------------------------------------------------


def _render(
    headers: list[str],
    rows: list[list[str]],
    right: set[int] | None = None,
    *,
    color: bool = False,
    paint: Callable[[int, str], str] | None = None,
) -> str:
    numeric = right or set()
    width = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            if index < len(width):
                width[index] = max(width[index], len(cell))

    def line(cells: list[str], *, header: bool = False) -> str:
        shown = []
        for index, cell in enumerate(cells):
            pad = width[index] if index < len(width) else 0
            text = cell.rjust(pad) if index in numeric else cell.ljust(pad)
            if color and not header and paint is not None:
                text = paint(index, text)
            shown.append(text)
        text = "  ".join(shown).rstrip()
        if color and header:
            return ansi(text, "2")
        return text

    lines = [line(headers, header=True)]
    if not rows:
        lines.append("(none)")
    lines.extend(line(row) for row in rows)
    return "\n".join(lines) + "\n"


def _paint(index: int, text: str, *, state: int = -1, deploy: int = -1) -> str:
    if index == state:
        return ansi(text, _attention(text.strip()))
    if index == deploy:
        return ansi(text, _deploy_attention(text.strip().split(" ", 1)[0]))
    return text


def _attention(state: str) -> str:
    if "no answer" in state:
        return "31"
    head = state.split(" ", 1)[0]
    if head in {"failed", "down"}:
        return "31"
    if head in {"unhealthy", "starting"}:
        return "33"
    return ""


def _deploy_attention(status: str) -> str:
    if status in {"failed", "interrupted"}:
        return "31"
    if status == "running":
        return "33"
    return ""


def format_prune(report: dict) -> str:
    checkouts = [row for row in report.get("checkouts") or [] if isinstance(row, dict)]
    images = [row for row in report.get("images") or [] if isinstance(row, dict)]
    refused = [row for row in images if row.get("kept")]
    word = "would remove" if report.get("dry") else "removed"
    days = report.get("days") or 7
    lines = [
        f"{word} {len(checkouts)} checkouts and {len(images) - len(refused)} images, "
        f"{format_bytes(int(report.get('freed') or 0))}",
        f"kept: every slot's live image and its revert image, and anything newer than {days} days",
    ]
    for row in refused:
        lines.append(f"kept {row.get('tag')} ({row.get('kept')})")
    return "\n".join(lines) + "\n"


def format_doctor(checks: list[dict[str, Any]], color: bool = False) -> str:
    width = max((len(str(check.get("name") or "")) for check in checks if isinstance(check, dict)), default=0)
    lines = []
    for check in checks:
        if not isinstance(check, dict):
            continue
        word = "ok" if check.get("ok") else "FAIL"
        if not check.get("ok") and color:
            word = ansi(word, "31")
        lines.append(f"{word:<5}{str(check.get('name') or ''):<{width}}  {check.get('detail') or ''}")
    return "\n".join(lines) + ("\n" if lines else "")


def ansi(text: str, code: str) -> str:
    if not text or not code:
        return text
    return f"\033[{code}m{text}\033[0m"


def parse_time(text: str) -> datetime | None:
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def format_span(started: str, ended: str) -> str:
    start = parse_time(started)
    stop = parse_time(ended)
    if start is None or stop is None or stop <= start:
        return ""
    return format_duration(stop - start)


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


def _ago(stamp: str, now: datetime) -> str:
    moment = parse_time(stamp)
    if moment is None or moment.year < 2000:
        return ""
    seconds = int((now - moment).total_seconds())
    if seconds < 5:
        return "now"
    if seconds < 60:
        return f"{seconds}s"
    minutes, _seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, _minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h"
    days, _hours = divmod(hours, 24)
    return f"{days}d"


def _when(stamp: str) -> str:
    return _ago(stamp, datetime.now(UTC))


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
