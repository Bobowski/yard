import json
import socketserver
import tempfile
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import cast

import pytest

from app.server.caddy import (
    Caddy,
    normalize_dial,
    proxy_route,
    read_routes,
)
from app.shared import YardError, check_window

OPS = Path(__file__).resolve().parents[2] / "ops"


class _Admin(socketserver.UnixStreamServer):
    """A fake Caddy admin API on a unix socket. `routes` is the live array."""

    routes: list = []
    calls: list = []


class _Handler(BaseHTTPRequestHandler):
    @property
    def _admin(self) -> _Admin:
        return cast("_Admin", self.server)

    def _reply(self, code: int = 200, body=b""):
        self.send_response(code)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("content-length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        if self.path == "/config/apps/http/servers/yard/routes":
            self._reply(200, json.dumps(self._admin.routes).encode())
        elif self.path == "/config/":
            self._reply(200, b"{}")
        else:
            self._reply(404, b"not found")

    def do_PATCH(self):
        self._admin.calls.append(("PATCH", self.path, self._body()))
        self._reply(200, b"{}")

    def do_POST(self):
        self._admin.calls.append(("POST", self.path, self._body()))
        self._reply(201, b"{}")

    def do_PUT(self):
        self._admin.calls.append(("PUT", self.path, self._body()))
        self._reply(200, b"{}")

    def do_DELETE(self):
        self._admin.calls.append(("DELETE", self.path))
        self._reply(200, b"{}")

    def log_message(self, format: str, *args) -> None:
        pass


def _serve(routes: list):
    # AF_UNIX paths cap at ~104 characters. pytest's tmp_path is deeper than that.
    sock = Path(tempfile.mkdtemp(prefix="yd-")) / "s.sock"
    server = _Admin(str(sock), _Handler)
    server.routes = routes
    server.calls = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return Caddy(sock), server


async def test_request_round_trip_on_a_unix_socket(tmp_path: Path):
    client, server = _serve(
        [
            {
                "@id": "yard-heldso.example.com",
                "match": [{"host": ["heldso.example.com"]}],
                "handle": [{"upstreams": [{"dial": "heldso:8000"}]}],
            }
        ],
    )
    code, raw = await client.request("GET", "/config/apps/http/servers/yard/routes")
    assert code == 200
    assert read_routes(raw) == [
        {"host": "heldso.example.com", "dial": "heldso:8000", "id": "yard-heldso.example.com", "try": ""}
    ]
    code, _ = await client.request("POST", "/config/apps/http/servers/yard/routes", b"{}")
    assert code == 201
    assert server.calls[0][0] == "POST"
    server.shutdown()


async def test_request_failures(tmp_path: Path):
    client = Caddy(tmp_path / "missing.sock")
    with pytest.raises(YardError) as caught:
        await client.request("GET", "/config/")
    assert caught.value.status == 502
    assert caught.value.code == "caddy"

    client, _server = _serve([])
    with pytest.raises(YardError) as caught:
        await client.request("GET", "/nope")
    assert caught.value.status == 502
    assert caught.value.code == "caddy"


def test_proxy_route_matches_the_host():
    body = proxy_route("heldso.example.com", "heldso:8000", "30s")
    assert body["match"] == [{"host": ["heldso.example.com"]}]


def test_the_committed_caddy_json():
    doc = json.loads((OPS / "caddy.json").read_text())
    assert doc["admin"] == {"listen": "unix//run/caddy/admin.sock"}
    http = doc["apps"]["http"]
    assert http["https_port"] == 443
    server = http["servers"]["yard"]
    assert server["listen"] == [":443"]
    assert server["automatic_https"] == {"disable_redirects": True}
    assert server["protocols"] == ["h1", "h2", "h3"]
    assert server["routes"] == []
    issuer = doc["apps"]["tls"]["automation"]["policies"][0]["issuers"][0]
    assert issuer["challenges"]["http"]["disabled"] is True


def test_compose_spec_matches_the_document():
    spec = (OPS / "compose.yml").read_text()
    for part in (
        "  app:",
        "  caddy:",
        "${YARD_ROOT}/apps/yard/caddy/config:/config",
        "${YARD_ROOT}/apps/yard/caddy/data:/data",
        "${YARD_ROOT}/apps/yard/caddy/run:/run/caddy",
        "${CHECKOUT}/ops/caddy.json:/etc/caddy/yard.json:ro",
        'command: ["run", "--config", "/etc/caddy/yard.json", "--resume"]',
        "CADDY_ADMIN: unix//run/caddy/admin.sock",
        "8443:443/tcp",
        "8443:443/udp",
        "127.0.0.1:8000:8000",
        "CONTAINER_HOST: unix:///run/podman/podman.sock",
    ):
        assert part in spec
    assert "8443:8443" not in spec
    assert "COMPOSE_ENV_FILE" not in spec
    # Compose names the containers yard-app-1 and yard-caddy-1 like every app.
    assert "container_name" not in spec
    # Compose's own labels name the project and the service. No yard.* label is needed.
    assert "labels:" not in spec


def test_dial_and_window():
    assert normalize_dial("heldso") == "heldso:8000"
    assert normalize_dial("http://heldso:9000/health") == "heldso:9000"
    assert check_window("") == "10s"
    assert check_window("30s") == "30s"
    with pytest.raises(YardError) as caught:
        normalize_dial("bad dial")
    assert caught.value.code == "bad_dial"
    with pytest.raises(YardError) as caught:
        check_window("soon")
    assert caught.value.code == "bad_window"


def test_proxy_route_keeps_one_id():
    body = proxy_route("heldso.example.com", "heldso:8000", "30s")
    assert body["@id"] == "yard-heldso.example.com"
    assert body["match"] == [{"host": ["heldso.example.com"]}]
    handle = body["handle"]
    assert isinstance(handle, list)
    proxy = handle[0]
    assert isinstance(proxy, dict)
    assert proxy["load_balancing"] == {"try_duration": "30s", "try_interval": "250ms"}
    assert proxy["upstreams"] == [{"dial": "heldso:8000"}]


def test_read_routes_marks_a_route_without_an_id():
    raw = json.dumps(
        [
            {"match": [{"host": ["other.example.com"]}]},
            {"@id": "yard-heldso.example.com", "match": [{"host": ["heldso.example.com"]}], "handle": []},
        ]
    )
    rows = read_routes(raw)
    assert rows[0]["id"] == ""
    assert rows[1]["id"] == "yard-heldso.example.com"
