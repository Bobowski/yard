import asyncio
import json
from pathlib import Path

import pytest

from app import caddy
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
        skip_deploy=True,
        boot=False,
        remote=False,
    )
    return Yard(config, Store.open(tmp_path))


def _save(yard: Yard, routes: list) -> None:
    path = yard.config.autosave()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(routes))


def _curl(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    calls: list[tuple] = []

    async def fake(self, method: str, path: str, body: bytes = b"") -> tuple[int, bytes]:
        calls.append((method, path, body))
        return 200, b""

    monkeypatch.setattr(Yard, "_caddy", fake)
    return calls


def test_caddy_put_posts_a_new_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path)
    calls = _curl(monkeypatch)
    try:
        found = asyncio.run(yard.caddy_put("heldso.example.com", "http://heldso/x", ""))
        assert found == {"host": "heldso.example.com", "dial": "heldso:8000", "id": "yard-heldso.example.com"}
        assert calls[0][0:2] == ("POST", caddy.ROUTES_PATH)
        assert json.loads(calls[0][2])["@id"] == "yard-heldso.example.com"
    finally:
        yard.store.close()


def test_caddy_put_replaces_a_stored_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path)
    _save(yard, [{"@id": "yard-heldso.example.com", "match": [{"host": ["heldso.example.com"]}]}])
    calls = _curl(monkeypatch)
    try:
        asyncio.run(yard.caddy_put("heldso.example.com", "heldso:9000", "30s"))
        assert calls[0][0:2] == ("PUT", "/id/yard-heldso.example.com")
    finally:
        yard.store.close()


def test_caddy_put_adopts_a_route_without_an_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path)
    _save(
        yard,
        [
            {"match": [{"host": ["other.example.com"]}]},
            {"match": [{"host": ["heldso.example.com"]}]},
        ],
    )
    calls = _curl(monkeypatch)
    try:
        asyncio.run(yard.caddy_put("heldso.example.com", "heldso", "10s"))
        assert calls[0][0:2] == ("PUT", f"{caddy.ROUTES_PATH}/1")
        asyncio.run(yard.caddy_delete("heldso.example.com"))
        assert calls[-1][0:2] == ("DELETE", f"{caddy.ROUTES_PATH}/1")
    finally:
        yard.store.close()


def test_caddy_delete_uses_the_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path)
    _save(yard, [{"@id": "yard-heldso.example.com", "match": [{"host": ["heldso.example.com"]}]}])
    calls = _curl(monkeypatch)
    try:
        asyncio.run(yard.caddy_delete("heldso.example.com"))
        assert calls[-1][0:2] == ("DELETE", "/id/yard-heldso.example.com")
        with pytest.raises(YardError) as caught:
            asyncio.run(yard.caddy_delete("missing.example.com"))
        assert caught.value.code == "not_found"
    finally:
        yard.store.close()


def test_caddy_needs_a_container(tmp_path: Path):
    yard = _yard(tmp_path)

    class _Down(Podman):
        async def run(
            self, *args: str, timeout: float | None = 20, env: dict | None = None, stdin: bytes | None = None
        ) -> str:
            return "[]"

    yard.podman = _Down("podman", False)
    try:
        with pytest.raises(YardError) as caught:
            asyncio.run(yard.caddy_put("heldso.example.com", "heldso", ""))
        assert caught.value.code == "no_caddy"
    finally:
        yard.store.close()


def test_route_list_marks_the_container(tmp_path: Path):
    yard = _yard(tmp_path)
    _save(
        yard,
        [
            {
                "@id": "yard-heldso.example.com",
                "match": [{"host": ["heldso.example.com"]}],
                "handle": [{"upstreams": [{"dial": "heldso-app:8000"}]}],
            },
            {
                "@id": "yard-other.example.com",
                "match": [{"host": ["other.example.com"]}],
                "handle": [{"upstreams": [{"dial": "other-app:8000"}]}],
            },
            {
                "@id": "yard-gone.example.com",
                "match": [{"host": ["gone.example.com"]}],
                "handle": [{"upstreams": [{"dial": "gone:8000"}]}],
            },
        ],
    )

    class _Fake(Podman):
        async def run(
            self, *args: str, timeout: float | None = 20, env: dict | None = None, stdin: bytes | None = None
        ) -> str:
            assert args[:2] == ("ps", "-a")
            return json.dumps(
                [
                    {"Names": ["heldso-app-1"], "State": "running", "Id": "a"},
                    {"Names": ["other-app"], "State": "exited", "Id": "b"},
                ]
            )

    yard.podman = _Fake("podman", False)
    try:
        rows = {row["host"]: row["state"] for row in asyncio.run(yard.route_list())}
        assert rows == {
            "heldso.example.com": "up",
            "other.example.com": "down",
            "gone.example.com": "absent",
        }
    finally:
        yard.store.close()


def test_route_put_replaces_the_container(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yard = _yard(tmp_path)
    _save(
        yard,
        [
            {
                "@id": "yard-old.example.com",
                "match": [{"host": ["old.example.com"]}],
                "handle": [{"upstreams": [{"dial": "heldso-app:8000"}]}],
            }
        ],
    )
    calls = _curl(monkeypatch)

    class _Fake(Podman):
        async def run(
            self, *args: str, timeout: float | None = 20, env: dict | None = None, stdin: bytes | None = None
        ) -> str:
            return "[]"

    yard.podman = _Fake("podman", False)
    try:
        found = asyncio.run(yard.route_put("new.example.com", "heldso-app", "10s"))
        assert found["host"] == "new.example.com"
        assert found["dial"] == "heldso-app:8000"
        assert found["state"] == "absent"
        assert ("DELETE", "/id/yard-old.example.com") in [call[:2] for call in calls]
        assert ("POST", caddy.ROUTES_PATH) in [call[:2] for call in calls]
    finally:
        yard.store.close()


def test_caddy_command_needs_words(tmp_path: Path):
    yard = _yard(tmp_path)
    try:
        with pytest.raises(YardError) as caught:
            asyncio.run(yard.caddy_cli([]))
        assert caught.value.code == "bad_args"
    finally:
        yard.store.close()
