"""Git forges: Cursor Origin and GitHub Apps. One app key, one installation, one token each.

state.json is the truth. The files are read on every access, so a setup run while
the server is up takes effect without a restart. Responses never echo a key, a
secret, or a token back out.
"""

import asyncio
import base64
import hashlib
import hmac
import json  # stdlib: JWT signing bytes and the sorted state file need a stable byte format.
import time
import urllib.error
import urllib.request
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from stario import json as stario_json

from app.shared import YardError, atomic_write
from app.shared import parse as _parse_stamp

_STATE = "state.json"
_KEY = "key.pem"


class Forge:
    """One forge app for this host. Subclasses name the API and sign the JWT."""

    name = "forge"
    host = ""
    api = ""
    reports = False

    def __init__(self, dir: Path, api: str = "") -> None:
        self.dir = dir
        self.api = (api or self.api).rstrip("/")
        self.last_delivery = ""
        self.last_check = ""
        self.last_report = ""
        self.last_report_error = ""
        self._token = ""
        self._token_until = 0.0

    def _state(self) -> dict:
        try:
            data = json.loads((self.dir / _STATE).read_text())
        except OSError, json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    @property
    def app_id(self) -> str:
        return str(self._state().get("app_id") or "")

    @property
    def installation_id(self) -> str:
        return str(self._state().get("installation_id") or "")

    @property
    def account(self) -> str:
        """The owner the installation lives on: GitHub's account, Origin's target."""
        return str(self._state().get("account") or "")

    def save(self, drop: Iterable[str] = (), **fields: str) -> None:
        """Merge fields into state.json. The write is atomic."""
        state = {**self._state(), **{key: value for key, value in fields.items() if value}}
        for key in drop:
            state.pop(key, None)
        atomic_write(self.dir / _STATE, json.dumps(state, indent=2, sort_keys=True) + "\n")

    def connected(self) -> bool:
        return (self.dir / _KEY).is_file() and bool(self.app_id) and bool(self.installation_id)

    def owns(self, repo: str) -> bool:
        """True when the installation's account owns the repo's owner.

        An empty account means the host installed before accounts were saved;
        keep reporting as before so nothing regresses.
        """
        account = self.account
        if not account:
            return True
        owner = urlsplit(repo).path.strip("/").split("/", 1)[0]
        return owner.lower() == account.lower()

    def selection(self, installation: dict) -> str:
        """all or selected. Subclasses read their own field."""
        return ""

    def summary(self) -> dict:
        """What the API reports. Never a key, a secret, or a token."""
        return {
            "name": self.name,
            "host": self.host,
            "connected": self.connected(),
            "app_id": self.app_id,
            "installation_id": self.installation_id,
            "selection": str(self._state().get("selection") or ""),
            "last_delivery": self.last_delivery,
            "last_check": self.last_check,
        }

    def app_jwt(self) -> str:
        """A signed JWT the forge checks against the app key. Subclasses implement it."""
        raise NotImplementedError

    def _headers(self) -> dict[str, str]:
        return {}

    async def _call(self, method: str, path: str, bearer: str, body: dict | None = None) -> object:
        return await asyncio.to_thread(self._request, method, path, bearer, body)

    def _request(self, method: str, path: str, bearer: str, body: dict | None) -> object:
        data = None if body is None else stario_json.dumps(body).encode()
        request = urllib.request.Request(
            self.api + path,
            data=data,
            method=method,
            headers={
                "authorization": f"Bearer {bearer}",
                "content-type": "application/json",
                **self._headers(),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                text = response.read().decode()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace").strip()[:400]
            raise YardError(502, self.name, f"{self.name} {method} {path}: {exc.code} {detail}".rstrip()) from exc
        except urllib.error.URLError as exc:
            raise YardError(502, self.name, f"{self.name} {method} {path}: {exc.reason}") from exc
        try:
            return stario_json.loads(text) if text.strip() else {}
        except ValueError:
            return {}

    async def get_app(self) -> dict:
        body = await self._call("GET", "/app", self.app_jwt())
        return body if isinstance(body, dict) else {}

    async def find_installation(self) -> dict | None:
        """The first installation. Saving its id is a side effect."""
        body = await self._call("GET", "/app/installations", self.app_jwt())
        found = body.get("installations") if isinstance(body, dict) else body
        if not isinstance(found, list) or not found or not isinstance(found[0], dict):
            return None
        installation = found[0]
        account = _account(installation)
        # A new installation can mean a new owner; a missing field clears the old one.
        self.save(
            installation_id=str(installation.get("id") or ""),
            account=account,
            drop=() if account else ("account",),
        )
        return installation

    async def token(self) -> str:
        """An installation access token. Cached until a minute before it expires."""
        if self._token and self._token_until - 60 > time.time():
            return self._token
        installation = self.installation_id
        if not installation:
            raise YardError(
                502,
                self.name,
                f"{self.name} has no installation",
                help_text=f"Run yard forge setup {self.name}.",
            )
        body = await self._call("POST", f"/app/installations/{installation}/access_tokens", self.app_jwt(), {})
        data = body if isinstance(body, dict) else {}
        token = str(data.get("token") or "")
        if not token:
            raise YardError(502, self.name, f"{self.name} returned no token")
        self._token = token
        moment = _parse_stamp(str(data.get("expiresAt") or data.get("expires_at") or ""))
        self._token_until = moment.timestamp() if moment else 0.0
        return self._token

    async def report(self, repo: str, sha: str, state: str, description: str, target_url: str = "") -> None:
        """A commit status on the forge. Only a forge with reports = True posts one."""


class Origin(Forge):
    """api.cursor.com. Yard makes the Ed25519 key; EdDSA signs the JWT."""

    name = "origin"
    host = "origin.cursor.com"
    api = "https://api.cursor.com/v1/origin"

    def _key(self) -> Ed25519PrivateKey:
        path = self.dir / _KEY
        if not path.is_file():
            private = Ed25519PrivateKey.generate()
            atomic_write(
                path,
                private.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                ).decode(),
            )
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise YardError(
                502,
                "origin",
                "key.pem is not an Ed25519 key",
                help_text="Delete the bad key.pem and run yard forge setup origin.",
            )
        return key

    def summary(self) -> dict:
        summary = super().summary()
        # The public key is safe to show; first read creates the keypair.
        summary["public_key"] = self.public_key()
        return summary

    def public_key(self) -> str:
        """The PEM public key. Creates the keypair on first call."""
        return (
            self._key()
            .public_key()
            .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
            .decode()
        )

    def _jwt(self, app_id: str) -> str:
        now = int(time.time())
        header = _b64(json.dumps({"alg": "EdDSA", "kid": app_id, "typ": "JWT"}, separators=(",", ":")).encode())
        claims = _b64(
            json.dumps(
                {"iss": app_id, "aud": "origin-apps", "iat": now, "exp": now + 600},
                separators=(",", ":"),
            ).encode()
        )
        sig = _b64(self._key().sign(f"{header}.{claims}".encode()))
        return f"{header}.{claims}.{sig}"

    def app_jwt(self) -> str:
        return self._jwt(self.app_id)

    def selection(self, installation: dict) -> str:
        return str(installation.get("repoSelectionMode") or installation.get("repo_selection_mode") or "")

    async def connect(self, app_id: str) -> dict:
        """Save the app id only when GET /app answers for it and the existing key."""
        info = await self.get_app_for(app_id)
        self.save(app_id=app_id, drop=("installation_id", "account"))
        self._token = ""
        self._token_until = 0.0
        return info

    async def get_app_for(self, app_id: str) -> dict:
        body = await self._call("GET", "/app", self._jwt(app_id))
        return body if isinstance(body, dict) else {}

    async def repos(self) -> list[str]:
        """The repo full names the installation can see. Follows the page token."""
        token = await self.token()
        found: list[str] = []
        page = ""
        while True:
            path = "/installation/repos?pageSize=100"
            if page:
                path += f"&pageToken={page}"
            body = await self._call("GET", path, token)
            items = (body.get("repositories") or body.get("repos") or []) if isinstance(body, dict) else []
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict) and item.get("fullName"):
                        found.append(str(item["fullName"]))
            page = str(body.get("nextPageToken") or body.get("next_page_token") or "") if isinstance(body, dict) else ""
            if not page:
                return found


class GitHub(Forge):
    """api.github.com. The user supplies the RSA key and the webhook secret."""

    name = "github"
    host = "github.com"
    api = "https://api.github.com"
    reports = True

    def _headers(self) -> dict[str, str]:
        # GitHub refuses a request with no User-Agent.
        return {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "yard",
        }

    def _key(self) -> rsa.RSAPrivateKey:
        try:
            key = serialization.load_pem_private_key((self.dir / _KEY).read_bytes(), password=None)
        except (OSError, ValueError) as exc:
            raise YardError(
                502,
                "github",
                "key.pem is missing or does not parse",
                help_text="Run yard forge setup github.",
            ) from exc
        if not isinstance(key, rsa.RSAPrivateKey):
            raise YardError(
                502,
                "github",
                "key.pem is not an RSA key",
                help_text="Run yard forge setup github.",
            )
        return key

    def _jwt(self, app_id: str, key: rsa.RSAPrivateKey) -> str:
        now = int(time.time())
        header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
        claims = _b64(
            json.dumps(
                {"iat": now - 60, "exp": now + 540, "iss": int(app_id)},
                separators=(",", ":"),
            ).encode()
        )
        sig = _b64(key.sign(f"{header}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256()))
        return f"{header}.{claims}.{sig}"

    def app_jwt(self) -> str:
        app_id = self.app_id
        if not app_id.isdigit():
            raise YardError(
                502,
                "github",
                "state.json has a bad app id",
                help_text="Run yard forge setup github.",
            )
        return self._jwt(app_id, self._key())

    def selection(self, installation: dict) -> str:
        return str(installation.get("repository_selection") or "")

    def summary(self) -> dict:
        summary = super().summary()
        # The slug names the public app page; it is not a secret.
        summary["slug"] = str(self._state().get("slug") or "")
        return summary

    async def connect(self, app_id: str, private_key: str, webhook_secret: str) -> dict:
        """Verify the new key against GET /app before anything is saved."""
        if not app_id.isdigit():
            raise YardError(400, "bad_app", "the app id is digits", help_text="The App ID on the app page.")
        if len(private_key.encode()) > 16 * 1024:
            raise YardError(
                400, "bad_key", "the private key is too large", help_text="Paste the key the app page gives."
            )
        try:
            key = serialization.load_pem_private_key(private_key.encode(), password=None)
        except ValueError as exc:
            raise YardError(
                400, "bad_key", "the private key does not parse", help_text="Paste the whole PEM, BEGIN to END."
            ) from exc
        if not isinstance(key, rsa.RSAPrivateKey):
            raise YardError(
                400, "bad_key", "the private key is not an RSA key", help_text="The app page gives an RSA key."
            )
        if len(webhook_secret) < 32:
            raise YardError(
                400,
                "bad_secret",
                "the webhook secret is too short",
                help_text="At least 32 characters, from the app page.",
            )
        body = await self._call("GET", "/app", self._jwt(app_id, key))
        info = body if isinstance(body, dict) else {}
        atomic_write(self.dir / _KEY, private_key if private_key.endswith("\n") else private_key + "\n")
        self.save(
            app_id=app_id,
            slug=str(info.get("slug") or ""),
            webhook_secret=webhook_secret,
            drop=("installation_id", "account"),
        )
        self._token = ""
        self._token_until = 0.0
        return info

    def verify(self, body: bytes, header: str) -> bool:
        """X-Hub-Signature-256 over the raw body. No secret means no signature is good."""
        secret = str(self._state().get("webhook_secret") or "")
        if not secret:
            return False
        digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(f"sha256={digest}", header)

    async def report(self, repo: str, sha: str, state: str, description: str, target_url: str = "") -> None:
        """POST /repos/<owner>/<name>/statuses/<sha> with the installation token."""
        full = urlsplit(repo).path.strip("/").removesuffix(".git")
        body: dict[str, str] = {
            "state": state,
            "description": description[:140],
            "context": "yard",
        }
        if target_url:
            body["target_url"] = target_url
        await self._call("POST", f"/repos/{full}/statuses/{sha}", await self.token(), body)


def _account(installation: dict) -> str:
    """GitHub answers account.login; Origin answers target.slug."""
    account = installation.get("account")
    if isinstance(account, dict) and account.get("login"):
        return str(account["login"])
    target = installation.get("target")
    if isinstance(target, dict):
        return str(target.get("slug") or "")
    return ""


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
