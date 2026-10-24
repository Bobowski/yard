"""The Node detector: npm install, an optional build, npm start."""

import json
import re

from app.server.detect.base import Plan, Repo, render
from app.shared import YardError

LANGUAGE = "node"
MARKERS = ("package.json",)

_NODE_ENGINE = re.compile(r"\d+")


def detect(repo: Repo, name: str) -> Plan | None:
    try:
        package = json.loads(repo.text("package.json"))
    except ValueError as exc:
        raise YardError(502, "detect", "package.json does not parse") from exc
    scripts = package.get("scripts") if isinstance(package, dict) else None
    scripts = scripts if isinstance(scripts, dict) else {}
    if not scripts.get("start"):
        raise YardError(
            502,
            "detect",
            "package.json has no start script",
            help_text="Add scripts.start, or add a Containerfile.",
        )
    for lockfile, manager in (("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn")):
        if repo.exists(lockfile):
            raise YardError(
                502,
                "detect",
                f"Yard's Node template uses npm; this repo uses {manager} ({lockfile}). Add a Containerfile.",
            )
    reason = "package.json start script"
    engines_raw = package.get("engines")
    engines: dict = engines_raw if isinstance(engines_raw, dict) else {}
    found = _NODE_ENGINE.search(str(engines.get("node") or ""))
    version = found.group(0) if found else "24"
    install = "npm ci" if repo.exists("package-lock.json") else "npm install"
    return Plan(
        "containerfile",
        "yard",
        reason,
        render(
            "node",
            reason,
            name,
            version=version,
            install=install,
            build="RUN npm run build" if scripts.get("build") else "",
            cmd='["npm", "start"]',
        ),
        "node",
    )
