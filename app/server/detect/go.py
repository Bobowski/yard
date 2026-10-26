"""The Go detector: one static binary on distroless."""

import re

from app.server.detect.base import Plan, Repo, pin, plan_template
from app.shared import YardError

LANGUAGE = "go"
MARKERS = ("go.mod",)

VERSIONS = ("1.26", "1.27")

_GO_DIRECTIVE = re.compile(r"^go\s+(\d+)(?:\.(\d+))?", re.M)


def detect(repo: Repo, name: str) -> Plan | None:
    found = _GO_DIRECTIVE.search(repo.text("go.mod"))
    version = _version(found.group(1), found.group(2) if found and found.group(2) else "0") if found else "1.26"
    mains = [p for p in repo.root.glob("*.go") if "package main" in p.read_text()]
    cmds = [d for d in sorted((repo.root / "cmd").iterdir()) if d.is_dir()] if (repo.root / "cmd").is_dir() else []
    cmds = [d for d in cmds if any("package main" in p.read_text() for p in d.glob("*.go"))]
    if mains:
        pkg = "."
    elif len(cmds) == 1:
        pkg = f"./cmd/{cmds[0].name}"
    else:
        listed = ", ".join(d.name for d in cmds) or "none found"
        raise YardError(
            502,
            "detect",
            f"found go.mod but no single main package at the root or in cmd/ ({listed})",
            help_text="Add a Containerfile, or keep one main package.",
        )
    return plan_template(
        "yard:go",
        "go: go.mod",
        name,
        template="go",
        golang=pin(f"golang:{version}"),
        static=pin("gcr.io/distroless/static-debian12"),
        pkg=pkg,
    )


def _version(major: str, minor: str) -> str:
    """The go directive is a minimum: the smallest supported version >= it."""
    ask = int(major) * 100 + int(minor)
    for version in VERSIONS:
        maj, min_ = version.split(".")
        if int(maj) * 100 + int(min_) >= ask:
            return version
    raise YardError(
        502,
        "detect",
        f"Yard's go templates cover {', '.join(VERSIONS)}; this repo asks {major}.{minor}",
        help_text="Add a Containerfile.",
    )
