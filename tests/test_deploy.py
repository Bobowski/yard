import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app import deploy
from app import yard as yard_mod
from app.config import Config
from app.errors import YardError
from app.podman import Podman
from app.store import Store
from app.yard import Yard


def _yard(tmp_path: Path) -> Yard:
    config = Config(
        root=tmp_path,
        caddy_sock=None,
        podman_sock="",
        podman_bin="podman",
        hook_url="http://127.0.0.1:8000",
        domain="",
        skip_deploy=False,
        boot=False,
        remote=False,
    )
    store = Store.open(tmp_path / "db")
    return Yard(config, store)


def _slot(yard: Yard, name: str = "heldso", job: bool = False, image: str = "", trigger: str = "") -> None:
    try:
        yard.store.create_repo("app")
    except YardError:
        pass
    yard.store.create_slot(name, "app", "main", "", "", trigger or ("every 5m" if job else ""))
    if image:
        yard.store.set_image(name, image)


ALIASED = "services:\n  app:\n    networks:\n      yard:\n        aliases:\n          - ${SLOT_NAME}\n"


def _resolved(doc: dict):
    async def fake(compose_file, env):
        return doc

    return fake


def _checkout(tmp_path: Path) -> Path:
    checkout = tmp_path / "checkouts" / "app" / "abc"
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
        self.by_command: dict[str, tuple[int, str]] = {}

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
        return self.by_command.get(command, (self.code, self.text))


def _a(fn):
    async def wrapped(*args, **kw):
        return fn(*args, **kw)

    return wrapped


def _absent_podman(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing container. retire_legacy then leaves the slot alone."""

    async def absent(self, *args, timeout=20, env=None):
        raise YardError(502, "podman", "no such container")

    monkeypatch.setattr(Podman, "run", absent)


def _stub(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, code: int = 0, text: str = OK_LOG) -> _Apply:
    recorder = _Apply(code, text)
    checkout = _checkout(tmp_path)
    monkeypatch.setattr("app.git.checkout", _a(lambda *args, **kw: checkout))
    monkeypatch.setattr("app.compose.apply", recorder)
    _absent_podman(monkeypatch)
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
        assert [call["command"] for call in recorder.calls] == ["build", "up"]
        assert recorder.calls[0]["project"].env["IMAGE"] == "localhost/app:abc"
        assert recorder.calls[1]["previous"] == ""
        assert recorder.calls[1]["detach"] is False

        row = yard.store.last_deploy("heldso")
        assert row is not None
        assert row.status == "ok"
        # The built stamp comes from the apply log, not the clock.
        assert row.built == "2026-10-05T10:00:00Z"
        assert row.ended
        assert "== ready" in row.log
        assert yard.store.slot("heldso").live_image == "localhost/app:abc"

        # A deploy writes no compose file. The env file and the data dir are all it leaves.
        assert sorted(path.name for path in tmp_path.iterdir()) == ["checkouts", "data", "db", "env"]
        assert (tmp_path / "env" / "heldso.env").read_text() == ""
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
    _absent_podman(monkeypatch)
    slot = yard.store.slot("heldso")
    try:
        outcome = asyncio.run(deploy.deploy(yard, slot, "abc"))
        assert outcome.status == "ok"
        assert [call["command"] for call in recorder.calls] == ["build", "up"]
        assert recorder.calls[0]["project"].env["IMAGE"] == "localhost/app:abc"

        # The warnings open the log; the apply output follows.
        row = yard.store.last_deploy("heldso")
        assert row is not None
        assert row.log.splitlines()[:3] == [
            "warning: compose: no service attaches env_file ${APP_ENV}; the slot env is not passed",
            "warning: compose: no service mounts ${DATA_DIR}; the slot has no /data",
            "== build localhost/app:abc",
        ]
        assert outcome.log == row.log
    finally:
        yard.store.close()


def test_deploy_fails_before_apply_without_the_alias(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path)
    _slot(yard, image="localhost/app:old9")
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

        row = yard.store.last_deploy("heldso")
        assert row is not None
        assert row.status == "failed"
        assert row.log == outcome.error + "\n"
        assert yard.store.slot("heldso").live_image == "localhost/app:old9"
    finally:
        yard.store.close()


def test_deploy_apply_failure_keeps_the_previous_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path)
    _slot(yard, image="localhost/app:old9")
    recorder = _stub(monkeypatch, tmp_path)
    recorder.by_command["up"] = (1, "== start localhost/app:abc\nboom\n")
    slot = yard.store.slot("heldso")
    try:
        outcome = asyncio.run(deploy.deploy(yard, slot, "abc"))
        assert outcome.status == "failed"
        assert "apply exited 1" in outcome.error
        assert "boom" in outcome.log
        # The build finished. The start saw the previous image for its revert.
        assert recorder.calls[0]["command"] == "build"
        assert recorder.calls[1]["command"] == "up"
        assert recorder.calls[1]["previous"] == "localhost/app:old9"

        row = yard.store.last_deploy("heldso")
        assert row is not None
        assert row.status == "failed"
        assert yard.store.slot("heldso").live_image == "localhost/app:old9"
    finally:
        yard.store.close()


def test_deploy_removes_a_legacy_container_and_copies_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path)
    _slot(yard)
    yard.store._write(
        "UPDATE slots SET env_json = ? WHERE name = ?",
        (json.dumps({"SESSION_SECRET": "s3cret"}), "heldso"),
    )
    recorder = _stub(monkeypatch, tmp_path)
    seen: list[tuple[str, ...]] = []

    async def fake_run(self, *args, timeout=20, env=None):
        seen.append(args)
        if args[:1] == ("inspect",):
            return '{"yard.slot": "heldso"}'
        return ""

    monkeypatch.setattr(Podman, "run", fake_run)
    slot = yard.store.slot("heldso")
    try:
        outcome = asyncio.run(deploy.deploy(yard, slot, "abc"))
        assert outcome.status == "ok"
        assert "removed heldso" in outcome.log
        assert ("rm", "-f", "-t", "10", "heldso") in seen
        assert [call["command"] for call in recorder.calls] == ["build", "up"]
        text = (tmp_path / "env" / "heldso.env").read_text()
        assert "SESSION_SECRET=" in text
        assert yard.read_env("heldso")["SESSION_SECRET"] == "s3cret"
    finally:
        yard.store.close()


def test_deploy_keeps_a_compose_container_and_a_filled_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path)
    _slot(yard)
    yard.write_env("heldso", {"SESSION_SECRET": "kept"})
    yard.store._write(
        "UPDATE slots SET env_json = ? WHERE name = ?",
        (json.dumps({"SESSION_SECRET": "other"}), "heldso"),
    )
    _stub(monkeypatch, tmp_path)
    seen: list[tuple[str, ...]] = []

    async def fake_run(self, *args, timeout=20, env=None):
        seen.append(args)
        return '{"com.docker.compose.project": "heldso"}'

    monkeypatch.setattr(Podman, "run", fake_run)
    slot = yard.store.slot("heldso")
    try:
        outcome = asyncio.run(deploy.deploy(yard, slot, "abc"))
        assert outcome.status == "ok"
        assert "removed heldso" not in outcome.log
        assert not any(args[:1] == ("rm",) for args in seen)
        assert yard.read_env("heldso")["SESSION_SECRET"] == "kept"
    finally:
        yard.store.close()


def test_deploy_build_failure_leaves_the_legacy_container(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path)
    _slot(yard, image="localhost/app:old9")
    recorder = _stub(monkeypatch, tmp_path, code=1, text="== build localhost/app:abc\nboom\n")
    seen: list[tuple[str, ...]] = []

    async def fake_run(self, *args, timeout=20, env=None):
        seen.append(args)
        return "{}"

    monkeypatch.setattr(Podman, "run", fake_run)
    slot = yard.store.slot("heldso")
    try:
        outcome = asyncio.run(deploy.deploy(yard, slot, "abc"))
        assert outcome.status == "failed"
        assert recorder.calls[0]["command"] == "build"
        assert len(recorder.calls) == 1
        assert seen == []
        assert yard.store.slot("heldso").live_image == "localhost/app:old9"
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
        assert yard.store.slot("nightly").live_image == "localhost/app:abc"
        row = yard.store.last_deploy("nightly")
        assert row is not None
        assert row.built == "2026-10-05T10:00:00Z"
    finally:
        yard.store.close()


def test_deploy_yard_slot_returns_pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path)
    _slot(yard, name="yard", image="localhost/app:old9")
    recorder = _stub(monkeypatch, tmp_path)
    slot = yard.store.slot("yard")
    try:
        outcome = asyncio.run(deploy.deploy(yard, slot, "abc"))
        assert outcome.status == "pending"
        assert recorder.calls[0]["detach"] is True

        # The row stays running. The next process claims it on boot.
        row = yard.store.last_deploy("yard")
        assert row is not None
        assert row.status == "running"
        assert row.ended == ""
    finally:
        yard.store.close()


def test_run_job_uses_the_saved_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path)
    _slot(yard, name="nightly", job=True, image="localhost/app:abc")
    recorder = _stub(monkeypatch, tmp_path)
    slot = yard.store.slot("nightly")
    try:
        asyncio.run(deploy.deploy(yard, slot, "abc"))
        recorder.calls.clear()
        out = asyncio.run(deploy.run_job(yard, slot))
        assert out == OK_LOG
        call = recorder.calls[0]
        assert call["command"] == "job"
        assert call["project"].env["IMAGE"] == "localhost/app:abc"
        assert call["project"].env["SHA"] == "abc"

        # A non-zero apply is a job failure, not silent.
        recorder.code = 7
        with pytest.raises(RuntimeError):
            asyncio.run(deploy.run_job(yard, slot))

        # No saved project means nothing to run.
        yard.store.create_slot("heldso", "app", "main", "", "", "every 5m")
        with pytest.raises(RuntimeError, match="no saved project"):
            asyncio.run(deploy.run_job(yard, yard.store.slot("heldso")))
    finally:
        yard.store.close()


def test_run_deploy_coalesces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path)
    _slot(yard)
    _stub(monkeypatch, tmp_path)
    try:
        # A finished deploy of the same sha is current, not a new build.
        slot = yard.store.slot("heldso")
        asyncio.run(deploy.deploy(yard, slot, "abc"))
        outcome = asyncio.run(yard.run_deploy("heldso", "abc"))
        assert outcome.status == "current"
        assert outcome.image == "localhost/app:abc"

        # A newer tip already on the ref supersedes this call.
        monkeypatch.setattr("app.yard.git.resolve", _a(lambda *args, **kw: "def"))
        outcome = asyncio.run(yard.run_deploy("heldso", "eee"))
        assert outcome.status == "superseded"
        assert outcome.image == "localhost/app:def"
    finally:
        yard.store.close()


def test_boot_claims_the_first_deploy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # install.sh ran `up` by hand; the first boot records that deploy as ok.
    yard = _yard(tmp_path)
    try:
        yard.store.create_repo("yard")
        yard.store.create_slot("yard", "yard", "origin/main", "", "", "")
        monkeypatch.setenv("SHA", "abc")
        monkeypatch.setenv("IMAGE", "localhost/yard:abc")
        asyncio.run(yard._claim_yard_deploy())
        row = yard.store.last_deploy("yard")
        assert row is not None
        assert row.status == "ok"
        assert yard.store.slot("yard").live_image == "localhost/yard:abc"
    finally:
        yard.store.close()


def _pod(yard: Yard, ps: str = "[]") -> None:
    class _Fake(Podman):
        async def run(self, *args: str, timeout=None, env=None) -> str:
            return ps if args[:1] == ("ps",) else ""

    yard.podman = _Fake("podman", False)


def _container(slot: str) -> str:
    return '[{"Names":["' + slot + '-app"],"State":"running","Labels":{"com.docker.compose.project":"' + slot + '"}}]'


def test_run_slot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path)
    _slot(yard, name="nightly", job=True, image="localhost/app:abc")
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
    _slot(yard, name="nightly", job=True, image="localhost/app:abc")
    busy = {"on": False, "count": 0}

    async def fake_job(y, slot):
        busy["on"] = True
        busy["count"] += 1
        await asyncio.sleep(0)
        return "ran\n"

    class _Fake(Podman):
        async def run(self, *args: str, timeout=None, env=None) -> str:
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
    return json.dumps([{"Names": ["yard-apply-nightly-job"], "State": "exited", "Created": created}])


def _clock(monkeypatch: pytest.MonkeyPatch, start: datetime) -> dict:
    now = {"t": start}
    monkeypatch.setattr(yard_mod, "_now", lambda: now["t"])
    return now


def _job_pod(yard: Yard, ps: dict) -> None:
    """Podman whose ps answer the test can change after each run."""

    class _Fake(Podman):
        async def run(self, *args: str, timeout=None, env=None) -> str:
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
    recorder = _stub(monkeypatch, tmp_path)
    _pod(yard)
    monkeypatch.setattr(deploy, "run_job", _a(lambda *a, **k: "== run localhost/app:abc\noutput\n"))
    slot = yard.store.slot("nightly")
    try:
        outcome = asyncio.run(deploy.deploy(yard, slot, "abc"))
        assert outcome.status == "ok"
        assert recorder.calls[0]["command"] == "build"
        assert "output" in outcome.log
        row = yard.store.last_deploy("nightly")
        assert row is not None
        assert "output" in row.log
    finally:
        yard.store.close()


def test_run_slot_starts_a_down_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path)
    _slot(yard)
    yard.store.set_image("heldso", "localhost/app:abc")
    _checkout(tmp_path)
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
