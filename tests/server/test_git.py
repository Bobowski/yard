"""The app clone: sync() resets it to the remote tip; remote_tip() reads ahead."""

import os
import subprocess
from pathlib import Path

import pytest

from app.server import git
from app.shared import YardError


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("GIT_AUTHOR_NAME", "Yard")
    env.setdefault("GIT_AUTHOR_EMAIL", "yard@example.com")
    env.setdefault("GIT_COMMITTER_NAME", "Yard")
    env.setdefault("GIT_COMMITTER_EMAIL", "yard@example.com")
    return env


def _git(directory: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(directory), *args], check=True, capture_output=True, env=_env())


def _out(directory: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(directory), *args], text=True, env=_env()).strip()


def _commit(directory: Path, name: str, text: str) -> None:
    (directory / name).write_text(text)
    _git(directory, "add", ".")
    _git(directory, "commit", "-m", text)


def _remote(tmp_path: Path, name: str = "remote") -> Path:
    """A bare remote with one commit on main. Returns its path."""
    remote = tmp_path / f"{name}.git"
    work = tmp_path / f"work-{name}"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _commit(work, "readme.txt", "one\n")
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True, env=_env())
    _git(work, "push", str(remote), "main")
    return remote


async def test_sync_clones_the_tip(tmp_path):
    remote = _remote(tmp_path)
    tip = _out(remote, "rev-parse", "main")
    clone = tmp_path / "apps" / "heldso" / "repo"
    sha = await git.sync(clone, str(remote), "main")
    assert sha == tip
    assert (clone / "readme.txt").read_text() == "one\n"
    assert git.head(clone) == tip


async def test_sync_follows_new_commits(tmp_path):
    remote = _remote(tmp_path)
    clone = tmp_path / "repo"
    await git.sync(clone, str(remote), "main")
    work = tmp_path / "work-remote"
    _commit(work, "more.txt", "two\n")
    _git(work, "push", str(remote), "main")
    tip = _out(remote, "rev-parse", "main")
    assert await git.sync(clone, str(remote), "main") == tip
    assert (clone / "more.txt").read_text() == "two\n"


async def test_sync_switches_refs(tmp_path):
    remote = _remote(tmp_path)
    work = tmp_path / "work-remote"
    _git(work, "checkout", "-b", "side")
    _commit(work, "side.txt", "side\n")
    _git(work, "push", str(remote), "side")
    clone = tmp_path / "repo"
    await git.sync(clone, str(remote), "main")
    sha = await git.sync(clone, str(remote), "side")
    assert sha == _out(remote, "rev-parse", "side")
    assert (clone / "side.txt").read_text() == "side\n"


async def test_sync_switches_urls(tmp_path):
    first = _remote(tmp_path, "one")
    second = _remote(tmp_path, "two")
    clone = tmp_path / "repo"
    await git.sync(clone, str(first), "main")
    sha = await git.sync(clone, str(second), "main")
    assert sha == _out(second, "rev-parse", "main")


async def test_sync_drops_untracked_files(tmp_path):
    remote = _remote(tmp_path)
    clone = tmp_path / "repo"
    await git.sync(clone, str(remote), "main")
    (clone / "junk.log").write_text("junk\n")
    (clone / "readme.txt").write_text("edited\n")
    await git.sync(clone, str(remote), "main")
    assert not (clone / "junk.log").exists()
    assert (clone / "readme.txt").read_text() == "one\n"


async def test_the_token_stays_out_of_the_config(tmp_path):
    remote = _remote(tmp_path)
    clone = tmp_path / "repo"
    await git.sync(clone, str(remote), "main", token="oit_secret123")
    config = (clone / ".git" / "config").read_text()
    assert "oit_secret123" not in config
    assert "credential" not in config


async def test_remote_tip(tmp_path):
    remote = _remote(tmp_path)
    tip = _out(remote, "rev-parse", "main")
    assert await git.remote_tip(None, str(remote), "main") == tip
    assert await git.remote_tip(None, str(remote), "missing") == ""


async def test_a_missing_branch_fails_the_sync(tmp_path):
    remote = _remote(tmp_path)
    with pytest.raises(YardError):
        await git.sync(tmp_path / "repo", str(remote), "missing")


def test_head_is_empty_without_a_clone(tmp_path):
    assert git.head(tmp_path / "repo") == ""
