import asyncio
import os
import socket
from pathlib import Path

import pytest

from app.caddy import document, routes, wait_socket
from app.store import Slot


def test_routes():
    heldso = Slot("heldso", "", "", "", "heldso.example.com", "", "", "")
    plain = Slot("plain", "", "", "", "", "", "", "")
    job = Slot("nightly", "", "", "", "nightly.example.com", "", "every 5m", "")
    found = routes([heldso, plain, job], "")
    assert [(route.host, route.dial) for route in found] == [("heldso.example.com", "heldso:8000")]

    found = routes([heldso], "yard.example.com")
    assert [route.host for route in found] == ["heldso.example.com", "yard.example.com"]
    assert found[1].dial == "yard:8000"

    # A slot that already owns the yard domain leaves the list alone.
    owner = Slot("yard", "", "", "", "yard.example.com", "", "", "")
    found = routes([owner], "yard.example.com")
    assert [route.host for route in found] == ["yard.example.com"]


def test_document():
    text = document([]).decode()
    heldso = Slot("heldso", "", "", "", "heldso.example.com", "", "", "")
    body = document(routes([heldso], "")).decode()
    assert '"dial": "heldso:8000"' in body
    assert '"lb_try_duration": "10s"' in body
    assert '"lb_try_interval": "250ms"' in body
    for part in (
        '"listen": "unix//run/caddy/caddy.sock|0600"',
        '"https_port": 8443',
        '":8443"',
        '"disable_redirects": true',
        '"disabled": true',
    ):
        assert part in text or part in body


def test_the_committed_caddy_json_is_document_empty():
    spec = (Path(__file__).resolve().parents[1] / "ops" / "caddy.json").read_bytes()
    assert spec == document([])


def test_compose_spec_matches_the_document():
    spec = (Path(__file__).resolve().parents[1] / "ops" / "compose.yml").read_text()
    for part in (
        "container_name: caddy",
        "${YARD_ROOT}/data/caddy:/config/caddy",
        "${YARD_ROOT}/data/caddy/lib:/data",
        "${YARD_ROOT}/run:/run/caddy",
        "${YARD_ROOT}/run/caddy.json:/etc/caddy/caddy.json:ro",
        'command: ["run", "--config", "/etc/caddy/caddy.json", "--resume"]',
        "8443:8443",
        "127.0.0.1:8000:8000",
        "CONTAINER_HOST: unix:///run/podman/podman.sock",
    ):
        assert part in spec
    assert "COMPOSE_ENV_FILE" not in spec
    # Compose's own labels name the project and the service. No yard.* label is needed.
    assert "yard." not in spec


def test_wait_socket(tmp_path: Path):
    sock = tmp_path / "run" / "caddy.sock"
    with pytest.raises(RuntimeError, match="not ready"):
        asyncio.run(wait_socket(sock, 0.1))

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    short = Path(f"/tmp/yard-test-{os.getpid()}.sock")
    try:
        server.bind(str(short))
        asyncio.run(wait_socket(short, 1))
    finally:
        server.close()
        short.unlink(missing_ok=True)
