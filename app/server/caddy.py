"""The Caddy admin API on its unix socket: one JSON request per bind change."""

import asyncio
import http.client
import re
import socket as socketlib
from pathlib import Path

from stario import json

from app.shared import APP_PORT, YardError

ROUTES_PATH = "/config/apps/http/servers/yard/routes"
_DIAL_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
_TIMEOUT = 10


class Caddy:
    """The admin client. The socket path comes from the config."""

    def __init__(self, socket: Path) -> None:
        self.socket = socket

    async def request(self, method: str, path: str, body: bytes | None = None) -> tuple[int, bytes]:
        """One admin call. A connection failure or a non-2xx answer is a 502."""
        try:
            code, raw = await asyncio.to_thread(_round_trip, self.socket, method, path, body)
        except (OSError, http.client.HTTPException) as exc:
            raise YardError(502, "caddy", f"caddy {method} {path}: {exc}") from exc
        if code >= 300:
            detail = raw.decode(errors="replace").strip()[:400]
            message = f"caddy {method} {path}: {code}"
            raise YardError(502, "caddy", f"{message} {detail}".rstrip())
        return code, raw


class _Connection(http.client.HTTPConnection):
    """HTTP over one AF_UNIX socket."""

    def __init__(self, socket: Path) -> None:
        super().__init__("localhost", timeout=_TIMEOUT)
        self.socket_path = str(socket)

    def connect(self) -> None:
        self.sock = socketlib.socket(socketlib.AF_UNIX, socketlib.SOCK_STREAM)
        self.sock.settimeout(_TIMEOUT)
        self.sock.connect(self.socket_path)


def _round_trip(socket: Path, method: str, path: str, body: bytes | None) -> tuple[int, bytes]:
    conn = _Connection(socket)
    try:
        conn.request(method, path, body=body, headers={"content-type": "application/json"} if body else {})
        with conn.getresponse() as response:
            return response.status, response.read()
    finally:
        conn.close()


def route_id(host: str) -> str:
    return f"yard-{host}"


def normalize_dial(text: str) -> str:
    """A container dial. A bare name uses port 8000."""
    raw = text.strip()
    for prefix in ("http://", "https://"):
        if raw.startswith(prefix):
            raw = raw[len(prefix) :]
    raw = raw.split("/", 1)[0]
    host, sep, port = raw.rpartition(":")
    if not sep:
        host, port = raw, str(APP_PORT)
    if not _DIAL_HOST.fullmatch(host) or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise YardError(
            400,
            "bad_dial",
            "dial must be a container name or name:port",
            context={"dial": text},
            help_text="Use the container name. Add :port when it is not 8000.",
            example="heldso-app-1:8000",
        )
    return f"{host}:{port}"


def proxy_route(host: str, dial: str, window: str = "10s") -> dict[str, object]:
    """One site. The id is the host, so a second write replaces this route only."""
    return {
        "@id": route_id(host),
        "handle": [
            {
                "handler": "reverse_proxy",
                "load_balancing": {"try_duration": window, "try_interval": "250ms"},
                "upstreams": [{"dial": dial}],
            }
        ],
        "match": [{"host": [host]}],
    }


def read_routes(raw: bytes | str) -> list[dict[str, str]]:
    """The yard server's route array as (host, dial, id, try) rows."""
    text = raw.decode() if isinstance(raw, bytes) else raw
    try:
        rows = json.loads(text or "[]")
    except ValueError:
        return []
    if not isinstance(rows, list):
        return []
    found: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        host = _match_host(row)
        if not host:
            continue
        ident = row.get("@id")
        found.append(
            {
                "host": host,
                "dial": _dial(row),
                "id": ident if isinstance(ident, str) else "",
                "try": _window(row),
            }
        )
    return found


def find_route(raw: bytes | str, host: str) -> tuple[int, str] | None:
    """The index of this host in the routes array, and its stored id."""
    text = raw.decode() if isinstance(raw, bytes) else raw
    try:
        rows = json.loads(text or "[]")
    except ValueError:
        return None
    if not isinstance(rows, list):
        return None
    for index, row in enumerate(rows):
        if isinstance(row, dict) and _match_host(row) == host:
            ident = row.get("@id")
            return index, ident if isinstance(ident, str) else ""
    return None


def _match_host(row: dict[str, object]) -> str:
    match = row.get("match")
    if not isinstance(match, list) or not match or not isinstance(match[0], dict):
        return ""
    names = match[0].get("host")
    if isinstance(names, list) and names:
        return str(names[0])
    return ""


def _window(row: dict[str, object]) -> str:
    handle = row.get("handle")
    if not isinstance(handle, list):
        return ""
    for item in handle:
        if not isinstance(item, dict):
            continue
        balancing = item.get("load_balancing")
        if isinstance(balancing, dict):
            window = balancing.get("try_duration")
        else:
            window = item.get("lb_try_duration")
        if isinstance(window, str) and window:
            return window
    return ""


def _dial(row: dict[str, object]) -> str:
    handle = row.get("handle")
    if not isinstance(handle, list):
        return ""
    for item in handle:
        if not isinstance(item, dict):
            continue
        upstreams = item.get("upstreams")
        if isinstance(upstreams, list) and upstreams and isinstance(upstreams[0], dict):
            return str(upstreams[0].get("dial") or "")
    return ""
