"""The Python detector: uv or pip, one of the known entrypoints, and the settings scan."""

import ast
import os
import re
from pathlib import Path

from app.server.detect import SET_BY_YARD, Plan, Repo, Setting, YardError, _render

LANGUAGE = "python"
MARKERS = ("pyproject.toml", "requirements.txt")

_REQUIRES_PY = re.compile(r">=\s*3\.(\d+)")
_DOT_VERSION = re.compile(r"^(\d+)\.(\d+)")
_SKIP_DIRS = {"tests", "test", "node_modules", "__pycache__", "venv"}


def detect(repo: Repo, name: str) -> Plan | None:
    marker = "pyproject.toml" if repo.exists("pyproject.toml") else "requirements.txt"
    return plan(repo, name, reason=marker)


def plan(repo: Repo, name: str, *, kind: str = "python", reason: str = "", env: dict[str, str] | None = None) -> Plan:
    """Render the one Python Containerfile: uv or pip, the entrypoint, PYTHONUNBUFFERED + PORT + env."""
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
                f"found {reason or 'pyproject.toml'} but no app/main.py, main.py, or app.py",
                help_text="Add one of those files, or add a Containerfile.",
            )
    if repo.exists("uv.lock"):
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
    elif repo.exists("requirements.txt"):
        install = {
            "builder": "RUN python -m venv /workspace/.venv",
            "deps": (
                "COPY requirements.txt ./\n\nRUN /workspace/.venv/bin/pip install --no-cache-dir -r requirements.txt"
            ),
            "project": "",
            "env": 'ENV PATH="/workspace/.venv/bin:$PATH"',
        }
    elif repo.exists("pyproject.toml"):
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
            f"found {reason or 'requirements.txt'} but no uv.lock or requirements.txt",
            help_text="Add requirements.txt, or add a Containerfile.",
        )
    extra = " ".join(f"{k}={v}" for k, v in (env or {}).items())
    if extra:
        install["env"] += f"\nENV {extra}"
    return Plan(
        "containerfile",
        "yard",
        reason,
        _render(kind, reason, name, template="python", python=_version(repo), cmd=cmd, **install),
        kind,
    )


def _version(repo: Repo) -> str:
    """The Python version: .python-version, then requires-python, else 3.14."""
    found = _DOT_VERSION.match(repo.text(".python-version").strip())
    if found:
        return f"{found.group(1)}.{found.group(2)}"
    requires = str(repo.pyproject.get("project", {}).get("requires-python") or "")
    found = _REQUIRES_PY.search(requires)
    return f"3.{found.group(1)}" if found else "3.14"


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
