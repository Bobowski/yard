"""Yard's value formats: names, refs, domains, durations, and env files. Pure."""

import ast
import json
import posixpath
import re
from typing import NoReturn
from urllib.parse import urlsplit, urlunsplit

from app.errors import YardError

_NAME = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_DIR = re.compile(r"^[A-Za-z0-9._/-]+$")
_DOMAIN = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")
_PLAIN_VALUE = re.compile(r"[A-Za-z0-9@%_+=:,./-]+")
_DURATION = re.compile(r"(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?")


def check_name(name: str) -> None:
    if name.startswith("yard-"):
        raise YardError(
            400,
            "bad_name",
            "name must be a short lowercase word",
            context={"name": name},
            help_text="yard-* is reserved for this host.",
            example="heldso",
        )
    if not _NAME.fullmatch(name):
        raise YardError(
            400,
            "bad_name",
            "name must be a short lowercase word",
            context={"name": name},
            help_text="Start with a letter. Then use letters, digits, and hyphens, up to 63 characters.",
            example="heldso",
        )


def check_slot_name(name: str) -> None:
    check_name(name)
    if name == "caddy":
        raise YardError(
            400,
            "bad_name",
            "name must be a short lowercase word",
            context={"name": name},
            help_text="caddy is reserved for the proxy.",
            example="heldso",
        )


def check_ref(ref: str) -> None:
    if not ref or ".." in ref or ref.startswith("/") or any(ch in ref for ch in " \t\\"):
        raise YardError(
            400,
            "bad_ref",
            "ref must be a branch name",
            context={"ref": ref},
            help_text="Use a branch name. Do not start with a slash, and do not use spaces or ..",
            example="main",
        )


def check_origin(url: str) -> str:
    url = url.strip()
    if any(ch in url for ch in " \t\r\n"):
        _bad_origin(url)
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or not parts.path.strip("/"):
        _bad_origin(url)
    return url


def origin_name(url: str) -> str:
    name = urlsplit(check_origin(url)).path.rstrip("/").rsplit("/", 1)[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return name.lower()


def public_origin(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def _bad_origin(url: str) -> NoReturn:
    raise YardError(
        400,
        "bad_origin",
        "remote must be an http or https git address",
        context={"remote": url.split("@")[-1]},
        help_text="Pass the GitHub address. Yard clones it on the host.",
        example="https://github.com/org/heldso",
    )


def check_domain(domain: str) -> None:
    if not domain:
        return
    if ".." in domain or not _DOMAIN.fullmatch(domain):
        raise YardError(
            400,
            "bad_domain",
            "domain must be a hostname",
            context={"domain": domain},
            help_text="Use a lowercase hostname. One domain belongs to one slot.",
            example="heldso.example.com",
        )


def clean_dir(directory: str) -> str:
    directory = directory.strip()
    if directory in {"", ".", "./"}:
        return ""
    if directory.startswith("/") or not _DIR.fullmatch(directory):
        _bad_dir(directory)
    clean = posixpath.normpath(directory)
    if clean in {".", ".."} or clean.startswith("../") or "/../" in f"/{clean}/":
        _bad_dir(directory)
    return clean


def _bad_dir(directory: str) -> NoReturn:
    raise YardError(
        400,
        "bad_dir",
        "directory must be a relative path inside the repo",
        context={"directory": directory},
        help_text="The path stays inside the repo. Do not start with / and do not use ..",
        example="projects/mono",
    )


def parse_duration(text: str) -> float:
    """Whole seconds for 1d, 1h, 1m, 1s, in that order. At least one group."""
    match = _DURATION.fullmatch(text)
    if match is None or not any(match.groups()):
        raise ValueError(text or "empty")
    days, hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return float(days * 86400 + hours * 3600 + minutes * 60 + seconds)


def parse_trigger(text: str) -> str:
    """The slot trigger: empty for a service, or push, manual, every <duration>."""
    text = " ".join(text.strip().lower().split())
    if text in {"", "off", "-", "service"}:
        return ""
    if text in {"push", "on push", "on_push"}:
        return "push"
    if text == "manual":
        return "manual"
    rest = text.removeprefix("every").lstrip(" :")
    if not text.startswith("every") or not rest:
        raise _bad_trigger(text)
    try:
        seconds = parse_duration(rest)
    except ValueError as exc:
        raise _bad_trigger(text) from exc
    if seconds < 1:
        raise _bad_trigger(text)
    # The largest exact unit: 2d, then 1h, 5m, 90s.
    for size, suffix in ((86400, "d"), (3600, "h"), (60, "m"), (1, "s")):
        if seconds % size == 0:
            return f"every {int(seconds // size)}{suffix}"
    return f"every {rest}"


def _bad_trigger(text: str) -> NoReturn:
    raise YardError(
        400,
        "bad_trigger",
        "trigger must be push, manual, every <duration>, or empty",
        context={"trigger": text},
        help_text="Empty means a service that stays up.",
        example="TRIGGER=every 1h",
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


def short_sha(sha: str) -> str:
    """The image tag and checkout name. Twelve chars, the docker convention."""
    return sha[:12]


def slug(directory: str) -> str:
    return directory.lower().replace("/", "-").replace("_", "-").replace(".", "-")


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


def format_env(env: dict[str, str]) -> str:
    """KEY=value lines that parse_env reads back. A value outside the plain set is a JSON string."""
    lines: list[str] = []
    for key in sorted(env):
        value = str(env[key])
        # Compose and parse_env both read a double-quoted value with backslash escapes.
        shown = value if value and _PLAIN_VALUE.fullmatch(value) else json.dumps(value, ensure_ascii=False)
        lines.append(f"{key}={shown}\n")
    return "".join(lines)


def _env_error(message: str) -> YardError:
    return YardError(
        400,
        "bad_key",
        message,
        help_text="A line is NAME=value. A # line is a comment.",
        example="DOMAIN=heldso.example.com",
    )
