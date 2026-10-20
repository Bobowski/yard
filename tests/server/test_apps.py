"""Apps: records, deploys, checks and their debounce, and prune. Adapters stay stubbed."""

import asyncio
import json
import os
import subprocess
import time
from dataclasses import asdict
from pathlib import Path

import pytest

from app.server import apps, compose, git, journal
from app.server.forges import Forge, Origin
from app.server.podman import Podman
from app.server.store import Config, Store
from app.server.yard import Yard
from app.shared import YardError
from tests.server.conftest import make_yard


def _clone(yard: Yard, name: str) -> Path:
    """A clone dir with one commit. The HEAD is the deploy sha."""
    root = yard.config.clone(name)
    root.mkdir(parents=True)
    (root / "Containerfile").write_text("FROM scratch\n")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Yard",
        "GIT_AUTHOR_EMAIL": "yard@example.com",
        "GIT_COMMITTER_NAME": "Yard",
        "GIT_COMMITTER_EMAIL": "yard@example.com",
    }
    subprocess.run(["git", "-C", str(root), "init", "-b", "main"], check=True, capture_output=True, env=env)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True, env=env)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "one"], check=True, capture_output=True, env=env)
    return root


OK_LOG = "== build localhost/heldso:abc\nbuilt 2026-10-05T10:00:00Z\n== started localhost/heldso:abc\n"


class _Apply:
    """Stands in for compose.apply. Records the call and returns (code, log)."""

    def __init__(self, code: int = 0, text: str = OK_LOG) -> None:
        self.calls: list[dict] = []
        self.code = code
        self.text = text

    async def __call__(self, config, app, command, project, *, detach=False) -> tuple[int, str]:
        self.calls.append({"command": command, "app": app, "project": project, "detach": detach})
        return self.code, self.text


def _connect(monkeypatch: pytest.MonkeyPatch, token: str = "oit_x") -> None:
    """Every forge connected, with a canned token."""
    monkeypatch.setattr(Forge, "connected", lambda self: True)

    async def fake_token(self):
        return token

    monkeypatch.setattr(Forge, "token", fake_token)


async def test_deploy_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = make_yard(tmp_path, skip=False)
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app")
    _clone(yard, "heldso")
    _connect(monkeypatch)
    recorder = _Apply()
    monkeypatch.setattr(compose, "apply", recorder)

    async def fake_sync(clone, url, ref, token=""):
        return git.head(clone)

    monkeypatch.setattr(git, "sync", fake_sync)
    outcome = await apps.deploy(yard, "heldso")
    assert outcome.status == "ok"
    assert outcome.image.startswith("localhost/heldso:")
    assert recorder.calls[0]["command"] == "up"


async def test_deploy_apply_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = make_yard(tmp_path, skip=False)
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app")
    _clone(yard, "heldso")
    _connect(monkeypatch)
    recorder = _Apply(code=1, text="build failed\n")
    monkeypatch.setattr(compose, "apply", recorder)

    async def fake_sync(clone, url, ref, token=""):
        return git.head(clone)

    monkeypatch.setattr(git, "sync", fake_sync)
    outcome = await apps.deploy(yard, "heldso")
    assert outcome.status == "failed"
    assert "apply exited 1" in outcome.error


async def test_deploy_yard_is_pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = make_yard(tmp_path, skip=False)
    yard.store.create_app("yard", "https://origin.cursor.com/org/yard")
    _clone(yard, "yard")
    _connect(monkeypatch)
    recorder = _Apply()
    monkeypatch.setattr(compose, "apply", recorder)

    async def fake_sync(clone, url, ref, token=""):
        return git.head(clone)

    monkeypatch.setattr(git, "sync", fake_sync)
    outcome = await apps.deploy(yard, "yard")
    assert outcome.status == "pending"
    assert recorder.calls[0]["detach"] is True


async def test_deploy_needs_a_repo(tmp_path: Path):
    yard = make_yard(tmp_path, skip=False)
    yard.store.create_app("yard", "")
    outcome = await apps.deploy(yard, "yard")
    assert outcome.status == "failed"
    assert "yard app set --repo" in outcome.log


async def test_deploy_needs_a_forge(tmp_path: Path):
    yard = make_yard(tmp_path, skip=False)
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app")
    yard.store.create_app("web", "https://github.com/org/web")
    outcome = await apps.deploy(yard, "heldso")
    assert outcome.status == "failed"
    assert outcome.error == "origin is not connected. Run yard forge setup origin."
    outcome = await apps.deploy(yard, "web")
    assert outcome.status == "failed"
    assert outcome.error == "github is not connected. Run yard forge setup github."


async def test_deploy_picks_the_forge_by_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = make_yard(tmp_path, skip=False)
    yard.store.create_app("web", "https://github.com/org/web")
    _clone(yard, "web")
    _connect(monkeypatch)
    recorder = _Apply()
    monkeypatch.setattr(compose, "apply", recorder)
    seen: list[str] = []

    async def fake_sync(clone, url, ref, token=""):
        seen.append(url)
        return git.head(clone)

    monkeypatch.setattr(git, "sync", fake_sync)
    outcome = await apps.deploy(yard, "web")
    assert outcome.status == "ok"
    assert seen == ["https://github.com/org/web.git"]


async def test_deploy_skips(tmp_path: Path):
    yard = make_yard(tmp_path, skip=True)
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app")
    outcome = await apps.deploy(yard, "heldso")
    assert outcome.status == "skipped"


async def test_deploy_missing_app(tmp_path: Path):
    yard = make_yard(tmp_path)
    outcome = await apps.deploy(yard, "ghost")
    assert outcome.status == "failed"


async def test_deploy_reverted_when_the_gate_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Apply exit 3 means the new image never settled and the old one is back."""
    yard = make_yard(tmp_path, skip=False)
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app")
    _clone(yard, "heldso")
    _connect(monkeypatch)
    monkeypatch.setattr(compose, "apply", _Apply(code=3, text="== revert localhost/heldso:old\n"))

    async def fake_sync(clone, url, ref, token=""):
        return git.head(clone)

    monkeypatch.setattr(git, "sync", fake_sync)
    outcome = await apps.deploy(yard, "heldso")
    assert outcome.status == "reverted"


async def test_deploy_emits_start_and_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = make_yard(tmp_path, skip=False)
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app")
    _clone(yard, "heldso")
    _connect(monkeypatch)
    monkeypatch.setattr(compose, "apply", _Apply())
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(journal, "send", lambda _c, event, _m, **kw: sent.append((event, kw)))

    async def fake_sync(clone, url, ref, token=""):
        return git.head(clone)

    monkeypatch.setattr(git, "sync", fake_sync)
    outcome = await apps.deploy(yard, "heldso", cause="push", actor="webhook:origin")
    assert outcome.status == "ok"
    assert sent[0][0] == "deploy.start"
    assert sent[0][1]["cause"] == "push" and sent[0][1]["actor"] == "webhook:origin"
    assert sent[-1][0] == "deploy.end"
    assert sent[-1][1]["status"] == "ok" and sent[-1][1]["container"] == "deploy-heldso"


async def test_ensure_host_app_is_unconditional(tmp_path: Path):
    yard = make_yard(tmp_path)
    apps.ensure_host(yard)
    app = yard.store.app("yard")
    assert app.repo == ""
    assert app.ref == "main"
    assert app.trigger == "manual"
    apps.ensure_host(yard)


async def test_check_all_deploys_a_moved_tip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = make_yard(tmp_path)
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app", "main", "push")
    yard.store.create_app("manual-app", "https://origin.cursor.com/org/other", "main", "manual")
    _connect(monkeypatch)

    tips = {"heldso": "abc123"}
    deployed: list[str] = []

    async def fake_tip(clone, url, ref, token=""):
        return tips.get(clone.parent.name, "")

    async def fake_deploy(host, name, **_):
        deployed.append(name)
        return apps.Outcome("ok")

    monkeypatch.setattr(git, "remote_tip", fake_tip)
    monkeypatch.setattr(git, "head", lambda clone: "")
    monkeypatch.setattr(apps, "deploy", fake_deploy)
    await apps.check_all(yard)
    assert deployed == ["heldso"]
    assert yard.tips["heldso"] == "abc123"
    assert yard.forges["origin.cursor.com"].last_check


async def test_check_all_skips_an_unchanged_tip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = make_yard(tmp_path)
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app", "main", "push")
    _connect(monkeypatch)
    deployed: list[str] = []

    async def fake_tip(clone, url, ref, token=""):
        return "same123"

    async def fake_deploy(host, name, **_):
        deployed.append(name)
        return apps.Outcome("ok")

    monkeypatch.setattr(git, "remote_tip", fake_tip)
    monkeypatch.setattr(git, "head", lambda clone: "same123")
    monkeypatch.setattr(apps, "deploy", fake_deploy)
    await apps.check_all(yard)
    assert deployed == []
    assert yard.tips["heldso"] == "same123"


async def test_check_all_without_origin_does_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = make_yard(tmp_path)
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app", "main", "push")
    seen: list[str] = []

    async def fake_tip(clone, url, ref, token=""):
        seen.append("tip")
        return "abc"

    monkeypatch.setattr(git, "remote_tip", fake_tip)
    await apps.check_all(yard)
    assert seen == []
    assert yard.forges["origin.cursor.com"].last_check == ""


async def test_check_repo_deploys_only_that_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = make_yard(tmp_path)
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app", "main", "push")
    yard.store.create_app("other", "https://origin.cursor.com/org/other", "main", "push")
    _connect(monkeypatch)
    seen: list[str] = []
    deployed: list[str] = []

    async def fake_tip(clone, url, ref, token=""):
        seen.append(url)
        return "newsha"

    async def fake_deploy(host, name, **_):
        deployed.append(name)
        return apps.Outcome("ok")

    monkeypatch.setattr(git, "remote_tip", fake_tip)
    monkeypatch.setattr(git, "head", lambda clone: "")
    monkeypatch.setattr(apps, "deploy", fake_deploy)
    await apps.check_repo(yard, "https://origin.cursor.com/org/app")
    assert seen == ["https://origin.cursor.com/org/app.git"]
    assert deployed == ["heldso"]


async def test_check_repo_debounces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = make_yard(tmp_path)
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app", "main", "push")
    _connect(monkeypatch)
    monkeypatch.setattr(apps, "_DEBOUNCE", 0.05)
    seen: list[str] = []

    async def fake_tip(clone, url, ref, token=""):
        seen.append("tip")
        return ""

    monkeypatch.setattr(git, "remote_tip", fake_tip)
    await apps.check_repo(yard, "https://origin.cursor.com/org/app")
    await apps.check_repo(yard, "https://origin.cursor.com/org/app")
    await apps.check_repo(yard, "https://origin.cursor.com/org/app")
    assert seen == ["tip"]
    # The calls inside the window queue one trailing run.
    await asyncio.sleep(0.1)
    assert seen == ["tip", "tip"]
    # A different repo is a different window.
    await apps.check_repo(yard, "https://origin.cursor.com/org/quiet")
    assert seen == ["tip", "tip"]


async def test_check_installation_debounces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = make_yard(tmp_path)
    monkeypatch.setattr(apps, "_DEBOUNCE", 0.05)
    calls: list[str] = []

    async def fake_find(self):
        calls.append("install")
        return None

    monkeypatch.setattr(Origin, "find_installation", fake_find)
    for _ in range(3):
        await apps.check_installation(yard, yard.forge_named("origin"))
    assert calls == ["install"]
    await asyncio.sleep(0.1)
    assert calls == ["install", "install"]


class _Podman(Podman):
    """ps and `image exists` answers for the deploy."""

    def __init__(self, ps: str = "[]", has_image: bool = False) -> None:
        super().__init__("podman", False)
        self.ps = ps
        self.has_image = has_image

    async def run(self, *args: str, timeout=None, env=None, stdin: bytes | None = None) -> str:
        if args[:1] == ("ps",):
            return self.ps
        if args[:2] == ("image", "exists"):
            if not self.has_image:
                raise YardError(502, "podman", "podman image exists: no such image")
            return ""
        return "[]"


def _deploy_yard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kw) -> tuple[Yard, _Apply, list[str]]:
    yard = make_yard(tmp_path, skip=False)
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app")
    clone = _clone(yard, "heldso")
    yard.podman = _Podman(**kw)
    _connect(monkeypatch)
    recorder = _Apply()
    monkeypatch.setattr(compose, "apply", recorder)
    synced: list[str] = []

    async def fake_sync(_clone, url, ref, token=""):
        synced.append(ref)
        return git.head(clone)

    monkeypatch.setattr(git, "sync", fake_sync)
    return yard, recorder, synced


async def test_deploy_a_sha_syncs_that_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard, _recorder, synced = _deploy_yard(tmp_path, monkeypatch)
    outcome = await apps.deploy(yard, "heldso", sha="d" * 40, requested="d" * 40)
    assert outcome.status == "ok"
    assert synced == ["d" * 40]


async def test_deploy_with_no_sha_syncs_the_ref(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard, _recorder, synced = _deploy_yard(tmp_path, monkeypatch)
    outcome = await apps.deploy(yard, "heldso")
    assert outcome.status == "ok"
    assert synced == ["main"]


async def test_deploy_reuses_a_local_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard, recorder, _synced = _deploy_yard(tmp_path, monkeypatch, has_image=True)
    outcome = await apps.deploy(yard, "heldso")
    assert outcome.status == "ok"
    assert recorder.calls[0]["project"].env["SKIP_BUILD"] == "1"


async def test_deploy_without_the_image_builds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard, recorder, _synced = _deploy_yard(tmp_path, monkeypatch, has_image=False)
    outcome = await apps.deploy(yard, "heldso")
    assert outcome.status == "ok"
    assert "SKIP_BUILD" not in recorder.calls[0]["project"].env


async def test_deploy_records_the_requested_sha(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard, _recorder, _synced = _deploy_yard(tmp_path, monkeypatch)
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(journal, "send", lambda _c, event, _m, **kw: sent.append((event, kw)))
    outcome = await apps.deploy(yard, "heldso", sha="d" * 40, requested="deadbeef")
    assert outcome.status == "ok"
    assert sent[0][0] == "deploy.start"
    assert sent[0][1]["requested"] == "deadbeef"


NOW = time.time()

IMAGES = json.dumps(
    [
        {"Names": ["localhost/app:ne11"], "Created": int(NOW - 30 * 86400), "Size": 100},
        {"Names": ["localhost/app:0ld"], "Created": int(NOW - 30 * 86400), "Size": 110},
        {"Names": ["localhost/app:st4le"], "Created": int(NOW - 30 * 86400), "Size": 400},
        {"Names": ["localhost/app:fr3sh"], "Created": int(NOW - 2 * 86400), "Size": 500},
        {"Names": ["localhost/busy:t4g"], "Created": int(NOW - 30 * 86400), "Size": 600},
        {"Names": ["docker.io/library/caddy:2"], "Created": int(NOW - 30 * 86400), "Size": 700},
        {"Names": ["localhost/yard-apply"], "Created": int(NOW - 30 * 86400), "Size": 800},
    ]
)


def _yard(tmp_path: Path) -> Yard:
    config = Config(
        root=tmp_path,
        podman_sock="",
        podman_bin="podman",
        skip_deploy=True,
        boot=False,
        remote=False,
        journal_dir=tmp_path / "no-journal",
        journalctl_bin="journalctl-missing",
        journal_socket=tmp_path / "no-journal.sock",
    )
    yard = Yard(config, Store.open(tmp_path))
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app", "main")
    return yard


class _Fake(Podman):
    def __init__(self) -> None:
        super().__init__("podman", False)
        self.removed: list[str] = []

    async def run(self, *args: str, timeout=None, env=None, stdin: bytes | None = None) -> str:
        if args[:1] == ("ps",):
            return (
                '[{"Names":["heldso-app-1"],"State":"running","Image":"localhost/app:ne11",'
                '"Labels":{"com.docker.compose.project":"heldso","com.docker.compose.service":"app"}}]'
            )
        if args[:1] == ("images",):
            return IMAGES
        if args[:1] == ("rmi",):
            self.removed.append(args[1])
            if args[1] == "localhost/busy:t4g":
                raise YardError(
                    502,
                    "podman",
                    "podman rmi localhost/busy:t4g: image is in use by container heldso-app-1",
                )
            return ""
        return "[]"


def test_prune_removes_what_no_app_needs(tmp_path: Path):
    yard = _yard(tmp_path)
    fake = _Fake()
    yard.podman = fake
    found = asdict(asyncio.run(apps.prune(yard, 7, False)))
    images = {row["tag"]: row["kept"] for row in found["images"]}
    assert images == {
        "localhost/app:0ld": "",
        "localhost/app:st4le": "",
        "localhost/busy:t4g": "image is in use by container heldso-app-1",
    }
    assert fake.removed == ["localhost/app:0ld", "localhost/app:st4le", "localhost/busy:t4g"]
    assert found["freed"] == 110 + 400
    assert "checkouts" not in found
    assert found["days"] == 7
    assert not found["dry"]


def test_prune_dry_run_removes_nothing(tmp_path: Path):
    yard = _yard(tmp_path)
    fake = _Fake()
    yard.podman = fake
    found = asdict(asyncio.run(apps.prune(yard, 7, True)))
    assert fake.removed == []
    assert found["dry"]


def test_prune_one_day_removes_the_recent_ones(tmp_path: Path):
    yard = _yard(tmp_path)
    fake = _Fake()
    yard.podman = fake
    found = asdict(asyncio.run(apps.prune(yard, 1, False)))
    assert "localhost/app:fr3sh" in fake.removed
    tags = {row["tag"] for row in found["images"]}
    assert tags == {"localhost/app:0ld", "localhost/app:st4le", "localhost/app:fr3sh", "localhost/busy:t4g"}
