import asyncio
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app import deploy
from app import yard as yard_mod
from app.compose import image_tag
from app.config import Config
from app.errors import YardError
from app.podman import Podman
from app.store import Store
from app.yard import Yard


def _yard(tmp_path: Path) -> Yard:
    config = Config(
        root=tmp_path,
        podman_sock="",
        podman_bin="podman",
        hook_url="http://127.0.0.1:8000",
        domain="",
        skip_deploy=False,
        boot=False,
        remote=False,
    )
    store = Store.open(tmp_path)
    yard = Yard(config, store)
    _pod(yard)
    return yard


def _slot(yard: Yard, name: str = "heldso", job: bool = False, trigger: str = "") -> None:
    try:
        yard.store.create_repo("app")
    except YardError:
        pass
    yard.store.create_slot(name, "app", "main", "", trigger or ("every 5m" if job else ""))


def _tree(yard: Yard, name: str) -> str:
    """A worktree with one commit. The HEAD is the saved image."""
    root = yard.config.worktree(name)
    root.mkdir(parents=True)
    (root / "Containerfile").write_text("FROM scratch\n")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Yard",
        "GIT_AUTHOR_EMAIL": "yard@example.com",
        "GIT_COMMITTER_NAME": "Yard",
        "GIT_COMMITTER_EMAIL": "yard@example.com",
    }
    subprocess.run(["git", "-C", str(root), "init", "-b", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "one"], check=True, capture_output=True, env=env)
    return subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()


ALIASED = "services:\n  app:\n    networks:\n      yard:\n        aliases:\n          - ${PROJECT_NAME}\n"


def _resolved(doc: dict):
    async def fake(compose_file, env):
        return doc

    return fake


def _checkout(tmp_path: Path, name: str = "heldso") -> Path:
    checkout = tmp_path / "worktree" / name
    checkout.mkdir(parents=True)
    (checkout / "Containerfile").write_text("FROM scratch\n")
    return checkout


OK_LOG = "== build localhost/app:abc\nbuilt 2026-10-05T10:00:00Z\n== ready localhost/app:abc\n"


class _Apply:
    """Stands in for compose.apply. Records the call and returns (code, log)."""

    def __init__(self, code: int = 0, text: str = OK_LOG) -> None:
        self.calls: list[dict] = []
        self.code = code
        self.text = text

    async def __call__(
        self,
        config,
        slot,
        command,
        project,
        *,
        previous="",
        detach=False,
    ) -> tuple[int, str]:
        self.calls.append(
            {
                "command": command,
                "slot": slot,
                "project": project,
                "previous": previous,
                "detach": detach,
            }
        )
        return self.code, self.text


def _a(fn):
    async def wrapped(*args, **kw):
        return fn(*args, **kw)

    return wrapped


def _stub(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, code: int = 0, text: str = OK_LOG) -> _Apply:
    recorder = _Apply(code, text)
    checkout = _checkout(tmp_path)
    monkeypatch.setattr("app.git.checkout", _a(lambda *args, **kw: checkout))
    monkeypatch.setattr("app.compose.apply", recorder)
    return recorder


def test_deploy_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path)
    _slot(yard)
    recorder = _stub(monkeypatch, tmp_path)
    slot = yard.store.slot("heldso")
    try:
        outcome = asyncio.run(deploy.deploy(yard, slot, "abc"))
        assert outcome.status == "ok"
        assert outcome.image == "localhost/app:abc"
        assert len(recorder.calls) == 1
        call = recorder.calls[0]
        assert call["command"] == "up"
        assert call["project"].env["IMAGE"] == "localhost/app:abc"
        assert call["detach"] is False

        assert "built 2026-10-05T10:00:00Z" in outcome.log
        assert "== ready" in outcome.log

        # A deploy writes the project file. The checkout in this test is a stand-in.
        assert sorted(path.name for path in tmp_path.iterdir()) == ["projects", "repos", "tokens", "worktree"]
        assert (tmp_path / "projects" / "heldso" / "project").is_file()
        assert not (tmp_path / "run").exists()
    finally:
        yard.store.close()


def test_deploy_with_a_repo_compose(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path)
    _slot(yard)
    recorder = _Apply()
    checkout = _checkout(tmp_path)
    (checkout / "compose.yml").write_text(ALIASED)
    monkeypatch.setattr("app.git.checkout", _a(lambda *args, **kw: checkout))
    monkeypatch.setattr(
        "app.compose.resolved", _resolved({"services": {"app": {"networks": {"yard": {"aliases": ["heldso"]}}}}})
    )
    monkeypatch.setattr("app.compose.apply", recorder)
    slot = yard.store.slot("heldso")
    try:
        outcome = asyncio.run(deploy.deploy(yard, slot, "abc"))
        assert outcome.status == "ok"
        assert [call["command"] for call in recorder.calls] == ["up"]
        assert recorder.calls[0]["project"].env["IMAGE"] == "localhost/app:abc"

        # The warnings open the log; the apply output follows.
        assert outcome.log.splitlines()[:3] == [
            "warning: compose: no service attaches env_file ${APP_ENV}; the project env is not passed",
            "warning: compose: no service mounts ${DATA_DIR}; the project has no /data",
            "== build localhost/app:abc",
        ]
    finally:
        yard.store.close()


def test_deploy_fails_before_apply_without_the_alias(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path)
    _slot(yard)
    recorder = _Apply()
    checkout = _checkout(tmp_path)
    (checkout / "compose.yml").write_text("services:\n  app: {}\n")
    monkeypatch.setattr("app.git.checkout", _a(lambda *args, **kw: checkout))
    monkeypatch.setattr("app.compose.resolved", _resolved({"services": {"app": {}}}))
    monkeypatch.setattr("app.compose.apply", recorder)
    slot = yard.store.slot("heldso")
    try:
        outcome = asyncio.run(deploy.deploy(yard, slot, "abc"))
        assert outcome.status == "failed"
        assert outcome.error == (
            "compose: no service on network yard with alias heldso; the health probe dials http://heldso:8000/"
        )
        assert recorder.calls == []
        assert outcome.log == outcome.error + "\n"
    finally:
        yard.store.close()


def test_deploy_apply_failure_keeps_the_previous_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path)
    _slot(yard)
    _pod(yard, _ps_app("heldso", "localhost/app:old9"))
    recorder = _stub(monkeypatch, tmp_path, code=1, text="== start localhost/app:abc\nboom\n")
    slot = yard.store.slot("heldso")
    try:
        outcome = asyncio.run(deploy.deploy(yard, slot, "abc"))
        assert outcome.status == "failed"
        assert "apply exited 1" in outcome.error
        assert "boom" in outcome.log
        # The failing apply saw the previous image for its revert.
        assert recorder.calls[0]["previous"] == "localhost/app:old9"
    finally:
        yard.store.close()


def test_deploy_checkout_failure_never_runs_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path)
    _slot(yard)
    recorder = _Apply()
    monkeypatch.setattr("app.compose.apply", recorder)

    async def dead(*args, **kw):
        raise RuntimeError("git failed")

    monkeypatch.setattr("app.git.checkout", dead)
    slot = yard.store.slot("heldso")
    try:
        outcome = asyncio.run(deploy.deploy(yard, slot, "abc"))
        assert outcome.status == "failed"
        assert "git failed" in outcome.log
        assert recorder.calls == []
    finally:
        yard.store.close()


def test_deploy_job_slot_builds_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path)
    _slot(yard, name="nightly", job=True)
    recorder = _stub(monkeypatch, tmp_path)
    slot = yard.store.slot("nightly")
    try:
        outcome = asyncio.run(deploy.deploy(yard, slot, "abc"))
        assert outcome.status == "ok"
        assert recorder.calls[0]["command"] == "build"
        assert "built 2026-10-05T10:00:00Z" in outcome.log
    finally:
        yard.store.close()


def test_deploy_yard_slot_returns_pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path)
    _slot(yard, name="yard")
    recorder = _stub(monkeypatch, tmp_path)
    slot = yard.store.slot("yard")
    try:
        outcome = asyncio.run(deploy.deploy(yard, slot, "abc"))
        assert outcome.status == "pending"
        assert recorder.calls[0]["detach"] is True
    finally:
        yard.store.close()


def test_run_job_uses_the_saved_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path)
    _slot(yard, name="nightly", job=True)
    sha = _tree(yard, "nightly")
    recorder = _stub(monkeypatch, tmp_path)
    slot = yard.store.slot("nightly")
    try:
        asyncio.run(deploy.deploy(yard, slot, "abc"))
        recorder.calls.clear()
        out = asyncio.run(deploy.run_job(yard, slot))
        assert out == OK_LOG
        call = recorder.calls[0]
        assert call["command"] == "job"
        assert call["project"].env["IMAGE"] == image_tag("app", "", sha)
        assert call["project"].env["SHA"] == sha

        # A non-zero apply is a job failure, not silent.
        recorder.code = 7
        with pytest.raises(RuntimeError):
            asyncio.run(deploy.run_job(yard, slot))

        # No saved project means nothing to run.
        yard.store.create_slot("heldso", "app", "main", "", "every 5m")
        with pytest.raises(RuntimeError, match="no compose file"):
            asyncio.run(deploy.run_job(yard, yard.store.slot("heldso")))
    finally:
        yard.store.close()


def test_run_deploy_coalesces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path)
    _slot(yard)
    _stub(monkeypatch, tmp_path)
    ps = {"text": "[]"}
    _job_pod(yard, ps)
    try:
        # A container that already has this image is current, not a new build.
        slot = yard.store.slot("heldso")
        asyncio.run(deploy.deploy(yard, slot, "abc"))
        ps["text"] = _ps_app("heldso", "localhost/app:abc")
        outcome = asyncio.run(yard.run_deploy("heldso", "abc"))
        assert outcome.status == "current"
        assert outcome.image == "localhost/app:abc"

        # A newer tip already on the ref supersedes this call.
        ps["text"] = "[]"
        monkeypatch.setattr("app.yard.git.resolve", _a(lambda *args, **kw: "def"))
        outcome = asyncio.run(yard.run_deploy("heldso", "eee"))
        assert outcome.status == "superseded"
        assert outcome.image == "localhost/app:def"
    finally:
        yard.store.close()


def test_follow_origin_fast_forwards_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path)
    calls: list[tuple[str, ...]] = []

    async def fake_git(*args, cwd=None, timeout=60):
        calls.append(tuple(args))
        if args[0] == "rev-parse" and args[-1] == "refs/remotes/origin/main":
            return "bbb\n"
        if args[0] == "rev-parse":
            return "aaa\n"
        if args[0] == "merge-base":
            return ""
        return ""

    monkeypatch.setattr("app.yard.git.git", fake_git)
    try:
        assert asyncio.run(yard._follow_origin()) == "main bbb\n"
        assert calls[-1] == ("update-ref", "refs/heads/main", "bbb")
    finally:
        yard.store.close()


def test_follow_origin_leaves_a_diverged_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path)

    async def fake_git(*args, cwd=None, timeout=60):
        if args[0] == "rev-parse" and args[-1] == "refs/remotes/origin/main":
            return "bbb"
        if args[0] == "rev-parse":
            return "aaa"
        raise RuntimeError("not an ancestor")

    monkeypatch.setattr("app.yard.git.git", fake_git)
    try:
        assert asyncio.run(yard._follow_origin()) == "main and origin/main have diverged\n"
    finally:
        yard.store.close()


def test_boot_ensures_the_yard_slot(tmp_path: Path):
    yard = _yard(tmp_path)
    try:
        yard.ensure_host_slot()
        with pytest.raises(YardError) as caught:
            yard.store.slot("yard")
        assert caught.value.code == "not_found"
        yard.store.create_repo("yard")
        yard.ensure_host_slot()
        slot = yard.store.slot("yard")
        assert slot.repo == "yard"
        assert slot.ref == "main"
        assert (tmp_path / "projects" / "yard" / "env").is_file()
    finally:
        yard.store.close()


def _pod(yard: Yard, ps: str = "[]") -> None:
    class _Fake(Podman):
        async def run(self, *args: str, timeout=None, env=None, stdin: bytes | None = None) -> str:
            return ps if args[:1] == ("ps",) else ""

    yard.podman = _Fake("podman", False)


def _container(slot: str) -> str:
    return '[{"Names":["' + slot + '-app"],"State":"running","Labels":{"com.docker.compose.project":"' + slot + '"}}]'


def _ps_app(slot: str, image: str) -> str:
    return json.dumps(
        [
            {
                "Names": [f"{slot}-app"],
                "State": "running",
                "Image": image,
                "Labels": {"com.docker.compose.project": slot, "com.docker.compose.service": "app"},
            }
        ]
    )


def test_run_slot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path)
    _slot(yard, name="nightly", job=True)
    _tree(yard, "nightly")
    _slot(yard)
    _pod(yard)
    try:
        monkeypatch.setattr(deploy, "run_job", _a(lambda *a, **k: "ran\n"))

        # The happy path returns the job's output.
        assert asyncio.run(yard.run_slot("nightly")) == "ran\n"

        # A running container of the slot blocks a second run, job or service.
        _pod(yard, _container("nightly"))
        with pytest.raises(YardError) as err:
            asyncio.run(yard.run_slot("nightly"))
        assert err.value.code == "running"
        _pod(yard, _container("heldso"))
        with pytest.raises(YardError) as err:
            asyncio.run(yard.run_slot("heldso"))
        assert err.value.code == "running"

        _pod(yard)
        # A stopped service has no build: deploy builds it first.
        with pytest.raises(YardError) as err:
            asyncio.run(yard.run_slot("heldso"))
        assert err.value.code == "no_image"

        # A job with no build yet asks for a deploy first.
        _slot(yard, name="fresh", job=True)
        with pytest.raises(YardError) as err:
            asyncio.run(yard.run_slot("fresh"))
        assert err.value.code == "no_image"
    finally:
        yard.store.close()


def test_run_slot_takes_the_slot_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path)
    _slot(yard, name="nightly", job=True)
    _tree(yard, "nightly")
    busy = {"on": False, "count": 0}

    async def fake_job(y, slot):
        busy["on"] = True
        busy["count"] += 1
        await asyncio.sleep(0)
        return "ran\n"

    class _Fake(Podman):
        async def run(self, *args: str, timeout=None, env=None, stdin: bytes | None = None) -> str:
            return _container("nightly") if busy["on"] else "[]"

    monkeypatch.setattr(deploy, "run_job", fake_job)
    yard.podman = _Fake("podman", False)
    try:

        async def both():
            return await asyncio.gather(yard.run_slot("nightly"), yard.run_slot("nightly"), return_exceptions=True)

        first, second = asyncio.run(both())
        for item in (first, second):
            if isinstance(item, YardError):
                assert item.code == "running"
            else:
                assert item == "ran\n"
        assert busy["count"] == 1
    finally:
        yard.store.close()


def _job_ps(created: str = "") -> str:
    if not created:
        return "[]"
    return json.dumps([{"Names": ["up-nightly-job"], "State": "exited", "Created": created}])


def _clock(monkeypatch: pytest.MonkeyPatch, start: datetime) -> dict:
    now = {"t": start}
    monkeypatch.setattr(yard_mod, "_now", lambda: now["t"])
    return now


def _job_pod(yard: Yard, ps: dict) -> None:
    """Podman whose ps answer the test can change after each run."""

    class _Fake(Podman):
        async def run(self, *args: str, timeout=None, env=None, stdin: bytes | None = None) -> str:
            return ps["text"] if args[:1] == ("ps",) else ""

    yard.podman = _Fake("podman", False)


def _run_stamp(now: dict) -> str:
    """The created stamp a real run's container would have."""
    return now["t"].strftime("%Y-%m-%dT%H:%M:%SZ")


def test_the_job_loop_runs_on_the_schedule(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path)
    _slot(yard, name="nightly", job=True)  # every 5m
    _pod(yard)
    try:
        now = _clock(monkeypatch, datetime(2024, 3, 9, 16, 0, 0, tzinfo=UTC))
        sleeps: list = []

        async def fake_sleep(wake, seconds):
            sleeps.append(seconds)
            if len(sleeps) == 2:
                raise asyncio.CancelledError
            now["t"] += timedelta(seconds=seconds)

        monkeypatch.setattr(yard_mod, "_sleep", fake_sleep)
        monkeypatch.setattr(Yard, "run_slot", _a(lambda *a, **k: ""))
        asyncio.run(yard._job_loop("nightly", asyncio.Event()))
        # No job container yet: the first sleep is the whole interval.
        assert sleeps == [300, 300]
    finally:
        yard.store.close()


def test_the_job_loop_sleeps_from_the_last_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path)
    _slot(yard, name="nightly", trigger="every 30m")
    start = datetime(2024, 3, 9, 16, 0, 0, tzinfo=UTC)
    ps = {"text": _job_ps((start - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ"))}
    _job_pod(yard, ps)
    try:
        now = _clock(monkeypatch, start)
        sleeps: list = []
        runs: list = []

        async def fake_sleep(wake, seconds):
            sleeps.append(seconds)
            if len(sleeps) == 2:
                raise asyncio.CancelledError
            now["t"] += timedelta(seconds=seconds)

        async def fake_run(self, name):
            runs.append(name)
            ps["text"] = _job_ps(_run_stamp(now))
            return ""

        monkeypatch.setattr(yard_mod, "_sleep", fake_sleep)
        monkeypatch.setattr(Yard, "run_slot", fake_run)
        asyncio.run(yard._job_loop("nightly", asyncio.Event()))
        # The container ran 10 minutes ago: 20 minutes remain of 30.
        assert sleeps == [1200, 1800]
        assert runs == ["nightly"]
    finally:
        yard.store.close()


def test_a_trigger_edit_reschedules_the_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path)
    _slot(yard, name="nightly", trigger="every 30m")
    start = datetime(2024, 3, 9, 16, 0, 0, tzinfo=UTC)
    ps = {"text": _job_ps((start - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ"))}
    _job_pod(yard, ps)
    try:
        now = _clock(monkeypatch, start)
        events: list = []

        async def fake_sleep(wake, seconds):
            events.append(seconds)
            if len(events) == 1:
                # The wake: the slot now runs every 15m. Time barely moved.
                yard.store.update_slot("nightly", trigger="every 15m")
                return
            if len(events) == 4:
                raise asyncio.CancelledError
            now["t"] += timedelta(seconds=seconds)

        async def fake_run(self, name):
            events.append("run")
            ps["text"] = _job_ps(_run_stamp(now))
            return ""

        monkeypatch.setattr(yard_mod, "_sleep", fake_sleep)
        monkeypatch.setattr(Yard, "run_slot", fake_run)
        asyncio.run(yard._job_loop("nightly", asyncio.Event()))
        # Wake does not run; the new due is 15m - 10m = 5m away, then it runs.
        assert events == [1200, 300, "run", 900]
    finally:
        yard.store.close()


def test_a_shorter_interval_already_due_runs_at_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path)
    _slot(yard, name="nightly", trigger="every 30m")
    start = datetime(2024, 3, 9, 16, 0, 0, tzinfo=UTC)
    ps = {"text": _job_ps((start - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ"))}
    _job_pod(yard, ps)
    try:
        now = _clock(monkeypatch, start)
        events: list = []

        async def fake_sleep(wake, seconds):
            events.append(seconds)
            if len(events) == 1:
                # The wake: the new interval is already past.
                yard.store.update_slot("nightly", trigger="every 15m")
                return
            raise asyncio.CancelledError

        async def fake_run(self, name):
            events.append("run")
            ps["text"] = _job_ps(_run_stamp(now))
            return ""

        monkeypatch.setattr(yard_mod, "_sleep", fake_sleep)
        monkeypatch.setattr(Yard, "run_slot", fake_run)
        asyncio.run(yard._job_loop("nightly", asyncio.Event()))
        assert events == [600, "run", 900]
    finally:
        yard.store.close()


def test_deploy_push_slot_runs_the_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path)
    _slot(yard, name="nightly", trigger="push")
    _tree(yard, "nightly")
    recorder = _stub(monkeypatch, tmp_path)
    _pod(yard)
    monkeypatch.setattr(deploy, "run_job", _a(lambda *a, **k: "== run localhost/app:abc\noutput\n"))
    slot = yard.store.slot("nightly")
    try:
        outcome = asyncio.run(deploy.deploy(yard, slot, "abc"))
        assert outcome.status == "ok"
        assert recorder.calls[0]["command"] == "build"
        assert "output" in outcome.log
    finally:
        yard.store.close()


def test_run_slot_starts_a_down_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path)
    _slot(yard)
    _tree(yard, "heldso")
    _pod(yard)
    calls: dict[str, str] = {}

    async def fake_apply(config, slot, command, project, **kw):
        calls["command"] = command
        return 0, "started\n"

    monkeypatch.setattr(deploy.compose, "apply", fake_apply)
    try:
        assert asyncio.run(yard.run_slot("heldso")) == "started\n"
        assert calls["command"] == "start"
    finally:
        yard.store.close()


def test_run_deploy_on_a_push_slot_does_not_deadlock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # run_deploy holds the slot lock; the push run must not take it again.
    yard = _yard(tmp_path)
    _slot(yard, name="nightly", trigger="push")
    _tree(yard, "nightly")
    recorder = _stub(monkeypatch, tmp_path)
    _pod(yard)
    monkeypatch.setattr(deploy, "run_job", _a(lambda *a, **k: "== run localhost/app:abc\noutput\n"))
    monkeypatch.setattr("app.git.resolve", _a(lambda *a, **k: "abc"))
    try:
        outcome = asyncio.run(asyncio.wait_for(yard.run_deploy("nightly", "abc"), 5))
        assert outcome.status == "ok"
        assert recorder.calls[0]["command"] == "build"
        assert "output" in outcome.log
    finally:
        yard.store.close()
