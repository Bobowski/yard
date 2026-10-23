"""A public image's manifest digest. HEAD over the registry HTTP API, anonymous tokens only."""

import asyncio
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from app.shared import YardError

_ACCEPT = (
    "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json,"
    " application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json"
)
_TIMEOUT = 30


def fetch(url: str, method: str, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
    """One request, the code/headers/body tuple. Replaced by tests."""
    req = urllib.request.Request(url, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as res:
            return res.status, dict(res.headers), res.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def _parse(ref: str) -> tuple[str, str, str]:
    """repo/name:tag -> (registry host, repo path, tag). Docker Hub gets its defaults."""
    name = ref.split("@", 1)[0]
    first, sep, rest = name.partition("/")
    if sep and ("." in first or ":" in first or first == "localhost"):
        host, repo = first, rest
    else:
        host, repo = "registry-1.docker.io", name
        if "/" not in repo:
            repo = f"library/{repo}"
    last = repo.rsplit("/", 1)[-1]
    if ":" in last:
        repo, tag = repo.rsplit(":", 1)
    else:
        tag = "latest"
    return host, repo, tag


async def digest(ref: str) -> str:
    """The sha256: digest for a ref. A ref already pinned by digest needs no request."""
    if "@" in ref:
        return ref.split("@", 1)[1] if ref.split("@", 1)[1].startswith("sha256:") else "sha256:" + ref.split("@", 1)[1]
    host, repo, tag = _parse(ref)
    url = f"https://{host}/v2/{repo}/manifests/{tag}"
    code, headers, _body = await asyncio.to_thread(fetch, url, "HEAD", {"Accept": _ACCEPT})
    if code == 401:
        token = await _token(headers, ref)
        auth = {"Accept": _ACCEPT, "Authorization": f"Bearer {token}"}
        code, headers, _body = await asyncio.to_thread(fetch, url, "HEAD", auth)
    if code == 404:
        raise YardError(502, "registry", f"{ref}: not found", help_text="tag not found")
    if code >= 300:
        raise YardError(502, "registry", f"{ref}: HTTP {code}")
    found = headers.get("Docker-Content-Digest") or headers.get("docker-content-digest")
    if not found:
        raise YardError(502, "registry", f"{ref}: the registry sent no digest")
    return found


async def _token(headers: dict[str, str], ref: str) -> str:
    challenge = headers.get("WWW-Authenticate") or headers.get("www-authenticate") or ""
    params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
    realm = params.get("realm", "")
    if not realm.startswith("https://"):
        raise YardError(502, "registry", f"{ref}: cannot answer the registry's auth challenge")
    query = urllib.parse.urlencode({"service": params.get("service", ""), "scope": params.get("scope", "")})
    code, _h, body = await asyncio.to_thread(fetch, f"{realm}?{query}", "GET", {})
    if code >= 300:
        raise YardError(502, "registry", f"{ref}: the token service answered HTTP {code}")
    try:
        token = json.loads(body).get("token") or json.loads(body).get("access_token") or ""
    except ValueError:
        token = ""
    if not token:
        raise YardError(502, "registry", f"{ref}: the token service sent no token")
    return token
