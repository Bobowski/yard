"""The shared spine of the detectors: the checkout handle, the plan, the settings row."""

import tomllib
from dataclasses import dataclass
from pathlib import Path

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


def render(kind: str, reason: str, name: str, template: str = "", **values: str) -> str:
    text = (_TEMPLATES / f"{template or kind}.Containerfile").read_text()
    values["name"] = name or "<name>"
    for key, value in values.items():
        text = text.replace(f"{{{{{key}}}}}", value)
    return _header(kind, reason, name) + text


def _join(items: list[str]) -> str:
    if len(items) <= 2:
        return " and ".join(items)
    return ", ".join(items[:-1]) + f", and {items[-1]}"
