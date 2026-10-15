import asyncio
import json
from pathlib import Path

import pytest

from app import status
from app.config import Config
from app.podman import Podman
from app.store import Store
from app.yard import Yard


def _yard(tmp_path: Path, ps: list[dict], stats: str = "[]", *, sock: bool = False, logs: str = "") -> Yard:
    config = Config(
        root=tmp_path,
        podman_sock="",
        podman_bin="podman",
        hook_url="http://127.0.0.1:8000",
        domain="",
        skip_deploy=True,
        boot=sock,
        remote=False,
    )
    store = Store.open(tmp_path)
    yard = Yard(config, store)

    class _Fake(Podman):
        async def run(
            self, *args: str, timeout: float | None = 20, env: dict | None = None, stdin: bytes | None = None
        ) -> str:
            if args and args[0] == "ps":
                return json.dumps(ps)
            if args and args[0] == "stats":
                return stats
            if args and args[0] == "info":
                return '{"host":{"cpus":4,"memTotal":1000},"store":{"graphRootAllocated":2000}}'
            if args and args[0] == "logs":
                return logs
            return "[]"

    yard.podman = _Fake("podman", False)
    return yard


def _app_container(slot: str, name: str, **extra):
    # An app container carries compose's own labels. The project is the slot.
    return {
        "Names": [name],
        "Image": f"localhost/{slot}:abc",
        "State": "running",
        "Labels": {"com.docker.compose.project": slot, "com.docker.compose.service": "app"},
        **extra,
    }


def test_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ps = [
        _app_container("heldso", "heldso-app", Status="Up 2 minutes (healthy)", StartedAt=1710000000),
        _app_container("heldso", "heldso-worker", State="exited", ExitCode=0),
        {
            "Names": ["up-nightly-job"],
            "State": "exited",
            "ExitCode": 0,
        },
        {
            "Names": ["up-heldso"],
            "State": "exited",
        },
        {"Names": ["caddy"], "State": "running"},
    ]
    stats = '[{"Name":"heldso-app","CPUPerc":"1.5%","MemUsage":"20MB / 1GB"}]'
    yard = _yard(tmp_path, ps, stats)
    try:
        yard.store.create_repo("app")
        yard.store.create_slot("heldso", "app", "main")
        yard.store.create_slot("nightly", "app", "main", "", "every 5m")
        yard.store.create_slot("gone", "app", "main")
        _bind(yard, "heldso.example.com", "heldso:8000")
        data = tmp_path / "projects" / "heldso" / "data"
        data.mkdir(parents=True)
        (data / "f").write_bytes(b"x" * 10)

        monkeypatch.setattr("app.status.git.tips", _a(lambda *args, **kw: {"main": "tip9"}))
        report = asyncio.run(status.report(yard))

        assert report["host"] == {
            "cpus": 4,
            "mem": 1000,
            "disk": 2000,
            "cpu_used": 1.5,
            "mem_used": 20_000_000,
            "data_used": 20,  # each heldso row reports the same 10-byte dir
        }
        rows = {row["container"]: row for row in report["rows"]}
        assert rows["heldso-app"]["project"] == "heldso"
        assert rows["heldso-app"]["service"] == "app"
        assert rows["heldso-app"]["running"]
        assert rows["heldso-app"]["health"] == "healthy"
        assert rows["heldso-app"]["cpu"] == "1.5%"
        assert rows["heldso-app"]["sha"] == "abc"
        assert rows["heldso-app"]["tip"] == "tip9"
        assert rows["heldso-app"]["domain"] == "heldso.example.com"
        assert rows["heldso-app"]["data"] == 10
        assert rows["heldso-app"]["deploy"]["status"] == "ok"
        assert rows["heldso-app"]["started"] == "2024-03-09T16:00:00Z"
        assert rows["heldso-worker"]["state"] == "exited"

        # The job container shows on its job slot. Deploy containers are skipped.
        assert rows["up-nightly-job"]["project"] == "nightly"
        assert rows["up-nightly-job"]["trigger"] == "every 5m"
        assert "up-heldso" not in rows

        # A slot with no container gets one absent row.
        absent = [row for row in report["rows"] if row["project"] == "gone"]
        assert len(absent) == 1
        assert absent[0]["state"] == "absent"
        assert absent[0]["container"] == ""
        assert absent[0]["health"] == "down"

        # A container with no labels still shows.
        assert rows["caddy"]["project"] == ""
        assert rows["caddy"]["running"]
    finally:
        yard.store.close()


def test_report_running_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ps = [
        _app_container("heldso", "heldso-app"),
        _app_container("heldso", "heldso-dead", State="exited"),
        {"Names": ["caddy"], "State": "exited"},
    ]
    yard = _yard(tmp_path, ps)
    try:
        yard.store.create_repo("app")
        yard.store.create_slot("heldso", "app", "main")
        yard.store.create_slot("gone", "app", "main")
        monkeypatch.setattr("app.status.git.tips", _a(lambda *args, **kw: {}))
        report = asyncio.run(status.report(yard, running_only=True))
        names = [row["container"] for row in report["rows"]]
        assert names == ["heldso-app"]
    finally:
        yard.store.close()


def _bind(yard: Yard, host: str, dial: str) -> None:
    path = yard.config.autosave()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            [
                {
                    "@id": f"yard-{host}",
                    "match": [{"host": [host]}],
                    "handle": [{"upstreams": [{"dial": dial}]}],
                }
            ]
        )
    )


def _a(fn):
    async def wrapped(*args, **kw):
        return fn(*args, **kw)

    return wrapped


def test_report_probes_running_app_containers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ps = [
        _app_container("heldso", "heldso-app", Restarts=2),
        _app_container("nightly", "nightly-app"),
        _app_container("gone", "gone-app", State="exited"),
    ]
    yard = _yard(tmp_path, ps, sock=True)
    try:
        yard.store.create_repo("app")
        yard.store.create_slot("heldso", "app", "main")
        yard.store.create_slot("nightly", "app", "main", "", "every 5m")
        yard.store.create_slot("gone", "app", "main")
        answered = {"heldso": True}

        async def fake_probe(host: str, port: int, timeout: float = 1.0) -> bool:
            return answered.get(host, False)

        monkeypatch.setattr(status, "probe", fake_probe)
        monkeypatch.setattr("app.status.git.tips", _a(lambda *args, **kw: {}))
        report = asyncio.run(status.report(yard))
        rows = {row["container"]: row for row in report["rows"]}
        assert rows["heldso-app"]["answers"] is True
        assert rows["heldso-app"]["restarts"] == 2
        # A job slot's app service and a stopped container are not probed.
        assert rows["nightly-app"]["answers"] is None
        assert rows["gone-app"]["answers"] is None
    finally:
        yard.store.close()


def test_slot_report_is_the_slot_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    log = "2026-10-05T12:00:00Z one\n2026-10-05T12:00:01Z two\n"
    yard = _yard(tmp_path, [_app_container("heldso", "heldso-app")], sock=True, logs=log)
    try:
        yard.store.create_repo("app")
        yard.store.create_slot("heldso", "app", "main")
        _bind(yard, "heldso.example.com", "heldso:8000")

        async def fake_probe(host: str, port: int, timeout: float = 1.0) -> bool:
            return True

        monkeypatch.setattr(status, "probe", fake_probe)
        monkeypatch.setattr("app.status.git.tips", _a(lambda *args, **kw: {"main": "tip9"}))
        found = asyncio.run(status.slot_report(yard, "heldso"))

        assert found["project"]["name"] == "heldso"
        assert "env" not in found["project"]
        assert found["env_file"].endswith("projects/heldso/env")
        assert found["route"] == {"host": "heldso.example.com", "dial": "heldso:8000"}
        assert [row["container"] for row in found["rows"]] == ["heldso-app"]
        assert found["rows"][0]["answers"] is True
        assert found["deploys"] == []
        assert "two" in found["log"]
    finally:
        yard.store.close()
