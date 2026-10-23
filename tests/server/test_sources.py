"""sources: manifest digests, recipe fetches, and the failure modes."""

import pytest

from app.server import sources
from app.shared import YardError


def _fake(calls, responses):
    async def _no():
        raise AssertionError("unused")

    def fetch(url, method, headers, **_kw):
        calls.append((url, method, dict(headers)))
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return fetch


async def test_docker_hub_normalization(monkeypatch):
    calls = []
    digest_headers = {"Docker-Content-Digest": "sha256:" + "a" * 64}
    responses = [
        (
            401,
            {
                "WWW-Authenticate": 'Bearer realm="https://auth.docker.io/token",'
                'service="registry.docker.io",scope="repository:library/postgres:pull"'
            },
            b"",
        ),
        (200, {}, b'{"token":"tok"}'),
        (200, digest_headers, b""),
    ]
    monkeypatch.setattr(sources, "fetch", _fake(calls, responses))
    found = await sources.digest("postgres:17")
    assert found == "sha256:" + "a" * 64
    assert calls[0][0] == "https://registry-1.docker.io/v2/library/postgres/manifests/17"
    assert calls[1][0].startswith("https://auth.docker.io/token?")
    assert calls[2][2]["Authorization"] == "Bearer tok"


async def test_ghcr_host_kept_and_no_auth(monkeypatch):
    calls = []
    monkeypatch.setattr(sources, "fetch", _fake(calls, [(200, {"Docker-Content-Digest": "sha256:" + "b" * 64}, b"")]))
    found = await sources.digest("ghcr.io/you/api:1.4")
    assert found == "sha256:" + "b" * 64
    assert calls[0][0] == "https://ghcr.io/v2/you/api/manifests/1.4"


async def test_pinned_ref_needs_no_request(monkeypatch):
    monkeypatch.setattr(sources, "fetch", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no request")))
    assert await sources.digest("postgres@sha256:" + "c" * 64) == "sha256:" + "c" * 64


async def test_404_is_tag_not_found(monkeypatch):
    calls = []
    monkeypatch.setattr(sources, "fetch", _fake(calls, [(404, {}, b"")]))
    with pytest.raises(YardError, match="not found"):
        await sources.digest("postgres:999")


async def test_docker_io_host_maps_to_registry(monkeypatch):
    calls = []
    monkeypatch.setattr(sources, "fetch", _fake(calls, [(200, {"Docker-Content-Digest": "sha256:" + "e" * 64}, b"")]))
    await sources.digest("docker.io/louislam/uptime-kuma:1")
    assert calls[0][0] == "https://registry-1.docker.io/v2/louislam/uptime-kuma/manifests/1"
