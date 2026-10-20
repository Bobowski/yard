import asyncio
import collections
import shutil
import socket
import tempfile
from dataclasses import asdict
from pathlib import Path

import pytest

from app.server import doctor
from app.server.caddy import Caddy
from app.server.forges import Origin
from app.server.podman import Podman
from app.server.store import Config, Store
from app.server.yard import Yard
from app.shared import YardError
from tests.server.conftest import FakeCaddy

CADDY_PS = (
    '[{"Names":["yard-caddy-1"],"State":"running",'
    '"Labels":{"com.docker.compose.project":"yard","com.docker.compose.service":"caddy"}}]'
)


def _journal_sock() -> Path:
    """A real AF_UNIX socket file at a short path. It stays a socket after close."""
    directory = Path(tempfile.mkdtemp(dir="/tmp"))
    path = directory / "journal.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.bind(str(path))
    return path


def _yard(
    tmp_path: Path,
    *,
    sock: bool = True,
    info: str = "",
    ps: str = "[]",
    inspect: str = "[]",
    journal_socket: Path | None = None,
) -> Yard:
    (tmp_path / "journal").mkdir(exist_ok=True)
    journalctl = tmp_path / "journalctl"
    journalctl.write_text("#!/bin/sh\nexit 0\n")
    journalctl.chmod(0o755)
    config = Config(
        root=tmp_path,
        podman_sock="",
        podman_bin="podman",
        skip_deploy=True,
        boot=sock,
        remote=False,
        journal_dir=tmp_path / "journal",
        journalctl_bin=str(journalctl),
        journal_socket=journal_socket or _journal_sock(),
    )
    yard = Yard(config, Store.open(tmp_path))

    class _Fake(Podman):
        async def run(
            self, *args: str, timeout: float | None = 20, env: dict | None = None, stdin: bytes | None = None
        ) -> str:
            if args and args[0] == "info":
                return info or (
                    '{"host":{"cpus":4,"memTotal":1000,"logDriver":"journald",'
                    '"eventLogger":"journald"},"version":{"Version":"5.3.1"}}'
                )
            if args and args[0] == "ps":
                return ps
            if args and args[0] == "inspect":
                return inspect
            return ""

    yard.podman = _Fake("podman", False)
    yard.caddy = FakeCaddy()
    return yard


def _route(host: str, dial: str) -> dict:
    return {
        "@id": f"yard-{host}",
        "match": [{"host": [host]}],
        "handle": [{"upstreams": [{"dial": dial}]}],
    }


def _run(yard: Yard) -> dict[str, dict]:
    return {check["name"]: check for check in map(asdict, asyncio.run(doctor.run(yard)))}


def test_doctor_all_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path, ps=CADDY_PS, inspect=JOURNALD_INSPECT)
    usage = collections.namedtuple("usage", "total used free")(200, 100, 100)
    monkeypatch.setattr(shutil, "disk_usage", lambda path: usage)
    found = _run(yard)
    assert found["podman"]["detail"] == "podman 5.3.1"
    assert found["network yard"]["ok"]
    assert found["apply image"]["ok"]
    assert found["caddy"]["detail"] == "running"
    assert found["caddy admin"]["detail"] == "ok"
    assert found["caddy routes"]["detail"] == "0 routes"
    assert found["podman journald"]["detail"] == "logs journald · events journald"
    assert found["journal"]["ok"]
    assert found["container logs"]["detail"] == "1 containers"
    assert found["yard events"]["ok"]
    assert found["disk"]["detail"] == "100B free of 200B"
    # A forge nothing uses earns no row.
    assert "origin" not in found
    assert "github" not in found


def test_doctor_marks_a_used_forge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path, ps=CADDY_PS)
    yard.store.create_app("heldso", "https://github.com/org/app", "main")
    usage = collections.namedtuple("usage", "total used free")(200, 100, 100)
    monkeypatch.setattr(shutil, "disk_usage", lambda path: usage)
    found = _run(yard)
    assert found["github"]["ok"] is False
    assert found["github"]["detail"] == "not connected; run yard forge setup github"
    assert "origin" not in found


def test_doctor_forge_rows_when_connected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path, ps=CADDY_PS)
    usage = collections.namedtuple("usage", "total used free")(200, 100, 100)
    monkeypatch.setattr(shutil, "disk_usage", lambda path: usage)
    monkeypatch.setattr(Origin, "connected", lambda self: True)

    async def fake_app(self):
        return {"displayName": "yard"}

    async def fake_find(self):
        return {"repoSelectionMode": "all", "target": {"slug": "org"}}

    async def fake_token(self):
        return "tok"

    monkeypatch.setattr(Origin, "get_app", fake_app)
    monkeypatch.setattr(Origin, "find_installation", fake_find)
    monkeypatch.setattr(Origin, "token", fake_token)
    found = _run(yard)
    assert found["origin app"]["detail"] == "yard"
    assert found["origin installation"]["detail"] == "org"
    assert found["origin token"]["ok"]
    assert found["origin webhook"]["detail"] == "none since boot"
    assert found["origin check"]["ok"]


def test_doctor_skips_the_proxy_checks_without_a_host(tmp_path: Path):
    yard = _yard(tmp_path, sock=False)
    found = _run(yard)
    assert found["caddy"] == {"name": "caddy", "ok": True, "detail": "skipped: no host"}
    assert found["caddy admin"]["ok"]
    assert found["caddy routes"]["ok"]
    assert not any(name.startswith("dns ") for name in found)


def test_doctor_flags_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path)
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app", "main")

    class _Dead(Podman):
        async def run(
            self, *args: str, timeout: float | None = 20, env: dict | None = None, stdin: bytes | None = None
        ) -> str:
            raise YardError(502, "podman", "podman is down")

    yard.podman = _Dead("podman", False)
    usage = collections.namedtuple("usage", "total used free")(100, 95, 5)
    monkeypatch.setattr(shutil, "disk_usage", lambda path: usage)
    found = _run(yard)
    assert not found["podman"]["ok"]
    assert not found["network yard"]["ok"]
    assert not found["caddy"]["ok"]
    assert not found["disk"]["ok"]
    assert not found["origin"]["ok"]
    assert "github" not in found


def test_doctor_flags_a_dead_admin_socket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path, ps=CADDY_PS)
    yard.caddy = FakeCaddy()
    usage = collections.namedtuple("usage", "total used free")(200, 100, 100)
    monkeypatch.setattr(shutil, "disk_usage", lambda path: usage)

    class _Down(Caddy):
        def __init__(self) -> None:
            pass

        async def request(self, method: str, path: str, body=None):
            raise YardError(502, "caddy", "caddy GET /config/: no socket")

    yard.caddy = _Down()
    found = _run(yard)
    assert found["caddy admin"]["ok"] is False
    assert found["caddy routes"]["ok"] is False


def test_doctor_routes_and_certificates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path, ps=CADDY_PS)
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app", "main")
    routes = [_route("heldso.example.com", "heldso:8000")]
    yard.caddy = FakeCaddy(routes)
    monkeypatch.setattr(doctor.socket, "getaddrinfo", lambda host, port: [(0, 0, 0, "", ("10.0.0.1", 443))])

    def refused(addr, timeout=0):
        raise OSError("connection refused")

    monkeypatch.setattr(doctor.socket, "create_connection", refused)
    found = _run(yard)
    assert found["caddy routes"]["detail"] == "1 routes"
    assert found["dns heldso.example.com"]["detail"] == "10.0.0.1"
    assert found["certificate heldso.example.com"]["detail"] == "not issued yet"
    assert not found["certificate heldso.example.com"]["ok"]

    # The certificate file ages out of the not-issued state.
    cert = yard.config.caddy_data() / "caddy" / "certificates" / "acme" / "heldso.example.com"
    cert.mkdir(parents=True)
    (cert / "heldso.example.com.crt").write_text("pem")
    found = _run(yard)
    assert found["certificate heldso.example.com"]["ok"]

    # A removed route leaves the count at zero. The host checks stop with it.
    routes.clear()
    found = _run(yard)
    assert found["caddy routes"]["ok"]
    assert found["caddy routes"]["detail"] == "0 routes"
    assert "dns heldso.example.com" not in found


def test_doctor_https(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path, ps=CADDY_PS)
    yard.store.create_app("yard", "https://origin.cursor.com/org/yard", "main")
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app", "main")
    yard.caddy = FakeCaddy(
        [
            _route("yard.example.com", "yard:8000"),
            _route("heldso.example.com", "heldso:8000"),
        ]
    )
    monkeypatch.setattr(doctor.socket, "getaddrinfo", lambda host, port: [(0, 0, 0, "", ("10.0.0.1", 443))])
    sent: list[bytes] = []

    class _Sock:
        def sendall(self, data):
            sent.append(data)

        def recv(self, n):
            return b"HTTP/1.1 200 OK\r\n\r\n"

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class _TLS:
        def wrap_socket(self, raw, server_hostname=""):
            return raw

    monkeypatch.setattr(doctor.socket, "create_connection", lambda addr, timeout=0: _Sock())
    monkeypatch.setattr(doctor.ssl, "create_default_context", lambda: _TLS())
    found = _run(yard)
    assert found["https yard.example.com"]["detail"] == "HTTP/1.1 200 OK"
    assert found["https heldso.example.com"]["ok"]
    assert sorted(line.split(b" ")[1] for line in sent) == [b"/", b"/health"]


def test_doctor_https_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path, ps=CADDY_PS)
    yard.store.create_app("heldso", "https://origin.cursor.com/org/app", "main")
    yard.caddy = FakeCaddy([_route("heldso.example.com", "heldso:8000")])
    monkeypatch.setattr(doctor.socket, "getaddrinfo", lambda host, port: [(0, 0, 0, "", ("10.0.0.1", 443))])

    def refused(addr, timeout=0):
        raise OSError("connection refused")

    monkeypatch.setattr(doctor.socket, "create_connection", refused)
    found = _run(yard)
    assert not found["https heldso.example.com"]["ok"]


JOURNALD_INSPECT = '[{"Name":"/yard-caddy-1","HostConfig":{"LogConfig":{"Type":"journald"}}}]'
K8S_INSPECT = '[{"Name":"/yard-caddy-1","HostConfig":{"LogConfig":{"Type":"k8s-file"}}}]'


def test_doctor_log_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path, ps=CADDY_PS, inspect=JOURNALD_INSPECT)
    usage = collections.namedtuple("usage", "total used free")(200, 100, 100)
    monkeypatch.setattr(shutil, "disk_usage", lambda path: usage)
    found = _run(yard)
    assert found["podman journald"]["detail"] == "logs journald · events journald"
    assert found["journal"]["ok"]
    assert found["container logs"]["detail"] == "1 containers"


def test_doctor_flags_a_wrong_log_driver(tmp_path: Path):
    yard = _yard(
        tmp_path,
        info='{"host":{"logDriver":"k8s-file"}}',
        ps=CADDY_PS,
        inspect=K8S_INSPECT,
    )
    found = _run(yard)
    assert found["podman journald"]["ok"] is False
    assert found["podman journald"]["detail"] == "logs k8s-file · events unset; rerun install.sh"
    assert found["container logs"]["ok"] is False
    assert found["container logs"]["detail"] == "redeploy to switch: yard-caddy-1"


def test_doctor_flags_a_missing_event_logger(tmp_path: Path):
    yard = _yard(tmp_path, info='{"host":{"logDriver":"journald","eventLogger":"file"}}', ps=CADDY_PS)
    found = _run(yard)
    assert found["podman journald"]["ok"] is False
    assert found["podman journald"]["detail"] == "logs journald · events file; rerun install.sh"


def test_doctor_flags_a_missing_journal_socket(tmp_path: Path):
    yard = _yard(tmp_path, ps=CADDY_PS, journal_socket=tmp_path / "no-socket")
    found = _run(yard)
    assert found["yard events"]["ok"] is False
    assert "no socket" in found["yard events"]["detail"]
    plain = tmp_path / "not-a-socket"
    plain.write_text("x")
    yard = _yard(tmp_path, ps=CADDY_PS, journal_socket=plain)
    found = _run(yard)
    assert "not a socket" in found["yard events"]["detail"]


def test_doctor_flags_a_missing_journal(tmp_path: Path):
    config = Config(
        root=tmp_path,
        podman_sock="",
        podman_bin="podman",
        skip_deploy=True,
        boot=True,
        remote=False,
        journal_dir=tmp_path / "no-journal",
        journalctl_bin="journalctl-missing",
    )
    yard = Yard(config, Store.open(tmp_path))
    found = _run(yard)
    assert found["journal"]["ok"] is False
    assert "no journal" in found["journal"]["detail"]
