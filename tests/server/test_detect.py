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
    assert resolved.generated.startswith("# Written by Yard")
    assert "yard app cat containerfile --app heldso" in resolved.generated
    assert "FROM python:3.14-slim-trixie AS builder" in resolved.generated
    assert "HEALTHCHECK" not in resolved.generated


def test_stario_needs_uv_lock(tmp_path: Path):
    _write(tmp_path, "pyproject.toml", PYPROJECT)
    with pytest.raises(YardError, match="uv.lock"):
        detect.resolve(tmp_path, "heldso")


def test_stario_marker(tmp_path: Path):
    _write(tmp_path, "pyproject.toml", '[tool.stario-create]\nrequires-python = ">=3.15"\n')
    _write(tmp_path, "uv.lock", "lock")
    resolved = detect.resolve(tmp_path, "heldso")
    assert resolved.kind == "stario"
    assert "python:3.15-slim-trixie" in resolved.generated


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
    assert "FROM golang:1.24 AS builder" in resolved.generated
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
    assert "FROM node:20-slim" in resolved.generated
    assert "RUN npm ci" in resolved.generated
    assert 'CMD ["npm", "start"]' in resolved.generated


def test_node_build_script(tmp_path: Path):
    _write(tmp_path, "package.json", '{"scripts": {"start": "x", "build": "tsc"}}')
    resolved = detect.resolve(tmp_path, "heldso")
    assert "RUN npm install" in resolved.generated
    assert "RUN npm run build" in resolved.generated
    assert "node:24-slim" in resolved.generated


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
