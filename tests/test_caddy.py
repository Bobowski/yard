import json
from pathlib import Path

import pytest

from app.caddy import (
    autosave_route_json,
    check_window,
    curl_words,
    document,
    find_route,
    normalize_dial,
    proxy_route,
    read_routes,
    routes_from_autosave,
    split_curl,
)
from app.errors import YardError


def test_autosave_routes(tmp_path: Path):
    path = tmp_path / "autosave.json"
    path.write_text(
        json.dumps(
            {
                "apps": {
                    "http": {
                        "servers": {
                            "yard": {
                                "routes": [
                                    {
                                        "@id": "yard-heldso.example.com",
                                        "match": [{"host": ["heldso.example.com"]}],
                                        "handle": [{"upstreams": [{"dial": "heldso:8000"}]}],
                                    }
                                ]
                            }
                        }
                    }
                }
            }
        )
    )
    found = routes_from_autosave(path)
    assert found == [{"host": "heldso.example.com", "dial": "heldso:8000", "id": "yard-heldso.example.com"}]
    assert '"heldso.example.com"' in autosave_route_json(path)


def test_curl_words_carry_the_status():
    words = curl_words("PUT", "/id/yard-heldso.example.com", True)
    assert words[0] == "curl"
    assert words[-1] == "http://127.0.0.1:2019/id/yard-heldso.example.com"
    assert "--data-binary" in words
    code, body = split_curl('{"ok":true}\n__YARD_200')
    assert code == 200
    assert body == b'{"ok":true}'


def test_document():
    text = document().decode()
    body = proxy_route("heldso.example.com", "heldso:8000", "30s")
    assert body["match"] == [{"host": ["heldso.example.com"]}]
    for part in (
        '"https_port": 8443',
        '":8443"',
        '"disable_redirects": true',
        '"disabled": true',
    ):
        assert part in text
    assert "caddy.sock" not in text


def test_the_committed_caddy_json_is_document_empty():
    spec = (Path(__file__).resolve().parents[1] / "ops" / "caddy.json").read_bytes()
    assert spec == document()


def test_compose_spec_matches_the_document():
    spec = (Path(__file__).resolve().parents[1] / "ops" / "compose.yml").read_text()
    for part in (
        "  app:",
        "  caddy:",
        "${YARD_ROOT}/projects/yard/caddy/config:/config",
        "${YARD_ROOT}/projects/yard/caddy/data:/data",
        "${CHECKOUT}/ops/caddy.json:/etc/caddy/yard.json:ro",
        'command: ["run", "--config", "/etc/caddy/yard.json", "--resume"]',
        "8443:8443",
        "127.0.0.1:8000:8000",
        "CONTAINER_HOST: unix:///run/podman/podman.sock",
    ):
        assert part in spec
    assert "COMPOSE_ENV_FILE" not in spec
    # Compose names the containers yard-app-1 and yard-caddy-1 like every slot.
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
    assert proxy["lb_try_duration"] == "30s"
    assert proxy["upstreams"] == [{"dial": "heldso:8000"}]


def test_find_route_uses_the_array_index():
    raw = json.dumps(
        [
            {"match": [{"host": ["other.example.com"]}]},
            {"@id": "yard-heldso.example.com", "match": [{"host": ["heldso.example.com"]}], "handle": []},
        ]
    )
    assert find_route(raw, "heldso.example.com") == (1, "yard-heldso.example.com")
    assert find_route(raw, "other.example.com") == (0, "")
    assert find_route(raw, "missing.example.com") is None
    assert read_routes(raw)[0]["id"] == ""
