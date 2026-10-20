import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from typing import cast

import pytest

from app.server import binds, caddy
from app.server.podman import Podman
from app.server.store import Config, Store
from app.server.yard import Yard
from app.shared import YardError
from tests.server.conftest import FakeCaddy


def _yard(tmp_path: Path, routes: list | None = None, ps: str = "[]") -> Yard:
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

    class _Fake(Podman):
        async def run(
            self, *args: str, timeout: float | None = 20, env: dict | None = None, stdin: bytes | None = None
        ) -> str:
            if args[:2] == ("ps", "-a"):
                return ps
            return "[]"

    yard.podman = _Fake("podman", False)
    yard.caddy = FakeCaddy(routes)
    return yard


def test_put_posts_a_new_host(tmp_path: Path):
    yard = _yard(tmp_path)
    found = asyncio.run(binds.put_bind(yard, "heldso.example.com", "http://heldso/x", ""))
    assert asdict(found) == {
        "host": "heldso.example.com",
        "dial": "heldso:8000",
        "id": "yard-heldso.example.com",
        "window": "10s",
        "state": "absent",
    }
    # The find check reads the route array first; the write follows.
    calls = [(m, p) for m, p, _b in _calls(yard)]
    assert ("GET", caddy.ROUTES_PATH) in calls
    assert ("POST", caddy.ROUTES_PATH) in calls
    body = next(body or b"" for m, _p, body in _calls(yard) if m == "POST")
    assert json.loads(body)["@id"] == "yard-heldso.example.com"


def test_put_replaces_a_stored_id(tmp_path: Path):
    routes = [{"@id": "yard-heldso.example.com", "match": [{"host": ["heldso.example.com"]}]}]
    yard = _yard(tmp_path, routes)
    asyncio.run(binds.put_bind(yard, "heldso.example.com", "heldso:9000", "30s"))
    calls = [(m, p) for m, p, _b in _calls(yard)]
    assert ("GET", caddy.ROUTES_PATH) in calls
    assert ("PATCH", "/id/yard-heldso.example.com") in calls
    assert _routes_for(yard, "heldso.example.com") == [
        {"host": "heldso.example.com", "dial": "heldso:9000", "id": "yard-heldso.example.com", "try": "30s"}
    ]


def test_put_twice_replaces_under_a_stored_id(tmp_path: Path):
    routes = [{"@id": "yard-heldso.example.com", "match": [{"host": ["heldso.example.com"]}]}]
    yard = _yard(tmp_path, routes)
    asyncio.run(binds.put_bind(yard, "heldso.example.com", "heldso:9000", "30s"))
    asyncio.run(binds.put_bind(yard, "heldso.example.com", "heldso:9100", "45s"))
    assert _routes_for(yard, "heldso.example.com") == [
        {"host": "heldso.example.com", "dial": "heldso:9100", "id": "yard-heldso.example.com", "try": "45s"}
    ]


def test_put_adopts_a_route_without_an_id(tmp_path: Path):
    routes = [
        {"match": [{"host": ["other.example.com"]}]},
        {"match": [{"host": ["heldso.example.com"]}]},
    ]
    yard = _yard(tmp_path, routes)
    asyncio.run(binds.put_bind(yard, "heldso.example.com", "heldso", "10s"))
    calls = [(m, p) for m, p, _b in _calls(yard)]
    assert ("PATCH", f"{caddy.ROUTES_PATH}/1") in calls
    assert _routes_for(yard, "heldso.example.com") == [
        {"host": "heldso.example.com", "dial": "heldso:8000", "id": "yard-heldso.example.com", "try": "10s"}
    ]
    # The other route is untouched, at its index.
    assert routes[0]["match"] == [{"host": ["other.example.com"]}]


def test_put_twice_keeps_one_route_adopted_by_index(tmp_path: Path):
    routes = [{"match": [{"host": ["heldso.example.com"]}]}]
    yard = _yard(tmp_path, routes)
    asyncio.run(binds.put_bind(yard, "heldso.example.com", "heldso:9000", "30s"))
    asyncio.run(binds.put_bind(yard, "heldso.example.com", "heldso:9100", "45s"))
    assert _routes_for(yard, "heldso.example.com") == [
        {"host": "heldso.example.com", "dial": "heldso:9100", "id": "yard-heldso.example.com", "try": "45s"}
    ]
    calls = [(m, p) for m, p, _b in _calls(yard)]
    assert ("PATCH", f"{caddy.ROUTES_PATH}/0") in calls
    assert ("PATCH", "/id/yard-heldso.example.com") in calls


def test_delete_uses_the_id(tmp_path: Path):
    routes = [{"@id": "yard-heldso.example.com", "match": [{"host": ["heldso.example.com"]}]}]
    yard = _yard(tmp_path, routes)
    host = asyncio.run(binds.delete_bind(yard, "heldso.example.com"))
    assert host == "heldso.example.com"
    assert ("DELETE", "/id/yard-heldso.example.com") in [(m, p) for m, p, _b in _calls(yard)]
    with pytest.raises(YardError) as caught:
        asyncio.run(binds.delete_bind(yard, "missing.example.com"))
    assert caught.value.code == "not_found"


def test_list_marks_the_container(tmp_path: Path):
    routes = [
        {
            "@id": "yard-heldso.example.com",
            "match": [{"host": ["heldso.example.com"]}],
            "handle": [{"upstreams": [{"dial": "heldso-app-1:8000"}]}],
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
    ]
    ps = json.dumps(
        [
            {"Names": ["heldso-app-1"], "State": "running", "Id": "a"},
            {"Names": ["other-app"], "State": "exited", "Id": "b"},
        ]
    )
    yard = _yard(tmp_path, routes, ps)
    rows = {row.host: row.state for row in asyncio.run(binds.list_binds(yard))}
    assert rows == {
        "heldso.example.com": "up",
        "other.example.com": "down",
        "gone.example.com": "absent",
    }


def test_list_matches_the_container_name(tmp_path: Path):
    routes = [
        {
            "@id": "yard-honorata.example.com",
            "match": [{"host": ["honorata.example.com"]}],
            "handle": [{"upstreams": [{"dial": "honorata-app-1:8000"}]}],
        },
        {
            "@id": "yard-yard.example.com",
            "match": [{"host": ["yard.example.com"]}],
            "handle": [{"upstreams": [{"dial": "yard-app-1:8000"}]}],
        },
    ]
    ps = json.dumps(
        [
            {
                "Names": ["honorata-app-1"],
                "State": "running",
                "Id": "a",
                "Labels": {
                    "com.docker.compose.project": "honorata",
                    "com.docker.compose.service": "app",
                },
            },
            {
                "Names": ["yard-app-1"],
                "State": "exited",
                "ExitCode": 1,
                "Id": "b",
                "Labels": {"com.docker.compose.project": "yard", "com.docker.compose.service": "app"},
            },
            {
                "Names": ["yard-caddy-1"],
                "State": "running",
                "Id": "c",
                "Labels": {"com.docker.compose.project": "yard", "com.docker.compose.service": "caddy"},
            },
        ]
    )
    yard = _yard(tmp_path, routes, ps)
    rows = {row.host: row.state for row in asyncio.run(binds.list_binds(yard))}
    assert rows == {"honorata.example.com": "up", "yard.example.com": "down"}


def test_put_keeps_the_other_host(tmp_path: Path):
    routes = [
        {
            "@id": "yard-old.example.com",
            "match": [{"host": ["old.example.com"]}],
            "handle": [{"upstreams": [{"dial": "heldso-app:8000"}]}],
        }
    ]
    yard = _yard(tmp_path, routes)
    found = asyncio.run(binds.put_bind(yard, "new.example.com", "heldso-app", "10s"))
    assert found.host == "new.example.com"
    assert found.dial == "heldso-app:8000"
    assert found.state == "absent"
    calls = [(m, p) for m, p, _b in _calls(yard)]
    assert ("DELETE", "/id/yard-old.example.com") not in calls
    assert ("POST", caddy.ROUTES_PATH) in calls
    assert [row["match"][0]["host"][0] for row in routes] == ["old.example.com", "new.example.com"]


def test_a_dead_socket_is_a_caddy_error(tmp_path: Path):
    yard = _yard(tmp_path)
    yard.caddy = caddy.Caddy(tmp_path / "admin.sock")
    with pytest.raises(YardError) as caught:
        asyncio.run(binds.list_binds(yard))
    assert caught.value.code == "caddy"
    assert caught.value.status == 502


def _calls(yard: Yard) -> list[tuple[str, str, bytes | None]]:
    return cast(FakeCaddy, yard.caddy).calls


def _routes_for(yard: Yard, host: str) -> list[dict[str, str]]:
    rows = caddy.read_routes(json.dumps(cast(FakeCaddy, yard.caddy).routes))
    return [row for row in rows if row["host"] == host]
