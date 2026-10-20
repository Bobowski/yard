"""Forges against local HTTP servers. Keys, JWTs, the token cache, paging, and connect."""

import base64
import hashlib
import hmac
import json
import stat
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from app.server.forges import GitHub, Origin
from app.shared import YardError, clone_url


def _send(handler: BaseHTTPRequestHandler, code: int, body: object) -> None:
    data = json.dumps(body).encode()
    handler.send_response(code)
    handler.send_header("content-type", "application/json")
    handler.send_header("content-length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _rfc(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _decode(part: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def _serve(handler) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


class _Box:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.token_seconds = 3600.0
        self.fail_app = False


def _handler(box: _Box):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:
            pass

        def _route(self, method: str) -> None:
            box.calls.append((method, self.path, self.headers.get("authorization", "")))
            if box.fail_app and self.path == "/app":
                _send(self, 500, {"error": "boom"})
                return
            if method == "GET" and self.path == "/app":
                _send(self, 200, {"displayName": "yard", "webhookUrl": "https://yard.example.com/hooks/origin"})
                return
            if method == "GET" and self.path == "/app/installations":
                _send(
                    self,
                    200,
                    {
                        "installations": [
                            {
                                "id": "ins_1",
                                "appId": "app_1",
                                "target": {"slug": "org"},
                                "repoSelectionMode": "all",
                            }
                        ]
                    },
                )
                return
            if method == "POST" and self.path.endswith("/access_tokens"):
                expires = datetime.now(UTC) + timedelta(seconds=box.token_seconds)
                _send(self, 200, {"token": f"oit_{len(box.calls)}", "expiresAt": _rfc(expires)})
                return
            if method == "GET" and self.path.startswith("/installation/repos"):
                if "pageToken=2" in self.path:
                    _send(self, 200, {"repositories": [{"fullName": "org/two"}]})
                else:
                    _send(self, 200, {"repositories": [{"fullName": "org/one"}], "nextPageToken": "2"})
                return
            _send(self, 404, {"error": "nope"})

        def do_GET(self) -> None:
            self._route("GET")

        def do_POST(self) -> None:
            self._route("POST")

    return Handler


@pytest.fixture
def api(tmp_path):
    box = _Box()
    server, url = _serve(_handler(box))
    origin = Origin(tmp_path / "origin", api=url)
    yield origin, box
    server.shutdown()


def _wire(origin: Origin) -> None:
    origin.save(app_id="app_1", installation_id="ins_1")


def test_public_key_is_idempotent_and_private(tmp_path):
    origin = Origin(tmp_path / "origin")
    key = origin.public_key()
    assert key.startswith("-----BEGIN PUBLIC KEY-----")
    pem = tmp_path / "origin" / "key.pem"
    assert stat.S_IMODE(pem.stat().st_mode) == 0o600
    assert origin.public_key() == key
    # A fresh Origin on the same dir reads the same key.
    assert Origin(tmp_path / "origin").public_key() == key


def test_app_jwt(tmp_path):
    origin = Origin(tmp_path / "origin")
    origin.save(app_id="app_1")
    token = origin.app_jwt()
    head, claims, sig = token.split(".")

    assert _decode(head) == {"alg": "EdDSA", "kid": "app_1", "typ": "JWT"}
    body = _decode(claims)
    assert body["iss"] == "app_1"
    assert body["aud"] == "origin-apps"
    assert 0 <= body["exp"] - body["iat"] == 600
    pub = serialization.load_pem_public_key(origin.public_key().encode())
    assert isinstance(pub, Ed25519PublicKey)
    raw_sig = base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4))
    pub.verify(raw_sig, f"{head}.{claims}".encode())


async def test_token_caches_and_refreshes(api):
    origin, box = api
    _wire(origin)
    # A token that dies inside the minute is not cached.
    box.token_seconds = 30
    first = await origin.token()
    assert await origin.token() != first

    # An hour left is cached: one POST serves two calls.
    box.token_seconds = 3600
    second = await origin.token()
    assert second != first
    assert await origin.token() == second
    posts = [call for call in box.calls if call[0] == "POST"]
    assert len(posts) == 3


async def test_find_installation_saves_the_id(api, tmp_path):
    origin, _box = api
    origin.save(app_id="app_1")
    found = await origin.find_installation()
    assert found is not None and found["id"] == "ins_1"
    assert origin.installation_id == "ins_1"
    state = json.loads((tmp_path / "origin" / "state.json").read_text())
    assert state == {"app_id": "app_1", "installation_id": "ins_1"}


async def test_repos_follows_the_page_token(api):
    origin, box = api
    _wire(origin)
    names = await origin.repos()
    assert names == ["org/one", "org/two"]
    paths = [path for _m, path, _a in box.calls if path.startswith("/installation/repos")]
    assert paths == ["/installation/repos?pageSize=100", "/installation/repos?pageSize=100&pageToken=2"]


async def test_a_bad_status_is_a_yard_error(api):
    origin, _box = api
    _wire(origin)
    with pytest.raises(YardError) as caught:
        await origin._call("GET", "/missing", "bearer")
    assert caught.value.status == 502
    assert caught.value.code == "origin"


async def test_origin_connect_saves_after_the_check(api, tmp_path):
    origin, _box = api
    origin.save(app_id="app_old", installation_id="ins_old")
    info = await origin.connect("app_1")
    assert info["displayName"] == "yard"
    assert origin.app_id == "app_1"
    # A new app drops the old installation.
    assert origin.installation_id == ""
    state = json.loads((tmp_path / "origin" / "state.json").read_text())
    assert "installation_id" not in state


async def test_origin_connect_failure_saves_nothing(api):
    origin, box = api
    origin.save(app_id="app_old", installation_id="ins_old")
    box.fail_app = True
    with pytest.raises(YardError):
        await origin.connect("app_new")
    assert origin.app_id == "app_old"
    assert origin.installation_id == "ins_old"


def test_connected_needs_all_three(tmp_path):
    origin = Origin(tmp_path / "origin")
    assert not origin.connected()
    origin.public_key()
    assert not origin.connected()
    origin.save(app_id="app_1")
    assert not origin.connected()
    origin.save(installation_id="ins_1")
    assert origin.connected()


def test_state_is_read_every_time(tmp_path):
    one = Origin(tmp_path / "origin")
    other = Origin(tmp_path / "origin")
    other.save(app_id="app_2", installation_id="ins_2")
    # A setup run wrote state.json while this Origin was already live.
    assert one.app_id == "app_2"
    assert one.installation_id == "ins_2"
    assert not (tmp_path / "origin" / "key.pem").exists()


def test_jwt_expires_soon(tmp_path):
    origin = Origin(tmp_path / "origin")
    origin.save(app_id="app_1")
    _, claims, _ = origin.app_jwt().split(".")
    body = _decode(claims)
    assert body["exp"] - time.time() < 601


def test_summary_has_no_secret(tmp_path):
    origin = Origin(tmp_path / "origin")
    key = origin.public_key()
    origin.save(app_id="app_1", installation_id="ins_1")
    summary = origin.summary()
    assert summary == {
        "name": "origin",
        "host": "origin.cursor.com",
        "connected": True,
        "app_id": "app_1",
        "installation_id": "ins_1",
        "selection": "",
        "last_delivery": "",
        "last_check": "",
        "public_key": key,
    }


# --- GitHub ------------------------------------------------------------------


def _rsa_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def _write_key(dir: Path, pem: str) -> None:
    dir.mkdir(parents=True, exist_ok=True)
    (dir / "key.pem").write_text(pem)


class _GhBox:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []
        self.bodies: list[tuple[str, dict]] = []
        self.token_seconds = 3600.0
        self.fail_app = False


def _github_handler(box: _GhBox):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:
            pass

        def _route(self, method: str) -> None:
            box.calls.append((method, self.path, dict(self.headers)))
            if box.fail_app and self.path == "/app":
                _send(self, 500, {"message": "boom"})
                return
            if method == "GET" and self.path == "/app":
                _send(self, 200, {"id": 42, "slug": "yard-host", "name": "yard-host", "events": ["push"]})
                return
            if method == "GET" and self.path == "/app/installations":
                _send(
                    self,
                    200,
                    [{"id": 101, "app_id": 42, "repository_selection": "all", "account": {"login": "org"}}],
                )
                return
            if method == "POST" and self.path.endswith("/access_tokens"):
                expires = datetime.now(UTC) + timedelta(seconds=box.token_seconds)
                _send(self, 200, {"token": f"ghs_{len(box.calls)}", "expires_at": _rfc(expires)})
                return
            if method == "POST" and self.path.startswith("/repos/") and "/statuses/" in self.path:
                length = int(self.headers.get("content-length") or 0)
                box.bodies.append((self.path, json.loads(self.rfile.read(length) or b"{}")))
                _send(self, 201, {"state": "created"})
                return
            _send(self, 404, {"message": "not found"})

        def do_GET(self) -> None:
            self._route("GET")

        def do_POST(self) -> None:
            self._route("POST")

    return Handler


@pytest.fixture
def gh_api(tmp_path):
    box = _GhBox()
    server, url = _serve(_github_handler(box))
    github = GitHub(tmp_path / "github", api=url)
    yield github, box
    server.shutdown()


SECRET = "w" * 64


def _gh_wire(github: GitHub, tmp_path: Path) -> None:
    _write_key(tmp_path / "github", _rsa_pem())
    github.save(app_id="42", installation_id="101", webhook_secret=SECRET)


def test_github_jwt_is_rs256(tmp_path):
    github = GitHub(tmp_path / "github")
    pem = _rsa_pem()
    _write_key(tmp_path / "github", pem)
    github.save(app_id="42")
    head, claims, sig = github.app_jwt().split(".")
    assert _decode(head) == {"alg": "RS256", "typ": "JWT"}
    body = _decode(claims)
    assert body["iss"] == 42
    assert body["exp"] - body["iat"] == 600
    now = time.time()
    assert body["iat"] <= now - 59 and now + 500 < body["exp"] < now + 601
    pub = serialization.load_pem_private_key(pem.encode(), password=None).public_key()
    assert isinstance(pub, rsa.RSAPublicKey)
    raw_sig = base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4))
    pub.verify(raw_sig, f"{head}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())


def test_github_jwt_rejects_a_bad_saved_app_id(tmp_path):
    """A hand-edited state.json is a YardError, not a stray ValueError."""
    github = GitHub(tmp_path / "github")
    _write_key(tmp_path / "github", _rsa_pem())
    github.save(app_id="abc")
    with pytest.raises(YardError) as caught:
        github.app_jwt()
    assert caught.value.status == 502
    assert caught.value.code == "github"


async def test_github_token_caches_and_sends_headers(gh_api, tmp_path):
    github, box = gh_api
    _gh_wire(github, tmp_path)
    box.token_seconds = 30
    first = await github.token()
    assert await github.token() != first
    box.token_seconds = 3600
    second = await github.token()
    assert await github.token() == second
    for _method, _path, headers in box.calls:
        lower = {str(key).lower(): value for key, value in headers.items()}
        assert lower["user-agent"] == "yard"
        assert lower["x-github-api-version"] == "2022-11-28"
        assert lower["accept"] == "application/vnd.github+json"


async def test_github_installation_saves_the_id(gh_api):
    github, _box = gh_api
    github.save(app_id="42")
    _write_key(github.dir, _rsa_pem())
    found = await github.find_installation()
    assert found is not None and found["id"] == 101
    assert github.installation_id == "101"
    assert github.selection(found) == "all"


async def test_github_connect_saves_after_the_check(gh_api, tmp_path):
    github, _box = gh_api
    github.save(app_id="7", installation_id="99", webhook_secret="old-secret-old-secret-old-secret")
    pem = _rsa_pem()
    info = await github.connect("42", pem, SECRET)
    assert info["slug"] == "yard-host"
    assert stat.S_IMODE((tmp_path / "github" / "key.pem").stat().st_mode) == 0o600
    state = json.loads((tmp_path / "github" / "state.json").read_text())
    assert state["app_id"] == "42"
    assert state["slug"] == "yard-host"
    assert state["webhook_secret"] == SECRET
    assert "installation_id" not in state
    assert github.summary()["slug"] == "yard-host"


async def test_github_connect_failure_saves_nothing(gh_api, tmp_path):
    github, box = gh_api
    github.save(app_id="7", installation_id="99")
    box.fail_app = True
    with pytest.raises(YardError):
        await github.connect("42", _rsa_pem(), SECRET)
    assert not (tmp_path / "github" / "key.pem").exists()
    assert github.app_id == "7"
    assert github.installation_id == "99"


@pytest.mark.parametrize(
    "app_id, pem, secret",
    [
        ("abc", _rsa_pem(), SECRET),  # id is not digits
        ("42", "not a pem", SECRET),  # garbage key
        ("42", "", SECRET),  # empty key
        ("42", _rsa_pem(), "short"),  # short secret
    ],
    ids=["bad_id", "garbage_pem", "empty_pem", "short_secret"],
)
async def test_github_connect_rejects_bad_input(gh_api, tmp_path, app_id, pem, secret):
    github, _box = gh_api
    with pytest.raises(YardError):
        await github.connect(app_id, pem, secret)
    assert not (tmp_path / "github" / "key.pem").exists()
    assert not (tmp_path / "github" / "state.json").exists()


async def test_github_connect_rejects_a_non_rsa_key(gh_api, tmp_path):
    github, _box = gh_api
    ed = (
        Ed25519PrivateKey.generate()
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )
    with pytest.raises(YardError):
        await github.connect("42", ed, SECRET)
    assert not (tmp_path / "github" / "key.pem").exists()


async def test_github_connect_rejects_a_huge_key(gh_api):
    github, _box = gh_api
    pem = _rsa_pem() + "#" * (17 * 1024)
    with pytest.raises(YardError):
        await github.connect("42", pem, SECRET)


def test_github_verify(tmp_path):
    github = GitHub(tmp_path / "github")
    github.save(webhook_secret=SECRET)
    body = b'{"ref": "refs/heads/main"}'
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert github.verify(body, f"sha256={digest}")
    assert not github.verify(body, "sha256=" + "0" * 64)
    assert not github.verify(body, "")
    assert not github.verify(b"tampered", f"sha256={digest}")
    # No secret stored: nothing verifies.
    assert not GitHub(tmp_path / "other").verify(body, f"sha256={digest}")


def test_github_summary_has_no_secret(tmp_path):
    github = GitHub(tmp_path / "github")
    github.save(app_id="42", installation_id="101", webhook_secret=SECRET, slug="yard-host")
    summary = github.summary()
    assert summary["name"] == "github" and summary["host"] == "github.com"
    assert summary["app_id"] == "42"
    assert summary["slug"] == "yard-host"
    assert SECRET not in json.dumps(summary)
    assert "webhook_secret" not in summary
    assert "key" not in summary


async def test_github_url():
    assert clone_url("https://github.com/org/heldso") == "https://github.com/org/heldso.git"


def test_reports_flags(tmp_path):
    from app.server.forges import Forge

    assert Forge.reports is False
    assert Origin.reports is False
    assert GitHub.reports is True


async def test_github_report_posts_a_commit_status(gh_api, tmp_path):
    github, box = gh_api
    _gh_wire(github, tmp_path)
    sha = "d" * 40
    await github.report("https://github.com/org/heldso", sha, "success", "live", "https://heldso.example.com")
    method, path, headers = box.calls[-1]
    assert method == "POST"
    assert path == f"/repos/org/heldso/statuses/{sha}"
    assert {key.lower(): value for key, value in headers.items()}["authorization"].startswith("Bearer ghs_")
    assert box.bodies[-1] == (
        path,
        {
            "state": "success",
            "description": "live",
            "context": "yard",
            "target_url": "https://heldso.example.com",
        },
    )


async def test_github_report_strips_git_truncates_and_skips_the_url(gh_api, tmp_path):
    github, box = gh_api
    _gh_wire(github, tmp_path)
    await github.report("https://github.com/org/heldso.git", "abc123", "pending", "x" * 200)
    _method, path, _headers = box.calls[-1]
    assert path == "/repos/org/heldso/statuses/abc123"
    body = box.bodies[-1][1]
    assert body["description"] == "x" * 140
    assert body["context"] == "yard"
    assert "target_url" not in body


async def test_origin_report_is_a_noop(api):
    origin, box = api
    _wire(origin)
    await origin.report("https://origin.cursor.com/org/heldso", "abc123", "success", "live")
    assert box.calls == []
