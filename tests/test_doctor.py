import asyncio
import collections
import json
import shutil
from pathlib import Path

import pytest

from app import doctor
from app.config import Config
from app.errors import YardError
from app.podman import Podman
from app.store import Store
from app.yard import Yard

CADDY_PS = (
    '[{"Names":["yard-caddy-1"],"State":"running",'
    '"Labels":{"com.docker.compose.project":"yard","com.docker.compose.service":"caddy"}}]'
)


def _yard(tmp_path: Path, *, sock: bool = True, info: str = "", ps: str = "[]") -> Yard:
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
    yard = Yard(config, Store.open(tmp_path))

    class _Fake(Podman):
        async def run(
            self, *args: str, timeout: float | None = 20, env: dict | None = None, stdin: bytes | None = None
        ) -> str:
            if args and args[0] == "info":
                return info or '{"host":{"cpus":4,"memTotal":1000},"version":{"Version":"5.3.1"}}'
            if args and args[0] == "ps":
                return ps
            return ""

    yard.podman = _Fake("podman", False)
    return yard


def _route(host: str, dial: str) -> dict:
    return {
        "@id": f"yard-{host}",
        "match": [{"host": [host]}],
        "handle": [{"upstreams": [{"dial": dial}]}],
    }


def _save(yard: Yard, routes: list) -> None:
    path = yard.config.autosave()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(routes))


def _run(yard: Yard) -> dict[str, dict]:
    return {check["name"]: check for check in asyncio.run(doctor.run(yard))}


def test_doctor_all_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path, ps=CADDY_PS)
    usage = collections.namedtuple("usage", "total used free")(200, 100, 100)
    monkeypatch.setattr(shutil, "disk_usage", lambda path: usage)
    try:
        yard.config.token_file.write_text("x")
        found = _run(yard)
        assert all(check["ok"] for check in found.values())
        assert found["podman"]["detail"] == "podman 5.3.1"
        assert found["network yard"]["ok"]
        assert found["apply image"]["ok"]
        assert found["caddy"]["detail"] == "running"
        assert found["caddy routes"]["detail"] == "0 routes"
        assert found["disk"]["detail"] == "100B free of 200B"
        assert found["env files"]["ok"]
        assert found["hook token"]["ok"]
    finally:
        yard.store.close()


def test_doctor_skips_the_proxy_checks_without_a_host(tmp_path: Path):
    yard = _yard(tmp_path, sock=False)
    try:
        found = _run(yard)
        assert found["caddy"] == {"name": "caddy", "ok": True, "detail": "skipped: no host"}
        assert found["caddy routes"]["ok"]
        assert not any(name.startswith("dns ") for name in found)
    finally:
        yard.store.close()


def test_doctor_flags_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path)
    try:
        yard.store.create_repo("app")
        yard.store.create_slot("heldso", "app", "main")

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
        assert "heldso" in found["env files"]["detail"]
        assert not found["hook token"]["ok"]
    finally:
        yard.store.close()


def test_doctor_routes_and_certificates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path, ps=CADDY_PS)
    try:
        yard.store.create_repo("app")
        yard.store.create_slot("heldso", "app", "main")
        routes = [_route("heldso.example.com", "heldso:8000")]
        _save(yard, routes)
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
        _save(yard, routes)
        found = _run(yard)
        assert found["caddy routes"]["ok"]
        assert found["caddy routes"]["detail"] == "0 routes"
        assert "dns heldso.example.com" not in found
    finally:
        yard.store.close()


def test_doctor_acme_and_https(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path, ps=CADDY_PS)
    try:
        yard.store.create_repo("app")
        yard.store.create_repo("yard")
        yard.store.create_slot("yard", "yard", "origin/main")
        yard.store.create_slot("heldso", "app", "main")
        _save(
            yard,
            [
                _route("yard.example.com", "yard:8000"),
                _route("heldso.example.com", "heldso:8000"),
            ],
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
        assert found["acme"]["ok"]
        assert found["https yard.example.com"]["detail"] == "HTTP/1.1 200 OK"
        assert found["https heldso.example.com"]["ok"]
        assert sorted(line.split(b" ")[1] for line in sent) == [b"/", b"/health"]
    finally:
        yard.store.close()


def test_doctor_https_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path, ps=CADDY_PS)
    try:
        yard.store.create_repo("app")
        yard.store.create_slot("heldso", "app", "main")
        _save(yard, [_route("heldso.example.com", "heldso:8000")])
        monkeypatch.setattr(doctor.socket, "getaddrinfo", lambda host, port: [(0, 0, 0, "", ("10.0.0.1", 443))])

        def refused(addr, timeout=0):
            raise OSError("connection refused")

        monkeypatch.setattr(doctor.socket, "create_connection", refused)
        found = _run(yard)
        assert not found["acme"]["ok"]
        assert not found["https heldso.example.com"]["ok"]
    finally:
        yard.store.close()
