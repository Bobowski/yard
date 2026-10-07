"""Property tests for the HTTP API. One fresh Yard per example."""

import asyncio
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from hypothesis import example, given, settings
from hypothesis import strategies as st
from stario.testing import TestClient

from app.main import bootstrap
from app.store import parse_every
from tests.strategies import (
    bad_every,
    bad_repos,
    bad_slots,
    env_keys,
    hostnames,
    repo_names,
    slot_names,
    titles,
)

API = settings(max_examples=12, deadline=None)

_ENV = (
    "YARD_ROOT",
    "YARD_SKIP_DEPLOY",
    "YARD_BOOT",
    "YARD_HOOK_URL",
    "YARD_PODMAN_BIN",
    "YARD_CADDY_SOCK",
    "YARD_DOMAIN",
)


@dataclass(frozen=True, slots=True)
class Surface:
    repo: str
    slot: str
    other: str
    domain: str
    every: str
    ref: str
    env_key: str
    env_value: str
    title: str
    body: str
    label_key: str
    label_value: str
    token_name: str


@st.composite
def surfaces(draw) -> Surface:
    slot, other = draw(st.lists(slot_names(), min_size=2, max_size=2, unique=True))
    repo = draw(st.one_of(st.just(slot), repo_names()))
    token_name = draw(repo_names().filter(lambda name: name != "admin"))
    return Surface(
        repo,
        slot,
        other,
        draw(st.one_of(st.just(""), hostnames())),
        draw(st.sampled_from(["", "5m", "every 5m", "1h", "off", "every 1h"])),
        draw(st.from_regex(r"[a-z][a-z0-9]{0,8}", fullmatch=True)),
        draw(env_keys()),
        draw(st.from_regex(r"[A-Za-z0-9]{1,12}", fullmatch=True)),
        draw(titles()),
        draw(st.from_regex(r"[A-Za-z0-9]{0,24}", fullmatch=True)),
        draw(st.from_regex(r"[a-z][a-z0-9]{0,8}", fullmatch=True).filter(lambda key: key != "id")),
        draw(st.from_regex(r"[A-Za-z0-9]{1,12}", fullmatch=True)),
        token_name,
    )


HELDSO = Surface(
    "heldso",
    "heldso",
    "other",
    "heldso.example.com",
    "every 5m",
    "release",
    "API_TOKEN",
    "secret",
    "Fixlogin",
    "Thecookie",
    "repo",
    "shop",
    "ada",
)


@contextmanager
def _env(root: Path):
    old = {key: os.environ.get(key) for key in _ENV}
    os.environ["YARD_ROOT"] = str(root)
    os.environ["YARD_SKIP_DEPLOY"] = "1"
    os.environ["YARD_BOOT"] = "0"
    os.environ["YARD_HOOK_URL"] = "http://127.0.0.1:8000"
    os.environ["YARD_PODMAN_BIN"] = "yard-podman-missing"
    os.environ.pop("YARD_CADDY_SOCK", None)
    os.environ.pop("YARD_DOMAIN", None)
    try:
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _run(fn, *args) -> None:
    with TemporaryDirectory() as tmp:
        with _env(Path(tmp)):
            asyncio.run(fn(*args))


def _error(res, status: int, code: str) -> None:
    assert res.status_code == status
    body = res.json()
    assert body["error"]["code"] == code
    assert body["error"]["message"]


async def _ask(client, method: str, path: str, token: str = "", body=None, params=None):
    headers = {"authorization": f"Bearer {token}"} if token else {}
    kwargs = {"headers": headers}
    if body is not None:
        kwargs["json"] = body
    if params is not None:
        kwargs["params"] = params
    return await client.request(method, path, **kwargs)


@API
@example(HELDSO)
@given(surfaces())
def test_api_surface(case: Surface) -> None:
    asyncio.run(_surface(case))


async def _surface(case: Surface) -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _env(root):
            async with TestClient(bootstrap) as client:
                await _walk(client, root, case)


async def _walk(client, root: Path, case: Surface) -> None:
    health = await client.get("/health")
    assert health.status_code == 200
    assert health.text == ""
    _error(await _ask(client, "GET", "/api/v1/slots"), 401, "unauthorized")

    token = (root / "data" / "yard" / "admin.token").read_text().strip()
    created = await _ask(client, "POST", "/api/v1/repos", token, {"name": case.repo})
    assert created.status_code == 200
    assert created.json()["name"] == case.repo
    hook = root / "repos" / f"{case.repo}.git" / "hooks" / "post-receive"
    assert "/api/v1/hooks/push" in hook.read_text()
    again = await _ask(client, "POST", "/api/v1/repos", token, {"name": case.repo, "force": True})
    _error(again, 409, "exists")
    assert "already exists" in again.text

    slot = await _ask(
        client,
        "POST",
        "/api/v1/slots",
        token,
        {
            "SLOT_NAME": case.slot,
            "SLOT_REPO": case.repo,
            "SLOT_DOMAIN": case.domain,
            "SLOT_INTERVAL": case.every,
        },
    )
    assert slot.status_code == 200
    values = slot.json()["values"]
    assert values["SLOT_NAME"] == case.slot
    assert values["SLOT_REPO"] == case.repo
    assert values["SLOT_BRANCH"] == "main"
    assert values["SLOT_DOMAIN"] == case.domain
    assert values["SLOT_INTERVAL"] == parse_every(case.every)
    assert slot.json()["deploy"]["status"] == "waiting"

    second = await _ask(
        client,
        "POST",
        "/api/v1/slots",
        token,
        {"SLOT_NAME": case.other, "SLOT_REPO": case.repo, "SLOT_DOMAIN": case.domain},
    )
    if case.domain:
        _error(second, 409, "domain_taken")
    else:
        assert second.status_code == 200
        assert (await _ask(client, "DELETE", f"/api/v1/slots/{case.other}", token)).status_code == 200

    replacement = "zz-other" if case.repo != "zz-other" else "yy-other"
    saved = await _ask(
        client,
        "PUT",
        f"/api/v1/slots/{case.slot}",
        token,
        {"SLOT_BRANCH": case.ref, case.env_key: case.env_value, "SLOT_REPO": replacement},
    )
    assert saved.status_code == 200
    assert "SLOT_REPO stays" in saved.text
    current = (await _ask(client, "GET", f"/api/v1/slots/{case.slot}", token)).json()
    assert current["SLOT_REPO"] == case.repo
    assert current["SLOT_BRANCH"] == case.ref
    assert current[case.env_key] == case.env_value
    _error(await _ask(client, "DELETE", f"/api/v1/repos/{case.repo}", token), 409, "repo_in_use")

    first = await _ask(
        client,
        "POST",
        "/api/v1/experiments",
        token,
        {"title": case.title, "body": case.body, "labels": [{"key": case.label_key, "value": case.label_value}]},
    )
    assert first.status_code == 200
    exp_id = first.json()["id"]
    assert isinstance(exp_id, int) and exp_id > 0
    one = await _ask(client, "GET", f"/api/v1/experiments/{exp_id}", token)
    assert one.json()["title"] == case.title
    assert one.json()["body"] == case.body
    assert one.json()["labels"] == [{"key": case.label_key, "value": case.label_value}]
    second_title = "Slowprobe" if case.title != "Slowprobe" else "Otherprobe"
    other = await _ask(
        client,
        "POST",
        "/api/v1/experiments",
        token,
        {"title": second_title, "body": "nope", "labels": [{"key": case.label_key, "value": case.label_value + "x"}]},
    )
    assert other.status_code == 200
    listed = await _ask(client, "GET", "/api/v1/experiments", token, params={case.label_key: case.label_value})
    assert listed.status_code == 200
    titles_found = [row["title"] for row in listed.json()]
    assert case.title in titles_found
    assert second_title not in titles_found
    missing = await _ask(
        client,
        "PUT",
        "/api/v1/experiments/99",
        token,
        {"title": case.title, "body": "x", "labels": []},
    )
    _error(missing, 404, "not_found")
    assert (await _ask(client, "DELETE", f"/api/v1/experiments/{other.json()['id']}", token)).status_code == 200

    minted = await _ask(client, "POST", "/api/v1/tokens", token, {"name": case.token_name})
    assert minted.status_code == 200
    assert minted.json()["parent"] == "admin"
    child = minted.json()["token"]
    assert (await _ask(client, "GET", "/api/v1/repos", child)).status_code == 200
    _error(await _ask(client, "DELETE", "/api/v1/tokens/admin", child), 403, "forbidden")
    assert (await _ask(client, "DELETE", f"/api/v1/tokens/{case.token_name}", token)).status_code == 200
    _error(await _ask(client, "GET", "/api/v1/repos", child), 401, "unauthorized")
    rows = (await _ask(client, "GET", "/api/v1/tokens", token)).json()
    saved_token = next(row for row in rows if row["name"] == case.token_name)
    assert saved_token["revoked"] is True
    assert saved_token["parent"] == "admin"

    assert (await _ask(client, "DELETE", f"/api/v1/slots/{case.slot}", token)).status_code == 200
    _error(await _ask(client, "GET", f"/api/v1/slots/{case.slot}", token), 404, "not_found")
    assert (await _ask(client, "DELETE", f"/api/v1/repos/{case.repo}", token)).status_code == 200


@API
@example("")
@example("yard")
@example("yard-sampler")
@example("Heldso")
@given(bad_repos())
def test_api_rejects_a_bad_repo_name(name: str) -> None:
    _run(_bad_repo, name)


async def _bad_repo(name: str):
    async with TestClient(bootstrap) as client:
        root = Path(os.environ["YARD_ROOT"])
        token = (root / "data" / "yard" / "admin.token").read_text().strip()
        _error(await _ask(client, "POST", "/api/v1/repos", token, {"name": name}), 400, "bad_name")
        names = [row["name"] for row in (await _ask(client, "GET", "/api/v1/repos", token)).json()]
        assert name not in names


@API
@example("caddy")
@example("yard")
@example("yard-sampler")
@given(bad_slots().filter(lambda name: name.strip() != ""))
def test_api_rejects_a_bad_slot_name(name: str) -> None:
    _run(_bad_slot, name)


async def _bad_slot(name: str):
    async with TestClient(bootstrap) as client:
        root = Path(os.environ["YARD_ROOT"])
        token = (root / "data" / "yard" / "admin.token").read_text().strip()
        assert (await _ask(client, "POST", "/api/v1/repos", token, {"name": "base"})).status_code == 200
        _error(
            await _ask(client, "POST", "/api/v1/slots", token, {"SLOT_NAME": name, "SLOT_REPO": "base"}),
            400,
            "bad_name",
        )
        if name:
            _error(await _ask(client, "GET", "/api/v1/slots/" + name, token), 404, "not_found")


@API
@example("tomorrow")
@example("0s")
@example("500ms")
@given(bad_every())
def test_api_rejects_a_bad_interval(text: str) -> None:
    _run(_bad_interval, text)


async def _bad_interval(text: str):
    async with TestClient(bootstrap) as client:
        root = Path(os.environ["YARD_ROOT"])
        token = (root / "data" / "yard" / "admin.token").read_text().strip()
        assert (await _ask(client, "POST", "/api/v1/repos", token, {"name": "base"})).status_code == 200
        res = await _ask(
            client,
            "POST",
            "/api/v1/slots",
            token,
            {"SLOT_NAME": "base", "SLOT_REPO": "base", "SLOT_INTERVAL": text},
        )
        _error(res, 400, "bad_every")
        _error(await _ask(client, "GET", "/api/v1/slots/base", token), 404, "not_found")
