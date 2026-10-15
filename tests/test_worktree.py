import asyncio
import subprocess
from pathlib import Path

import pytest

from app.config import Config
from app.git import checkout, remove_worktree


def _git(cwd: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)
    return out.stdout.strip()


def _config(root: Path) -> Config:
    return Config(
        root=root,
        podman_sock="",
        podman_bin="podman",
        hook_url="http://127.0.0.1:8000",
        domain="",
        skip_deploy=True,
        boot=False,
        remote=False,
    )


def _bare(tmp_path: Path) -> tuple[Path, str, str]:
    src = tmp_path / "src"
    (src / "projects" / "web").mkdir(parents=True)
    (src / "marker").write_text("one\n")
    (src / "projects" / "web" / "compose.yml").write_text("name: one\n")
    _git(src, "init", "-b", "main")
    _git(src, "config", "user.email", "yard@example.com")
    _git(src, "config", "user.name", "yard")
    _git(src, "add", ".")
    _git(src, "commit", "-m", "one")
    bare = tmp_path / "repos" / "app.git"
    bare.parent.mkdir()
    _git(tmp_path, "clone", "--bare", str(src), str(bare))
    first = _git(bare, "rev-parse", "HEAD")
    (src / "marker").write_text("two\n")
    _git(src, "add", "marker")
    _git(src, "commit", "-m", "two")
    _git(bare, "fetch", str(src), "+refs/heads/main:refs/heads/main")
    second = _git(bare, "rev-parse", "HEAD")
    return bare, first, second


def test_one_worktree_moves_to_the_new_sha(tmp_path: Path):
    bare, first, second = _bare(tmp_path)
    config = _config(tmp_path)

    path = asyncio.run(checkout(config, "heldso", "app", "", first))
    assert path == tmp_path / "projects" / "heldso" / "worktree"
    assert (path / "marker").read_text() == "one\n"

    (path / "marker").write_text("dirty\n")
    (path / "extra").write_text("x\n")
    again = asyncio.run(checkout(config, "heldso", "app", "", second))
    assert again == path
    assert (path / "marker").read_text() == "two\n"
    assert not (path / "extra").exists()

    other = asyncio.run(checkout(config, "heldso-feature", "app", "", first))
    assert other == tmp_path / "projects" / "heldso-feature" / "worktree"
    assert (other / "marker").read_text() == "one\n"
    assert (path / "marker").read_text() == "two\n"

    web = asyncio.run(checkout(config, "web", "app", "projects/web", second))
    assert web == tmp_path / "projects" / "web" / "worktree" / "projects" / "web"
    assert (web / "compose.yml").read_text() == "name: one\n"

    with pytest.raises(RuntimeError, match="not in the repo"):
        asyncio.run(checkout(config, "missing", "app", "no/such", second))

    asyncio.run(remove_worktree(config, "heldso", "app"))
    assert not path.exists()
    listed = {line.split()[0] for line in _git(bare, "worktree", "list").splitlines()}
    assert str(path) not in listed
    assert str(other) in listed
