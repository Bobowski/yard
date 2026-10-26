"""The Python detectors: uv or pip, one of the known entrypoints, and the settings scan."""

import ast
import os
import re
from pathlib import Path

from app.server.detect.base import SET_BY_YARD, Plan, Repo, Setting, pin, plan_template
from app.shared import YardError

LANGUAGE = "python"
MARKERS = ("pyproject.toml", "requirements.txt")

VERSIONS = ("3.12", "3.13", "3.14")
_DEFAULT = "3.14"

_REQUIRES_PY = re.compile(r">=\s*3\.(\d+)")
_DOT_VERSION = re.compile(r"^3\.(\d+)")
_SKIP_DIRS = {"tests", "test", "node_modules", "__pycache__", "venv"}


def detect(repo: Repo, name: str) -> Plan | None:
    if repo.exists("uv.lock"):
        return plan(repo, name, reason="python: uv.lock")
    if repo.exists("requirements.txt"):
        return plan(repo, name, reason="python: requirements.txt")
    if repo.exists("pyproject.toml"):
        raise YardError(
            502,
            "detect",
            "found pyproject.toml but no uv.lock or requirements.txt",
            help_text="Run uv lock and commit uv.lock, add requirements.txt, or add a Containerfile.",
        )
    return None


def plan(repo: Repo, name: str, *, reason: str) -> Plan:
    """Render the one Python Containerfile: uv or pip, the entrypoint, PYTHONUNBUFFERED + PORT."""
    if repo.exists("app/main.py"):
        cmd = '["python", "-m", "app.main"]'
    elif repo.exists("main.py"):
        cmd = '["python", "main.py"]'
    elif repo.exists("app.py"):
        cmd = '["python", "app.py"]'
    else:
        scripts = repo.pyproject.get("project", {}).get("scripts", {})
        if repo.exists("uv.lock") and repo.pyproject.get("build-system") and len(scripts) == 1:
            cmd = f'["{next(iter(scripts))}"]'
        else:
            raise YardError(
                502,
                "detect",
                f"found {reason.split(':', 1)[-1].strip()} but no app/main.py, main.py, or app.py",
                help_text="Add one of those files, or add a Containerfile.",
            )
    if repo.exists("uv.lock"):
        answer = "yard:python-uv"
        install = {
            "builder": f"COPY --from={pin('ghcr.io/astral-sh/uv:0.13.5')} /uv /uvx /bin/\n\n"
            "ENV UV_COMPILE_BYTECODE=1 \\\n"
            "    UV_LINK_MODE=copy \\\n"
            "    UV_PYTHON_DOWNLOADS=0",
            "deps": (
                "RUN --mount=type=cache,target=/root/.cache/uv \\\n"
                "    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \\\n"
                "    --mount=type=bind,source=uv.lock,target=uv.lock \\\n"
                "    uv sync --locked --no-install-project --no-dev"
            ),
            "project": "RUN --mount=type=cache,target=/root/.cache/uv \\\n    uv sync --locked --no-dev",
            "env": 'ENV PATH="/workspace/.venv/bin:$PATH"',
        }
    else:
        answer = "yard:python-pip"
        install = {
            "builder": "RUN python -m venv /workspace/.venv",
            "deps": (
                "COPY requirements.txt ./\n\nRUN /workspace/.venv/bin/pip install --no-cache-dir -r requirements.txt"
            ),
            "project": "",
            "env": 'ENV PATH="/workspace/.venv/bin:$PATH"',
        }
    return plan_template(
        answer, reason, name, template="python", python=pin(f"python:{_version(repo)}-slim-trixie"), cmd=cmd, **install
    )


def _version(repo: Repo) -> str:
    """An exact .python-version must be in the set; >=3.N takes the smallest cover; default 3.14."""
    pinned = _DOT_VERSION.match(repo.text(".python-version").strip())
    if pinned:
        minor = f"3.{pinned.group(1)}"
        if minor not in VERSIONS:
            raise _version_error(minor)
        return minor
    requires = str(repo.pyproject.get("project", {}).get("requires-python") or "")
    found = _REQUIRES_PY.search(requires)
    if found:
        ask = int(found.group(1))
        fit = next((v for v in VERSIONS if int(v.split(".")[1]) >= ask), "")
        if not fit:
            raise _version_error(f"3.{ask}")
        return fit
    return _DEFAULT


def _version_error(ask: str) -> YardError:
    return YardError(
        502,
        "detect",
        f"Yard's python templates cover {', '.join(VERSIONS)}; this repo asks {ask}",
        help_text="Add a Containerfile.",
    )


def _files(repo: Repo):
    count = 0
    for root, dirs, names in os.walk(repo.root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for filename in names:
            if filename.endswith(".py"):
                path = Path(root) / filename
                count += 1
                if count > 2000:
                    return
                if path.stat().st_size <= 1024 * 1024:
                    yield path


def _call_name(node) -> str:
    """The dotted path of a call or attribute target: os.environ.get -> 'os.environ.get'."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def settings(repo: Repo) -> list[Setting]:
    """Env keys the code reads: os.environ[...] is required, .get/.getenv optional."""
    found: dict[str, Setting] = {}
    for path in _files(repo):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError, ValueError:
            continue
        rel = path.relative_to(repo.root)
        for node in ast.walk(tree):
            key, required = "", False
            if isinstance(node, ast.Subscript):
                target = _call_name(node.value)
                if (
                    target in ("os.environ", "environ")
                    and isinstance(node.slice, ast.Constant)
                    and isinstance(node.slice.value, str)
                ):
                    key, required = node.slice.value, True
            elif (
                isinstance(node, ast.Call)
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                if _call_name(node.func) in ("os.environ.get", "environ.get", "os.getenv", "getenv"):
                    key = node.args[0].value
            if not key or key in SET_BY_YARD:
                continue
            where = f"{rel}:{getattr(node, 'lineno', 0)}"
            current = found.get(key)
            if current is None or (required and not current.required):
                found[key] = Setting(key, required, where)
    return sorted(found.values(), key=lambda s: s.key)
