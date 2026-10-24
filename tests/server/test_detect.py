"""detect.resolve: which file runs the app, and the Containerfile Yard writes."""

from pathlib import Path

import pytest

from app.server import detect
from app.shared import YardError

PYPROJECT = '[project]\nname = "heldso"\ndependencies = ["stario>=5"]\n'


def _write(checkout: Path, name: str, text: str = "x") -> None:
    path = checkout / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_repo_compose_wins(tmp_path: Path):
    _write(tmp_path, "compose.yml")
    _write(tmp_path, "Dockerfile")
    resolved = detect.resolve(tmp_path, "heldso")
    assert (resolved.field, resolved.value, resolved.reason) == ("compose", "compose.yml", "found compose.yml")


def test_repo_containerfile_beats_language(tmp_path: Path):
    _write(tmp_path, "ops/Containerfile")
    _write(tmp_path, "package.json", "{}")
    resolved = detect.resolve(tmp_path, "heldso")
    assert (resolved.field, resolved.value) == ("containerfile", "ops/Containerfile")


def test_stario(tmp_path: Path):
    _write(tmp_path, "pyproject.toml", PYPROJECT)
    _write(tmp_path, "uv.lock", "lock")
    _write(tmp_path, "app/main.py")
    resolved = detect.resolve(tmp_path, "heldso")
    assert (resolved.field, resolved.value, resolved.kind) == ("containerfile", "yard", "stario")
    assert resolved.reason == "pyproject.toml declares stario"
    assert resolved.generated.startswith("# Written by Yard for a stario app (pyproject.toml declares stario).")
    assert "yard app cat containerfile --app heldso" in resolved.generated
    assert "\nFROM python:3.14-slim-trixie AS builder\n" in "\n" + resolved.generated
    assert "HEALTHCHECK" not in resolved.generated


def test_stario_needs_uv_lock(tmp_path: Path):
    _write(tmp_path, "pyproject.toml", PYPROJECT)
    with pytest.raises(YardError, match="uv.lock"):
        detect.resolve(tmp_path, "heldso")


def test_stario_marker(tmp_path: Path):
    _write(
        tmp_path,
        "pyproject.toml",
        '[project]\nrequires-python = ">=3.15"\n\n[tool.stario-create]\n',
    )
    _write(tmp_path, "uv.lock", "lock")
    _write(tmp_path, "app/main.py")
    resolved = detect.resolve(tmp_path, "heldso")
    assert resolved.kind == "stario"
    assert "\nFROM python:3.15-slim-trixie AS builder\n" in "\n" + resolved.generated


def test_python_requirements(tmp_path: Path):
    _write(tmp_path, "requirements.txt", "flask\n")
    _write(tmp_path, "app.py")
    resolved = detect.resolve(tmp_path, "heldso")
    assert resolved.kind == "python"
    assert resolved.reason == "requirements.txt"
    assert "pip install --no-cache-dir -r requirements.txt" in resolved.generated
    assert 'CMD ["python", "app.py"]' in resolved.generated


def test_python_uv_and_entrypoint_order(tmp_path: Path):
    _write(tmp_path, "pyproject.toml", '[project]\nrequires-python = ">=3.13"\n')
    _write(tmp_path, "uv.lock", "lock")
    _write(tmp_path, "app/main.py")
    _write(tmp_path, "main.py")
    resolved = detect.resolve(tmp_path, "heldso")
    assert resolved.kind == "python"
    assert "uv sync --locked --no-dev" in resolved.generated
    assert 'CMD ["python", "-m", "app.main"]' in resolved.generated


def test_python_pyproject_without_lock_or_requirements(tmp_path: Path):
    _write(tmp_path, "pyproject.toml", '[project]\nname="x"\n')
    _write(tmp_path, "main.py")
    with pytest.raises(YardError, match="no uv.lock or requirements.txt"):
        detect.resolve(tmp_path, "heldso")


def test_python_no_entrypoint(tmp_path: Path):
    _write(tmp_path, "requirements.txt", "flask\n")
    with pytest.raises(YardError, match="no app/main.py, main.py, or app.py"):
        detect.resolve(tmp_path, "heldso")


def test_go_root_main(tmp_path: Path):
    _write(tmp_path, "go.mod", "module example.com/heldso\n\ngo 1.24\n")
    _write(tmp_path, "main.go", "package main\n")
    resolved = detect.resolve(tmp_path, "heldso")
    assert resolved.kind == "go"
    assert "\nFROM golang:1.24 AS builder\n" in "\n" + resolved.generated
    assert "go build -trimpath -o /out/app ." in resolved.generated
    assert "gcr.io/distroless/static-debian12" in resolved.generated


def test_go_one_cmd(tmp_path: Path):
    _write(tmp_path, "go.mod", "module example.com/heldso\n")
    _write(tmp_path, "cmd/server/main.go", "package main\n")
    resolved = detect.resolve(tmp_path, "heldso")
    assert "go build -trimpath -o /out/app ./cmd/server" in resolved.generated


def test_go_two_cmds_fails(tmp_path: Path):
    _write(tmp_path, "go.mod", "module example.com/heldso\n")
    _write(tmp_path, "cmd/a/main.go", "package main\n")
    _write(tmp_path, "cmd/b/main.go", "package main\n")
    with pytest.raises(YardError, match="a, b"):
        detect.resolve(tmp_path, "heldso")


def test_node(tmp_path: Path):
    _write(tmp_path, "package.json", '{"scripts": {"start": "node server.js"}, "engines": {"node": ">=20"}}')
    _write(tmp_path, "package-lock.json", "{}")
    resolved = detect.resolve(tmp_path, "heldso")
    assert resolved.kind == "node"
    assert "\nFROM node:20-slim\n" in "\n" + resolved.generated
    assert "RUN npm ci" in resolved.generated
    assert 'CMD ["npm", "start"]' in resolved.generated


def test_node_build_script(tmp_path: Path):
    _write(tmp_path, "package.json", '{"scripts": {"start": "x", "build": "tsc"}}')
    resolved = detect.resolve(tmp_path, "heldso")
    assert "RUN npm install" in resolved.generated
    assert "RUN npm run build" in resolved.generated
    assert "\nFROM node:24-slim\n" in "\n" + resolved.generated


def test_node_no_start(tmp_path: Path):
    _write(tmp_path, "package.json", '{"scripts": {}}')
    with pytest.raises(YardError, match="no start script"):
        detect.resolve(tmp_path, "heldso")


def test_node_pnpm_fails(tmp_path: Path):
    _write(tmp_path, "package.json", '{"scripts": {"start": "x"}}')
    _write(tmp_path, "pnpm-lock.yaml", "lock")
    with pytest.raises(YardError, match="pnpm"):
        detect.resolve(tmp_path, "heldso")


def test_ambiguous_fails(tmp_path: Path):
    _write(tmp_path, "pyproject.toml", '[project]\nname="x"\n')
    _write(tmp_path, "package.json", "{}")
    with pytest.raises(YardError, match="pyproject.toml and package.json"):
        detect.resolve(tmp_path, "heldso")


def test_nothing_fails(tmp_path: Path):
    with pytest.raises(YardError, match="no compose file, Containerfile, or known language"):
        detect.resolve(tmp_path, "heldso")


def test_python_version_sources(tmp_path: Path):
    _write(tmp_path, ".python-version", "3.12\n")
    _write(tmp_path, "requirements.txt", "x\n")
    _write(tmp_path, "main.py")
    assert "python:3.12-slim-trixie" in detect.resolve(tmp_path, "n").generated


def test_python_single_script_entrypoint(tmp_path: Path):
    _write(
        tmp_path,
        "pyproject.toml",
        '[project]\nname="x"\n[project.scripts]\nserve = "x:main"\n\n[build-system]\nrequires=[]\n',
    )
    _write(tmp_path, "uv.lock", "lock")
    assert 'CMD ["serve"]' in detect.resolve(tmp_path, "n").generated


def test_python_script_without_build_system_fails(tmp_path: Path):
    _write(tmp_path, "pyproject.toml", '[project]\nname="x"\n[project.scripts]\nserve = "x:main"\n')
    _write(tmp_path, "uv.lock", "lock")
    with pytest.raises(Exception, match="app/main.py"):
        detect.resolve(tmp_path, "n")


def test_stario_dep_matching(tmp_path: Path):
    from app.server.detect import Repo, stario

    _write(
        tmp_path,
        "pyproject.toml",
        '[project]\nrequires-python=">=3.14"\ndependencies=["stario>=5"]\n',
    )
    _write(tmp_path, "uv.lock", "l")
    _write(tmp_path, "app/main.py")
    plan = stario.detect(Repo(tmp_path), "n")
    assert plan is not None and plan.kind == "stario"
    _write(tmp_path, "pyproject.toml", '[project]\nrequires-python=">=3.14"\ndependencies=["Stario[x]==5"]\n')
    plan = stario.detect(Repo(tmp_path), "n")
    assert plan is not None and plan.kind == "stario"
    _write(tmp_path, "pyproject.toml", '[project]\nrequires-python=">=3.14"\ndependencies=["stariox==1"]\n')
    assert stario.detect(Repo(tmp_path), "n") is None


def test_invalid_pyproject_is_empty(tmp_path: Path):
    _write(tmp_path, "pyproject.toml", "not [valid")
    _write(tmp_path, "uv.lock", "l")
    _write(tmp_path, "main.py")
    resolved = detect.resolve(tmp_path, "n")
    assert resolved.kind == "python" and "python:3.14" in resolved.generated


def _settings(tmp_path: Path, body: str, rel: str = "app/config.py"):
    _write(tmp_path, "pyproject.toml", '[project]\nrequires-python=">=3.14"\n')
    _write(tmp_path, "main.py")
    _write(tmp_path, rel, body)
    return {s.key: (s.required, s.where) for s in detect.settings(tmp_path)}


def test_settings_scan(tmp_path: Path):
    found = _settings(
        tmp_path,
        "import os\n"
        'A = os.environ["GOOGLE_CLIENT_ID"]\n'
        'B = os.environ.get("OPT", "x")\n'
        'C = os.getenv("NO_DEFAULT")\n'
        'D = os.environ["PROJECT_NAME"]\n',
    )
    assert found["GOOGLE_CLIENT_ID"] == (True, "app/config.py:2")
    assert found["OPT"] == (False, "app/config.py:3")
    assert found["NO_DEFAULT"] == (False, "app/config.py:4")
    assert "PROJECT_NAME" not in found


def test_settings_environ_alias_and_required_wins(tmp_path: Path):
    found = _settings(
        tmp_path,
        'from os import environ\nA = environ.get("K")\nB = environ["K"]\n',
    )
    assert found["K"] == (True, "app/config.py:3")


def test_settings_skips_dirs_and_bad_files(tmp_path: Path):
    _write(tmp_path, "pyproject.toml", '[project]\nrequires-python=">=3.14"\n')
    _write(tmp_path, "main.py")
    _write(tmp_path, "tests/t.py", 'import os\nX = os.environ["IN_TESTS"]\n')
    _write(tmp_path, ".hidden/x.py", 'import os\nX = os.environ["IN_HIDDEN"]\n')
    _write(tmp_path, "app/broken.py", "def (:\n")
    found = detect.settings(tmp_path)
    assert [s.key for s in found] == []


def test_settings_empty_for_ambiguous(tmp_path: Path):
    _write(tmp_path, "pyproject.toml", '[project]\nname="x"\n')
    _write(tmp_path, "main.py", 'import os\nA = os.environ["K"]\n')
    _write(tmp_path, "package.json", "{}")
    assert detect.settings(tmp_path) == []
