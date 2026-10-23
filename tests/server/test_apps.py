"""Apps: records, deploys, checks and their debounce, and prune. Adapters stay stubbed."""

import asyncio
import json
import os
import subprocess
import time
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from app.server import apps, compose, git, journal, main, sources
from app.server.forges import Forge, GitHub, Origin
from app.server.podman import Podman
from app.server.store import Config, Store
from app.server.yard import Yard
from app.shared import YardError
from tests.server.conftest import FakeCaddy, make_yard


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
    """Apply exit 3 means the new image never got healthy and the old one is back."""
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


PS_HELDSO = (
    '[{"Names":["heldso-app-1"],"State":"running","Image":"localhost/heldso:abc",'
    '"Labels":{"com.docker.compose.project":"heldso","com.docker.compose.service":"app"}}]'
)


def _delete_yard(tmp_path: Path, *, routes: list[dict] | None = None, ps: str = "[]") -> tuple[Yard, FakeCaddy]:
    yard = make_yard(tmp_path)
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app")
    yard.podman = _Podman(ps=ps)
    caddy = FakeCaddy(routes if routes is not None else [])
    yard.caddy = caddy
    return yard, caddy


async def test_delete_refuses_the_yard_app(tmp_path: Path):
    yard = make_yard(tmp_path)
    yard.store.create_app("yard", "")
    with pytest.raises(YardError) as caught:
        await apps.delete(yard, "yard")
    assert caught.value.status == 400
    assert caught.value.code == "forbidden"
    assert "yard-app-1" in (caught.value.help_text or "")


async def test_delete_reports_the_binds_it_leaves(tmp_path: Path):
    yard, caddy = _delete_yard(tmp_path, routes=[_route("heldso.example.com", "heldso-app-1:8000")], ps=PS_HELDSO)
    removed = await apps.delete(yard, "heldso")
    assert removed.name == "heldso"
    assert removed.binds_left == [("heldso.example.com", "heldso-app-1:8000")]
    assert removed.binds_removed == []
    assert removed.binds_error == ""
    assert len(caddy.routes) == 1  # nothing was removed


async def test_delete_with_binds_removes_them(tmp_path: Path):
    yard, caddy = _delete_yard(
        tmp_path,
        routes=[
            _route("heldso.example.com", "heldso-app-1:8000"),
            _route("other.example.com", "other-app-1:8000"),
        ],
        ps=PS_HELDSO,
    )
    removed = await apps.delete(yard, "heldso", with_binds=True)
    assert removed.binds_removed == [("heldso.example.com", "heldso-app-1:8000")]
    assert removed.binds_left == []
    # Only the app's own bind went.
    assert [row["match"][0]["host"][0] for row in caddy.routes] == ["other.example.com"]


async def test_delete_counts_an_unlabeled_container_by_name(tmp_path: Path):
    """The `<name>-app-1` guess covers a bind written before the container existed."""
    yard, _caddy = _delete_yard(tmp_path, routes=[_route("heldso.example.com", "heldso-app-1:8000")], ps="[]")
    removed = await apps.delete(yard, "heldso")
    assert removed.binds_left == [("heldso.example.com", "heldso-app-1:8000")]


async def test_delete_keeps_going_when_the_binds_are_unreadable(tmp_path: Path):
    yard, _caddy = _delete_yard(tmp_path, ps=PS_HELDSO)

    class _Down(FakeCaddy):
        async def request(self, method: str, path: str, body=None):
            raise YardError(502, "caddy", "caddy GET /config: no socket")

    yard.caddy = _Down()
    removed = await apps.delete(yard, "heldso")
    assert "no socket" in removed.binds_error
    assert removed.binds_removed == [] and removed.binds_left == []
    with pytest.raises(YardError):
        yard.store.app("heldso")  # the app is gone anyway


async def test_prune_loop_uses_the_configured_days(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The loop waits ten minutes, then prunes with YARD_PRUNE_DAYS once a day."""
    yard = make_yard(tmp_path)
    yard.config = replace(yard.config, prune_days=3)
    calls: list[tuple[int, bool, str]] = []

    async def fake_prune(host, days, dry, *, actor=""):
        calls.append((days, dry, actor))
        return apps.Report(freed=100)

    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(apps, "prune", fake_prune)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await apps.prune_loop(yard)
    assert calls == [(3, False, "auto")]
    assert sleeps == [600, 24 * 3600]
    assert yard.last_prune
    assert yard.last_prune_freed == 100
    assert yard.last_prune_error == ""


async def test_prune_loop_records_an_error_and_keeps_going(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = make_yard(tmp_path)
    runs = 0

    async def fake_prune(host, days, dry, *, actor=""):
        nonlocal runs
        runs += 1
        if runs == 1:
            raise YardError(502, "podman", "podman is down")
        return apps.Report()

    seen_errors: list[str] = []

    async def fake_sleep(seconds):
        seen_errors.append(yard.last_prune_error)
        if runs > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(apps, "prune", fake_prune)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await apps.prune_loop(yard)
    # The boot wait runs first, then each prune's 24 h wait sees the last result.
    assert runs == 2
    assert seen_errors == ["", "podman is down", ""]
    assert yard.last_prune_error == ""
    assert yard.last_prune


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


def _reports(monkeypatch: pytest.MonkeyPatch, error: Exception | None = None) -> list[tuple]:
    """Stands in for GitHub.report. Records (repo, sha, state, description, target_url)."""
    calls: list[tuple] = []

    async def fake_report(self, repo, sha, state, description, target_url=""):
        if error is not None:
            raise error
        calls.append((repo, sha, state, description, target_url))

    monkeypatch.setattr(GitHub, "report", fake_report)
    return calls


async def _drain() -> None:
    """The deploy spawns its reports; let them finish before asserting."""
    others = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
    await asyncio.gather(*others, return_exceptions=True)


def _deploy_github(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str = "heldso", apply: _Apply | None = None, **kw
) -> tuple[Yard, _Apply]:
    """A github app ready to deploy: clone, forge token, apply recorder, sync."""
    yard = make_yard(tmp_path, skip=False)
    yard.store.create_app(name, "https://github.com/org/app")
    clone = _clone(yard, name)
    yard.podman = _Podman(**kw)
    _connect(monkeypatch)
    recorder = apply or _Apply()
    monkeypatch.setattr(compose, "apply", recorder)

    async def fake_sync(_clone, url, ref, token=""):
        return git.head(clone)

    monkeypatch.setattr(git, "sync", fake_sync)
    return yard, recorder


def _route(host: str, dial: str) -> dict:
    return {
        "@id": f"yard-{host}",
        "match": [{"host": [host]}],
        "handle": [{"upstreams": [{"dial": dial}]}],
    }


async def test_deploy_reports_pending_then_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard, _recorder = _deploy_github(tmp_path, monkeypatch)
    calls = _reports(monkeypatch)
    head = git.head(yard.config.clone("heldso"))
    outcome = await apps.deploy(yard, "heldso")
    await _drain()
    assert outcome.status == "ok"
    assert calls == [
        ("https://github.com/org/app", head, "pending", f"deploying {head[:12]}", ""),
        ("https://github.com/org/app", head, "success", "live", ""),
    ]
    forge = yard.forge("https://github.com/org/app")
    assert forge.last_report
    assert forge.last_report_error == ""


async def test_deploy_report_links_the_public_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard, _recorder = _deploy_github(tmp_path, monkeypatch)
    calls = _reports(monkeypatch)
    yard.caddy = FakeCaddy([_route("heldso.example.com", "heldso-app-1:8000")])
    outcome = await apps.deploy(yard, "heldso")
    await _drain()
    assert outcome.status == "ok"
    assert calls[-1][2:] == ("success", "live on heldso.example.com", "https://heldso.example.com")


async def test_deploy_report_matches_the_exact_dial_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A bind on foo-bar-app-1 is not app foo's; a prefix match would link the wrong host."""
    yard, _recorder = _deploy_github(tmp_path, monkeypatch, name="foo")
    calls = _reports(monkeypatch)
    yard.caddy = FakeCaddy(
        [
            _route("foo-bar.example.com", "foo-bar-app-1:8000"),
            _route("foo.example.com", "foo:8000"),
        ]
    )
    outcome = await apps.deploy(yard, "foo")
    await _drain()
    assert outcome.status == "ok"
    assert calls[-1][2:] == ("success", "live on foo.example.com", "https://foo.example.com")


async def test_deploy_reports_pending_then_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard, _recorder = _deploy_github(tmp_path, monkeypatch, apply=_Apply(code=1, text="build failed\n"))
    calls = _reports(monkeypatch)
    outcome = await apps.deploy(yard, "heldso")
    await _drain()
    assert outcome.status == "failed"
    assert [call[2] for call in calls] == ["pending", "failure"]
    assert calls[-1][3] == "failed: apply exited 1"


async def test_deploy_reports_pending_then_reverted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard, _recorder = _deploy_github(tmp_path, monkeypatch, apply=_Apply(code=3, text="== revert\n"))
    calls = _reports(monkeypatch)
    outcome = await apps.deploy(yard, "heldso")
    await _drain()
    assert outcome.status == "reverted"
    assert [call[2] for call in calls] == ["pending", "failure"]
    assert calls[-1][3].startswith("reverted: ")


async def test_a_skipped_deploy_reports_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = make_yard(tmp_path, skip=True)
    yard.store.create_app("heldso", "https://github.com/org/app")
    calls = _reports(monkeypatch)
    outcome = await apps.deploy(yard, "heldso")
    await _drain()
    assert outcome.status == "skipped"
    assert calls == []


async def test_a_sync_failure_reports_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """No resolved sha, no status: a failed sync never reaches the forge report."""
    yard, _recorder = _deploy_github(tmp_path, monkeypatch)
    calls = _reports(monkeypatch)

    async def broken_sync(_clone, url, ref, token=""):
        raise YardError(502, "git", "git fetch: the remote is down")

    monkeypatch.setattr(git, "sync", broken_sync)
    outcome = await apps.deploy(yard, "heldso")
    await _drain()
    assert outcome.status == "failed"
    assert calls == []


async def test_a_report_failure_never_fails_the_deploy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard, _recorder = _deploy_github(tmp_path, monkeypatch)
    _reports(monkeypatch, error=YardError(502, "github", "github POST /repos/org/app/statuses/x: 403"))
    outcome = await apps.deploy(yard, "heldso")
    await _drain()
    assert outcome.status == "ok"
    forge = yard.forge("https://github.com/org/app")
    assert "403" in forge.last_report_error


async def test_report_skips_a_repo_the_installation_does_not_own(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A push-trigger yard app on Bobowski/yard must not POST a status there."""
    yard = make_yard(tmp_path)
    forge = yard.forge("https://github.com/Bobowski/yard")
    forge.save(account="org")
    calls = _reports(monkeypatch)
    await apps.report(forge, "https://github.com/Bobowski/yard", "abc123", "success", "live")
    assert calls == []
    # Skipping is not an error: nothing lands on the doctor row.
    assert forge.last_report == ""
    assert forge.last_report_error == ""


async def test_report_still_posts_for_an_owned_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = make_yard(tmp_path)
    forge = yard.forge("https://github.com/org/app")
    forge.save(account="org")
    calls = _reports(monkeypatch)
    await apps.report(forge, "https://github.com/org/app", "abc123", "success", "live")
    assert calls == [("https://github.com/org/app", "abc123", "success", "live", "")]
    assert forge.last_report


async def test_a_yard_self_deploy_stays_pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The detached apply means no success: the commit reads pending until the boot report."""
    yard, _recorder = _deploy_github(tmp_path, monkeypatch, name="yard")
    calls = _reports(monkeypatch)
    outcome = await apps.deploy(yard, "yard")
    await _drain()
    assert outcome.status == "pending"
    assert [call[2] for call in calls] == ["pending"]


async def test_an_origin_deploy_reports_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard, _recorder, _synced = _deploy_yard(tmp_path, monkeypatch)
    calls = []

    async def fake_report(self, repo, sha, state, description, target_url=""):
        calls.append((state, description))

    monkeypatch.setattr(Forge, "report", fake_report)
    outcome = await apps.deploy(yard, "heldso")
    await _drain()
    assert outcome.status == "ok"
    assert calls == []


async def test_boot_report_marks_the_running_sha_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = make_yard(tmp_path)
    yard.store.create_app("yard", "https://github.com/org/yard")
    _connect(monkeypatch)
    calls = _reports(monkeypatch)
    yard.config = replace(yard.config, sha="c" * 40)
    await main._boot_report(yard)
    assert calls == [("https://github.com/org/yard", "c" * 40, "success", "live", "")]


async def test_boot_report_says_nothing_without_a_sha_or_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = make_yard(tmp_path)
    yard.store.create_app("yard", "")
    _connect(monkeypatch)
    calls = _reports(monkeypatch)
    await main._boot_report(yard)  # config.sha is ""
    assert calls == []

    yard.config = replace(yard.config, sha="c" * 40)
    await main._boot_report(yard)  # the yard app has no repo
    assert calls == []

    monkeypatch.setattr(Forge, "connected", lambda self: False)
    yard.store.update_app("yard", repo="https://github.com/org/yard")
    await main._boot_report(yard)  # github is not connected
    assert calls == []


async def test_boot_report_learns_the_installation_account(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A host installed before accounts existed picks it up once at boot."""
    yard = make_yard(tmp_path)
    yard.store.create_app("yard", "https://github.com/org/yard")
    _connect(monkeypatch)
    calls = _reports(monkeypatch)
    found: list[str] = []

    async def fake_find(self):
        found.append(self.name)
        self.save(installation_id="101", account="org")
        return {"id": "101"}

    monkeypatch.setattr(GitHub, "find_installation", fake_find)
    yard.config = replace(yard.config, sha="c" * 40)
    await main._boot_report(yard)
    assert found == ["github"]
    assert calls == [("https://github.com/org/yard", "c" * 40, "success", "live", "")]

    # The account is saved; the next boot does not ask again.
    await main._boot_report(yard)
    assert found == ["github"]


async def test_boot_report_ignores_a_find_installation_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = make_yard(tmp_path)
    yard.store.create_app("yard", "https://github.com/org/yard")
    _connect(monkeypatch)
    calls = _reports(monkeypatch)

    async def down(self):
        raise YardError(502, "github", "github GET /app/installations: refused")

    monkeypatch.setattr(GitHub, "find_installation", down)
    yard.config = replace(yard.config, sha="c" * 40)
    await main._boot_report(yard)  # best effort; the report still lands
    assert calls == [("https://github.com/org/yard", "c" * 40, "success", "live", "")]


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


def _stario_clone(yard: Yard, name: str) -> Path:
    clone = yard.config.clone(name)
    clone.mkdir(parents=True, exist_ok=True)
    (clone / "pyproject.toml").write_text('[project]\ndependencies = ["stario>=5"]\n')
    (clone / "uv.lock").write_text("lock")
    (clone / "app").mkdir()
    (clone / "app" / "main.py").write_text("")
    subprocess.run(["git", "-C", str(clone), "init", "-b", "main"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(clone), "-c", "user.email=t@t", "-c", "user.name=t", "add", "."],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(clone), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "one"],
        check=True,
        capture_output=True,
    )
    return clone


async def test_first_deploy_resolves_and_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = make_yard(tmp_path, skip=False)
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app")
    _stario_clone(yard, "heldso")
    _connect(monkeypatch)
    recorder = _Apply()
    monkeypatch.setattr(compose, "apply", recorder)

    async def fake_sync(clone, url, ref, token=""):
        return git.head(clone)

    monkeypatch.setattr(git, "sync", fake_sync)
    outcome = await apps.deploy(yard, "heldso")
    assert outcome.status == "ok"
    app = yard.store.app("heldso")
    assert app.containerfile == "yard"
    written = (yard.config.app_dir("heldso") / "Containerfile").read_text()
    assert written.startswith("# Written by Yard")
    env = recorder.calls[0]["project"].env
    assert env["DOCKERFILE"] == str(yard.config.app_dir("heldso") / "Containerfile")
    assert outcome.log.startswith("== runs containerfile yard (stario: pyproject.toml declares stario)")


async def test_second_deploy_keeps_the_written_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = make_yard(tmp_path, skip=False)
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app")
    _stario_clone(yard, "heldso")
    _connect(monkeypatch)
    monkeypatch.setattr(compose, "apply", _Apply())

    async def fake_sync(clone, url, ref, token=""):
        return git.head(clone)

    monkeypatch.setattr(git, "sync", fake_sync)
    await apps.deploy(yard, "heldso")
    stored = yard.config.app_dir("heldso") / "Containerfile"
    stored.write_text("# custom\nFROM scratch\n")
    outcome = await apps.deploy(yard, "heldso")
    assert outcome.status == "ok"
    assert stored.read_text() == "# custom\nFROM scratch\n"


async def test_deploy_repo_path_missing_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = make_yard(tmp_path, skip=False)
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app", compose="deploy/x.yml")
    _clone(yard, "heldso")
    _connect(monkeypatch)
    monkeypatch.setattr(compose, "apply", _Apply())

    async def fake_sync(clone, url, ref, token=""):
        return git.head(clone)

    monkeypatch.setattr(git, "sync", fake_sync)
    outcome = await apps.deploy(yard, "heldso")
    assert outcome.status == "failed"
    assert "compose is set to deploy/x.yml; it is not in " in outcome.error
    assert "yard app detect" in outcome.log


async def test_deploy_containerfile_yard_missing_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = make_yard(tmp_path, skip=False)
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app", containerfile="yard")
    _clone(yard, "heldso")
    _connect(monkeypatch)
    monkeypatch.setattr(compose, "apply", _Apply())

    async def fake_sync(clone, url, ref, token=""):
        return git.head(clone)

    monkeypatch.setattr(git, "sync", fake_sync)
    outcome = await apps.deploy(yard, "heldso")
    assert outcome.status == "failed"
    assert "yard app detect" in outcome.log


async def test_deploy_image_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):

    yard = make_yard(tmp_path, skip=False)
    yard.store.create_app("db", "", image="postgres:17")
    recorder = _Apply()
    monkeypatch.setattr(compose, "apply", recorder)

    async def fake_digest(ref):
        return "sha256:" + "d" * 64

    monkeypatch.setattr(sources, "digest", fake_digest)
    outcome = await apps.deploy(yard, "db")
    assert outcome.status == "ok"
    project = recorder.calls[0]["project"]
    assert project.files == [compose.IMAGE_COMPOSE]
    assert project.env["IMAGE"] == f"postgres:17@sha256:{'d' * 64}"
    assert project.env["SKIP_BUILD"] == "1"
    assert (tmp_path / "apps" / "db" / "revision").read_text().strip() == "sha256:" + "d" * 64


async def test_deploy_image_registry_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):

    yard = make_yard(tmp_path, skip=False)
    yard.store.create_app("db", "", image="postgres:17")
    monkeypatch.setattr(compose, "apply", _Apply())

    async def fake_digest(ref):
        raise YardError(502, "registry", "postgres:17: not found")

    monkeypatch.setattr(sources, "digest", fake_digest)
    outcome = await apps.deploy(yard, "db")
    assert outcome.status == "failed"
    assert "not found" in outcome.error
    assert not (tmp_path / "apps" / "db" / "revision").exists()


RECIPE_BODY = b"# port: 3000\nservices:\n  app:\n    image: x\n    networks:\n      - yard\n"


async def test_deploy_recipe_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = make_yard(tmp_path, skip=False)
    yard.store.create_app("umami", "", recipe="https://example.com/umami.yml")
    recorder = _Apply()
    monkeypatch.setattr(compose, "apply", recorder)

    async def fake_fetch(url):
        return RECIPE_BODY

    monkeypatch.setattr(sources, "fetch_recipe", fake_fetch)
    outcome = await apps.deploy(yard, "umami")
    assert outcome.status == "ok"
    import hashlib

    revision = "sha256:" + hashlib.sha256(RECIPE_BODY).hexdigest()
    recipe_dir = tmp_path / "apps" / "umami" / "recipe"
    files = list(recipe_dir.glob("*.yml"))
    assert len(files) == 1
    assert files[0].stem == revision[7:19]
    project = recorder.calls[0]["project"]
    assert project.files == [str(files[0])]
    assert project.env["SHA"] == revision
    assert "PREVIOUS_COMPOSE_FILE" not in project.env

    # Second deploy: the previous file is the revert target.
    outcome = await apps.deploy(yard, "umami")
    assert outcome.status == "ok"
    project = recorder.calls[1]["project"]
    assert project.env["PREVIOUS_COMPOSE_FILE"] == str(files[0])
    assert len(list(recipe_dir.glob("*.yml"))) == 1  # same body, same file


async def test_deploy_recipe_keeps_two_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = make_yard(tmp_path, skip=False)
    yard.store.create_app("umami", "", recipe="https://example.com/umami.yml")
    monkeypatch.setattr(compose, "apply", _Apply())
    bodies = iter([b"a\n", b"b\n", b"c\n"])

    async def fake_fetch(url):
        return next(bodies)

    monkeypatch.setattr(sources, "fetch_recipe", fake_fetch)
    for _ in range(3):
        outcome = await apps.deploy(yard, "umami")
        assert outcome.status == "ok"
    recipe_dir = tmp_path / "apps" / "umami" / "recipe"
    assert len(list(recipe_dir.glob("*.yml"))) <= 2


async def test_fetch_recipe_http_redirect_fails():
    async def _run():
        def fake_fetch(url, method, headers, **_kw):
            return 302, {"Location": "http://evil.example.com/x.yml"}, b""

        old = sources.fetch
        sources.fetch = fake_fetch
        try:
            await sources.fetch_recipe("https://example.com/x.yml")
        finally:
            sources.fetch = old

    with pytest.raises(YardError, match="not https"):
        await _run()


async def test_check_source_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):

    yard = make_yard(tmp_path, skip=False)
    yard.store.create_app("db", "", image="postgres:17", trigger="push")
    yard.store.create_app("site", "", image="nginx:1", trigger="manual")
    calls = []

    async def fake_digest(ref):
        calls.append(ref)
        return "sha256:" + ("e" if ref == "postgres:17" else "f") * 64

    monkeypatch.setattr(sources, "digest", fake_digest)
    monkeypatch.setattr(compose, "apply", _Apply())
    yard.forges = {}
    await apps.check_all(yard)
    assert yard.tips["db"] == "sha256:" + "e" * 64
    assert yard.tips["site"] == "sha256:" + "f" * 64
    # push app deployed; manual app only updated tips
    assert (tmp_path / "apps" / "db" / "revision").is_file()
    assert not (tmp_path / "apps" / "site" / "revision").exists()


async def test_check_source_error_and_clear(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):

    yard = make_yard(tmp_path, skip=False)
    yard.store.create_app("db", "", image="postgres:17", trigger="push")
    monkeypatch.setattr(compose, "apply", _Apply())

    async def fail(ref):
        raise YardError(502, "registry", "postgres:17: boom")

    monkeypatch.setattr(sources, "digest", fail)
    yard.forges = {}
    await apps.check_all(yard)
    assert yard.source_errors["db"] == "postgres:17: boom"
    assert not (tmp_path / "apps" / "db" / "revision").exists()

    async def ok(ref):
        return "sha256:" + "a" * 64

    monkeypatch.setattr(sources, "digest", ok)
    await apps.check_all(yard)
    assert "db" not in yard.source_errors


async def test_check_source_one_get_per_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = make_yard(tmp_path, skip=False)
    yard.store.create_app("a", "", recipe="https://example.com/x.yml", trigger="manual")
    yard.store.create_app("b", "", recipe="https://example.com/x.yml", trigger="manual")
    calls = []

    async def fake_fetch(url):
        calls.append(url)
        return b"body\n"

    monkeypatch.setattr(sources, "fetch_recipe", fake_fetch)
    yard.forges = {}
    await apps.check_all(yard)
    assert calls == ["https://example.com/x.yml"]
