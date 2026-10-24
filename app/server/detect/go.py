"""The Go detector: one static binary on distroless."""

import re

from app.server.detect.base import Plan, Repo, render
from app.shared import YardError

LANGUAGE = "go"
MARKERS = ("go.mod",)

_GO_DIRECTIVE = re.compile(r"^go\s+(\d+)(?:\.(\d+))?", re.M)


def detect(repo: Repo, name: str) -> Plan | None:
    reason = "go.mod"
    found = _GO_DIRECTIVE.search(repo.text("go.mod"))
    version = f"{found.group(1)}.{found.group(2)}" if found and found.group(2) else "1"
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
    return Plan("containerfile", "yard", reason, render("go", reason, name, version=version, pkg=pkg), "go")
