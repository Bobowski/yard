"""The http-nu detector: a cross.stream (xs) app is one serve.nu, run by one static binary."""

import re

from app.server.detect.base import DOWNLOADS, SET_BY_YARD, Plan, Repo, Setting, pin, plan_template
from app.shared import YardError

LANGUAGE = "nushell"
MARKERS = ("serve.nu",)

_BUILTINS = {
    "PATH",
    "Path",
    "HOME",
    "PWD",
    "USER",
    "FILE_PWD",
    "CURRENT_FILE",
    "LAST_EXIT_CODE",
    "CMD_DURATION_MS",
    "PROCESS_PATH",
    "NU_LIB_DIRS",
    "NU_PLUGIN_DIRS",
    "config",
    "TERM",
    "LANG",
    "TZ",
    "DATASTAR_JS_PATH",
}
_SKIP_DIRS = {"tests", "test", "node_modules", "__pycache__", "venv", "store"}
_ENV = re.compile(r"\$env\.([A-Za-z_][A-Za-z0-9_]*)(\?)?")


def detect(repo: Repo, name: str) -> Plan | None:
    if "{|" not in repo.text("serve.nu"):
        raise YardError(
            502,
            "detect",
            "found serve.nu but it has no handler closure",
            help_text="http-nu runs serve.nu; its last expression is {|req| ...}. Or add a Containerfile.",
        )
    download = DOWNLOADS["http-nu"]
    return plan_template(
        "yard:http-nu",
        "http-nu: serve.nu",
        name,
        template="http-nu",
        fetch=pin("alpine:3.22"),
        static=pin("gcr.io/distroless/static-debian12"),
        version=download["version"],
        sha_amd64=download["sha256"]["amd64"],
        sha_arm64=download["sha256"]["arm64"],
    )


def _files(repo: Repo):
    count = 0
    for path in repo.root.rglob("*.nu"):
        rel = path.relative_to(repo.root)
        if any(part.startswith(".") or part in _SKIP_DIRS for part in rel.parts[:-1]):
            continue
        count += 1
        if count > 2000 or path.stat().st_size > 1024 * 1024:
            continue
        yield path


def settings(repo: Repo) -> list[Setting]:
    """Env keys the Nushell code reads: $env.KEY is required, $env.KEY? is optional."""
    found: dict[str, Setting] = {}
    for path in _files(repo):
        try:
            lines = path.read_text().splitlines()
        except ValueError:
            continue
        rel = path.relative_to(repo.root)
        for number, line in enumerate(lines, 1):
            if line.strip().startswith("#"):
                continue
            for match in _ENV.finditer(line):
                key = match.group(1)
                if key in SET_BY_YARD or key in _BUILTINS:
                    continue
                rest = line[match.end() :].lstrip()
                if rest.startswith("=") and not rest.startswith("=="):
                    continue  # an assignment, not a read
                required = match.group(2) != "?"
                where = f"{rel}:{number}"
                current = found.get(key)
                if current is None or (required and not current.required):
                    found[key] = Setting(key, required, where)
    return sorted(found.values(), key=lambda s: s.key)
