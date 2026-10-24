"""What an app runs when its record does not say: a repo compose, a repo Containerfile, or a written one.

A detector is one module in this package for one language or framework. It exposes:

- LANGUAGE: the language it is an opinionated kind of (several detectors may share one).
- MARKERS: repo files that prove the language is present (first match names the language).
- detect(repo, name) -> Plan | None: None means "not mine"; YardError means "mine but broken".
- settings(repo) -> list[Setting], optional: the env keys the code reads.

A detector imports Repo, Plan, Setting, SET_BY_YARD, and render from detect.base.
A generated Containerfile lives in templates/ next to this file and renders through render.
A detector must refuse (YardError or None) rather than guess an install or entrypoint.

A PR adding a detector adds the module, its template, an entry in DETECTORS (specific
before generic), and tests under tests/server/.
"""

import logging
from pathlib import Path

from app.server import git
from app.server.compose import COMPOSE_NAMES, DOCKERFILES
from app.server.detect import go, node, python, stario
from app.server.detect.base import Plan, Repo, Setting, _join
from app.shared import YardError

log = logging.getLogger("yard")

DETECTORS = (stario, python, go, node)

__all__ = ["DETECTORS", "Plan", "Repo", "Setting", "language", "resolve", "settings"]


def _languages(repo: Repo) -> dict[str, list[str]]:
    """The languages present in the checkout, language name -> marker files."""
    found: dict[str, list[str]] = {}
    for mod in DETECTORS:
        marker = next((m for m in mod.MARKERS if repo.exists(m)), "")
        if marker:
            found.setdefault(mod.LANGUAGE, []).append(marker)
    return found


def resolve(checkout: Path, name: str = "") -> Plan:
    """What the checked-out repo runs. A written Containerfile comes back in generated."""
    for candidate in COMPOSE_NAMES:
        if (checkout / candidate).is_file():
            return Plan("compose", candidate, f"found {candidate}")
    for candidate in DOCKERFILES:
        if (checkout / candidate).is_file():
            return Plan("containerfile", candidate, f"found {candidate}")
    return language(checkout, name)


def language(checkout: Path, name: str = "") -> Plan:
    """Which bundled Containerfile the repo's language gets. Repo compose and Containerfile files are not read."""
    repo = Repo(checkout)
    found = _languages(repo)
    if len(found) > 1:
        markers = [markers[0] for _lang, markers in found.items()]
        raise YardError(
            502,
            "detect",
            f"found {_join(markers)}; Yard will not guess",
            help_text="Add a Containerfile, or run yard app set --containerfile PATH.",
        )
    if not found:
        sha = git.head(checkout)[:12] or "the checkout"
        raise YardError(
            502,
            "detect",
            f"no compose file, Containerfile, or known language (Python, Go, Node) in {sha}",
            help_text="Add a Containerfile: stario.dev/yard/first-app",
        )
    lang = next(iter(found))
    for mod in DETECTORS:
        if mod.LANGUAGE != lang:
            continue
        plan = mod.detect(repo, name)
        if plan is not None:
            return plan
    raise YardError(502, "detect", f"found {found[lang][0]} but no detector claims it")


def settings(checkout: Path) -> list[Setting]:
    """The env keys the checkout's code reads, when exactly one language owns it. Never raises."""
    try:
        repo = Repo(checkout)
        found = _languages(repo)
        if len(found) != 1:
            return []
        lang = next(iter(found))
        for mod in DETECTORS:
            if mod.LANGUAGE == lang and hasattr(mod, "settings"):
                return mod.settings(repo)
    except Exception as exc:
        log.warning("detect.settings %s: %s", checkout, exc)
    return []
