"""What an app runs when its record does not say: a repo compose, a repo Containerfile, or a written one."""

import json
import re
from dataclasses import dataclass
from pathlib import Path

from app.server import git
from app.server.compose import COMPOSE_NAMES, DOCKERFILES
from app.shared import YardError

_TEMPLATES = Path(__file__).resolve().parent / "templates"

_STARIO_DEP = re.compile(r"[\"']stario\s*(?:[<>=!~\[;@ ]|[\"'])")
_REQUIRES_PY = re.compile(r"requires-python\s*=\s*[\"'][^\"']*?>=\s*3\.(\d+)")
_GO_DIRECTIVE = re.compile(r"^go\s+(\d+)(?:\.(\d+))?", re.M)
_NODE_ENGINE = re.compile(r"\d+")


@dataclass(slots=True)
class Resolved:
    """One answer to "what does the app run"."""

    field: str  # "compose" or "containerfile"
    value: str  # a repo-relative path, or "yard" for the written Containerfile
    reason: str  # human text: "found compose.yml", "pyproject.toml declares stario"
    generated: str = ""  # the rendered Containerfile when value is "yard"
    kind: str = ""  # stario, python, go, or node


def resolve(checkout: Path, name: str = "") -> Resolved:
    """What the checked-out repo runs. A written Containerfile comes back in generated."""
    for candidate in COMPOSE_NAMES:
        if (checkout / candidate).is_file():
            return Resolved("compose", candidate, f"found {candidate}")
    for candidate in DOCKERFILES:
        if (checkout / candidate).is_file():
            return Resolved("containerfile", candidate, f"found {candidate}")
    return language(checkout, name)


def language(checkout: Path, name: str = "") -> Resolved:
    """Which bundled Containerfile the repo's language gets. compose/Containerfile files are not read."""
    """Language detection, when the repo ships no compose file and no Containerfile."""
    pyproject = (checkout / "pyproject.toml").read_text() if (checkout / "pyproject.toml").is_file() else ""
    if pyproject and ("[tool.stario-create]" in pyproject or _STARIO_DEP.search(pyproject)):
        return _stario(checkout, name, pyproject)
    found: list[tuple[str, str]] = []
    if pyproject or (checkout / "requirements.txt").is_file():
        found.append(("python", "pyproject.toml" if pyproject else "requirements.txt"))
    if (checkout / "go.mod").is_file():
        found.append(("go", "go.mod"))
    if (checkout / "package.json").is_file():
        found.append(("node", "package.json"))
    if len(found) > 1:
        markers = [marker for _kind, marker in found]
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
    kind = found[0][0]
    if kind == "python":
        return _python(checkout, name, found[0][1], pyproject)
    if kind == "go":
        return _go(checkout, name)
    return _node(checkout, name)


def _join(items: list[str]) -> str:
    if len(items) <= 2:
        return " and ".join(items)
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def _python_version(pyproject: str) -> str:
    found = _REQUIRES_PY.search(pyproject)
    return f"3.{found.group(1)}" if found else "3.14"


def _header(kind: str, reason: str, name: str) -> str:
    return (
        f"# Written by Yard for a {kind} app ({reason})."
        f" To change it, copy it into your repo: yard app cat containerfile --app {name or '<name>'} > Containerfile\n"
    )


def _render(kind: str, reason: str, name: str, **values: str) -> str:
    text = (_TEMPLATES / f"{kind}.Containerfile").read_text()
    values["name"] = name or "<name>"
    for key, value in values.items():
        text = text.replace(f"{{{{{key}}}}}", value)
    return _header(kind, reason, name) + text


def _stario(checkout: Path, name: str, pyproject: str) -> Resolved:
    """A Stario app: uv sync in two stages, the source image from stario-create."""
    reason = "pyproject.toml declares stario"
    if not (checkout / "uv.lock").is_file():
        raise YardError(
            502,
            "detect",
            "a Stario app needs uv.lock; run uv lock and commit it",
            help_text="uv lock && git add uv.lock && git commit && git push",
        )
    return Resolved(
        "containerfile",
        "yard",
        reason,
        _render("stario", reason, name, python=_python_version(pyproject)),
        "stario",
    )


def _python(checkout: Path, name: str, marker: str, pyproject: str) -> Resolved:
    """A Python app: an entrypoint, and uv or pip for the dependencies."""
    if (checkout / "app" / "main.py").is_file():
        cmd = '["python", "-m", "app.main"]'
    elif (checkout / "main.py").is_file():
        cmd = '["python", "main.py"]'
    elif (checkout / "app.py").is_file():
        cmd = '["python", "app.py"]'
    else:
        raise YardError(
            502,
            "detect",
            f"found {marker} but no app/main.py, main.py, or app.py",
            help_text="Add one of those files, or add a Containerfile.",
        )
    if pyproject and (checkout / "uv.lock").is_file():
        install = {
            "builder": (
                "COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /uvx /bin/\n\n"
                "ENV UV_COMPILE_BYTECODE=1 \\\n"
                "    UV_LINK_MODE=copy \\\n"
                "    UV_PYTHON_DOWNLOADS=0"
            ),
            "deps": (
                "RUN --mount=type=cache,target=/root/.cache/uv \\\n"
                "    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \\\n"
                "    --mount=type=bind,source=uv.lock,target=uv.lock \\\n"
                "    uv sync --locked --no-install-project --no-dev"
            ),
            "project": "RUN --mount=type=cache,target=/root/.cache/uv \\\n    uv sync --locked --no-dev",
            "env": 'ENV PATH="/workspace/.venv/bin:$PATH"',
        }
    elif (checkout / "requirements.txt").is_file():
        install = {
            "builder": "RUN python -m venv /workspace/.venv",
            "deps": (
                "COPY requirements.txt ./\n\nRUN /workspace/.venv/bin/pip install --no-cache-dir -r requirements.txt"
            ),
            "project": "",
            "env": 'ENV PATH="/workspace/.venv/bin:$PATH"',
        }
    elif pyproject:
        raise YardError(
            502,
            "detect",
            "found pyproject.toml but no uv.lock or requirements.txt",
            help_text="Run uv lock and commit uv.lock, add requirements.txt, or add a Containerfile.",
        )
    else:
        raise YardError(
            502,
            "detect",
            f"found {marker} but no uv.lock or requirements.txt",
            help_text="Add requirements.txt, or add a Containerfile.",
        )
    return Resolved(
        "containerfile",
        "yard",
        marker,
        _render("python", marker, name, python=_python_version(pyproject), cmd=cmd, **install),
        "python",
    )


def _go(checkout: Path, name: str) -> Resolved:
    """A Go app: one static binary on distroless."""
    reason = "go.mod"
    text = (checkout / "go.mod").read_text()
    found = _GO_DIRECTIVE.search(text)
    version = f"{found.group(1)}.{found.group(2)}" if found and found.group(2) else "1"
    mains = [path for path in checkout.glob("*.go") if "package main" in path.read_text()]
    cmds = [d for d in sorted((checkout / "cmd").iterdir()) if d.is_dir()] if (checkout / "cmd").is_dir() else []
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
    return Resolved(
        "containerfile",
        "yard",
        reason,
        _render("go", reason, name, version=version, pkg=pkg),
        "go",
    )


def _node(checkout: Path, name: str) -> Resolved:
    """A Node app: npm install, an optional build, npm start."""
    text = (checkout / "package.json").read_text()
    try:
        package = json.loads(text)
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
        if (checkout / lockfile).is_file():
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
    install = "npm ci" if (checkout / "package-lock.json").is_file() else "npm install"
    return Resolved(
        "containerfile",
        "yard",
        reason,
        _render(
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
