"""What an app runs when its record does not say: a repo compose, a repo Containerfile, or a written one.

A detector is one module in this package for one language or framework. It exposes:

- LANGUAGE: the language it is an opinionated kind of (several detectors may share one).
- MARKERS: repo files that prove the language is present (first match names the language).
- detect(repo, name) -> Plan | None: None means "not mine"; YardError means "mine but broken".
- settings(repo) -> list[Setting], optional: the env keys the code reads.

A generated Containerfile lives in templates/ next to this file and renders through _render.
A detector must refuse (YardError or None) rather than guess an install or entrypoint.

A PR adding a detector adds the module, its template, an entry in DETECTORS (specific
before generic), and tests under tests/server/.
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path

from app.server import git
from app.server.compose import COMPOSE_NAMES, DOCKERFILES
from app.shared import YardError

_TEMPLATES = Path(__file__).resolve().parent / "templates"

SET_BY_YARD = {
    "PROJECT_NAME",
    "PROJECT_DATA_DIR",
    "SHA",
    "PORT",
    "STARIO_HOST",
    "STARIO_PORT",
    "STARIO_TRACER",
}


class Repo:
    """A checkout path with small cached helpers."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._pyproject: dict | None = None

    def exists(self, rel: str) -> bool:
        return (self.root / rel).is_file()

    def text(self, rel: str) -> str:
        path = self.root / rel
        return path.read_text() if path.is_file() else ""

    @property
    def pyproject(self) -> dict:
        if self._pyproject is None:
            try:
                self._pyproject = tomllib.loads(self.text("pyproject.toml"))
            except ValueError:
                self._pyproject = {}
        return self._pyproject


@dataclass(slots=True)
class Plan:
    """One answer to "what does the app run"."""

    field: str  # "compose" or "containerfile"
    value: str  # a repo-relative path, or "yard" for the written Containerfile
    reason: str  # human text: "found compose.yml", "pyproject.toml declares stario"
    generated: str = ""  # the rendered Containerfile when value is "yard"
    kind: str = ""  # stario, python, go, or node


@dataclass(frozen=True, slots=True)
class Setting:
    """One env key the app's code reads."""

    key: str
    required: bool
    where: str  # "app/config.py:37"


def _header(kind: str, reason: str, name: str) -> str:
    return (
        f"# Written by Yard for a {kind} app ({reason})."
        f" To change it, copy it into your repo: yard app cat containerfile --app {name or '<name>'} > Containerfile\n"
    )


def _render(kind: str, reason: str, name: str, template: str = "", **values: str) -> str:
    text = (_TEMPLATES / f"{template or kind}.Containerfile").read_text()
    values["name"] = name or "<name>"
    for key, value in values.items():
        text = text.replace(f"{{{{{key}}}}}", value)
    return _header(kind, reason, name) + text


def _join(items: list[str]) -> str:
    if len(items) <= 2:
        return " and ".join(items)
    return ", ".join(items[:-1]) + f", and {items[-1]}"


from app.server.detect import go, node, python, stario  # noqa: E402

DETECTORS = (stario, python, go, node)


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
    except Exception:
        pass
    return []
